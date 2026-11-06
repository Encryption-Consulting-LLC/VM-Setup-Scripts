"""The deploy and bake recipes, with ESXi stubbed out."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import vmkit
from configgen import NetworkConfig, ValidationError

import recipes
from recipes import _bake as bake_module

STATIC = NetworkConfig(
    mode="static", ip="192.168.1.50", prefix=24, gateway="192.168.1.1", dns1="192.168.1.10"
)
SETUP_DIR = Path(__file__).resolve().parents[1] / "base-vm-setup" / "linux-server"


def clone_result(name="web01", powered_on=True):
    return vmkit.CloneResult(
        name=name, mac="00:50:56:00:00:01", guest_os="ubuntu-64", total_vms=3,
        powered_on=powered_on,
    )


@pytest.fixture
def fake_clone(monkeypatch):
    calls = []

    def _clone(conn, **kwargs):
        # The ISO must exist while the clone uploads it.
        assert Path(kwargs["iso_path"]).is_file()
        calls.append(kwargs)
        return clone_result(kwargs["name"], kwargs.get("power_on", False))

    monkeypatch.setattr(vmkit, "clone_workflow", _clone)
    return calls


def test_render_scripts_names_and_order():
    spec = recipes.DeploySpec(
        name="web01", platform="linux", network=STATIC, backend="netplan",
        accounts=(("root", "S3cret!"), ("svc", "Other1!")),
    )
    names = [name for name, _ in recipes.render_scripts(spec)]
    assert names == ["10-hostname.sh", "20-network.sh", "30-password-root.sh",
                     "30-password-svc.sh"]


def test_render_scripts_windows_suffix():
    spec = recipes.DeploySpec(name="dc01", platform="windows", network=NetworkConfig(mode="dhcp"))
    assert [n for n, _ in recipes.render_scripts(spec)] == ["10-hostname.ps1", "20-network.ps1"]


def test_render_scripts_validates():
    spec = recipes.DeploySpec(name="bad_name!", platform="linux", network=STATIC)
    with pytest.raises(ValidationError):
        recipes.render_scripts(spec)


def test_deploy_builds_iso_and_clones(fake_clone, tmp_path):
    extra = tmp_path / "40-app.sh"
    extra.write_text("echo app\n")
    payload = tmp_path / "agent.bin"
    payload.write_bytes(b"\x00\x01")
    spec = recipes.DeploySpec(
        name="web01", platform="linux", network=STATIC, backend="netplan",
        scripts=(extra,), files=(payload,),
    )
    result = recipes.deploy(object(), spec, base="ub-24.04-base", datastore="ds1",
                            cpus=2, mem_mb=4096, power_on=True)

    assert result.iso.scripts == ["10-hostname.sh", "20-network.sh", "40-app.sh"]
    assert result.iso.files == ["agent.bin"]
    assert result.iso_path is None
    (call,) = fake_clone
    assert call["name"] == "web01" and call["base"] == "ub-24.04-base"
    assert not Path(call["iso_path"]).exists()  # temp ISO cleaned up


def test_deploy_keep_iso(fake_clone, tmp_path):
    keep = tmp_path / "isos" / "web01-config.iso"
    spec = recipes.DeploySpec(name="web01", platform="linux", network=STATIC)
    result = recipes.deploy(object(), spec, keep_iso=keep, base="b", datastore="ds",
                            cpus=2, mem_mb=4096)
    assert result.iso_path == keep and keep.is_file()


def test_bake_iso_packs_runner_as_payload_not_script(tmp_path):
    contents = recipes.build_bake_iso(SETUP_DIR, tmp_path / "bake.iso")
    assert contents.scripts == [recipes.SETUP_SCRIPT]
    assert contents.files == [recipes.RUNNER_SCRIPT]


def test_bake_iso_missing_files(tmp_path):
    with pytest.raises(recipes.SetupFilesNotFoundError):
        recipes.build_bake_iso(tmp_path, tmp_path / "bake.iso")


class FakeVms:
    """Power states reported on successive polls; None means the VM is gone."""

    def __init__(self, states):
        self.states = list(states)

    def __call__(self, content, name):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return None if state is None else SimpleNamespace(runtime=SimpleNamespace(powerState=state))


CONN = SimpleNamespace(content=None)


def test_wait_for_power_off_polls_until_off(monkeypatch):
    states = FakeVms(["poweredOn", "poweredOn", "poweredOff"])
    monkeypatch.setattr(bake_module, "get_vm_by_name", states)
    sleeps = []
    recipes.wait_for_power_off(CONN, "b", poll_s=5, sleep=sleeps.append)
    assert sleeps == [5, 5]


def test_wait_for_power_off_times_out(monkeypatch):
    monkeypatch.setattr(bake_module, "get_vm_by_name", FakeVms(["poweredOn"]))
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    with pytest.raises(recipes.BakeTimeoutError, match="still poweredOn"):
        recipes.wait_for_power_off(CONN, "b", timeout_s=60, poll_s=15, sleep=sleep,
                                   clock=lambda: now[0])


def test_wait_for_power_off_vm_vanished(monkeypatch):
    monkeypatch.setattr(bake_module, "get_vm_by_name", FakeVms([None]))
    with pytest.raises(vmkit.VmNotFoundError):
        recipes.wait_for_power_off(CONN, "b", sleep=lambda s: None)


@pytest.fixture
def fake_update(monkeypatch):
    calls = []
    monkeypatch.setattr(vmkit, "update_workflow", lambda conn, **kw: calls.append(kw))
    return calls


def test_bake_waits_then_detaches(monkeypatch, fake_clone, fake_update):
    monkeypatch.setattr(bake_module, "get_vm_by_name", FakeVms(["poweredOff"]))
    result = recipes.bake(CONN, name="ub-base", pre="ub-base-pre", setup_dir=SETUP_DIR,
                          datastore="ds1", cpus=2, mem_mb=4096)
    (clone,) = fake_clone
    assert clone["base"] == "ub-base-pre" and clone["power_on"] is True
    assert fake_update == [{"name": "ub-base", "datastore": "ds1", "remove_iso": True}]
    assert result.finished and result.iso_detached


def test_bake_no_wait(fake_clone, fake_update):
    result = recipes.bake(CONN, name="ub-base", pre="ub-base-pre", setup_dir=SETUP_DIR,
                          datastore="ds1", wait=False, cpus=2, mem_mb=4096)
    assert not result.finished and fake_update == []


def test_bake_keep_disc(monkeypatch, fake_clone, fake_update):
    monkeypatch.setattr(bake_module, "get_vm_by_name", FakeVms(["poweredOff"]))
    result = recipes.bake(CONN, name="ub-base", pre="ub-base-pre", setup_dir=SETUP_DIR,
                          datastore="ds1", detach_iso=False, cpus=2, mem_mb=4096)
    assert result.finished and not result.iso_detached and fake_update == []


# --------------------------------------------------------------------------- #
# install                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_create(monkeypatch):
    calls = []

    def _create(conn, **kwargs):
        calls.append(kwargs)
        return vmkit.CreateResult(
            name=kwargs["name"], mac="00:50:56:00:00:02", guest_os=kwargs["guest_os"],
            disk_gb=kwargs["disk_gb"], cdroms=list(kwargs["cdroms"]), total_vms=4,
            powered_on=kwargs["power_on"],
        )

    monkeypatch.setattr(vmkit, "create_workflow", _create)
    return calls


def _install(**overrides):
    kwargs = dict(name="ub-base-pre", datastore="datastore1 (3)",
                  installer="ISO/ubuntu-autoinstall.iso", seed="ISO/seed.iso")
    kwargs.update(overrides)
    return recipes.install(CONN, **kwargs)


def test_install_creates_with_installer_first_then_waits_and_detaches(
    monkeypatch, fake_create, fake_update
):
    monkeypatch.setattr(bake_module, "get_vm_by_name", FakeVms(["poweredOn", "poweredOff"]))
    result = _install(poll_s=0)
    (create,) = fake_create
    # EFI boots sata0:0 on a blank disk, so the installer must be first.
    assert create["cdroms"] == ["ISO/ubuntu-autoinstall.iso", "ISO/seed.iso"]
    assert create["power_on"] is True
    assert create["guest_os"] == "ubuntu-64" and create["disk_gb"] == 40
    assert fake_update == [{"name": "ub-base-pre", "datastore": "datastore1 (3)",
                            "remove_iso": True}]
    assert result.finished and result.discs_detached


def test_install_passes_create_options_through(monkeypatch, fake_create, fake_update):
    monkeypatch.setattr(bake_module, "get_vm_by_name", FakeVms(["poweredOff"]))
    _install(cpus=4, mem_mb=8192, network="Lab", thin=False, disk_gb=80)
    (create,) = fake_create
    assert (create["cpus"], create["mem_mb"], create["network"]) == (4, 8192, "Lab")
    assert create["thin"] is False and create["disk_gb"] == 80


def test_install_no_wait(fake_create, fake_update):
    result = _install(wait=False)
    assert not result.finished and not result.discs_detached and fake_update == []


def test_install_keep_discs(monkeypatch, fake_create, fake_update):
    monkeypatch.setattr(bake_module, "get_vm_by_name", FakeVms(["poweredOff"]))
    result = _install(detach_discs=False)
    assert result.finished and not result.discs_detached and fake_update == []


def test_install_timeout_keeps_discs_and_explains(monkeypatch, fake_create, fake_update):
    monkeypatch.setattr(bake_module, "get_vm_by_name", FakeVms(["poweredOn"]))
    # install() takes no clock/sleep hooks; a near-zero timeout and poll keep it fast.
    with pytest.raises(recipes.InstallTimeoutError, match="firstboot-bootstrap.log") as info:
        _install(timeout_s=0.01, poll_s=0.001)
    assert "Both discs are still attached" in str(info.value)
    assert fake_update == []  # never detach a VM that may still be mid-install


def test_install_timeout_is_not_a_bake_timeout_but_shares_exit_code_7():
    assert not issubclass(recipes.InstallTimeoutError, recipes.BakeTimeoutError)
    assert issubclass(recipes.InstallTimeoutError, recipes.PowerOffTimeoutError)
    assert issubclass(recipes.BakeTimeoutError, recipes.PowerOffTimeoutError)


def test_install_missing_iso_propagates(monkeypatch, fake_update):
    def _create(conn, **kwargs):
        raise vmkit.DatastoreFileNotFoundError("ISO not found on the datastore: [ds] x.iso")

    monkeypatch.setattr(vmkit, "create_workflow", _create)
    with pytest.raises(vmkit.DatastoreFileNotFoundError):
        _install()
    assert fake_update == []


def test_bake_timeout_message_is_unchanged(monkeypatch):
    monkeypatch.setattr(bake_module, "get_vm_by_name", FakeVms(["poweredOn"]))
    now = [0.0]
    with pytest.raises(recipes.BakeTimeoutError) as info:
        recipes.wait_for_power_off(CONN, "b", timeout_s=60, poll_s=15,
                                   sleep=lambda s: now.__setitem__(0, now[0] + s),
                                   clock=lambda: now[0])
    assert str(info.value) == (
        "VM 'b' is still poweredOn after 1 min, so the bake most likely failed: "
        "firstboot-setup.sh powers the VM off when it succeeds. Read /var/log/firstboot.log "
        "and /var/log/firstboot-error.log on its console. The bake disc is still attached."
    )
