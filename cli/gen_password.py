#!/usr/bin/env python3
"""gen-password — generate a per-VM password-change first-boot script (Linux or Windows).

Thin CLI over ``configgen.render_password``: parse args, render, write the file.
The new password is baked plaintext into the script (and thus the config ISO),
the same as every other generated value — the ISO is config, not a secret store.
Treat the disc accordingly.
"""

import argparse
import getpass
import sys
from pathlib import Path

import configgen
from cli._common import arg_validator

_DEFAULT_OUTPUT = {"linux": "30-password.sh", "windows": "30-password.ps1"}
_DEFAULT_USERNAME = {"linux": "root", "windows": "Administrator"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gen-password",
        description=(
            "Generate the per-VM password-change first-boot script. It resets a local "
            "account's password unattended on first boot. Pack it into a config ISO "
            "with pack-iso; the guest's first-boot runner applies it on first boot."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --platform linux -o 30-password.sh\n"
            "  %(prog)s --platform windows -u Administrator -o 30-password.ps1\n\n"
            "If --password is omitted you are prompted securely (and asked to confirm).\n"
            "The password is baked plaintext into the script and ISO -- treat the ISO\n"
            "as a secret."
        ),
    )
    parser.add_argument(
        "--platform", required=True, choices=configgen.PLATFORMS,
        help="Target OS family for the generated script.",
    )
    parser.add_argument(
        "-u", "--username", default=None,
        type=arg_validator(configgen.validate_username),
        metavar="USER",
        help="Account to reset. Defaults to root (linux) / Administrator (windows).",
    )
    parser.add_argument(
        "-p", "--password", default=None,
        type=arg_validator(configgen.validate_password),
        metavar="PASS",
        help="New password. If omitted, prompts securely.",
    )
    parser.add_argument(
        "-o", "--output", default=None, metavar="FILE",
        help="Output path. Defaults to 30-password.sh (linux) / 30-password.ps1 (windows).",
    )
    return parser


def _resolve_password(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    """Return the password from args, or prompt twice and confirm. Exits 130 on cancel."""
    if args.password is not None:
        return args.password
    try:
        first = getpass.getpass("New password: ", echo_char="*")
        again = getpass.getpass("Confirm password: ", echo_char="*")
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(130)
    if first != again:
        parser.error("passwords do not match.")
    try:
        return configgen.validate_password(first)
    except configgen.ValidationError as exc:
        parser.error(str(exc))


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    username = args.username or _DEFAULT_USERNAME[args.platform]
    password = _resolve_password(args, parser)

    script = configgen.render_password(args.platform, username, password)

    output_path = Path(args.output or _DEFAULT_OUTPUT[args.platform])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(script, encoding="utf-8")

    print(f"Script written to: {output_path}")
    print(f"  platform    = {args.platform}")
    print(f"  username    = {username}")
    print("  password    = (hidden)")
    print("\nNext: pack it into an ISO with")
    print(f"  pack-iso {output_path} -o isos/<hostname>-config.iso")


if __name__ == "__main__":
    main()
