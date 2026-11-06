"""install-base: its config keys, argument handling and exit codes."""

import pytest

import recipes
from cli import install_base
from cli._common import exit_on_error
from cli._config import INSTALL_KEYS, KNOWN_KEYS, apply_config

CONFIG = """
[defaults]
server = "192.168.1.207"
user = "root"
datastore = "datastore1 (3)"

[profile.ubuntu]
installer = "ISO/ubuntu-24.04.5-autoinstall.iso"
disk-gb = 60
guest-os = "ubuntu-64"
"""


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    monkeypatch.setenv("VMSETUP_CONFIG", str(path))
    monkeypatch.delenv("VMSETUP_PROFILE", raising=False)
    return path


def parse(argv):
    parser = install_base.build_parser()
    apply_config(parser, INSTALL_KEYS, argv)
    return parser.parse_args(argv)


def test_install_keys_are_known_config_keys():
    assert INSTALL_KEYS <= KNOWN_KEYS


def test_profile_supplies_installer_and_disk_size(config):
    args = parse(["ub-base", "--profile", "ubuntu", "--seed", "ISO/seed.iso"])
    assert args.installer == "ISO/ubuntu-24.04.5-autoinstall.iso"
    assert args.disk_gb == 60
    assert args.datastore == "datastore1 (3)"
    assert args.server == "192.168.1.207"


def test_installer_and_seed_required_without_config(tmp_path, monkeypatch):
    monkeypatch.setenv("VMSETUP_CONFIG", str(tmp_path / "empty.toml"))
    (tmp_path / "empty.toml").write_text("")
    with pytest.raises(SystemExit):
        parse(["ub-base", "-s", "h", "-u", "root"])


@pytest.mark.parametrize("bad", ["0", "-1", "forty", "1.5"])
def test_disk_size_must_be_a_positive_integer(config, bad):
    with pytest.raises(SystemExit):
        parse(["ub-base", "--profile", "ubuntu", "--seed", "s.iso", "--disk-gb", bad])


def test_dry_run_plan_names_the_vm_and_discs_in_boot_order(config, capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["install-base", "ub-base", "--profile", "ubuntu",
                                     "--seed", "[other] seed.iso", "--dry-run"])
    install_base.main()
    out = capsys.readouterr().out
    assert "Create VM 'ub-base-pre'" in out
    assert out.index("CD-ROM 0 (boots): [datastore1 (3)] ISO/ubuntu-24.04.5-autoinstall.iso") \
        < out.index("CD-ROM 1:         [other] seed.iso")
    assert "Then: bake-base ub-base" in out


def test_as_overrides_the_vm_name(config, capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["install-base", "ub-base", "--as", "custom-pre",
                                     "--profile", "ubuntu", "--seed", "s.iso", "--dry-run"])
    install_base.main()
    out = capsys.readouterr().out
    assert "Create VM 'custom-pre'" in out and "bake-base ub-base --from custom-pre" in out


def test_as_must_differ_from_name(config, monkeypatch):
    monkeypatch.setattr("sys.argv", ["install-base", "ub-base", "--as", "ub-base",
                                     "--profile", "ubuntu", "--seed", "s.iso", "--dry-run"])
    with pytest.raises(SystemExit):
        install_base.main()


def test_install_timeout_exits_7():
    import logging
    with pytest.raises(SystemExit) as info:
        with exit_on_error(logging.getLogger("test"), "Install"):
            raise recipes.InstallTimeoutError("still running")
    assert info.value.code == 7
