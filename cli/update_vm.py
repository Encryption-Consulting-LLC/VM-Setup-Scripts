#!/usr/bin/env python3
"""
update_vm.py — Update a registered VM's VMX and/or config ISO.

Updates the VMX (CPU, RAM, network, etc.) and optionally uploads a new config ISO
for an existing VM. Does not modify the disk. The VM must exist and be powered off.

KNOWN LIMITATION — swapping the config ISO does NOT re-run first-boot config on an
already-deployed VM. FirstBoot.ps1 is launched by SetupComplete.cmd, which Windows
fires only ONCE, during the post-Sysprep specialize/OOBE pass. After a VM has booted
past first boot, attaching a new ISO and powering on re-applies nothing — the runner
never runs again. Re-apply would need a persistent boot-time trigger (a non-one-shot
scheduled task or a small agent that detects a new config disc); that is intentionally
out of scope here. Use a new ISO at clone time (clone_vm.py) for per-VM config.

Examples:
  ./update_vm.py -n dc01 -s esxi7.example.com -u root -c 4 -r 8192
  ./update_vm.py -n dc01 -s esxi7.example.com -u root --iso isos/dc01-config.iso --power-on
"""

import argparse
import getpass
import logging
import os
import sys
import tempfile
import time

from vmkit.validate import (
    validate_cpus,
    validate_hostname_rfc,
    validate_iso_path,
    validate_mac,
    validate_memory,
)
from vmkit.progress import setup_logging
from vmkit.vmx import DEFAULT_GUEST_OS, parse_guest_os, render_vmx
from vmkit.esxi import (
    connect,
    get_datacenter,
    get_vm_by_name,
    power_off_vm,
    power_on_vm,
)
from vmkit.datastore import (
    read_datastore_file,
    upload_file,
)
from pyVmomi import vim

log = logging.getLogger("update-vm")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="update_vm.py",
        description=(
            "Update a registered VM's VMX (CPU, RAM, network, etc.) and optionally "
            "upload a new config ISO. The VM must exist and be powered off."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s -n dc01 -s esxi7.example.com -u root -c 4 -r 8192\n"
            "  %(prog)s -n dc01 -s esxi7.example.com -u root "
            "--iso isos/dc01-config.iso --power-on"
        ),
    )

    # Identity
    parser.add_argument(
        "-n",
        "--name",
        required=True,
        type=validate_hostname_rfc,
        metavar="NAME",
        help="VM name / hostname (RFC 1123).",
    )

    # VMX generation (optional, only if not all defaults)
    parser.add_argument(
        "-m",
        "--mac-address",
        default=None,
        type=validate_mac,
        metavar="MAC",
        help=(
            "Static MAC for ethernet0 (XX:XX:XX:XX:XX:XX). "
            "If omitted, uses the existing MAC."
        ),
    )
    parser.add_argument(
        "-c",
        "--cpus",
        default=None,
        type=validate_cpus,
        metavar="N",
        help="Number of vCPUs (power of 2, 1–128). If omitted, uses existing value.",
    )
    parser.add_argument(
        "-r",
        "--ram",
        default=None,
        type=validate_memory,
        metavar="MB",
        help="RAM in MB (multiple of 4, min 512). If omitted, uses existing value.",
    )
    parser.add_argument(
        "--iso",
        default=None,
        type=validate_iso_path,
        metavar="FILE",
        help=(
            "Local .iso file to upload before registration. "
            "Stored as {name}-config.iso in the VM folder and attached as CD-ROM."
        ),
    )
    parser.add_argument(
        "--remove-iso",
        action="store_true",
        help="Remove the CD-ROM attachment (do not upload or attach any ISO).",
    )

    # Connection
    parser.add_argument(
        "-s",
        "--server",
        required=True,
        metavar="HOST",
        help="ESXi host FQDN or IP.",
    )
    parser.add_argument(
        "-u",
        "--user",
        required=True,
        metavar="USER",
        help="ESXi username.",
    )
    parser.add_argument(
        "-p",
        "--password",
        default=None,
        metavar="PASS",
        help="ESXi password. If omitted, prompts securely.",
    )
    parser.add_argument(
        "-P",
        "--port",
        type=int,
        default=443,
        metavar="PORT",
        help="HTTPS port (default: 443).",
    )

    # Datastore
    parser.add_argument(
        "-d",
        "--datastore",
        default="datastore1",
        metavar="DS",
        help="Datastore name (default: datastore1).",
    )

    # Post-update
    parser.add_argument(
        "-o",
        "--power-on",
        action="store_true",
        help="Power on the VM after updating.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose (DEBUG) console output.",
    )
    return parser


