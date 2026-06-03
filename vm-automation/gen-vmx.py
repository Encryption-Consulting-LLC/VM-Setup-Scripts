#!/usr/bin/env python3
"""
gen-vmx.py — Generate a VMX configuration file for ESXi deployment.

Replaces the template hostname placeholder with the supplied hostname,
sets the MAC address (static), and writes the result to a .vmx file.
If --mac-address is omitted, a random MAC in the VMware static OUI
range (00:50:56:00:00:00 – 00:50:56:3F:FF:FF) is generated.

Usage:
    ./gen-vmx.py -n dc01
    ./gen-vmx.py -n dc01 -m 00:50:56:00:00:02
    ./gen-vmx.py -n dc01 -m 00:50:56:00:00:02 -c 4 -r 8192
    ./gen-vmx.py -n dc01 -m 00:50:56:00:00:02 -o my-dc01.vmx
"""

import argparse
import random
import re
import sys
from pathlib import Path

VMX_TEMPLATE = """\
.encoding = "UTF-8"
config.version = "8"
virtualHW.version = "21"
nvram = "{hostname}.nvram"
svga.present = "TRUE"
vmci0.present = "TRUE"
hpet0.present = "TRUE"
floppy0.present = "FALSE"
RemoteDisplay.maxConnections = "-1"
numvcpus = "{num_cpus}"
memSize = "{mem_mb}"
bios.bootRetry.delay = "10"
firmware = "efi"
powerType.powerOff = "default"
powerType.suspend = "soft"
powerType.reset = "default"
tools.upgrade.policy = "manual"
sched.cpu.units = "mhz"
sched.cpu.affinity = "all"
sched.cpu.latencySensitivity = "normal"
scsi0.virtualDev = "pvscsi"
scsi0.present = "TRUE"
sata0.present = "TRUE"
usb_xhci.present = "TRUE"
svga.autodetect = "TRUE"
scsi0:0.deviceType = "scsi-hardDisk"
scsi0:0.fileName = "{hostname}.vmdk"
sched.scsi0:0.shares = "normal"
sched.scsi0:0.throughputCap = "off"
scsi0:0.present = "TRUE"
ethernet0.virtualDev = "vmxnet3"
ethernet0.networkName = "VM Network"
ethernet0.addressType = "static"
ethernet0.address = "{mac_address}"
ethernet0.wakeOnPcktRcv = "FALSE"
ethernet0.uptCompatibility = "TRUE"
ethernet0.present = "TRUE"
displayName = "{hostname}"
guestOS = "windows2022srvNext-64"
chipset.motherboardLayout = "acpi"
uefi.secureBoot.enabled = "TRUE"
disk.EnableUUID = "TRUE"
toolScripts.afterPowerOn = "TRUE"
toolScripts.afterResume = "TRUE"
toolScripts.beforeSuspend = "TRUE"
toolScripts.beforePowerOff = "TRUE"
tools.syncTime = "FALSE"
sched.cpu.min = "0"
sched.cpu.shares = "normal"
sched.mem.min = "0"
sched.mem.minSize = "0"
sched.mem.shares = "normal"
vmxstats.filename = "{hostname}.scoreboard"
numa.autosize.cookie = "20012"
numa.autosize.vcpu.maxPerVirtualNode = "{num_cpus}"
cpuid.coresPerSocket.cookie = "{num_cpus}"
pciBridge1.present = "TRUE"
pciBridge1.virtualDev = "pciRootBridge"
pciBridge1.functions = "1"
pciBridge1:0.pxm = "0"
pciBridge0.present = "TRUE"
pciBridge0.virtualDev = "pciRootBridge"
pciBridge0.functions = "1"
pciBridge0.pxm = "-1"
scsi0.pciSlotNumber = "32"
ethernet0.pciSlotNumber = "33"
usb_xhci.pciSlotNumber = "34"
sata0.pciSlotNumber = "35"
migrate.hostlog = "./{hostname}.hlog"
scsi0:0.redo = ""
svga.vramSize = "16777216"
vmotion.checkpointFBSize = "4194304"
vmotion.checkpointSVGAPrimarySize = "16777216"
vmotion.svga.mobMaxSize = "16777216"
vmotion.svga.graphicsMemoryKB = "16384"
extendedConfigFile = "{hostname}.vmxf"
monitor.phys_bits_used = "45"
cleanShutdown = "TRUE"
softPowerOff = "TRUE"
sata0:0.startConnected = "TRUE"
svga.guestBackedPrimaryAware = "TRUE"
tools.capability.verifiedSamlToken = "TRUE"
tools.remindInstall = "FALSE"
toolsInstallManager.updateCounter = "1"
usb_xhci:4.present = "TRUE"
usb_xhci:4.deviceType = "hid"
usb_xhci:4.port = "4"
usb_xhci:4.parent = "-1"
"""

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

MAC_RE = re.compile(
    r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"
)

VMWARE_STATIC_OUI = "00:50:56"

def validate_mac(mac: str) -> str:
    """Validate MAC address format and warn if outside VMware static OUI."""
    if not MAC_RE.match(mac):
        raise argparse.ArgumentTypeError(
            f"Invalid MAC address '{mac}'. "
            "Expected format: XX:XX:XX:XX:XX:XX (e.g. 00:50:56:00:00:01)"
        )
    if not mac.lower().startswith(VMWARE_STATIC_OUI):
        print(
            f"WARNING: MAC '{mac}' is outside the VMware static OUI range "
            f"({VMWARE_STATIC_OUI}:00:00:00 – {VMWARE_STATIC_OUI}:3F:FF:FF). "
            "ESXi may reject or override it.",
            file=sys.stderr,
        )
    return mac.lower()


