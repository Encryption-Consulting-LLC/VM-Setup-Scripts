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
from pathlib import Path

import configgen
from cli._common import arg_validator, parse_accounts, resolve_accounts
from cli._config import add_config_args, apply_config

_DEFAULT_OUTPUT = {"linux": "30-password.sh", "windows": "30-password.ps1"}
_DEFAULT_USERNAME = {"linux": "root", "windows": "Administrator"}


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
        type=arg_validator(parse_accounts),
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
    add_config_args(parser)
    return parser


def _output_path(base: Path, username: str, multiple: bool) -> Path:
    """Per-account output path: ``base`` as given for one account, else ``base``
    with ``-<username>`` inserted before its suffix."""
    if not multiple:
        return base
    return base.with_name(f"{base.stem}-{username}{base.suffix}")


def main() -> None:
    parser = build_parser()
    apply_config(parser, {"platform"})
    args = parser.parse_args()

    accounts = args.users or [(_DEFAULT_USERNAME[args.platform], None)]
    base = Path(args.output or _DEFAULT_OUTPUT[args.platform])
    multiple = len(accounts) > 1

    resolved = resolve_accounts(accounts, parser)

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
