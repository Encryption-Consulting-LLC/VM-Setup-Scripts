#!/usr/bin/env python3
"""
deploy-vm.py — Automate cloning a base VM and registering it on a standalone ESXi host.

The .vmdk / .nvram copies happen entirely on the ESXi server (no download/re-upload).
Only the small .vmx is uploaded from the local machine.

Examples:
  ./deploy-vm.py -n dc01 -f dc01.vmx -s esxi7.example.com -u root
  ./deploy-vm.py -n dc01 -f dc01.vmx -s esxi7.example.com -u root \\
      --datastore datastore1 --base ws-2025-base --power-on
"""

import argparse
import atexit
import getpass
import logging
import ssl
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim

from tqdm import tqdm


def make_progress_bar(
    total: float, desc: str, unit: str = "%", unit_scale: bool = False
) -> tqdm:
    """Build an apt-style, single-line progress bar backed by tqdm.

    tqdm draws on the current line with a carriage return (no scroll region, no
    bottom-pinning), so ordinary log lines keep scrolling above it normally and
    no blank gap is introduced. ``leave=False`` erases the bar on ``close()`` so
    the following log line takes its place cleanly.

    ``disable=None`` auto-disables the bar when stdout is not a TTY
    (redirected/piped output), keeping captured text and the log file clean.
    """
    return tqdm(
        total=total,
        desc=desc,
        unit=unit,
        unit_scale=unit_scale,
        leave=False,
        file=sys.stdout,  # share the stream with logging
        dynamic_ncols=True,  # adapt to terminal width / resizes
        disable=None,  # auto-disable when stdout is not a TTY
    )


