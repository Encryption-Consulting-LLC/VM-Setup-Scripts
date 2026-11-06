"""recipes — multi-step workflows composed from vmkit, configgen and isokit.

The libraries stay independent of each other (vmkit knows nothing about
configgen), so the workflows that chain them live here. Same contract as the
libraries: parameters in, results or exceptions out, no prompting, printing or
``sys.exit`` — the CLIs (deploy-vm, bake-base) and a future API share them.

    deploy(conn, spec, ...)  hostname + network (+ password) scripts -> config ISO -> clone
    install(conn, name=..., installer=..., seed=...)  autoinstall a -base-pre VM from scratch
    deploy_batch(connect, specs, ...) / update_batch(connect, names, ...)  many VMs, -j at a time
    bake(conn, name=..., pre=..., setup_dir=...)  generalize a -base-pre clone into a base
"""

from recipes._bake import (
    DEFAULT_TIMEOUT_S as DEFAULT_BAKE_TIMEOUT_S,
    RUNNER_SCRIPT,
    SETUP_SCRIPT,
    BakeResult,
    bake,
    build_bake_iso,
    wait_for_power_off,
)
from recipes._batch import (
    DEFAULT_JOBS,
    MAX_BATCH,
    PLACEHOLDER,
    BatchResult,
    Outcome,
    current_item,
    expand,
    parse_range,
    run_batch,
)
from recipes._deploy import (
    SCRIPT_SUFFIX,
    DeployResult,
    DeploySpec,
    build_deploy_iso,
    deploy,
    deploy_batch,
    preflight_deploy_batch,
    render_scripts,
)
from recipes._update import update_batch
from recipes._install import (
    DEFAULT_DISK_GB,
    DEFAULT_GUEST_OS as DEFAULT_INSTALL_GUEST_OS,
    DEFAULT_TIMEOUT_S as DEFAULT_INSTALL_TIMEOUT_S,
    InstallResult,
    install,
)
from recipes.errors import (
    BakeTimeoutError,
    BatchPreflightError,
    InstallTimeoutError,
    PowerOffTimeoutError,
    RecipeError,
    SetupFilesNotFoundError,
)

__all__ = [
    "DEFAULT_JOBS",
    "MAX_BATCH",
    "PLACEHOLDER",
    "BatchPreflightError",
    "BatchResult",
    "Outcome",
    "current_item",
    "deploy_batch",
    "expand",
    "parse_range",
    "preflight_deploy_batch",
    "run_batch",
    "update_batch",
    "DEFAULT_BAKE_TIMEOUT_S",
    "DEFAULT_DISK_GB",
    "DEFAULT_INSTALL_GUEST_OS",
    "DEFAULT_INSTALL_TIMEOUT_S",
    "RUNNER_SCRIPT",
    "SCRIPT_SUFFIX",
    "SETUP_SCRIPT",
    "BakeResult",
    "BakeTimeoutError",
    "DeployResult",
    "DeploySpec",
    "InstallResult",
    "InstallTimeoutError",
    "PowerOffTimeoutError",
    "RecipeError",
    "SetupFilesNotFoundError",
    "bake",
    "build_bake_iso",
    "build_deploy_iso",
    "deploy",
    "install",
    "render_scripts",
    "wait_for_power_off",
]
