"""Config-file defaults and named profiles for the CLIs.

Values you would otherwise retype on every command (ESXi host, datastore, base
VM, gateway, DNS, ...) live in TOML:

    [defaults]              # applies to every command
    server = "esxi.example.com"
    user = "root"

    [profile.ubuntu]        # applies with --profile ubuntu (or $VMSETUP_PROFILE)
    platform = "linux"
    base = "ub-24.04-base"
    gateway = "192.168.1.1"

Keys are the commands' long option names (``dns-suffix`` and ``dns_suffix`` are
both accepted). Each command reads only the keys it has an option for, so one
``[defaults]`` table serves them all. The keys that name a single VM (name, IP,
MAC) or hold a secret (passwords) cannot be set here, and neither can
behaviour switches like ``--power-on``.

Files, lowest precedence first (a later file overrides an earlier one):
    $XDG_CONFIG_HOME/vm-setup/config.toml   (default ~/.config/vm-setup/config.toml)
    ./vm-setup.toml                         (project / working directory)
``--config FILE`` or ``$VMSETUP_CONFIG`` replaces both with that one file.

Overall precedence: command-line flag > profile > [defaults] > built-in default.
A profile beats every file's ``[defaults]``.

Values are validated through the same argparse ``type=`` / ``choices`` as the
flag, so a bad config value fails just like a bad flag, naming the file.
"""

import argparse
import os
import tomllib
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

CONFIG_ENV = "VMSETUP_CONFIG"
PROFILE_ENV = "VMSETUP_PROFILE"
PROJECT_FILE = "vm-setup.toml"

#: ESXi connection settings, shared by every command that talks to a host.
CONNECTION_KEYS = frozenset({"server", "user", "port", "datastore"})
#: Settings for cloning a VM (clone-vm, deploy-vm, bake-base).
CLONE_KEYS = CONNECTION_KEYS | {"base", "network", "guest_os", "cpus", "ram", "max_usage"}
#: Settings for installing a -base-pre VM from scratch (install-base).
INSTALL_KEYS = CONNECTION_KEYS | {
    "network", "guest_os", "cpus", "ram", "disk_gb", "installer", "seed"
}
#: Settings for the generated first-boot network script (gen-network, deploy-vm).
NETWORK_KEYS = frozenset(
    {"platform", "backend", "dhcp", "prefix", "gateway", "dns1", "dns2", "dns_suffix"}
)
KNOWN_KEYS = CLONE_KEYS | NETWORK_KEYS | INSTALL_KEYS

_SECRET_KEYS = frozenset({"password"})


class ConfigError(Exception):
    """A config file is missing, unreadable, or holds an invalid entry."""


def _normalize(key: str) -> str:
    return key.replace("-", "_")


def config_paths(explicit: str | None = None) -> list[Path]:
    """Files to read, lowest precedence first. Only an explicit file must exist."""
    explicit = explicit or os.environ.get(CONFIG_ENV)
    if explicit:
        return [Path(explicit)]
    xdg = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return [Path(xdg) / "vm-setup" / "config.toml", Path.cwd() / PROJECT_FILE]


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc.strerror}.") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc

    unknown = set(data) - {"defaults", "profile"}
    if unknown:
        raise ConfigError(
            f"{path}: unknown table(s) {', '.join(sorted(unknown))}; "
            "expected [defaults] and [profile.<name>]."
        )
    if not isinstance(data.get("defaults", {}), dict):
        raise ConfigError(f"{path}: 'defaults' must be a table.")
    profiles = data.get("profile", {})
    if not isinstance(profiles, dict) or not all(isinstance(p, dict) for p in profiles.values()):
        raise ConfigError(f"{path}: profiles must be tables, e.g. [profile.ubuntu].")
    return data


