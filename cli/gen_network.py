#!/usr/bin/env python3
"""gen-network — generate a per-VM network first-boot script (Linux or Windows).

Thin CLI over ``configgen.render_network``: parse args, build a validated
NetworkConfig, render, write the file. The static/DHCP contract is enforced by
NetworkConfig; this CLI surfaces any ValidationError as an argparse error.
"""

import argparse
from pathlib import Path

import configgen
from configgen import NetworkConfig, ValidationError
from cli._common import arg_validator

_DEFAULT_OUTPUT = {"linux": "20-network.sh", "windows": "20-network.ps1"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gen-network",
        description=(
            "Generate the per-VM network first-boot script for a static IP or DHCP. "
            "Pack it into a config ISO with pack-iso."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --platform linux --ip 192.168.1.50 --prefix 24 "
            "--gateway 192.168.1.1 --dns1 192.168.1.10 -o 20-network.sh\n"
            "  %(prog)s --platform windows --dhcp -o 20-network.ps1"
        ),
    )
    parser.add_argument(
        "--platform", required=True, choices=configgen.PLATFORMS,
        help="Target OS family for the generated script.",
    )
    parser.add_argument(
        "--dhcp", action="store_true",
        help="Configure via DHCP instead of a static IP. "
             "Makes --ip/--prefix/--gateway invalid and --dns1 optional.",
    )
    parser.add_argument(
        "--ip", type=arg_validator(configgen.validate_ipv4),
        metavar="ADDR", help="Static IPv4 address. Required unless --dhcp.",
    )
    parser.add_argument(
        "--prefix", type=arg_validator(configgen.validate_prefix),
        metavar="LEN", help="Subnet prefix length (1-32). Required unless --dhcp.",
    )
    parser.add_argument(
        "--gateway", type=arg_validator(configgen.validate_ipv4),
        metavar="ADDR", help="Default gateway IPv4 address. Required unless --dhcp.",
    )
    parser.add_argument(
        "--dns1", default=None, type=arg_validator(configgen.validate_ipv4),
        metavar="ADDR", help="Primary DNS server. Required for static; optional for DHCP.",
    )
    parser.add_argument(
        "--dns2", default=None, type=arg_validator(configgen.validate_ipv4),
        metavar="ADDR", help="Secondary DNS server (optional).",
    )
    parser.add_argument(
        "--dns-suffix", default=None, metavar="SUFFIX",
        help="DNS search suffix, e.g. corp.example.com (optional).",
    )
    parser.add_argument(
        "-o", "--output", default=None, metavar="FILE",
        help="Output path. Defaults to 20-network.sh (linux) / 20-network.ps1 (windows).",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        cfg = NetworkConfig(
            mode="dhcp" if args.dhcp else "static",
            ip=args.ip,
            prefix=args.prefix,
            gateway=args.gateway,
            dns1=args.dns1,
            dns2=args.dns2,
            dns_suffix=args.dns_suffix,
        )
    except ValidationError as exc:
        parser.error(str(exc))

    script = configgen.render_network(args.platform, cfg)

    output_path = Path(args.output or _DEFAULT_OUTPUT[args.platform])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(script, encoding="utf-8")

    print(f"Script written to: {output_path}")
    print(f"  platform    = {args.platform}")
    print(f"  mode        = {cfg.mode}")
    if cfg.mode == "static":
        print(f"  ip/prefix   = {cfg.ip}/{cfg.prefix}")
        print(f"  gateway     = {cfg.gateway}")
    for label, value in (("dns1", cfg.dns1), ("dns2", cfg.dns2), ("dns-suffix", cfg.dns_suffix)):
        if value is not None:
            print(f"  {label:<11} = {value}")
    print("\nNext: pack it into an ISO with")
    print(f"  pack-iso {output_path} -o isos/<hostname>-config.iso")


if __name__ == "__main__":
    main()
