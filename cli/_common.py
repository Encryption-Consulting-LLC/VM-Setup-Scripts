"""Shared helpers for the CLI front-ends."""

import argparse
import getpass
import logging
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import configgen
import isokit
import vmkit
from vmkit.progress import setup_logging

import recipes
from recipes import PowerOffTimeoutError

#: Exit code for a batch in which at least one VM failed (others may have succeeded).
EXIT_BATCH_FAILED = 8
#: Exit code for a batch stopped with Ctrl-C.
EXIT_CANCELLED = 130

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
    6 not enough datastore space, 7 a bake or install did not finish in time.
    (8, a batch with failed VMs, is set by ``batch_exit_code`` after the run.)
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
    except PowerOffTimeoutError as exc:
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


# --------------------------------------------------------------------------- #
# Batches (--range)                                                           #
# --------------------------------------------------------------------------- #
class _BatchTagFilter(logging.Filter):
    """Prefix each line logged from a batch worker with its VM's name.

    Installed on handlers, so one record may pass several (console + file); the
    marker keeps it from being tagged twice.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        name = recipes.current_item.get()
        if name and not getattr(record, "_batch_tagged", False):
            record.msg = f"[{name}] {record.msg}"
            record._batch_tagged = True
        return True


def tag_batch_log_lines() -> None:
    """After ``setup_cli_logging``: tag concurrent workers' log lines by VM."""
    tag = _BatchTagFilter()
    for logger_name in ("vmkit", *CLI_LOGGERS):
        for handler in logging.getLogger(logger_name).handlers:
            handler.addFilter(tag)


def batch_connector(args: argparse.Namespace, password: str) -> Callable[[], vmkit.Connection]:
    """A factory opening one ESXi session per batch worker (closed by the batch)."""

    def connect() -> vmkit.Connection:
        return vmkit.open_connection(
            args.server, args.user, password, args.port, register_atexit=False
        )

    return connect


def batch_range(args: argparse.Namespace, parser: argparse.ArgumentParser) -> list[str]:
    """The numbers of ``--range``, or exit with a usage error."""
    try:
        return recipes.parse_range(args.range)
    except ValueError as exc:
        parser.error(f"--range: {exc}")


def log_batch_summary(
    log: logging.Logger,
    result: "recipes.BatchResult",
    describe: Callable[[object], str],
) -> None:
    """One line per VM, in batch order. ``describe(result)`` details a success."""
    log.info("=" * 60)
    log.info("Batch: %d succeeded, %d failed, %d skipped%s",
             len(result.succeeded), len(result.failed), len(result.skipped),
             " (cancelled)" if result.cancelled else "")
    width = max((len(o.name) for o in result.outcomes), default=0)
    for o in result.outcomes:
        if o.ok:
            log.info("  %-*s  ok       %s", width, o.name, describe(o.result))
        elif o.skipped:
            log.info("  %-*s  skipped  %s", width, o.name, o.note)
        else:
            detail = f"{o.error}" + (f" ({o.note})" if o.note else "")
            log.error("  %-*s  FAILED   %s", width, o.name, detail)
    log.info("=" * 60)


def batch_exit_code(result: "recipes.BatchResult") -> int:
    """0 if every VM succeeded, 130 if cancelled, else 8."""
    if result.ok:
        return 0
    return EXIT_CANCELLED if result.cancelled else EXIT_BATCH_FAILED
