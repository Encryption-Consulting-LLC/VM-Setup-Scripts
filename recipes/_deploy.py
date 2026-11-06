"""Deploy one VM: render its first-boot scripts, pack the config ISO, clone it.

The same steps as ``gen-hostname`` + ``gen-network`` (+ ``gen-password``) +
``pack-iso`` + ``clone-vm``, with the scripts rendered in memory and the ISO
written to a temporary directory unless the caller asks to keep it.
"""

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import configgen
import vmkit
from configgen import NetworkConfig
from isokit import Entry, IsoContents, build_config_iso

from recipes._batch import DEFAULT_JOBS, BatchResult, Outcome, run_batch
from recipes.errors import BatchPreflightError

log = logging.getLogger("recipes.deploy")

SCRIPT_SUFFIX = {"linux": ".sh", "windows": ".ps1"}


@dataclass(frozen=True)
class DeploySpec:
    """What goes on one VM's config ISO.

    ``backend`` is the Linux network stack (``None`` lets configgen pick its own
    default, so callers that want netplan must say so). ``accounts`` are
    ``(username, new_password)`` pairs to reset on first boot. ``scripts`` run
    after the generated ones, in order; ``files`` are staged, never executed.
    Both take isokit entries: a ``Path`` or a ``(disc_name, source)`` pair.
    """

    name: str
    platform: str
    network: NetworkConfig
    backend: str | None = None
    accounts: tuple[tuple[str, str], ...] = ()
    scripts: tuple[Entry, ...] = field(default=())
    files: tuple[Entry, ...] = field(default=())


@dataclass(frozen=True)
class DeployResult:
    clone: vmkit.CloneResult
    iso: IsoContents
    #: Where the config ISO was kept, or ``None`` if it was temporary.
    iso_path: Path | None


def render_scripts(spec: DeploySpec) -> list[tuple[str, str]]:
    """Render the generated first-boot scripts as ``(disc_name, text)`` pairs.

    Names match what the single-purpose CLIs write (10-hostname, 20-network,
    30-password-<user>). Raises ``configgen.ValidationError`` on bad input.
    """
    hostname = configgen.render_hostname(spec.platform, spec.name)  # validates platform too
    suffix = SCRIPT_SUFFIX[spec.platform]
    network = configgen.render_network(spec.platform, spec.network, spec.backend)
    scripts = [(f"10-hostname{suffix}", hostname), (f"20-network{suffix}", network)]
    for username, password in spec.accounts:
        scripts.append(
            (f"30-password-{username}{suffix}",
             configgen.render_password(spec.platform, username, password))
        )
    return scripts


def build_deploy_iso(spec: DeploySpec, output_path: Path) -> IsoContents:
    """Write the VM's config ISO to ``output_path``. Raises configgen / isokit errors."""
    return build_config_iso(
        Path(output_path),
        scripts=[*render_scripts(spec), *spec.scripts],
        files=list(spec.files),
        volume_id=spec.name[:32],
    )


def deploy(
    conn: vmkit.Connection,
    spec: DeploySpec,
    *,
    keep_iso: Path | None = None,
    **clone_options: Any,
) -> DeployResult:
    """Build ``spec``'s config ISO and clone the VM with it attached.

    ``clone_options`` go to ``vmkit.clone_workflow`` (base, datastore, cpus,
    mem_mb, network, power_on, ...); ``name`` and ``iso_path`` come from here.
    ``keep_iso`` saves the ISO at that path instead of a temporary file.

    Nothing touches ESXi until the ISO has been built, so invalid input fails
    before any clone starts. On a clone failure the caller may run
    ``vmkit.cleanup_partial_clone``, as with ``clone_workflow``.
    """
    with TemporaryDirectory(prefix="vmsetup-deploy-") as tmp:
        iso_path = Path(keep_iso) if keep_iso else Path(tmp) / f"{spec.name}-config.iso"
        contents = build_deploy_iso(spec, iso_path)
        log.info("Config ISO built: %s (scripts: %s)", iso_path, ", ".join(contents.scripts))
        result = vmkit.clone_workflow(
            conn, name=spec.name, iso_path=str(iso_path), **clone_options
        )
    return DeployResult(clone=result, iso=contents, iso_path=Path(keep_iso) if keep_iso else None)


def _cleanup_failed_clone(datastore: str) -> Callable[[vmkit.Connection | None, DeploySpec, BaseException], str]:
    """Batch failure hook: delete a failed clone's datastore files, unless the VM
    registered -- then it is left for inspection."""

    def cleanup(conn: vmkit.Connection | None, spec: DeploySpec, error: BaseException) -> str:
        if conn is None:
            return "no session to the host; nothing was created"
        if isinstance(error, vmkit.VmExistsError):
            return "a VM of that name already existed; left untouched"
        try:
            done = vmkit.cleanup_partial_clone(conn, spec.name, datastore)
        except vmkit.VmExistsError:
            return "VM registered before failing; left in place for inspection"
        return "partial clone files deleted" if done.deleted else "nothing on the datastore to clean up"

    return cleanup


