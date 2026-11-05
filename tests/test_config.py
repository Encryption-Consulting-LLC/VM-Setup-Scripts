"""Config-file defaults and profiles (cli/_config.py)."""

import argparse

import pytest

from cli import clone_vm, deploy_vm, gen_network, update_vm
from cli._config import (
    CLONE_KEYS,
    CONNECTION_KEYS,
    NETWORK_KEYS,
    ConfigError,
    apply_config,
    load_settings,
)

CONFIG = """
[defaults]
server = "esxi.example.com"
user = "root"
datastore = "datastore2"
cpus = 4

[profile.ubuntu]
platform = "linux"
base = "ub-24.04-base"
gateway = "192.168.1.1"
dns1 = "192.168.1.10"
dns-suffix = "corp.example.com"
prefix = 24
datastore = "fast-ssd"

[profile.lab]
platform = "linux"
dhcp = true
"""


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    monkeypatch.setenv("VMSETUP_CONFIG", str(path))
    monkeypatch.delenv("VMSETUP_PROFILE", raising=False)
    return path


def parse(module, keys, argv):
    parser = module.build_parser()
    applied = apply_config(parser, keys, argv)
    return parser.parse_args(argv), applied


def test_defaults_satisfy_required_connection_args(config):
    args, _ = parse(clone_vm, CLONE_KEYS, ["-n", "web01"])
    assert (args.server, args.user, args.datastore, args.cpus) == (
        "esxi.example.com", "root", "datastore2", 4,
    )


def test_profile_overrides_defaults_and_flags_override_profile(config):
    args, _ = parse(clone_vm, CLONE_KEYS, ["-n", "web01", "--profile", "ubuntu", "-u", "admin"])
    assert args.datastore == "fast-ssd"  # profile beats [defaults]
    assert args.base == "ub-24.04-base"
    assert args.user == "admin"  # flag beats config


def test_profile_from_environment(config, monkeypatch):
    monkeypatch.setenv("VMSETUP_PROFILE", "ubuntu")
    args, _ = parse(clone_vm, CLONE_KEYS, ["-n", "web01"])
    assert args.base == "ub-24.04-base"


def test_hyphenated_keys_map_to_options(config):
    args, _ = parse(gen_network, NETWORK_KEYS, ["--profile", "ubuntu"])
    assert args.dns_suffix == "corp.example.com"
    assert args.prefix == 24


def test_update_vm_only_takes_connection_keys(config):
    # cpus in [defaults] must not resize a VM that update-vm touches.
    args, _ = parse(update_vm, CONNECTION_KEYS, ["-n", "web01"])
    assert args.cpus is None
    assert args.server == "esxi.example.com"


def test_boolean_from_profile_can_be_overridden(config):
    args, _ = parse(gen_network, NETWORK_KEYS, ["--profile", "lab"])
    assert args.dhcp is True
    args, _ = parse(gen_network, NETWORK_KEYS, ["--profile", "lab", "--no-dhcp"])
    assert args.dhcp is False


def test_required_still_enforced_without_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VMSETUP_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    parser = clone_vm.build_parser()
    apply_config(parser, CLONE_KEYS, ["-n", "web01"])
    with pytest.raises(SystemExit):
        parser.parse_args(["-n", "web01"])


def test_project_file_overrides_user_file(tmp_path, monkeypatch):
    user_dir = tmp_path / "xdg" / "vm-setup"
    user_dir.mkdir(parents=True)
    (user_dir / "config.toml").write_text('[defaults]\nserver = "user-host"\nuser = "root"\n')
    project = tmp_path / "project"
    project.mkdir()
    (project / "vm-setup.toml").write_text('[defaults]\nserver = "project-host"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("VMSETUP_CONFIG", raising=False)
    monkeypatch.chdir(project)
    settings = load_settings()
    assert settings["server"][0] == "project-host"
    assert settings["user"][0] == "root"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('[defaults]\npassword = "x"\n', "not allowed"),
        ('[defaults]\nservr = "x"\n', "unknown key 'servr'"),
        ('[esxi]\nserver = "x"\n', "unknown table"),
        ("[defaults\n", "Invalid TOML"),
    ],
)
def test_bad_files_are_rejected(tmp_path, body, message):
    path = tmp_path / "c.toml"
    path.write_text(body)
    with pytest.raises(ConfigError, match=message):
        load_settings(paths=[path])


def test_unknown_profile_lists_available(config):
    with pytest.raises(ConfigError, match="Available: lab, ubuntu"):
        load_settings("nope", paths=[config])


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("[defaults]\ncpus = 3\n", "power of 2"),
        ('[defaults]\nplatform = "bsd"\n', "invalid choice"),
        ('[defaults]\ndhcp = "yes"\n', "expected true or false"),
        ('[defaults]\ngateway = "300.1.1.1"\n', "gateway"),
    ],
)
def test_invalid_values_fail_like_flags(tmp_path, monkeypatch, capsys, body, message):
    path = tmp_path / "c.toml"
    path.write_text(body)
    monkeypatch.setenv("VMSETUP_CONFIG", str(path))
    parser = deploy_vm.build_parser()
    with pytest.raises(SystemExit):
        apply_config(parser, CLONE_KEYS | NETWORK_KEYS, ["web01"])
    assert message in capsys.readouterr().err


def test_apply_config_reports_applied_values(config):
    _, applied = parse(clone_vm, CLONE_KEYS, ["-n", "web01", "--profile", "ubuntu"])
    assert applied["base"] == "ub-24.04-base"
    assert "platform" not in applied  # clone-vm has no --platform


def test_parser_without_config_args_is_untouched(config):
    parser = argparse.ArgumentParser()
    parser.add_argument("--server")
    apply_config(parser, CONNECTION_KEYS, [])
    assert parser.parse_args([]).server == "esxi.example.com"
