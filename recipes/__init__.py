"""recipes — multi-step workflows composed from vmkit, configgen and isokit.

The libraries stay independent of each other (vmkit knows nothing about
configgen), so the workflows that chain them live here. Same contract as the
libraries: parameters in, results or exceptions out, no prompting, printing or
``sys.exit`` — the CLIs (deploy-vm, bake-base) and a future API share them.

    deploy(conn, spec, ...)  hostname + network (+ password) scripts -> config ISO -> clone
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
from recipes._deploy import (
    SCRIPT_SUFFIX,
    DeployResult,
    DeploySpec,
    build_deploy_iso,
    deploy,
    render_scripts,
)
from recipes.errors import BakeTimeoutError, RecipeError, SetupFilesNotFoundError

__all__ = [
    "DEFAULT_BAKE_TIMEOUT_S",
    "RUNNER_SCRIPT",
    "SCRIPT_SUFFIX",
    "SETUP_SCRIPT",
    "BakeResult",
    "BakeTimeoutError",
    "DeployResult",
    "DeploySpec",
    "RecipeError",
    "SetupFilesNotFoundError",
    "bake",
    "build_bake_iso",
    "build_deploy_iso",
    "deploy",
    "render_scripts",
    "wait_for_power_off",
]
