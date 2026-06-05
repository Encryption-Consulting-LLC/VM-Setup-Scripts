#!/usr/bin/env python3
"""
deploy_vm.py — Generate a VMX, clone a base VM's disk, and register it on ESXi.

The .vmdk / .nvram copies happen entirely on the ESXi server (no download/re-upload).
The VMX is rendered in memory and uploaded. An optional config ISO can be uploaded
to the VM's folder before registration.

Examples:
  ./deploy_vm.py -n dc01 -s esxi7.example.com -u root
  ./deploy_vm.py -n dc01 -s esxi7.example.com -u root \\
      --datastore datastore1 --base ws-2025-base --iso isos/dc01-config.iso --power-on
"""

import argparse
import getpass
import logging
import os
import sys
import tempfile
import time

from vmlib.validate import (
    validate_cpus,
    validate_hostname_rfc,
    validate_iso_path,
    validate_mac,
    validate_memory,
)
from vmlib.progress import human_bytes, setup_logging
from vmlib.vmx import random_mac, render_vmx
from vmlib.esxi import (
    connect,
    get_datacenter,
    get_datastore,
    list_vm_names,
    power_on_vm,
    register_vm,
)
from vmlib.datastore import (
    copy_datastore_file,
    copy_virtual_disk,
    get_base_vmdk_size,
    make_directory,
    upload_file,
)

log = logging.getLogger("deploy-vm")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploy_vm.py",
        description=(
            "Render a VMX in memory, clone a base VM's disk server-side, and register "
            "it on a standalone ESXi host. Optionally upload a config ISO before "
            "registration and power on the VM when done."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s -n dc01 -s esxi7.example.com -u root\n"
            "  %(prog)s -n dc01 -s esxi7.example.com -u root "
            "--base ws-2025-base --iso isos/dc01-config.iso --power-on"
        ),
    )

    # Identity
    parser.add_argument(
        "-n",
        "--name",
        required=True,
        type=validate_hostname_rfc,
        metavar="NAME",
        help="VM name / hostname (RFC 1123). Used as the folder and file base name.",
    )

    # VMX generation
    parser.add_argument(
        "-m",
        "--mac-address",
        default=None,
        type=validate_mac,
        metavar="MAC",
        help=(
            "Static MAC for ethernet0 (XX:XX:XX:XX:XX:XX). "
            "VMware static range: 00:50:56:00:00:00 – 00:50:56:3F:FF:FF. "
            "Defaults to a random MAC in that range."
        ),
    )
    parser.add_argument(
        "-c",
        "--cpus",
        default=2,
        type=validate_cpus,
        metavar="N",
        help="Number of vCPUs (power of 2, 1–128). Default: 2.",
    )
    parser.add_argument(
        "-r",
        "--ram",
        default=4096,
        type=validate_memory,
        metavar="MB",
        help="RAM in MB (multiple of 4, min 512). Default: 4096 (4 GB).",
    )
    parser.add_argument(
        "--iso",
        default=None,
        type=validate_iso_path,
        metavar="FILE",
        help=(
            "Local .iso file to upload before VMX registration. "
            "Stored as {name}-config.iso in the VM folder and attached as CD-ROM."
        ),
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

    # Datastore / clone
    parser.add_argument(
        "-d",
        "--datastore",
        default="datastore1",
        metavar="DS",
        help="Datastore name (default: datastore1).",
    )
    parser.add_argument(
        "-b",
        "--base",
        default="ws-2025-base",
        metavar="BASE",
        help="Base VM folder/file name to clone (default: ws-2025-base).",
    )
    parser.add_argument(
        "--max-usage",
        type=float,
        default=80.0,
        metavar="PCT",
        help=(
            "Abort if the datastore would exceed this %% full after cloning "
            "the base VMDK (default: 80)."
        ),
    )
    parser.add_argument(
        "--skip-disk-check",
        action="store_true",
        help="Skip the datastore free-space pre-flight check entirely.",
    )

    # Post-deploy
    parser.add_argument(
        "-o",
        "--power-on",
        action="store_true",
        help="Power on the VM after registration.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose (DEBUG) console output.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logfile_path = setup_logging(args.name, args.verbose)

    log.info("=" * 60)
    log.info(
        "Deploy VM: %s  (base: %s, datastore: %s)",
        args.name,
        args.base,
        args.datastore,
    )
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

    # Pre-flight: refuse if name already exists
    existing = list_vm_names(content)
    if args.name in existing:
        log.error("A VM named '%s' already exists. Aborting.", args.name)
        sys.exit(3)

    # Pre-flight: refuse if cloning the base VMDK would over-fill the datastore
    if args.skip_disk_check:
        log.warning(
            "Skipping datastore free-space check (--skip-disk-check). "
            "Hope you know what you're doing!"
        )
    else:
        log.info("Checking disk usage constraints...")
        ds = get_datastore(content, args.datastore)
        capacity = ds.summary.capacity
        free = ds.summary.freeSpace
        used = capacity - free
        vmdk_size = get_base_vmdk_size(ds, args.base)
        used_after = used + vmdk_size
        pct_now = (used / capacity * 100) if capacity else 0.0
        pct_after = (used_after / capacity * 100) if capacity else 0.0

        log.info("Datastore '%s' disk usage:", args.datastore)
        log.info("  capacity      : %s", human_bytes(capacity))
        log.info("  used (now)    : %s (%.1f%%)", human_bytes(used), pct_now)
        log.info("  base VMDK     : %s", human_bytes(vmdk_size))
        log.info("  used (after)  : %s (%.1f%%)", human_bytes(used_after), pct_after)

        if pct_after > args.max_usage:
            log.error(
                "Limited disk resource: cloning the base VMDK would leave datastore "
                "'%s' at %.1f%% full, exceeding the %.1f%% limit. Aborting.",
                args.datastore,
                pct_after,
                args.max_usage,
            )
            sys.exit(6)

    # Resolve MAC
    mac = args.mac_address or random_mac()
    mac_source = "specified" if args.mac_address else "randomly generated"
    log.info("MAC address: %s (%s)", mac, mac_source)

    # Render VMX in memory
    iso_filename = f"{args.name}-config.iso" if args.iso else None
    vmx_content = render_vmx(
        args.name, mac, args.cpus, args.ram, iso_filename=iso_filename
    )
    log.debug("VMX rendered (%d bytes)", len(vmx_content))

    tmp_vmx = None
    try:
        make_directory(content, dc, args.datastore, args.name)
        copy_virtual_disk(content, dc, args.datastore, args.base, args.name)
        copy_datastore_file(content, dc, args.datastore, args.base, args.name, "nvram")

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

        register_vm(content, dc, args.datastore, args.name)

    except Exception as exc:
        log.error("Deployment failed: %s", exc)
        log.debug("Traceback:", exc_info=True)
        sys.exit(4)
    finally:
        if tmp_vmx is not None:
            try:
                os.unlink(tmp_vmx)
            except OSError:
                pass

    # Confirm
    log.info("Waiting for inventory to settle ...")
    time.sleep(3)
    names_after = list_vm_names(content)
    if args.name in names_after:
        log.info("CONFIRMED: VM '%s' is now in inventory.", args.name)
    else:
        log.error("VM '%s' NOT found in inventory after registration!", args.name)
        sys.exit(5)

    if args.power_on:
        power_on_vm(content, args.name)

    log.info("Done. Total VMs in inventory: %d", len(names_after))


if __name__ == "__main__":
    main()
