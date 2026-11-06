#!/usr/bin/env python3
"""install-base — Create and autoinstall a Linux ``-base-pre`` VM from scratch.

Thin CLI over ``recipes.install``. Replaces creating the VM by hand in the ESXi
Host Client: makes ``NAME-pre`` with a blank disk and the autoinstall installer +
seed ISOs as CD-ROMs, powers it on, waits for the install and its bootstrap to
power it off, then detaches both discs. Follow with ``bake-base NAME``.

The ISOs must already be on a datastore (build them with
base-vm-setup/linux-server/autoinstall/, upload them with upload-file).
--dry-run prints the plan instead.

Linux only.
"""

import argparse
import logging

import configgen
import vmkit
from vmkit.vmx import DEFAULT_NETWORK
from vmkit.validate import validate_cpus, validate_memory

import recipes
from recipes import DEFAULT_DISK_GB, DEFAULT_INSTALL_GUEST_OS, DEFAULT_INSTALL_TIMEOUT_S
from cli._common import (
    add_connection_args,
    arg_validator,
    exit_on_error,
    resolve_password,
    setup_cli_logging,
)
from cli._config import INSTALL_KEYS, add_config_args, apply_config

log = logging.getLogger("deploy-vm")


def _disk_gb(value: str) -> int:
    try:
        size = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"disk size must be a whole number of GiB, got {value!r}")
    if size <= 0:
        raise argparse.ArgumentTypeError(f"disk size must be positive, got {size}")
    return size


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install-base",
        description=(
            "Install a Linux -base-pre VM unattended: create NAME-pre with a blank "
            "disk and the autoinstall installer + seed ISOs, power it on, wait for "
            "the install and its bootstrap to power it off, then detach both discs. "
            "Then run `bake-base NAME` to generalize it into the base."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "ISO paths are '[datastore] path' or relative to -d.\n\n"
            "Examples:\n"
            "  %(prog)s ub-24.04.5-base -d 'datastore1 (3)' \\\n"
            "      --installer ISO/ubuntu-24.04.5-autoinstall.iso --seed ISO/ub-24.04.5-seed.iso\n"
            "  %(prog)s ub-24.04.5-base --profile ubuntu --seed ISO/ub-24.04.5-seed.iso\n"
            "  %(prog)s ub-24.04.5-base ... --dry-run\n\n"
            "NAME-pre must not exist yet. Then: bake-base NAME."
        ),
    )
    parser.add_argument(
        "name", type=arg_validator(configgen.validate_hostname_rfc), metavar="NAME",
        help="Base the VM is for, e.g. ub-24.04.5-base. The VM created is NAME-pre.",
    )
    parser.add_argument(
        "--as", dest="pre", type=arg_validator(configgen.validate_hostname_rfc),
        metavar="PRE", help="Name the VM PRE instead of NAME-pre.",
    )
    parser.add_argument(
        "--installer", required=True, metavar="ISO",
        help="Autoinstall installer ISO on the datastore (autoinstall/remaster-iso.sh). "
             "Attached first, so it is what boots.",
    )
    parser.add_argument(
        "--seed", required=True, metavar="ISO",
        help="Seed ISO on the datastore (autoinstall/build-seed.sh).",
    )

    vm = parser.add_argument_group("VM and ESXi")
    add_connection_args(vm)
    vm.add_argument("-d", "--datastore", default="datastore1", metavar="DS",
                    help="Datastore for the VM, and for ISO paths without a [datastore] "
                         "prefix (default: datastore1).")
    vm.add_argument("--network", default=DEFAULT_NETWORK, metavar="PORTGROUP",
                    help=f"Port group for ethernet0; needs DHCP for the install "
                         f"(default: {DEFAULT_NETWORK!r}).")
    vm.add_argument("-c", "--cpus", default=2, type=arg_validator(validate_cpus), metavar="N",
                    help="vCPUs (power of 2). Default: 2.")
    vm.add_argument("-r", "--ram", default=4096, type=arg_validator(validate_memory),
                    metavar="MB", help="RAM in MB (the installer wants 2048+). Default: 4096.")
    vm.add_argument("--disk-gb", default=DEFAULT_DISK_GB, type=_disk_gb, metavar="GB",
                    help=f"Disk size in GiB. Clones copy it. Default: {DEFAULT_DISK_GB}.")
    vm.add_argument("--thick", action="store_true",
                    help="Preallocate the disk (default: thin).")
    vm.add_argument("--guest-os", default=DEFAULT_INSTALL_GUEST_OS, metavar="ID",
                    help=f"VMware guestOS id (default: {DEFAULT_INSTALL_GUEST_OS}).")

    run = parser.add_argument_group("run")
    run.add_argument("--no-wait", dest="wait", action="store_false",
                     help="Return once the VM is powered on; do not wait or detach.")
    run.add_argument("--timeout", type=float, default=DEFAULT_INSTALL_TIMEOUT_S / 60,
                     metavar="MIN",
                     help=f"How long to wait for the install to finish "
                          f"(default: {DEFAULT_INSTALL_TIMEOUT_S // 60} min).")
    run.add_argument("--keep-discs", dest="detach", action="store_false",
                     help="Leave both discs attached after the install. Dangerous: the "
                          "installer wipes the disk if the VM boots from it again.")
    run.add_argument("--dry-run", action="store_true",
                     help="Print what would be created; change nothing.")
    run.add_argument("-v", "--verbose", action="store_true",
                     help="Verbose (DEBUG) console output.")

    add_config_args(parser)
    return parser


