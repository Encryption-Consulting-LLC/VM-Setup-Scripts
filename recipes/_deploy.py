"""Deploy one VM: render its first-boot scripts, pack the config ISO, clone it.

The same steps as ``gen-hostname`` + ``gen-network`` (+ ``gen-password``) +
``pack-iso`` + ``clone-vm``, with the scripts rendered in memory and the ISO
written to a temporary directory unless the caller asks to keep it.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import configgen
import vmkit
from configgen import NetworkConfig
from isokit import Entry, IsoContents, build_config_iso

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
