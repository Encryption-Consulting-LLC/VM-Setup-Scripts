import logging
import time
from pathlib import Path
from typing import IO

import urllib3
import requests

from pyVmomi import vim

from vmlib.progress import make_progress_bar

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("deploy-vm")


def make_directory(
    content: vim.ServiceInstanceContent,
    dc: vim.Datacenter,
    datastore: str,
    name: str,
) -> None:
    """Create <datastore>/<name>/ directory (no error if it already exists)."""
    path = f"[{datastore}] {name}"
    log.info("Creating directory: %s", path)
    fm = content.fileManager
    assert fm is not None
    try:
        fm.MakeDirectory(name=path, datacenter=dc, createParentDirectories=True)
        log.info("  Directory created.")
    except vim.fault.FileAlreadyExists:
        log.warning("  Directory already exists, continuing.")


def copy_virtual_disk(
    content: vim.ServiceInstanceContent,
    dc: vim.Datacenter,
    datastore: str,
    base: str,
    name: str,
) -> None:
    """Server-side copy of the base VMDK to the new VM folder."""
    from vmlib.esxi import wait_for_task

    src = f"[{datastore}] {base}/{base}.vmdk"
    dst = f"[{datastore}] {name}/{name}.vmdk"
    log.info("Copying virtual disk:")
    log.info("  src: %s", src)
    log.info("  dst: %s", dst)
    vdm = content.virtualDiskManager
    assert vdm is not None
    task = vdm.CopyVirtualDisk_Task(
        sourceName=src,
        sourceDatacenter=dc,
        destName=dst,
        destDatacenter=dc,
        force=False,
    )
    wait_for_task(task, "VMDK copy")


def copy_datastore_file(
    content: vim.ServiceInstanceContent,
    dc: vim.Datacenter,
    datastore: str,
    base: str,
    name: str,
    ext: str,
) -> None:
    """Server-side copy of an arbitrary datastore file (e.g. .nvram)."""
    from vmlib.esxi import wait_for_task

    src = f"[{datastore}] {base}/{base}.{ext}"
    dst = f"[{datastore}] {name}/{name}.{ext}"
    log.info("Copying %s file:", ext)
    log.info("  src: %s", src)
    log.info("  dst: %s", dst)
    fm = content.fileManager
    assert fm is not None
    task = fm.CopyDatastoreFile_Task(
        sourceName=src,
        sourceDatacenter=dc,
        destinationName=dst,
        destinationDatacenter=dc,
        force=False,
    )
    wait_for_task(task, f"{ext} copy")


def upload_file(
    host: str,
    user: str,
    password: str,
    port: int,
    datastore: str,
    dc_name: str,
    vm_name: str,
    local_path: str,
    remote_filename: str | None = None,
) -> None:
    """Upload a local file to <datastore>/<vm_name>/<remote_filename> via HTTPS PUT."""
    local = Path(local_path)
    if not local.is_file():
        raise FileNotFoundError(f"Local file not found: {local}")

    if remote_filename is None:
        remote_filename = f"{vm_name}.vmx"
    remote_path = f"{vm_name}/{remote_filename}"
    url = (
        f"https://{host}:{port}/folder/{remote_path}"
        f"?dcPath={dc_name}&dsName={datastore}"
    )
    total = local.stat().st_size
    log.info("Uploading %s:", remote_filename)
    log.info("  local : %s (%d bytes)", local, total)
    log.info("  remote: [%s] %s", datastore, remote_path)

    _remote_filename = remote_filename

    class _ProgressReader:
        def __init__(self, fileobj: IO[bytes], total_bytes: int) -> None:
            self.f = fileobj
            self.total = total_bytes
            self.bar = make_progress_bar(
                total=total_bytes,
                desc=f"upload {_remote_filename}",
                unit="B",
                unit_scale=True,
            )

        def read(self, size: int = -1) -> bytes:
            chunk = self.f.read(size)
            self.bar.update(len(chunk))
            return chunk

        def close(self) -> None:
            self.bar.close()

        def __len__(self) -> int:
            return self.total

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
    log.info("  %s: done", remote_filename)


def read_datastore_file(
    host: str,
    user: str,
    password: str,
    port: int,
    datastore: str,
    dc_name: str,
    remote_path: str,
) -> str:
    """Read a datastore file via HTTPS GET and return its text.

    The HTTPS GET mirror of upload_file(). ``remote_path`` is the path within the
    datastore (e.g. ``ws-2025-base/ws-2025-base.vmx``). Raises on non-200 so the
    caller can fall back to a default.
    """
    url = (
        f"https://{host}:{port}/folder/{remote_path}"
        f"?dcPath={dc_name}&dsName={datastore}"
    )
    log.info("Reading [%s] %s", datastore, remote_path)
    resp = requests.get(
        url,
        auth=(user, password),
        verify=False,
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Read failed (HTTP {resp.status_code}): {resp.text[:200]}"
        )
    return resp.text


def get_base_vmdk_size(datastore_obj: vim.Datastore, base: str) -> int:
    """Return the total on-disk size (bytes) of the base VM's .vmdk file(s)."""
    browser = datastore_obj.browser
    details = vim.host.DatastoreBrowser.FileInfo.Details()
    details.fileSize = True
    details.fileType = True
    spec = vim.host.DatastoreBrowser.SearchSpec()
    spec.matchPattern = [f"{base}*.vmdk"]
    spec.details = details
    folder = f"[{datastore_obj.name}] {base}"
    task = browser.SearchDatastore_Task(datastorePath=folder, searchSpec=spec)
    while task.info.state in (
        vim.TaskInfo.State.running,
        vim.TaskInfo.State.queued,
    ):
        time.sleep(0.5)
    task_info = task.info
    if task_info.state != vim.TaskInfo.State.success:
        err = task_info.error
        msg = task_info.error.msg if err else "unknown error"
        raise RuntimeError(f"Could not read base VMDK size: {msg}")
    return sum(
        (getattr(f, "fileSize", 0) or 0) for f in (task_info.result.file or [])
    )
