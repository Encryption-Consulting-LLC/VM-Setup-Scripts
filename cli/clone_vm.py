#!/usr/bin/env python3
"""clone-vm — Clone a base VM's disk and register it on ESXi.

Thin CLI over ``vmkit.clone_workflow``: parse args, prompt for the password,
open a connection, run the workflow, and map vmkit errors to exit codes. The
.vmdk/.nvram copies happen server-side; the VMX is rendered and uploaded.
"""

import argparse
import logging
import sys

import configgen
import vmkit
from vmkit.progress import setup_logging
from vmkit.vmx import DEFAULT_GUEST_OS, DEFAULT_NETWORK
from vmkit.validate import validate_cpus, validate_iso_path, validate_mac, validate_memory

from cli._common import add_connection_args, arg_validator, resolve_password

log = logging.getLogger("deploy-vm")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clone-vm",
        description=(
            "Clone a base VM's disk server-side, render a VMX, and register it on "
            "a standalone ESXi host. Optionally upload a config ISO before registration "
            "and power on the VM when done."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s -n dc01 -s esxi7.example.com -u root\n"
            "  %(prog)s -n dc01 -s esxi7.example.com -u root "
            "--base ws-2025-base --network 'VLAN 20' --iso isos/dc01-config.iso --power-on"
        ),
    )
    parser.add_argument(
        "-n", "--name", required=True,
        type=arg_validator(configgen.validate_hostname_rfc),
        metavar="NAME",
        help="VM name / hostname (RFC 1123). Used as the folder and file base name.",
    )
    parser.add_argument(
        "-m", "--mac-address", default=None, type=arg_validator(validate_mac),
        metavar="MAC",
        help="Static MAC for ethernet0 (VMware static range). Defaults to a random MAC.",
    )
    parser.add_argument(
        "-c", "--cpus", default=2, type=arg_validator(validate_cpus),
        metavar="N", help="Number of vCPUs (power of 2, 1–128). Default: 2.",
    )
    parser.add_argument(
        "-r", "--ram", default=4096, type=arg_validator(validate_memory),
        metavar="MB", help="RAM in MB (multiple of 4, min 512). Default: 4096.",
    )
    parser.add_argument(
        "--iso", default=None, type=arg_validator(validate_iso_path),
        metavar="FILE",
        help="Local .iso to upload as {name}-config.iso and attach as CD-ROM.",
    )

    add_connection_args(parser)

    parser.add_argument(
        "-d", "--datastore", default="datastore1", metavar="DS",
        help="Datastore name (default: datastore1).",
    )
    parser.add_argument(
        "-b", "--base", default="ws-2025-base", metavar="BASE",
        help="Base VM folder/file name to clone (default: ws-2025-base).",
    )
    parser.add_argument(
        "--network", default=DEFAULT_NETWORK, metavar="NAME",
        help=("Port group for ethernet0. Must already exist on the host, or the VM "
              f"registers with a dead NIC (default: {DEFAULT_NETWORK!r})."),
    )
    parser.add_argument(
        "--guest-os", default=None, metavar="ID",
        help=("VMware guestOS id to bake into the VMX. If omitted, read from the "
              f"base VM's VMX; if unreadable, defaults to {DEFAULT_GUEST_OS}."),
    )
    parser.add_argument(
        "--max-usage", type=float, default=80.0, metavar="PCT",
        help="Abort if the datastore would exceed this %% full after cloning (default: 80).",
    )
    parser.add_argument(
        "--skip-disk-check", action="store_true",
        help="Skip the datastore free-space pre-flight check entirely.",
    )
    parser.add_argument(
        "-o", "--power-on", action="store_true",
        help="Power on the VM after registration.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose (DEBUG) console output.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    setup_logging(args.name, args.verbose)

    log.info("=" * 60)
    log.info("Clone VM: %s  (base: %s, datastore: %s, network: %s)",
             args.name, args.base, args.datastore, args.network)
    log.info("=" * 60)

    password = resolve_password(args)

    try:
        conn = vmkit.open_connection(args.server, args.user, password, args.port)
        result = vmkit.clone_workflow(
            conn,
            name=args.name,
            base=args.base,
            datastore=args.datastore,
            cpus=args.cpus,
            mem_mb=args.ram,
            mac=args.mac_address,
            iso_path=args.iso,
            guest_os=args.guest_os,
            network=args.network,
            max_usage_pct=args.max_usage,
            skip_disk_check=args.skip_disk_check,
            power_on=args.power_on,
        )
    except (vmkit.AuthenticationError, vmkit.ConnectionFailedError) as exc:
        log.error("%s", exc)
        sys.exit(2)
    except vmkit.VmExistsError as exc:
        log.error("%s Aborting.", exc)
        sys.exit(3)
    except vmkit.InsufficientSpaceError as exc:
        log.error("Limited disk resource: %s Aborting.", exc)
        sys.exit(6)
    except vmkit.VmkitError as exc:
        log.error("Clone failed: %s", exc)
        sys.exit(4)
    except Exception as exc:
        log.error("Clone failed: %s", exc)
        log.debug("Traceback:", exc_info=True)
        sys.exit(4)

    log.info("Done. VM '%s' registered (total VMs in inventory: %d).",
             result.name, result.total_vms)


if __name__ == "__main__":
    main()
