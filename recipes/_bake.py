"""Bake a Linux base: generalize a clone of ``<name>-pre`` into ``<name>``.

The manual steps it replaces:

    pack-iso firstboot-setup.sh --file firstboot-runner.sh -o bake.iso
    clone-vm -n <name> -b <name>-pre --iso bake.iso --power-on
    (wait for the clone to power itself off)
    update-vm -n <name> --remove-iso

The runner armed on ``-base-pre`` runs ``firstboot-setup.sh`` from the disc,
which reinstalls the runner from the disc's payload, generalizes, and powers
off. The runner always travels as a payload file: listed as a script it would
run itself, rescan the disc and recurse. This module is the one place that
packing order is written down, so callers cannot get it wrong.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import vmkit
from isokit import IsoContents, build_config_iso
from vmkit.esxi import get_vm_by_name

from recipes.errors import BakeTimeoutError, PowerOffTimeoutError, SetupFilesNotFoundError

log = logging.getLogger("recipes.bake")

SETUP_SCRIPT = "firstboot-setup.sh"
RUNNER_SCRIPT = "firstboot-runner.sh"

DEFAULT_TIMEOUT_S = 30 * 60
DEFAULT_POLL_S = 15.0

#: What a bake timeout means and where to look.
BAKE_TIMEOUT_HINT = (
    "so the bake most likely failed: firstboot-setup.sh powers the VM off when it "
    "succeeds. Read /var/log/firstboot.log and /var/log/firstboot-error.log on its "
    "console. The bake disc is still attached."
)


@dataclass(frozen=True)
class BakeResult:
    clone: vmkit.CloneResult
    iso: IsoContents
    #: True once the VM powered itself off (the bake finished); False with wait=False.
    finished: bool
    #: True if the bake disc was detached after the bake finished.
    iso_detached: bool


def build_bake_iso(setup_dir: Path, output_path: Path) -> IsoContents:
    """Pack ``setup_dir``'s setup script (run) and runner (payload) into a bake disc."""
    setup_dir = Path(setup_dir)
    script, runner = setup_dir / SETUP_SCRIPT, setup_dir / RUNNER_SCRIPT
    missing = [str(p) for p in (script, runner) if not p.is_file()]
    if missing:
        raise SetupFilesNotFoundError(
            f"Bake files not found: {', '.join(missing)}. Point setup_dir at "
            "base-vm-setup/linux-server/."
        )
    return build_config_iso(Path(output_path), scripts=[script], files=[runner])


def wait_for_power_off(
    conn: vmkit.Connection,
    name: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    poll_s: float = DEFAULT_POLL_S,
    should_cancel: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout_error: type[PowerOffTimeoutError] = BakeTimeoutError,
    timeout_hint: str = BAKE_TIMEOUT_HINT,
) -> None:
    """Block until VM ``name`` reports poweredOff.

    Raises ``VmNotFoundError`` if it vanishes, ``timeout_error`` (a bake's, by
    default) after ``timeout_s`` -- its message ends with ``timeout_hint``, which
    says what the timeout means for that guest and where to look -- and
    ``vmkit.TaskCancelledError`` if ``should_cancel`` returns true.
    """
    deadline = clock() + timeout_s
    while True:
        vm = get_vm_by_name(conn.content, name)
        if vm is None:
            raise vmkit.VmNotFoundError(f"VM '{name}' not found while waiting for it to power off.")
        state = str(vm.runtime.powerState)
        if state == "poweredOff":
            return
        if clock() >= deadline:
            raise timeout_error(
                f"VM '{name}' is still {state} after {timeout_s / 60:g} min, {timeout_hint}"
            )
        if should_cancel is not None and should_cancel():
            raise vmkit.TaskCancelledError(f"Cancelled while waiting for '{name}' to power off.")
        log.debug("VM '%s' is %s; checking again in %gs.", name, state, poll_s)
        sleep(poll_s)


def bake(
    conn: vmkit.Connection,
    *,
    name: str,
    pre: str,
    setup_dir: Path,
    datastore: str,
    wait: bool = True,
    detach_iso: bool = True,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    poll_s: float = DEFAULT_POLL_S,
    keep_iso: Path | None = None,
    should_cancel: Callable[[], bool] | None = None,
    **clone_options: Any,
) -> BakeResult:
    """Clone ``pre`` as ``name`` with the bake disc, power it on, and (with
    ``wait``) block until it has generalized and powered off, then detach the disc.

    ``clone_options`` go to ``vmkit.clone_workflow`` (cpus, mem_mb, network,
    guest_os, ...). ``keep_iso`` saves the bake disc at that path.
    """
    with TemporaryDirectory(prefix="vmsetup-bake-") as tmp:
        iso_path = Path(keep_iso) if keep_iso else Path(tmp) / f"{name}-config.iso"
        contents = build_bake_iso(setup_dir, iso_path)
        log.info("Bake disc built: %s", iso_path)
        result = vmkit.clone_workflow(
            conn,
            name=name,
            base=pre,
            datastore=datastore,
            iso_path=str(iso_path),
            power_on=True,
            should_cancel=should_cancel,
            **clone_options,
        )

    if not wait:
        return BakeResult(clone=result, iso=contents, finished=False, iso_detached=False)

    log.info("Waiting for '%s' to generalize and power off (up to %g min)...",
             name, timeout_s / 60)
    wait_for_power_off(conn, name, timeout_s=timeout_s, poll_s=poll_s, should_cancel=should_cancel)
    log.info("'%s' powered off: bake finished.", name)

    if detach_iso:
        vmkit.update_workflow(conn, name=name, datastore=datastore, remove_iso=True)
        log.info("Bake disc detached from '%s'.", name)
    return BakeResult(clone=result, iso=contents, finished=True, iso_detached=detach_iso)
