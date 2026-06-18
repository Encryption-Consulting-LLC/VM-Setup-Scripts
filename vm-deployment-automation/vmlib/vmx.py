import random
import re

# Guest OS baked into a freshly rendered VMX when none can be read from an
# existing VMX (e.g. a base VM whose .vmx is missing or unreadable). This keeps
# the historical Windows Server default for backward compatibility.
DEFAULT_GUEST_OS = "windows2022srvNext-64"

_GUEST_OS_RE = re.compile(r'^\s*guestOS\s*=\s*"(.+?)"', re.IGNORECASE | re.MULTILINE)

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
guestOS = "{guest_os}"
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
{cdrom_block}svga.guestBackedPrimaryAware = "TRUE"
tools.capability.verifiedSamlToken = "TRUE"
tools.remindInstall = "FALSE"
toolsInstallManager.updateCounter = "1"
usb_xhci:4.present = "TRUE"
usb_xhci:4.deviceType = "hid"
usb_xhci:4.port = "4"
usb_xhci:4.parent = "-1"
"""

_CDROM_BLOCK = (
    'sata0:0.present = "TRUE"\n'
    'sata0:0.deviceType = "cdrom-image"\n'
    'sata0:0.fileName = "{iso_filename}"\n'
    'sata0:0.startConnected = "TRUE"\n'
)


def random_mac() -> str:
    """Generate a random MAC in the VMware static OUI range 00:50:56:00:00:00 – 00:50:56:3F:FF:FF."""
    b1 = random.randint(0x00, 0x3F)
    b2 = random.randint(0x00, 0xFF)
    b3 = random.randint(0x00, 0xFF)
    return f"00:50:56:{b1:02x}:{b2:02x}:{b3:02x}"


def parse_guest_os(vmx_text: str) -> str | None:
    """Return the guestOS identifier from VMX text, or None if absent.

    Reads the literal value of the `guestOS = "..."` line so it can be re-emitted
    verbatim into a freshly rendered VMX (preserving a base/existing VM's OS type
    instead of hardcoding one).
    """
    match = _GUEST_OS_RE.search(vmx_text)
    return match.group(1) if match else None


def render_vmx(
    hostname: str,
    mac_address: str,
    num_cpus: int,
    mem_mb: int,
    iso_filename: str | None = None,
    guest_os: str = DEFAULT_GUEST_OS,
) -> str:
    cdrom_block = (
        _CDROM_BLOCK.format(iso_filename=iso_filename)
        if iso_filename is not None
        else ""
    )
    return VMX_TEMPLATE.format(
        hostname=hostname,
        mac_address=mac_address,
        num_cpus=num_cpus,
        mem_mb=mem_mb,
        cdrom_block=cdrom_block,
        guest_os=guest_os,
    )