def human_bytes(n: float) -> str:
    """Format a byte count as a human-readable string (e.g. 12.3 GiB)."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

log = logging.getLogger("deploy-vm")


def setup_logging(name: str, verbose: bool) -> str:
    """Configure console + timestamped file logging. Returns the log file path."""
    log.setLevel(logging.DEBUG)  # handlers filter further

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler — honours -v for verbosity.
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(fmt)
    log.addHandler(ch)

    # File handler — stamp the datetime into the filename so each run gets its
    # own log (e.g. deploy-vm.log -> deploy-vm_20260603-232240.log). The file
    # always captures DEBUG-level detail regardless of -v.
    stamp = datetime.now().strftime("%s")
    logfile = f"deploy_{name}_{stamp}.log"
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    log.debug("Logging initialised. Log file: %s", logfile)
    return str(logfile)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def connect(host: str, user: str, password: str, port: int):
    """Connect to ESXi/vCenter, return the ServiceInstance."""
    log.info("Connecting to %s as %s ...", host, user)
    ctx = ssl._create_unverified_context()
    try:
        si = SmartConnect(host=host, user=user, pwd=password, port=port, sslContext=ctx)
    except vim.fault.InvalidLogin:
        log.error("Login failed: invalid username or password.")
        sys.exit(2)
    except Exception as exc:
        log.error("Could not connect to %s: %s", host, exc)
        sys.exit(2)
    atexit.register(Disconnect, si)
    log.info("Connected. API version: %s", si.content.about.fullName)
    return si


# ---------------------------------------------------------------------------
# Inventory helpers
# ---------------------------------------------------------------------------


def get_datacenter(content):
    """Return the first (only, on standalone ESXi) datacenter."""
    for child in content.rootFolder.childEntity:
        if isinstance(child, vim.Datacenter):
            return child
    raise RuntimeError("No datacenter found on host.")


def list_vm_names(content) -> set:
    """Return a set of all VM names currently in inventory."""
    view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    try:
        return {vm.name for vm in view.view}
    finally:
        view.Destroy()


def get_datastore(content, name):
    """Return the datastore object matching ``name``."""
    view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.Datastore], True
    )
    try:
        for ds in view.view:
            if ds.name == name:
                return ds
    finally:
        view.Destroy()
    raise RuntimeError(f"Datastore '{name}' not found on host.")


def get_base_vmdk_size(datastore_obj, base) -> int:
    """Return the total on-disk size (bytes) of the base VM's .vmdk file(s).

    Browses the base VM's folder and sums the size of every matching .vmdk
    component (descriptor + flat/extent files), giving the actual datastore
    space a server-side clone of the disk will consume.
    """
    browser = datastore_obj.browser
    details = vim.host.DatastoreBrowser.FileInfo.Details(fileSize=True, fileType=True)
    spec = vim.host.DatastoreBrowser.SearchSpec(
        matchPattern=[f"{base}*.vmdk"], details=details
    )
    folder = f"[{datastore_obj.name}] {base}"
    task = browser.SearchDatastore_Task(datastorePath=folder, searchSpec=spec)
    while task.info.state in (vim.TaskInfo.State.running, vim.TaskInfo.State.queued):
        time.sleep(0.5)
    if task.info.state != vim.TaskInfo.State.success:
        err = task.info.error
        msg = err.msg if err else "unknown error"
        raise RuntimeError(f"Could not read base VMDK size: {msg}")
    return sum((getattr(f, "fileSize", 0) or 0) for f in (task.info.result.file or []))


# ---------------------------------------------------------------------------
# Task progress
# ---------------------------------------------------------------------------


def wait_for_task(task, label: str) -> object:
    """Block until a vSphere task completes, showing a progress bar (logging untouched)."""
    bar = make_progress_bar(total=100, desc=label, unit="%")
    last_pct = 0
    while task.info.state in (vim.TaskInfo.State.running, vim.TaskInfo.State.queued):
        pct = task.info.progress or 0
        if pct > last_pct:
            if bar is not None:
                bar.update(pct - last_pct)
            last_pct = pct
        time.sleep(1)

    if task.info.state == vim.TaskInfo.State.success:
        if bar is not None:
            bar.update(100 - last_pct)  # ensure it lands on 100%
            bar.close()
        log.info("  %s: done", label)
        return task.info.result
    else:
        if bar is not None:
            bar.close()
        err = task.info.error
        msg = err.msg if err else "unknown error"
        log.error("  %s FAILED: %s", label, msg)
        raise RuntimeError(f"{label} failed: {msg}")


# ---------------------------------------------------------------------------
# Datastore operations
# ---------------------------------------------------------------------------


def make_directory(content, dc, datastore: str, name: str) -> None:
    """Create <datastore>/<name>/ directory (no error if it already exists)."""
    path = f"[{datastore}] {name}"
    log.info("Creating directory: %s", path)
    fm = content.fileManager
    try:
        fm.MakeDirectory(name=path, datacenter=dc, createParentDirectories=True)
        log.info("  Directory created.")
    except vim.fault.FileAlreadyExists:
        log.warning("  Directory already exists, continuing.")


def copy_virtual_disk(content, dc, datastore, base, name) -> None:
    """Server-side copy of the base VMDK to the new VM folder."""
    src = f"[{datastore}] {base}/{base}.vmdk"
    dst = f"[{datastore}] {name}/{name}.vmdk"
    log.info("Copying virtual disk:")
    log.info("  src: %s", src)
    log.info("  dst: %s", dst)
    vdm = content.virtualDiskManager
    task = vdm.CopyVirtualDisk_Task(
        sourceName=src,
        sourceDatacenter=dc,
        destName=dst,
        destDatacenter=dc,
        force=False,
    )
    wait_for_task(task, "VMDK copy")


def copy_datastore_file(content, dc, datastore, base, name, ext) -> None:
    """Server-side copy of an arbitrary datastore file (e.g. .nvram)."""
    src = f"[{datastore}] {base}/{base}.{ext}"
    dst = f"[{datastore}] {name}/{name}.{ext}"
    log.info("Copying %s file:", ext)
    log.info("  src: %s", src)
    log.info("  dst: %s", dst)
    fm = content.fileManager
    task = fm.CopyDatastoreFile_Task(
        sourceName=src,
        sourceDatacenter=dc,
        destinationName=dst,
        destinationDatacenter=dc,
        force=False,
    )
    wait_for_task(task, f"{ext} copy")


def upload_file(
    host, user, password, port, datastore, dc_name, name, local_path
) -> None:
    """Upload a local file to <datastore>/<name>/<name>.vmx via HTTPS PUT, with progress."""
    local = Path(local_path)
    if not local.is_file():
        raise FileNotFoundError(f"Local VMX not found: {local}")

    remote_path = f"{name}/{name}.vmx"
    url = (
        f"https://{host}:{port}/folder/{remote_path}"
        f"?dcPath={dc_name}&dsName={datastore}"
    )
    total = local.stat().st_size
    log.info("Uploading VMX:")
    log.info("  local : %s (%d bytes)", local, total)
    log.info("  remote: [%s] %s", datastore, remote_path)

    class _ProgressReader:
        """Wrap a file object to drive a byte-based progress bar."""

        def __init__(self, fileobj, total_bytes):
            self.f = fileobj
            self.total = total_bytes
            self.bar = make_progress_bar(
                total=total_bytes, desc="VMX upload", unit="B", unit_scale=True
            )

        def read(self, size=-1):
            chunk = self.f.read(size)
            self.bar.update(len(chunk))
            return chunk

        def close(self):
            self.bar.close()

        def __len__(self):
            return self.total

    reader = None
    with open(local, "rb") as fh:
        reader = _ProgressReader(fh, total)
        try:
            resp = requests.put(
                url,
                data=reader,
                auth=(user, password),
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(total),
                },
                verify=False,
                timeout=120,
            )
        finally:
            reader.close()
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Upload failed (HTTP {resp.status_code}): {resp.text[:200]}"
        )
    log.info("  VMX upload: done")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_vm(content, dc, datastore, name) -> None:
    """Register the VM from its .vmx file."""
    vmx_ds_path = f"[{datastore}] {name}/{name}.vmx"
    log.info("Registering VM from: %s", vmx_ds_path)

    # Find a host + resource pool to register against (standalone ESXi: just one)
    host_view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.HostSystem], True
    )
    try:
        esxi_host = host_view.view[0]
    finally:
        host_view.Destroy()
    resource_pool = esxi_host.parent.resourcePool

    task = dc.vmFolder.RegisterVM_Task(
        path=vmx_ds_path,
        name=name,
        asTemplate=False,
        pool=resource_pool,
        host=esxi_host,
    )
    wait_for_task(task, "Register VM")


def power_on_vm(content, name) -> None:
    """Power on the named VM."""
    view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    try:
        target = next((vm for vm in view.view if vm.name == name), None)
    finally:
        view.Destroy()
    if not target:
        log.warning("Could not find VM '%s' to power on.", name)
        return
    log.info("Powering on VM: %s", name)
    task = target.PowerOnVM_Task()
    wait_for_task(task, "Power on")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploy-vm.py",
        description=(
            "Clone a base VM (server-side disk copy) and register it on a "
            "standalone ESXi host, then confirm it appears in inventory."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s -n dc01 -f dc01.vmx -s esxi7.example.com -u root\n"
            "  %(prog)s -n dc01 -f dc01.vmx -s esxi7.example.com -u root "
            "--base ws-2025-base --power-on"
        ),
    )
    parser.add_argument(
        "-n",
        "--name",
        required=True,
        metavar="NAME",
        help="Name for the new VM (and its folder/files).",
    )
    parser.add_argument(
        "-f",
        "--vmx-file",
        required=False,
        default=None,
        metavar="FILE",
        help="Local path to the .vmx file to upload. "
        "If omitted, defaults to <NAME>.vmx in the current directory.",
    )
    parser.add_argument(
        "-s", "--server", required=True, metavar="HOST", help="ESXi host FQDN or IP."
    )
    parser.add_argument(
        "-u", "--user", required=True, metavar="USER", help="ESXi username."
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
        help="Abort if the datastore would exceed this %% full after cloning "
        "the base VMDK (default: 80).",
    )
    parser.add_argument(
        "--skip-disk-check",
        action="store_true",
        help="Skip the datastore free-space pre-flight check entirely.",
    )
    parser.add_argument(
        "-o",
        "--power-on",
        action="store_true",
        help="Power on the VM after registration.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose (DEBUG) console output."
    )
    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    args = build_parser().parse_args()
    logfile_path = setup_logging(args.name, args.verbose)

    log.info("=" * 60)
    log.info(
        "Deploy VM: %s  (base: %s, datastore: %s)", args.name, args.base, args.datastore
    )
    log.info("=" * 60)
    log.info("Logging to: %s", logfile_path)

    # Resolve VMX path — fall back to <NAME>.vmx when -f is omitted
    if args.vmx_file:
        vmx_file = args.vmx_file
    else:
        vmx_file = f"{args.name}.vmx"
        log.info(
            "No -f/--vmx-file supplied; looking for '%s' in current directory.",
            vmx_file,
        )
        if not Path(vmx_file).is_file():
            log.error(
                "Expected VMX '%s' not found. Generate it first "
                "(e.g. gen_vmx.py -n %s) or pass -f. Aborting.",
                vmx_file,
                args.name,
            )
            sys.exit(3)

    if args.password:
        password = args.password
    else:
        try:
            # echo_char (Python 3.14+) masks each typed character with '*'.
            password = getpass.getpass(
                f"Password for {args.user}@{args.server}: ", echo_char="*"
            )
        except (KeyboardInterrupt, EOFError):
            print()  # move off the prompt line
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
            "Skipping datastore free-space check (--skip-disk-check).\n Hope you know what you're doing!"
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
                "Limited disk resource: cloning the base VMDK would leave datastore '%s' at %.1f%% full, exceeding the %.1f%% limit. Aborting.",
                args.datastore,
                pct_after,
                args.max_usage,
            )
            sys.exit(6)

    try:
        make_directory(content, dc, args.datastore, args.name)
        copy_virtual_disk(content, dc, args.datastore, args.base, args.name)
        copy_datastore_file(content, dc, args.datastore, args.base, args.name, "nvram")
        upload_file(
            args.server,
            args.user,
            password,
            args.port,
            args.datastore,
            dc.name,
            args.name,
            vmx_file,
        )
        register_vm(content, dc, args.datastore, args.name)
    except Exception as exc:
        log.error("Deployment failed: %s", exc)
        log.debug("Traceback:", exc_info=True)
        sys.exit(4)

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
