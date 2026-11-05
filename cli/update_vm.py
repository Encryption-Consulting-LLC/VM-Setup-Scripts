#!/usr/bin/env python3
"""update-vm — Update a registered VM's VMX and/or config ISO.

Thin CLI over ``vmkit.update_workflow``. Updates the VMX (CPU, RAM, MAC) and
optionally swaps the config ISO for an existing, powered-off VM. Does not touch
the disk.

KNOWN LIMITATION — swapping the config ISO does NOT re-run first-boot config on an
already-deployed VM. FirstBoot.ps1 is launched by SetupComplete.cmd, which Windows
fires only ONCE, during the post-Sysprep specialize/OOBE pass. After a VM has booted
past first boot, attaching a new ISO and powering on re-applies nothing. Use a new
ISO at clone time (clone-vm) for per-VM config.
"""

import argparse
import logging

import configgen
import vmkit
from vmkit.validate import validate_cpus, validate_iso_path, validate_mac, validate_memory

from cli._common import (
    add_connection_args,
    arg_validator,
    exit_on_error,
    resolve_password,
    setup_cli_logging,
)
from cli._config import CONNECTION_KEYS, add_config_args, apply_config

log = logging.getLogger("deploy-vm")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="update-vm",
        description=(
            "Update a registered VM's VMX (CPU, RAM, MAC) and optionally swap its "
            "config ISO. The VM must exist; it is powered off before the update."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s -n dc01 -s esxi7.example.com -u root -c 4 -r 8192\n"
            "  %(prog)s -n dc01 -s esxi7.example.com -u root "
            "--iso isos/dc01-config.iso --power-on"
        ),
    )
    parser.add_argument(
        "-n", "--name", required=True,
        type=arg_validator(configgen.validate_hostname_rfc),
        metavar="NAME", help="VM name / hostname (RFC 1123).",
    )
    parser.add_argument(
        "-m", "--mac-address", default=None, type=arg_validator(validate_mac),
        metavar="MAC", help="Static MAC for ethernet0. If omitted, keeps the existing MAC.",
    )
    parser.add_argument(
        "-c", "--cpus", default=None, type=arg_validator(validate_cpus),
        metavar="N", help="Number of vCPUs (power of 2, 1–128). If omitted, keeps current.",
    )
    parser.add_argument(
        "-r", "--ram", default=None, type=arg_validator(validate_memory),
        metavar="MB", help="RAM in MB (multiple of 4, min 512). If omitted, keeps current.",
    )
    parser.add_argument(
        "--iso", default=None, type=arg_validator(validate_iso_path),
        metavar="FILE", help="Local .iso to upload as {name}-config.iso and attach.",
    )
    parser.add_argument(
        "--remove-iso", action="store_true",
        help="Remove the CD-ROM attachment (do not upload or attach any ISO).",
    )

    add_connection_args(parser)

    parser.add_argument(
        "-d", "--datastore", default="datastore1", metavar="DS",
        help="Datastore name (default: datastore1).",
    )
    parser.add_argument(
        "-o", "--power-on", action="store_true", help="Power on the VM after updating.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose (DEBUG) console output.",
    )
    add_config_args(parser)
    return parser


def main() -> None:
    parser = build_parser()
    # Connection settings only: a profile's cpus/ram must not silently resize the VM.
    apply_config(parser, CONNECTION_KEYS)
    args = parser.parse_args()
    setup_cli_logging(args.name, args.verbose)

    log.info("=" * 60)
    log.info("Update VM: %s  (datastore: %s)", args.name, args.datastore)
    log.info("=" * 60)

    password = resolve_password(args)

    with exit_on_error(log, "Update"):
        conn = vmkit.open_connection(args.server, args.user, password, args.port)
        result = vmkit.update_workflow(
            conn,
            name=args.name,
            datastore=args.datastore,
            cpus=args.cpus,
            mem_mb=args.ram,
            mac=args.mac_address,
            iso_path=args.iso,
            remove_iso=args.remove_iso,
            power_on=args.power_on,
        )

    log.info("Done. VM '%s' updated: %d CPUs, %d MB RAM, ISO %s.",
             result.name, result.cpus, result.mem_mb, result.iso_action)


if __name__ == "__main__":
    main()
