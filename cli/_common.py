"""Shared helpers for the CLI front-ends."""

import argparse
import getpass
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager

import configgen
import isokit
import vmkit
from vmkit.progress import setup_logging

from recipes import BakeTimeoutError

PASSWORD_ENV = "VMSETUP_PASSWORD"

#: The Ubuntu Server base image manages its network with netplan.
DEFAULT_LINUX_BACKEND = "netplan"

#: The loggers the CLIs and recipes write to. vmkit's ``setup_logging`` only wires
#: up the ``vmkit`` logger, so these need its handlers too or their INFO lines are lost.
CLI_LOGGERS = ("deploy-vm", "recipes")


def arg_validator(fn):
    """Adapt a library validator (raises ValueError / ValidationError) into an
    argparse ``type=``, preserving the message. Works for both configgen and
    vmkit validators (both raise ValueError subclasses), keeping the libraries
    free of any argparse dependency.
    """

    def _type(value):
        try:
            return fn(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc))

    _type.__name__ = getattr(fn, "__name__", "value")
    return _type


def add_connection_args(parser: argparse.ArgumentParser) -> None:
    """Add the shared ESXi connection options (-s/-u/-p/-P)."""
    parser.add_argument("-s", "--server", required=True, metavar="HOST",
                        help="ESXi host FQDN or IP.")
    parser.add_argument("-u", "--user", required=True, metavar="USER",
                        help="ESXi username.")
    parser.add_argument("-p", "--password", default=None, metavar="PASS",
                        help=f"ESXi password. If omitted, read from ${PASSWORD_ENV}, "
                             "else prompted for securely.")
    parser.add_argument("-P", "--port", type=int, default=443, metavar="PORT",
                        help="HTTPS port (default: 443).")


def resolve_password(args: argparse.Namespace) -> str:
    """Return the ESXi password from args, then $VMSETUP_PASSWORD, else prompt
    securely. Exits 130 on cancel."""
    if args.password:
        return args.password
    if env := os.environ.get(PASSWORD_ENV):
        return env
    try:
        return getpass.getpass(
            f"Password for {args.user}@{args.server}: ", echo_char="*"
        )
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(130)


def setup_cli_logging(name: str, verbose: bool) -> str:
    """vmkit's console + file logging, extended to the CLI and recipe loggers.
    Returns the log file path."""
    vmkit_log = logging.getLogger("vmkit")
    before = set(vmkit_log.handlers)
    logfile = setup_logging(name, verbose)
    added = [h for h in vmkit_log.handlers if h not in before]
    for logger_name in CLI_LOGGERS:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        for handler in added:
            logger.addHandler(handler)
    return logfile


@contextmanager
def exit_on_error(log: logging.Logger, action: str) -> Iterator[None]:
    """Map library failures to the CLIs' exit codes:

    2 connection / authentication, 3 VM already exists or not found,
    4 any other failure, 5 config ISO could not be built,
    6 not enough datastore space, 7 a bake did not finish in time.
    """
    try:
        yield
    except (vmkit.AuthenticationError, vmkit.ConnectionFailedError) as exc:
        log.error("%s", exc)
        sys.exit(2)
    except (vmkit.VmExistsError, vmkit.VmNotFoundError) as exc:
        log.error("%s Aborting.", exc)
        sys.exit(3)
    except vmkit.InsufficientSpaceError as exc:
        log.error("Limited disk resource: %s Aborting.", exc)
        sys.exit(6)
    except isokit.IsoKitError as exc:
        log.error("Could not build the config ISO: %s", exc)
        sys.exit(5)
    except BakeTimeoutError as exc:
        log.error("%s", exc)
        sys.exit(7)
    except Exception as exc:
        log.error("%s failed: %s", action, exc)
        log.debug("Traceback:", exc_info=True)
        sys.exit(4)


def parse_accounts(value: str) -> list[tuple[str, str | None]]:
    """Parse ``user:pass,user2,...`` into ordered ``(username, password | None)`` pairs.

    Each comma-separated entry is either ``username:password`` or a bare
    ``username`` (password prompted for later). Only the first colon splits, so
    passwords may contain colons; they may not contain a comma — use the bare
    form and be prompted instead. Raises ``ValidationError`` on a bad entry.
    """
    pairs: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for entry in value.split(","):
        entry = entry.strip()
        if not entry:
            raise configgen.ValidationError(
                f"Empty account entry in '{value}': expected user:pass or user, "
                "comma-separated.",
                field="users",
                code="users_empty_entry",
            )
        username, sep, password = entry.partition(":")
        try:
            username = configgen.validate_username(username)
            password = configgen.validate_password(password) if sep else None
        except configgen.ValidationError as exc:
            # A bare entry that is not a valid username is most often the tail of a
            # password containing a comma, which the split above tore off.
            hint = "" if sep else (
                " A password containing a comma cannot be given inline — pass the"
                " username alone to be prompted for it."
            )
            raise configgen.ValidationError(
                f"{exc} (in account entry '{entry}').{hint}",
                field=exc.field,
                code=exc.code,
            )
        if username in seen:
            raise configgen.ValidationError(
                f"Duplicate account '{username}': each account may appear once.",
                field="users",
                code="users_duplicate",
            )
        seen.add(username)
        pairs.append((username, password))
    return pairs


def prompt_new_password(username: str, parser: argparse.ArgumentParser) -> str:
    """Prompt twice for ``username``'s new password and confirm. Exits 130 on cancel."""
    try:
        first = getpass.getpass(f"New password for '{username}': ", echo_char="*")
        again = getpass.getpass(f"Confirm password for '{username}': ", echo_char="*")
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(130)
    if first != again:
        parser.error(f"passwords for '{username}' do not match.")
    try:
        return configgen.validate_password(first)
    except configgen.ValidationError as exc:
        parser.error(str(exc))


def resolve_accounts(
    accounts: list[tuple[str, str | None]], parser: argparse.ArgumentParser
) -> list[tuple[str, str]]:
    """Fill in any missing passwords by prompting."""
    return [
        (username, password if password is not None else prompt_new_password(username, parser))
        for username, password in accounts
    ]
