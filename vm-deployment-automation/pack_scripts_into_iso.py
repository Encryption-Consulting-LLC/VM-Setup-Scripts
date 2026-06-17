#!/usr/bin/env python3
"""
pack_scripts_into_iso.py — Pack first-boot PowerShell scripts into a config ISO.

Builds an ISO 9660 image (with Joliet + Rock Ridge so Windows sees real long
filenames) containing one or more PowerShell scripts plus a `firstboot.manifest`
at the root. On a deployed VM, FirstBoot.ps1 locates the disc by that manifest
and runs the listed scripts, in order, as SYSTEM.

The manifest doubles as a marker: it is how FirstBoot.ps1 tells our config disc
apart from unrelated CD media (VMware Tools, install ISOs). Scripts run in the
exact order they are listed here.

This tool is content-agnostic — it just packages whatever scripts it is given.
The per-VM hostname and network scripts are produced by the generators under
script-generators/windows-server/ (hostname.py and network.py), which only emit
.ps1 files; pack them here in execution order.

Examples:
  ./pack_scripts_into_iso.py 10-hostname.ps1 20-network.ps1 -o isos/dc01-config.iso
  ./pack_scripts_into_iso.py --dir ./scripts -o isos/dc01-config.iso
"""

import argparse
import io
import json
from pathlib import Path

import pycdlib

MANIFEST_NAME = "firstboot.manifest"
MANIFEST_VERSION = 1


def _to_crlf(text: str) -> str:
    """Normalize any line endings to CRLF (safest for .ps1 read on Windows)."""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def _iso9660_name(index: int) -> str:
    """A valid ISO 9660 (8.3, uppercase, ;1) name. The Joliet/RR name carries
    the real long filename; this is only the fallback for level-1 readers."""
    return f"/SCRIPT{index:02d}.PS1;1"


def build_script_iso(script_paths: list[Path], output_path: Path) -> list[str]:
    """Pack the given .ps1 files into a Joliet+Rock-Ridge ISO at output_path,
    with a firstboot.manifest listing them in the order received.

    Returns the ordered list of script filenames written to the manifest.
    """
    if not script_paths:
        raise ValueError("No scripts to package.")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge="1.09")

    ordered_names: list[str] = []
    for index, path in enumerate(script_paths, start=1):
        name = path.name
        if name in ordered_names:
            raise ValueError(f"Duplicate script name '{name}' in bundle.")
        data = _to_crlf(path.read_text(encoding="utf-8")).encode("utf-8")
        iso.add_fp(
            io.BytesIO(data),
            len(data),
            _iso9660_name(index),
            joliet_path=f"/{name}",
            rr_name=name,
        )
        ordered_names.append(name)

    manifest = {"version": MANIFEST_VERSION, "scripts": ordered_names}
    manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
    iso.add_fp(
        io.BytesIO(manifest_bytes),
        len(manifest_bytes),
        "/FIRSTBT.MAN;1",
        joliet_path=f"/{MANIFEST_NAME}",
        rr_name=MANIFEST_NAME,
    )

    iso.write(str(output_path))
    iso.close()
    return ordered_names


def _collect_scripts(args: argparse.Namespace) -> list[Path]:
    scripts: list[Path] = []
    if args.dir:
        directory = Path(args.dir)
        if not directory.is_dir():
            raise SystemExit(f"--dir not a directory: {directory}")
        scripts.extend(sorted(directory.glob("*.ps1")))
    scripts.extend(Path(f) for f in args.files)
    missing = [str(p) for p in scripts if not p.is_file()]
    if missing:
        raise SystemExit("Script(s) not found: " + ", ".join(missing))
    if not scripts:
        raise SystemExit("No .ps1 scripts given. Pass file paths and/or --dir.")
    return scripts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pack_scripts_into_iso.py",
        description=(
            "Pack first-boot PowerShell scripts into a config ISO.\n\n"
            "The ISO carries the scripts plus a firstboot.manifest listing them\n"
            "in execution order. FirstBoot.ps1 finds the disc by that manifest\n"
            "and runs the scripts in order, as SYSTEM, on first boot."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s 10-hostname.ps1 20-network.ps1 -o isos/dc01-config.iso\n"
            "  %(prog)s --dir ./scripts -o isos/dc01-config.iso"
        ),
    )
    parser.add_argument(
        "files",
        nargs="*",
        metavar="SCRIPT.ps1",
        help="PowerShell scripts to package, in execution order.",
    )
    parser.add_argument(
        "--dir",
        default=None,
        metavar="DIR",
        help="Directory of *.ps1 scripts to include (sorted by name, before FILES).",
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
