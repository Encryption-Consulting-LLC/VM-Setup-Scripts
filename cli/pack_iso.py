#!/usr/bin/env python3
"""pack-iso — Pack first-boot config scripts (and payload files) into a config ISO.

Thin CLI over ``isokit.build_config_iso``: collect the script paths (from args
and/or --dir) plus any payload files (--file), then build a Joliet+Rock-Ridge
ISO carrying them and a ``firstboot.manifest`` at the root. On a deployed VM the
first-boot runner finds the disc by that manifest, stages the payload files, and
runs the listed scripts, in order.

Generate the per-VM scripts with gen-hostname / gen-network, then pack them here
in execution order. Payload files (binaries welcome) are never executed — the
runner stages them where scripts can consume them ($FIRSTBOOT_FILES_DIR).

Examples:
  pack-iso 10-hostname.ps1 20-network.ps1 -o isos/dc01-config.iso
  pack-iso 10-hostname.sh  20-network.sh  -o isos/web01-config.iso
  pack-iso --dir ./scripts -o isos/dc01-config.iso
  pack-iso 10-setup.ps1 30-install.cmd --file agent.exe -o isos/app01-config.iso
"""

import argparse
from pathlib import Path

from isokit import MANIFEST_NAME, build_config_iso


def _collect_scripts(args: argparse.Namespace) -> list[Path]:
    scripts: list[Path] = []
    if args.dir:
        directory = Path(args.dir)
        if not directory.is_dir():
            raise SystemExit(f"--dir not a directory: {directory}")
        found = [p for pat in ("*.ps1", "*.sh", "*.cmd", "*.bat") for p in directory.glob(pat)]
        scripts.extend(sorted(found))
    scripts.extend(Path(f) for f in args.files)
    missing = [str(p) for p in scripts if not p.is_file()]
    if missing:
        raise SystemExit("Script(s) not found: " + ", ".join(missing))
    if not scripts:
        raise SystemExit("No scripts given. Pass file paths and/or --dir.")
    return scripts


def _collect_payload(args: argparse.Namespace) -> list[Path]:
    payload = [Path(f) for f in args.payload]
    missing = [str(p) for p in payload if not p.is_file()]
    if missing:
        raise SystemExit("Payload file(s) not found: " + ", ".join(missing))
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pack-iso",
        description=(
            "Pack first-boot config scripts (.ps1/.sh/.cmd/.bat) and payload files\n"
            "into a config ISO.\n\n"
            "The ISO carries the scripts plus a firstboot.manifest listing them\n"
            "in execution order. The guest's first-boot runner finds the disc by\n"
            "that manifest, stages any payload files, and runs the scripts in\n"
            "order on first boot."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s 10-hostname.ps1 20-network.ps1 -o isos/dc01-config.iso\n"
            "  %(prog)s 10-hostname.sh  20-network.sh  -o isos/web01-config.iso\n"
            "  %(prog)s --dir ./scripts -o isos/dc01-config.iso\n"
            "  %(prog)s 10-setup.ps1 --file agent.exe -o isos/app01-config.iso"
        ),
    )
    parser.add_argument(
        "files",
        nargs="*",
        metavar="SCRIPT",
        help="Scripts (.ps1, .sh, .cmd, .bat) to package, in execution order.",
    )
    parser.add_argument(
        "--dir",
        default=None,
        metavar="DIR",
        help="Directory of *.ps1/*.sh/*.cmd/*.bat scripts to include (sorted by name, before FILES).",
    )
    parser.add_argument(
        "-f",
        "--file",
        action="append",
        default=[],
        dest="payload",
        metavar="FILE",
        help=(
            "Payload file to stage on the target (repeatable). Listed in the "
            "manifest 'files' section, never executed; binaries welcome."
        ),
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
    payload = _collect_payload(args)
    output_path = Path(args.output)
    result = build_config_iso(output_path, scripts=scripts, files=payload)

    print(f"ISO written to: {output_path}")
    print(f"  manifest    = {MANIFEST_NAME} (version {result.manifest_version})")
    print("  scripts     =")
    for name in result.scripts:
        print(f"                {name}")
    if result.files:
        print("  files       =")
        for name in result.files:
            print(f"                {name}")


if __name__ == "__main__":
    main()