def _check_table(table: dict[str, Any], where: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for raw_key, value in table.items():
        key = _normalize(raw_key)
        if key in _SECRET_KEYS:
            raise ConfigError(
                f"{where}: '{raw_key}' is not allowed in a config file. Passwords are "
                "prompted for, or read from $VMSETUP_PASSWORD."
            )
        if key not in KNOWN_KEYS:
            allowed = ", ".join(sorted(k.replace("_", "-") for k in KNOWN_KEYS))
            raise ConfigError(f"{where}: unknown key '{raw_key}'. Allowed keys: {allowed}.")
        out[key] = value
    return out


def load_settings(
    profile: str | None = None, paths: Sequence[Path] | None = None, *, explicit: str | None = None
) -> dict[str, tuple[Any, str]]:
    """Merge the config files into ``{key: (value, source)}``.

    ``source`` names the file and table a value came from, for error messages.
    Raises ``ConfigError`` for an unreadable file, a bad key, or an unknown profile.
    """
    if paths is None:
        paths = config_paths(explicit)
        must_exist = bool(explicit or os.environ.get(CONFIG_ENV))
    else:
        must_exist = False

    files: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        if path.is_file() or must_exist:
            files.append((path, _read(path)))

    merged: dict[str, tuple[Any, str]] = {}
    for path, data in files:
        for key, value in _check_table(data.get("defaults", {}), f"{path} [defaults]").items():
            merged[key] = (value, f"{path} [defaults]")

    if profile:
        found = False
        for path, data in files:
            table = data.get("profile", {}).get(profile)
            if table is None:
                continue
            found = True
            where = f"{path} [profile.{profile}]"
            for key, value in _check_table(table, where).items():
                merged[key] = (value, where)
        if not found:
            available = sorted({name for _, data in files for name in data.get("profile", {})})
            searched = ", ".join(str(p) for p in paths)
            raise ConfigError(
                f"Profile '{profile}' not found (searched: {searched}). "
                + (f"Available: {', '.join(available)}." if available else "No profiles defined.")
            )
    return merged


def add_config_args(parser: argparse.ArgumentParser) -> None:
    """Add --config / --profile to a parser (parsed early by ``apply_config``)."""
    group = parser.add_argument_group("config file")
    group.add_argument(
        "--config", default=None, metavar="FILE",
        help=f"Read defaults from this TOML file only (also ${CONFIG_ENV}). Without it, "
             f"~/.config/vm-setup/config.toml and ./{PROJECT_FILE} are read.",
    )
    group.add_argument(
        "--profile", default=None, metavar="NAME",
        help=f"Apply [profile.NAME] from the config (also ${PROFILE_ENV}). "
             "Flags still override it.",
    )


def _coerce(action: argparse.Action, value: Any, where: str) -> Any:
    """Validate a config value the way argparse would validate the flag."""
    if action.nargs == 0:  # store_true / BooleanOptionalAction
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: expected true or false, got {value!r}.")
        return value
    if isinstance(value, (bool, list, dict)):
        raise ConfigError(f"{where}: expected a single string or number, got {value!r}.")
    text = str(value)
    try:
        result = action.type(text) if action.type else text
    except (argparse.ArgumentTypeError, ValueError, TypeError) as exc:
        raise ConfigError(f"{where}: {exc}") from exc
    if action.choices is not None and result not in action.choices:
        choices = ", ".join(map(str, action.choices))
        raise ConfigError(f"{where}: invalid choice {result!r} (choose from {choices}).")
    return result


def apply_config(
    parser: argparse.ArgumentParser,
    keys: Iterable[str],
    argv: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Load the config and install the values for ``keys`` as parser defaults.

    Call after building the parser and before ``parse_args``. Options that a
    config value satisfies stop being required. Errors exit via ``parser.error``.
    Returns the applied ``{key: value}``.
    """
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=None)
    pre.add_argument("--profile", default=None)
    known, _ = pre.parse_known_args(argv)
    profile = known.profile or os.environ.get(PROFILE_ENV) or None

    try:
        settings = load_settings(profile, explicit=known.config)
        actions = {action.dest: action for action in parser._actions}
        applied: dict[str, Any] = {}
        for key in sorted(set(keys) & settings.keys()):
            action = actions.get(key)
            if action is None:
                continue
            value, source = settings[key]
            applied[key] = _coerce(action, value, f"{source} {key}")
            action.required = False
    except ConfigError as exc:
        parser.error(str(exc))

    parser.set_defaults(**applied)
    return applied
