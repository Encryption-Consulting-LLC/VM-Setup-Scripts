"""Install a Linux ``-base-pre`` VM unattended: create, autoinstall, detach the discs.

The manual steps it replaces (the README's "Install -base-pre" step):

    create a VM in the Host Client: blank disk, EFI, the autoinstall installer
        ISO and the seed ISO as two CD-ROMs
    power it on, wait for it to power itself off
    untick "Connect at power on" on both CD-ROMs

Both ISOs must already be on a datastore: the installer from
``autoinstall/remaster-iso.sh`` and the seed from ``autoinstall/build-seed.sh``,
uploaded with ``upload-file``. The installer goes on ``sata0:0``, which EFI boots
on a blank disk; the seed is found by its CIDATA label wherever it sits.

The guest powers itself off once the post-install bootstrap has run, which is the
completion signal. The discs are then detached: the remastered installer
installs without asking, so left attached it wipes the disk on the next boot.

The VM is created from vmkit's clone template, so it gets the same NIC PCI slot
as every clone -- the base image's interface name matches its clones'.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import vmkit

from recipes._bake import DEFAULT_POLL_S, wait_for_power_off
from recipes.errors import InstallTimeoutError

log = logging.getLogger("recipes.install")

DEFAULT_TIMEOUT_S = 60 * 60
#: The VMX guestOS id (not the vSphere API's "ubuntu64Guest", which ESXi refuses).
DEFAULT_GUEST_OS = "ubuntu-64"
DEFAULT_DISK_GB = 40

#: What an install timeout means and where to look.
INSTALL_TIMEOUT_HINT = (
    "so the install most likely failed: the post-install bootstrap powers the VM off "
    "when it succeeds. On its console, an installer screen means the install itself "
    "failed; a login prompt means the bootstrap did -- read "
    "/var/log/firstboot-bootstrap.log. Both discs are still attached: detach them "
    "before booting it again, or the installer wipes the disk and starts over."
)


@dataclass(frozen=True)
class InstallResult:
    create: vmkit.CreateResult
    #: True once the VM powered itself off (install and bootstrap done); False with wait=False.
    finished: bool
    #: True if the installer and seed discs were detached after it finished.
    discs_detached: bool


def install(
    conn: vmkit.Connection,
    *,
    name: str,
    datastore: str,
    installer: str,
    seed: str,
    guest_os: str = DEFAULT_GUEST_OS,
    disk_gb: int = DEFAULT_DISK_GB,
    wait: bool = True,
    detach_discs: bool = True,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    poll_s: float = DEFAULT_POLL_S,
    should_cancel: Callable[[], bool] | None = None,
    **create_options: Any,
) -> InstallResult:
    """Create ``name`` with a blank disk and the ``installer`` + ``seed`` discs,
    power it on, and (with ``wait``) block until the install and its bootstrap
    have powered it off, then detach the discs.

    ``installer`` and ``seed`` are ``"[datastore] path"`` or paths relative to
    ``datastore``; both are checked before anything is created
    (``vmkit.DatastoreFileNotFoundError``). ``create_options`` go to
    ``vmkit.create_workflow`` (cpus, mem_mb, network, mac, thin, ...).
    """
    result = vmkit.create_workflow(
        conn,
        name=name,
        datastore=datastore,
        guest_os=guest_os,
        disk_gb=disk_gb,
        cdroms=[installer, seed],
        power_on=True,
        should_cancel=should_cancel,
        **create_options,
    )

    if not wait:
        return InstallResult(create=result, finished=False, discs_detached=False)

    log.info("Waiting for '%s' to install, run its bootstrap and power off (up to %g min)...",
             name, timeout_s / 60)
    wait_for_power_off(
        conn, name, timeout_s=timeout_s, poll_s=poll_s, should_cancel=should_cancel,
        timeout_error=InstallTimeoutError, timeout_hint=INSTALL_TIMEOUT_HINT,
    )
    log.info("'%s' powered off: install finished.", name)

    if detach_discs:
        vmkit.update_workflow(conn, name=name, datastore=datastore, remove_iso=True)
        log.info("Installer and seed discs detached from '%s'.", name)
    return InstallResult(create=result, finished=True, discs_detached=detach_discs)