def random_mac() -> str:
    """Generate a random MAC in the VMware static OUI range 00:50:56:00:00:00 – 00:50:56:3F:FF:FF."""
    # Last three octets: first is capped at 0x3F to stay inside the valid static range
    b1 = random.randint(0x00, 0x3F)
    b2 = random.randint(0x00, 0xFF)
    b3 = random.randint(0x00, 0xFF)
    return f"00:50:56:{b1:02x}:{b2:02x}:{b3:02x}"


def validate_hostname(hostname: str) -> str:
    """Basic hostname sanity check — no spaces or characters invalid in filenames."""
    invalid = set(' /\\:*?"<>|')
    bad = [c for c in hostname if c in invalid]
    if bad:
        raise argparse.ArgumentTypeError(
            f"Hostname '{hostname}' contains invalid characters: "
            + " ".join(repr(c) for c in bad)
        )
    return hostname


def validate_cpus(value: str) -> int:
    """Validate CPU count: positive integer, power of 2, max 128."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid CPU count '{value}': must be a positive integer."
        )
    if n < 1:
        raise argparse.ArgumentTypeError(
            f"Invalid CPU count '{value}': must be at least 1."
        )
    if n > 128:
        raise argparse.ArgumentTypeError(
            f"Invalid CPU count '{value}': ESXi supports a maximum of 128 vCPUs."
        )
    if (n & (n - 1)) != 0:
        raise argparse.ArgumentTypeError(
            f"Invalid CPU count '{value}': must be a power of 2 (1, 2, 4, 8, 16…)."
        )
    return n


def validate_memory(value: str) -> int:
    """Validate memory in MB: positive integer, multiple of 4, min 512 MB."""
    try:
        mb = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid memory value '{value}': must be a positive integer (MB)."
        )
    if mb < 512:
        raise argparse.ArgumentTypeError(
            f"Invalid memory value '{value}': minimum is 512 MB."
        )
    if mb % 4 != 0:
        raise argparse.ArgumentTypeError(
            f"Invalid memory value '{value}': must be a multiple of 4 MB."
        )
    return mb


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gen-vmx.py",
        description=(
            "Generate an ESXi VMX configuration file for a Windows Server 2025 VM.\n\n"
            "The script substitutes HOSTNAME throughout the template and sets the\n"
            "NIC to a static MAC address. UUID/genid fields are omitted so ESXi\n"
            "regenerates unique values on first registration.\n\n"
            "VMware static MAC OUI: 00:50:56:00:00:00 – 00:50:56:3F:FF:FF\n"
            "If --mac-address is omitted, a random MAC in this range is generated."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s -n dc01                                   # random MAC, 2 CPUs, 4 GB\n"
            "  %(prog)s -n dc01 -m 00:50:56:00:00:02\n"
            "  %(prog)s -n dc01 -c 4 -r 8192                      # 4 vCPUs, 8 GB RAM\n"
            "  %(prog)s -n dc01 -m 00:50:56:00:00:02 -c 4 -r 16384 -o /tmp/dc01.vmx"
        )
    )

    parser.add_argument(
        "-n", "--hostname",
        required=True,
        type=validate_hostname,
        metavar="HOSTNAME",
        help=(
            "VM hostname / display name. Used as the base name for all "
            "hostname-derived filenames in the VMX (e.g. HOSTNAME.vmdk, "
            "HOSTNAME.nvram, HOSTNAME.scoreboard)."
        ),
    )

    parser.add_argument(
        "-m", "--mac-address",
        required=False,
        default=None,
        type=validate_mac,
        metavar="MAC",
        help=(
            "Static MAC address for the primary NIC (ethernet0). "
            "Format: XX:XX:XX:XX:XX:XX. "
            "VMware static range: 00:50:56:00:00:00 – 00:50:56:3F:FF:FF. "
            "If omitted, a random MAC in this range is generated."
        ),
    )

    parser.add_argument(
        "-c", "--cpus",
        required=False,
        default=2,
        type=validate_cpus,
        metavar="N",
        help=(
            "Number of vCPUs. Must be a power of 2 between 1 and 128. "
            "Default: 2."
        ),
    )

    parser.add_argument(
        "-r", "--ram",
        required=False,
        default=4096,
        type=validate_memory,
        metavar="MB",
        help=(
            "RAM size in megabytes. Must be a multiple of 4, minimum 512. "
            "Default: 4096 (4 GB). "
            "Examples: 2048 (2 GB), 8192 (8 GB), 16384 (16 GB)."
        ),
    )

    parser.add_argument(
        "-o", "--output",
        default=None,
        metavar="FILE",
        help=(
            "Output file path. Defaults to HOSTNAME.vmx in the current "
            "directory if not specified."
        ),
    )

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Resolve MAC — use supplied value or generate a random one
    if args.mac_address:
        mac = args.mac_address
        mac_source = "specified"
    else:
        mac = random_mac()
        mac_source = "randomly generated"

    # Determine output path
    output_path = Path(args.output) if args.output else Path(f"{args.hostname}.vmx")

    # Render template
    vmx_content = VMX_TEMPLATE.format(
        hostname=args.hostname,
        mac_address=mac,
        num_cpus=args.cpus,
        mem_mb=args.ram,
    )

    # Write output
    try:
        output_path.write_text(vmx_content, encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: Could not write to '{output_path}': {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"VMX written to: {output_path}")
    print(f"  displayName  = {args.hostname}")
    print(f"  MAC address  = {mac}  ({mac_source})")
    print(f"  vCPUs        = {args.cpus}")
    print(f"  RAM          = {args.ram} MB  ({args.ram // 1024} GB)")
    print(f"  Disk file    = {args.hostname}.vmdk")


if __name__ == "__main__":
    main()
