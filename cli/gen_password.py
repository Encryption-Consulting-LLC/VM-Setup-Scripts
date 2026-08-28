#!/usr/bin/env python3
"""gen-password — generate per-VM password-change first-boot scripts (Linux or Windows).

Thin CLI over ``configgen.render_password``: parse the account list, render one
script per account, write the files. The library resets a single account per
script, so N accounts produce N scripts (``30-password-<user>.sh``); pack them
all into the same config ISO. The new passwords are baked plaintext into the
scripts (and thus the config ISO), the same as every other generated value —
the ISO is config, not a secret store. Treat the disc accordingly.
"""

import argparse
import getpass
import sys
from pathlib import Path

import configgen
from cli._common import arg_validator

_DEFAULT_OUTPUT = {"linux": "30-password.sh", "windows": "30-password.ps1"}
_DEFAULT_USERNAME = {"linux": "root", "windows": "Administrator"}


def _parse_users(value: str) -> list[tuple[str, str | None]]:
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gen-password",
        description=(
            "Generate the per-VM password-change first-boot scripts. Each resets one "
            "local account's password unattended on first boot. Pack them into a "
            "config ISO with pack-iso; the guest's first-boot runner applies them on "
            "first boot."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --platform linux -u root:r00t,svcapp:Svc123\n"
            "  %(prog)s --platform linux -u root,svcapp -o scripts/30-password.sh\n"
            "  %(prog)s --platform windows -u Administrator:P@ssw0rd\n\n"
            "-u takes comma-separated user:pass pairs. Give a username with no colon\n"
            "to be prompted securely for its password (and asked to confirm); that is\n"
            "also the only way to use a password containing a comma. One script is\n"
            "written per account -- with more than one account the username is\n"
            "appended to the output name (30-password-root.sh, ...).\n\n"
            "Passwords are baked plaintext into the scripts and ISO -- treat the ISO\n"
            "as a secret."
        ),
    )
    parser.add_argument(
        "--platform", required=True, choices=configgen.PLATFORMS,
        help="Target OS family for the generated scripts.",
    )
    parser.add_argument(
        "-u", "--users", default=None,
        type=arg_validator(_parse_users),
        metavar="USER[:PASS][,...]",
        help=(
            "Comma-separated accounts to reset, each user:pass or a bare user "
            "(prompted). Defaults to root (linux) / Administrator (windows), prompted."
        ),
    )
    parser.add_argument(
        "-o", "--output", default=None, metavar="FILE",
        help=(
            "Output path. Defaults to 30-password.sh (linux) / 30-password.ps1 "
            "(windows). With multiple accounts the username is inserted before the "
            "suffix (30-password-root.sh)."
        ),
    )
    return parser


def _prompt_password(username: str, parser: argparse.ArgumentParser) -> str:
    """Prompt twice for ``username``'s password and confirm. Exits 130 on cancel."""
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


def _output_path(base: Path, username: str, multiple: bool) -> Path:
    """Per-account output path: ``base`` as given for one account, else ``base``
    with ``-<username>`` inserted before its suffix."""
    if not multiple:
        return base
    return base.with_name(f"{base.stem}-{username}{base.suffix}")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    accounts = args.users or [(_DEFAULT_USERNAME[args.platform], None)]
    base = Path(args.output or _DEFAULT_OUTPUT[args.platform])
    multiple = len(accounts) > 1

    resolved = [
        (username, password if password is not None else _prompt_password(username, parser))
        for username, password in accounts
    ]

    written: list[Path] = []
    for username, password in resolved:
        script = configgen.render_password(args.platform, username, password)
        output_path = _output_path(base, username, multiple)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(script, encoding="utf-8")
        written.append(output_path)
        print(f"Script written to: {output_path}")

    print(f"  platform    = {args.platform}")
    print(f"  accounts    = {', '.join(username for username, _ in resolved)}")
    print("  passwords   = (hidden)")
    print(f"\nNext: pack {'them' if multiple else 'it'} into an ISO with")
    print(f"  pack-iso {' '.join(str(p) for p in written)} -o isos/<hostname>-config.iso")


if __name__ == "__main__":
    main()