def _plan(args: argparse.Namespace) -> list[str]:
    def iso(path: str) -> str:
        return path if path.lstrip().startswith("[") else f"[{args.datastore}] {path}"

    lines = [
        f"Create VM '{args.pre}' on {args.server}, datastore '{args.datastore}':",
        f"  {args.cpus} vCPU, {args.ram} MB RAM, {args.disk_gb} GiB "
        f"{'thick' if args.thick else 'thin'} disk, EFI, guest OS {args.guest_os}",
        f"  network '{args.network}'",
        f"  CD-ROM 0 (boots): {iso(args.installer)}",
        f"  CD-ROM 1:         {iso(args.seed)}",
        "Power it on.",
    ]
    if args.wait:
        lines.append(f"Wait up to {args.timeout:g} min for it to power itself off.")
        if args.detach:
            lines.append("Detach both discs.")
    lines.append(f"Then: bake-base {args.name}" + (f" --from {args.pre}" if args.pre != f"{args.name}-pre" else ""))
    return lines


def main() -> None:
    parser = build_parser()
    apply_config(parser, INSTALL_KEYS)
    args = parser.parse_args()
    args.pre = args.pre or f"{args.name}-pre"
    if args.pre == args.name:
        parser.error("--as must differ from NAME: NAME is the base bake-base will create.")
    if args.timeout <= 0:
        parser.error("--timeout must be positive.")

    if args.dry_run:
        print("Dry run:")
        for line in _plan(args):
            print("  " + line)
        return

    setup_cli_logging(args.pre, args.verbose)
    log.info("=" * 60)
    log.info("Install base-pre: %s  (datastore: %s)", args.pre, args.datastore)
    log.info("=" * 60)

    password = resolve_password(args)

    with exit_on_error(log, "Install"):
        conn = vmkit.open_connection(args.server, args.user, password, args.port)
        result = recipes.install(
            conn,
            name=args.pre,
            datastore=args.datastore,
            installer=args.installer,
            seed=args.seed,
            guest_os=args.guest_os,
            disk_gb=args.disk_gb,
            wait=args.wait,
            detach_discs=args.detach,
            timeout_s=args.timeout * 60,
            cpus=args.cpus,
            mem_mb=args.ram,
            network=args.network,
            thin=not args.thick,
        )

    if not result.finished:
        log.info("Install started on '%s'. It powers itself off when done; then detach "
                 "its discs (update-vm -n %s --remove-iso) before anything else.",
                 args.pre, args.pre)
    elif result.discs_detached:
        log.info("Done. '%s' is installed and powered off. Next: bake-base %s%s",
                 args.pre, args.name,
                 f" --from {args.pre}" if args.pre != f"{args.name}-pre" else "")
    else:
        log.info("Done. '%s' is installed and powered off; both discs are still attached. "
                 "Detach them before booting it again.", args.pre)


if __name__ == "__main__":
    main()
