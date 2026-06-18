#!/usr/bin/env python3
"""gen-hostname — generate a per-VM hostname first-boot script (Linux or Windows).

Thin CLI over ``configgen.render_hostname``: parse args, render, write the file.
"""

import argparse
from pathlib import Path

import configgen
from cli._common import arg_validator

_DEFAULT_OUTPUT = {"linux": "10-hostname.sh", "windows": "10-hostname.ps1"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gen-hostname",
        description=(
            "Generate the per-VM hostname first-boot script. Pack it into a config "
            "ISO with pack-iso; the guest's first-boot runner applies it on first boot."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --platform linux -n web01 -o 10-hostname.sh\n"
            "  %(prog)s --platform windows -n dc01 -o 10-hostname.ps1"
        ),
    )
    parser.add_argument(
        "--platform", required=True, choices=configgen.PLATFORMS,
        help="Target OS family for the generated script.",
    )
    parser.add_argument(
        "-n", "--hostname", required=True,
        type=arg_validator(configgen.validate_hostname_rfc),
        metavar="HOSTNAME", help="VM hostname (RFC 1123).",
    )
    parser.add_argument(
        "-o", "--output", default=None, metavar="FILE",
        help="Output path. Defaults to 10-hostname.sh (linux) / 10-hostname.ps1 (windows).",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    script = configgen.render_hostname(args.platform, args.hostname)

    output_path = Path(args.output or _DEFAULT_OUTPUT[args.platform])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(script, encoding="utf-8")

    print(f"Script written to: {output_path}")
    print(f"  platform    = {args.platform}")
    print(f"  hostname    = {args.hostname}")
    print("\nNext: pack it into an ISO with")
    print(f"  pack-iso {output_path} -o isos/{args.hostname}-config.iso")


if __name__ == "__main__":
    main()