def preflight_deploy_batch(
    conn: vmkit.Connection,
    specs: Sequence[DeploySpec],
    *,
    base: str,
    datastore: str,
    cpus: int,
    mem_mb: int,
    max_usage_pct: float = 80.0,
    skip_disk_check: bool = False,
) -> vmkit.CapacityPlan | None:
    """Check a whole deploy batch before any of it starts.

    Raises ``BatchPreflightError`` listing every problem found: duplicate names
    or static IPs within the batch, names already on the host, scripts that
    fail to render. Then checks the datastore can take every clone at once
    (``vmkit.InsufficientSpaceError``) unless ``skip_disk_check``. Returns the
    capacity plan, whose ``warnings`` (e.g. memory overcommit) the caller
    should show.
    """
    problems: list[str] = []
    names = [s.name for s in specs]
    for name in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"name {name} appears {names.count(name)} times in the batch")
    ips = [s.network.ip for s in specs if s.network.mode == "static" and s.network.ip]
    for ip in sorted({ip for ip in ips if ips.count(ip) > 1}):
        problems.append(f"IP {ip} is given to {ips.count(ip)} VMs")
    existing = vmkit.list_vm_names(conn.content)
    for name in names:
        if name in existing:
            problems.append(f"a VM named {name} already exists on the host")
    for spec in specs:
        try:
            render_scripts(spec)
        except configgen.ValidationError as exc:
            problems.append(f"{spec.name}: {exc}")
    if problems:
        raise BatchPreflightError(problems)

    if skip_disk_check:
        log.warning("Skipping the batch datastore free-space check.")
        return None
    plan = vmkit.plan_capacity(
        conn.content, datastore,
        [vmkit.VmSpec(name=s.name, base=base, cpus=cpus, mem_mb=mem_mb) for s in specs],
        max_usage_pct,
    )
    return plan.raise_if_insufficient()


def deploy_batch(
    connect: Callable[[], vmkit.Connection],
    specs: Sequence[DeploySpec],
    *,
    base: str,
    datastore: str,
    cpus: int,
    mem_mb: int,
    jobs: int = DEFAULT_JOBS,
    max_usage_pct: float = 80.0,
    skip_disk_check: bool = False,
    keep_isos: Mapping[str, Path] | None = None,
    cancel: threading.Event | None = None,
    on_outcome: Callable[[Outcome[DeploySpec]], None] | None = None,
    on_plan: Callable[[vmkit.CapacityPlan], None] | None = None,
    **clone_options: Any,
) -> BatchResult[DeploySpec]:
    """Deploy every spec, ``jobs`` at a time, after checking the batch as a whole.

    ``connect`` opens a session; it is called once for the checks and once per
    worker. Each VM gets a MAC drawn up front, so none repeat within the batch.
    A VM that fails has its partial clone files deleted unless it registered.
    ``clone_options`` go to every ``vmkit.clone_workflow`` (network, guest_os,
    power_on, ...); a fixed ``mac`` is refused, since VMs would share it.

    Raises ``BatchPreflightError`` / ``vmkit.InsufficientSpaceError`` before
    anything starts; after that, failures are per VM in the result.
    """
    if "mac" in clone_options:
        raise ValueError("a batch cannot share one MAC; leave mac unset")
    keep_isos = dict(keep_isos or {})

    conn = connect()
    try:
        plan = preflight_deploy_batch(
            conn, specs, base=base, datastore=datastore, cpus=cpus, mem_mb=mem_mb,
            max_usage_pct=max_usage_pct, skip_disk_check=skip_disk_check,
        )
    finally:
        conn.close()
    if plan is not None and on_plan is not None:
        on_plan(plan)

    used: set[str] = set()
    macs = {}
    for spec in specs:
        macs[spec.name] = vmkit.random_mac(used)
        used.add(macs[spec.name])

    def work(conn: vmkit.Connection, spec: DeploySpec, should_cancel: Callable[[], bool]):
        return deploy(
            conn, spec,
            keep_iso=keep_isos.get(spec.name),
            base=base, datastore=datastore, cpus=cpus, mem_mb=mem_mb,
            mac=macs[spec.name],
            max_usage_pct=max_usage_pct, skip_disk_check=skip_disk_check,
            progress_context=spec.name, should_cancel=should_cancel,
            **clone_options,
        )

    return run_batch(
        specs, name_of=lambda s: s.name, work=work, connect=connect, jobs=jobs,
        on_failure=_cleanup_failed_clone(datastore), cancel=cancel, on_outcome=on_outcome,
    )
