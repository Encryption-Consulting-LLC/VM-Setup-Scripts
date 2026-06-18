#!/usr/bin/env python3
"""pack-iso — Pack first-boot config scripts into a config ISO.

Thin CLI over ``isokit.build_script_iso``: collect the script paths (from args
and/or --dir), then build a Joliet+Rock-Ridge ISO carrying the scripts plus a
``firstboot.manifest`` at the root. On a deployed VM the first-boot runner finds
the disc by that manifest and runs the listed scripts, in order.

Generate the per-VM scripts with gen-hostname / gen-network, then pack them here
in execution order.

Examples:
  pack-iso 10-hostname.ps1 20-network.ps1 -o isos/dc01-config.iso
  pack-iso 10-hostname.sh  20-network.sh  -o isos/web01-config.iso
  pack-iso --dir ./scripts -o isos/dc01-config.iso
"""

import argparse
from pathlib import Path

from isokit import MANIFEST_NAME, MANIFEST_VERSION, build_script_iso


def _collect_scripts(args: argparse.Namespace) -> list[Path]:
    scripts: list[Path] = []
    if args.dir:
        directory = Path(args.dir)
        if not directory.is_dir():
            raise SystemExit(f"--dir not a directory: {directory}")
        found = [p for pat in ("*.ps1", "*.sh") for p in directory.glob(pat)]
        scripts.extend(sorted(found))
    scripts.extend(Path(f) for f in args.files)
    missing = [str(p) for p in scripts if not p.is_file()]
    if missing:
        raise SystemExit("Script(s) not found: " + ", ".join(missing))
    if not scripts:
        raise SystemExit("No scripts given. Pass file paths and/or --dir.")
    return scripts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pack-iso",
        description=(
            "Pack first-boot config scripts (.ps1 and/or .sh) into a config ISO.\n\n"
            "The ISO carries the scripts plus a firstboot.manifest listing them\n"
            "in execution order. The guest's first-boot runner finds the disc by\n"
            "that manifest and runs the scripts in order on first boot."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s 10-hostname.ps1 20-network.ps1 -o isos/dc01-config.iso\n"
            "  %(prog)s 10-hostname.sh  20-network.sh  -o isos/web01-config.iso\n"
            "  %(prog)s --dir ./scripts -o isos/dc01-config.iso"
        ),
    )
    parser.add_argument(
        "files",
        nargs="*",
        metavar="SCRIPT",
        help="Scripts (.ps1 or .sh) to package, in execution order.",
    )
    parser.add_argument(
        "--dir",
        default=None,
        metavar="DIR",
        help="Directory of *.ps1/*.sh scripts to include (sorted by name, before FILES).",
    )
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        metavar="FILE",
        help="Output ISO path.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    scripts = _collect_scripts(args)
    output_path = Path(args.output)
    ordered = build_script_iso(scripts, output_path)

    print(f"ISO written to: {output_path}")
    print(f"  manifest    = {MANIFEST_NAME} (version {MANIFEST_VERSION})")
    print("  scripts     =")
    for name in ordered:
        print(f"                {name}")


if __name__ == "__main__":
    main()