def get_vm_config(vm: vim.VirtualMachine) -> dict:
    """Extract current CPU, RAM, and MAC from a registered VM."""
    config = vm.config
    cpus = config.hardware.numCPU
    mem_mb = config.hardware.memoryMB
    mac = None

    for device in config.hardware.device:
        if isinstance(device, vim.VirtualEthernetCard):
            mac = device.macAddress
            break

    return {"cpus": cpus, "mem_mb": mem_mb, "mac": mac}


def main() -> None:
    args = build_parser().parse_args()
    logfile_path = setup_logging(args.name, args.verbose)

    log.info("=" * 60)
    log.info("Update VM: %s  (datastore: %s)", args.name, args.datastore)
    log.info("=" * 60)
    log.info("Logging to: %s", logfile_path)

    if args.password:
        password = args.password
    else:
        try:
            password = getpass.getpass(
                f"Password for {args.user}@{args.server}: ", echo_char="*"
            )
        except (KeyboardInterrupt, EOFError):
            print()
            log.error("Authentication cancelled.")
            sys.exit(130)

    si = connect(args.server, args.user, password, args.port)
    content = si.content
    dc = get_datacenter(content)

    # Pre-flight: VM must exist
    vm = get_vm_by_name(content, args.name)
    if vm is None:
        log.error("VM '%s' not found. Aborting.", args.name)
        sys.exit(3)

    # Get current VM config
    current = get_vm_config(vm)
    log.info(
        "Current config: %d CPUs, %d MB RAM, MAC %s",
        current["cpus"],
        current["mem_mb"],
        current["mac"],
    )

    # Use provided values or fall back to current
    cpus = args.cpus if args.cpus is not None else current["cpus"]
    mem_mb = args.ram if args.ram is not None else current["mem_mb"]
    mac = args.mac_address if args.mac_address is not None else current["mac"]

    log.info(
        "New config: %d CPUs, %d MB RAM, MAC %s",
        cpus,
        mem_mb,
        mac,
    )

    # Render VMX in memory
    iso_filename = None
    if args.remove_iso:
        log.info("ISO: removing CD-ROM attachment")
        iso_filename = None
    elif args.iso:
        log.info("ISO: uploading new config ISO")
        iso_filename = f"{args.name}-config.iso"
    else:
        log.info("ISO: keeping existing attachment (if any)")
        iso_filename = f"{args.name}-config.iso"

    # Preserve the VM's guest OS: read it from the existing VMX rather than
    # letting the template default clobber it (e.g. back to Windows on a Linux VM).
    guest_os = DEFAULT_GUEST_OS
    try:
        existing_vmx = read_datastore_file(
            args.server, args.user, password, args.port,
            args.datastore, dc.name, f"{args.name}/{args.name}.vmx",
        )
        detected = parse_guest_os(existing_vmx)
        if detected:
            guest_os = detected
            log.info("Guest OS: %s (preserved from existing VMX)", guest_os)
        else:
            log.warning("No guestOS line in existing VMX; defaulting to %s.", guest_os)
    except Exception as exc:
        log.warning(
            "Could not read existing VMX (%s); defaulting guest OS to %s.",
            exc, guest_os,
        )

    vmx_content = render_vmx(
        args.name, mac, cpus, mem_mb, iso_filename=iso_filename, guest_os=guest_os
    )
    log.debug("VMX rendered (%d bytes)", len(vmx_content))

    # Power off the VM
    log.info("Powering off VM...")
    if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn:
        power_off_vm(content, args.name)
        time.sleep(2)
    else:
        log.info("VM is already powered off.")

    tmp_vmx = None
    try:
        # Upload new config ISO if provided
        if args.iso:
            upload_file(
                args.server,
                args.user,
                password,
                args.port,
                args.datastore,
                dc.name,
                args.name,
                args.iso,
                remote_filename=f"{args.name}-config.iso",
            )

        # Upload updated VMX
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".vmx", delete=False, encoding="utf-8"
        ) as f:
            f.write(vmx_content)
            tmp_vmx = f.name

        upload_file(
            args.server,
            args.user,
            password,
            args.port,
            args.datastore,
            dc.name,
            args.name,
            tmp_vmx,
            remote_filename=f"{args.name}.vmx",
        )

        log.info("VM configuration updated.")

    except Exception as exc:
        log.error("Update failed: %s", exc)
        log.debug("Traceback:", exc_info=True)
        sys.exit(4)
    finally:
        if tmp_vmx is not None:
            try:
                os.unlink(tmp_vmx)
            except OSError:
                pass

    if args.power_on:
        power_on_vm(content, args.name)

    log.info("Done.")


if __name__ == "__main__":
    main()
