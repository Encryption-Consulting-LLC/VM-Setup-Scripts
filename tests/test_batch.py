"""Batches: the engine (recipes._batch), deploy/update batches, and the --range CLIs."""

import logging
import threading
import time
from types import SimpleNamespace

import pytest
import vmkit
from configgen import NetworkConfig

import recipes
from cli import deploy_vm, update_vm
from cli._common import _BatchTagFilter, batch_exit_code

# --------------------------------------------------------------------------- #
# parse_range / expand                                                        #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spec, expected", [
    ("91-96", ["91", "92", "93", "94", "95", "96"]),
    ("91,92,95", ["91", "92", "95"]),
    ("1-3,7", ["1", "2", "3", "7"]),
    ("01-03", ["01", "02", "03"]),
    ("8-10", ["8", "9", "10"]),
    (" 5 ", ["5"]),
])
def test_parse_range(spec, expected):
    assert recipes.parse_range(spec) == expected


@pytest.mark.parametrize("spec, message", [
    ("", "invalid range part"),
    ("96-91", "backwards"),
    ("a-b", "invalid range part"),
    ("1-3,2", "repeats 2"),
    ("1,,2", "invalid range part"),
    ("1-1000", "longer than"),
])
def test_parse_range_rejects(spec, message):
    with pytest.raises(ValueError, match=message):
        recipes.parse_range(spec)


def test_expand():
    assert recipes.expand("cbom-ha-{n}", "91") == "cbom-ha-91"
    assert recipes.expand("192.168.4.{n}/16", "7") == "192.168.4.7/16"


# --------------------------------------------------------------------------- #
# run_batch                                                                   #
# --------------------------------------------------------------------------- #
class Conn:
    opened = 0

    def __init__(self):
        Conn.opened += 1
        self.closed = False
        self.content = None

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def reset_conn():
    Conn.opened = 0


def _run(items, work, **kw):
    return recipes.run_batch(items, name_of=str, work=work, connect=Conn, **kw)


def test_results_keep_input_order_and_failures_do_not_stop_others():
    def work(conn, item, should_cancel):
        time.sleep(0.01 * (5 - item))  # finish out of order
        if item == 2:
            raise RuntimeError("boom")
        return item * 10

    result = _run([0, 1, 2, 3, 4], work, jobs=3)
    assert [o.name for o in result.outcomes] == ["0", "1", "2", "3", "4"]
    assert [o.result for o in result.succeeded] == [0, 10, 30, 40]
    (failed,) = result.failed
    assert failed.name == "2" and str(failed.error) == "boom"
    assert not result.ok and batch_exit_code(result) == 8


def test_concurrency_never_exceeds_jobs():
    lock, running, peak = threading.Lock(), [0], [0]

    def work(conn, item, should_cancel):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.02)
        with lock:
            running[0] -= 1

    _run(list(range(9)), work, jobs=3)
    assert peak[0] == 3


def test_one_session_per_worker_reused_and_closed():
    conns = []

    def work(conn, item, should_cancel):
        conns.append(conn)
        time.sleep(0.02)  # long enough that both workers take items

    _run(list(range(6)), work, jobs=2)
    assert Conn.opened == 2
    assert len({id(c) for c in conns}) == 2
    assert all(c.closed for c in conns)


def test_jobs_capped_at_batch_size():
    _run([1], lambda c, i, s: None, jobs=8)
    assert Conn.opened == 1


def test_failed_connect_fails_that_item_and_is_retried():
    calls = []

    def connect():
        calls.append(1)
        if len(calls) == 1:
            raise vmkit.AuthenticationError("bad password")
        return Conn()

    result = recipes.run_batch([1, 2], name_of=str, work=lambda c, i, s: i,
                               connect=connect, jobs=1)
    assert [o.ok for o in result.outcomes] == [False, True]
    assert isinstance(result.outcomes[0].error, vmkit.AuthenticationError)


def test_on_failure_note_is_recorded():
    def work(conn, item, should_cancel):
        raise RuntimeError("clone failed")

    result = _run([1], work, on_failure=lambda conn, item, exc: "files deleted")
    assert result.outcomes[0].note == "files deleted"


def test_a_failing_cleanup_is_reported_not_raised():
    def cleanup(conn, item, exc):
        raise OSError("datastore gone")

    result = _run([1], lambda c, i, s: 1 / 0, on_failure=cleanup)
    assert "cleanup after the failure also failed" in result.outcomes[0].note


def test_cancel_skips_unstarted_items_and_signals_running_ones():
    cancel = threading.Event()
    seen_cancel = []

    def work(conn, item, should_cancel):
        if item == 0:
            cancel.set()
            seen_cancel.append(should_cancel())
        return item

    result = _run([0, 1, 2, 3], work, jobs=1, cancel=cancel)
    assert seen_cancel == [True]
    assert [o.name for o in result.skipped] == ["1", "2", "3"]
    assert result.cancelled and batch_exit_code(result) == 130


def test_current_item_is_set_inside_work_only():
    seen = []
    _run(["a", "b"], lambda c, i, s: seen.append(recipes.current_item.get()), jobs=1)
    assert seen == ["a", "b"]
    assert recipes.current_item.get() is None


def test_on_outcome_sees_every_item():
    seen = []
    _run([1, 2, 3], lambda c, i, s: i, on_outcome=lambda o: seen.append(o.name), jobs=2)
    assert sorted(seen) == ["1", "2", "3"]


def test_all_ok_exits_zero():
    assert batch_exit_code(_run([1, 2], lambda c, i, s: i)) == 0


# --------------------------------------------------------------------------- #
# deploy_batch                                                                #
# --------------------------------------------------------------------------- #
def spec(name, ip):
    net = NetworkConfig(mode="static", ip=ip, prefix=24, gateway="192.168.4.1",
                        dns1="192.168.4.1")
    return recipes.DeploySpec(name=name, platform="linux", network=net, backend="netplan")


SPECS = [spec(f"cbom-ha-{n}", f"192.168.4.{n}") for n in (91, 92, 93)]


@pytest.fixture
def host(monkeypatch):
    state = SimpleNamespace(existing=set(), clones=[], cleaned=[], plan_calls=[],
                            fail=set(), registered_then_failed=set(), fits=True)

    monkeypatch.setattr(vmkit, "list_vm_names", lambda content: set(state.existing))

    def plan(content, datastore, specs, max_usage_pct):
        state.plan_calls.append([s.name for s in specs])
        p = SimpleNamespace(pct_now=10.0, pct_after=40.0, vm_count=len(specs), warnings=())

        def raise_if_insufficient():
            if not state.fits:
                raise vmkit.InsufficientSpaceError("datastore would be 95% full")
            return p

        p.raise_if_insufficient = raise_if_insufficient
        return p

    monkeypatch.setattr(vmkit, "plan_capacity", plan)

    def clone(conn, **kw):
        state.clones.append(kw)
        if kw["name"] in state.fail:
            raise vmkit.TaskFailedError("VMDK copy failed", label="VMDK copy")
        return vmkit.CloneResult(name=kw["name"], mac=kw["mac"], guest_os="ubuntu-64",
                                 total_vms=9, powered_on=kw.get("power_on", False))

    monkeypatch.setattr(vmkit, "clone_workflow", clone)

    def cleanup(conn, name, datastore):
        if name in state.registered_then_failed:
            raise vmkit.VmExistsError("registered")
        state.cleaned.append(name)
        return vmkit.CleanupResult(name=name, deleted=True)

    monkeypatch.setattr(vmkit, "cleanup_partial_clone", cleanup)
    return state


def _deploy(specs=SPECS, **kw):
    kwargs = dict(base="ub-2404-base", datastore="datastore1 (3)", cpus=2, mem_mb=4096, jobs=3)
    kwargs.update(kw)
    return recipes.deploy_batch(Conn, specs, **kwargs)


def test_deploy_batch_clones_every_vm_with_distinct_macs(host):
    result = _deploy(power_on=True)
    assert result.ok
    assert sorted(c["name"] for c in host.clones) == ["cbom-ha-91", "cbom-ha-92", "cbom-ha-93"]
    macs = [c["mac"] for c in host.clones]
    assert len(set(macs)) == 3
    assert all(c["progress_context"] == c["name"] for c in host.clones)
    assert all(c["power_on"] is True and c["base"] == "ub-2404-base" for c in host.clones)


def test_capacity_is_planned_once_for_the_whole_batch(host):
    _deploy()
    assert host.plan_calls == [["cbom-ha-91", "cbom-ha-92", "cbom-ha-93"]]


def test_insufficient_space_stops_the_batch_before_any_clone(host):
    host.fits = False
    with pytest.raises(vmkit.InsufficientSpaceError):
        _deploy()
    assert host.clones == []


def test_skip_disk_check_skips_the_plan(host):
    _deploy(skip_disk_check=True)
    assert host.plan_calls == [] and len(host.clones) == 3


def test_preflight_lists_every_problem_and_clones_nothing(host):
    host.existing = {"cbom-ha-92"}
    bad = [*SPECS, spec("cbom-ha-91", "192.168.4.93")]  # dup name, dup IP
    with pytest.raises(recipes.BatchPreflightError) as info:
        _deploy(bad)
    problems = "\n".join(info.value.problems)
    assert "name cbom-ha-91 appears 2 times" in problems
    assert "IP 192.168.4.93 is given to 2 VMs" in problems
    assert "cbom-ha-92 already exists" in problems
    assert host.clones == []


def test_a_failed_clone_is_cleaned_up_and_the_rest_continue(host):
    host.fail = {"cbom-ha-92"}
    result = _deploy()
    assert [o.ok for o in result.outcomes] == [True, False, True]
    assert host.cleaned == ["cbom-ha-92"]
    assert result.outcomes[1].note == "partial clone files deleted"


def test_a_vm_that_registered_before_failing_is_left_in_place(host):
    host.fail = host.registered_then_failed = {"cbom-ha-93"}
    result = _deploy()
    assert result.outcomes[2].note == "VM registered before failing; left in place for inspection"
    assert host.cleaned == []


def test_a_shared_mac_is_refused(host):
    with pytest.raises(ValueError, match="MAC"):
        _deploy(mac="00:50:56:00:00:01")


def test_plan_warnings_reach_the_caller(host):
    plans = []
    _deploy(on_plan=plans.append)
    assert plans and plans[0].vm_count == 3


# --------------------------------------------------------------------------- #
# update_batch                                                                #
# --------------------------------------------------------------------------- #
def test_update_batch_requires_every_vm_to_exist(monkeypatch):
    monkeypatch.setattr(vmkit, "list_vm_names", lambda content: {"vm-1", "vm-3"})
    calls = []
    monkeypatch.setattr(vmkit, "update_workflow", lambda conn, **kw: calls.append(kw))
    with pytest.raises(recipes.BatchPreflightError, match="no VM named vm-2"):
        recipes.update_batch(Conn, ["vm-1", "vm-2", "vm-3"], datastore="ds")
    assert calls == []


def test_update_batch_updates_each_with_its_own_iso(monkeypatch):
    monkeypatch.setattr(vmkit, "list_vm_names", lambda content: {"vm-1", "vm-2"})
    calls = []
    monkeypatch.setattr(vmkit, "update_workflow", lambda conn, **kw: calls.append(kw) or kw)
    result = recipes.update_batch(Conn, ["vm-1", "vm-2"], datastore="ds", cpus=4,
                                  mem_mb=8192, iso_paths={"vm-2": "/tmp/two.iso"})
    assert result.ok
    by_name = {c["name"]: c for c in calls}
    assert by_name["vm-1"]["iso_path"] is None and by_name["vm-2"]["iso_path"] == "/tmp/two.iso"
    assert all(c["cpus"] == 4 and c["mem_mb"] == 8192 for c in calls)


# --------------------------------------------------------------------------- #
# Log tagging                                                                 #
# --------------------------------------------------------------------------- #
def test_log_lines_are_tagged_once_across_handlers():
    tag = _BatchTagFilter()
    record = logging.LogRecord("vmkit", logging.INFO, __file__, 1, "Copying disk", None, None)
    token = recipes.current_item.set("cbom-ha-91")
    try:
        tag.filter(record)
        tag.filter(record)  # a second handler sees the same record
    finally:
        recipes.current_item.reset(token)
    assert record.getMessage() == "[cbom-ha-91] Copying disk"


def test_lines_outside_a_batch_are_untouched():
    record = logging.LogRecord("vmkit", logging.INFO, __file__, 1, "Connecting", None, None)
    _BatchTagFilter().filter(record)
    assert record.getMessage() == "Connecting"


# --------------------------------------------------------------------------- #
# CLIs                                                                        #
# --------------------------------------------------------------------------- #
DEPLOY = ["--platform", "linux", "--gateway", "192.168.4.1", "--dns1", "192.168.4.1",
          "-s", "esxi", "-u", "root", "-b", "ub-2404-base", "--dry-run"]


@pytest.fixture
def no_config(tmp_path, monkeypatch):
    empty = tmp_path / "empty.toml"
    empty.write_text("")
    monkeypatch.setenv("VMSETUP_CONFIG", str(empty))
    monkeypatch.delenv("VMSETUP_PROFILE", raising=False)


def run_cli(module, argv, monkeypatch):
    monkeypatch.setattr("sys.argv", [module.__name__, *argv])
    module.main()


def test_deploy_range_dry_run_expands_names_and_ips(no_config, monkeypatch, capsys):
    run_cli(deploy_vm, ["cbom-ha-{n}", "--range", "91,93", "--ip", "192.168.4.{n}/16",
                        "-j", "2", *DEPLOY], monkeypatch)
    out = capsys.readouterr().out
    assert "deploy 2 VMs" in out and "2 at a time" in out
    assert "-n cbom-ha-91 " in out and "--ip 192.168.4.91 --prefix 16" in out
    assert "-n cbom-ha-93 " in out and "--ip 192.168.4.93 --prefix 16" in out
    assert "cbom-ha-92" not in out


def test_single_deploy_is_unchanged(no_config, monkeypatch, capsys):
    run_cli(deploy_vm, ["web01", "--ip", "192.168.4.50/24", *DEPLOY], monkeypatch)
    out = capsys.readouterr().out
    assert "Dry run: deploy 'web01'" in out and "--ip 192.168.4.50 --prefix 24" in out


@pytest.mark.parametrize("argv, message", [
    (["web-{n}", "--ip", "192.168.4.5/24"], "--range was not given"),
    (["web01", "--range", "1-3", "--ip", "192.168.4.{n}/24"], "needs {n} in NAME"),
    (["web-{n}", "--range", "1-3", "--ip", "192.168.4.5/24"], "same address"),
    (["web-{n}", "--range", "1-3", "--dhcp", "-m", "00:50:56:00:00:01"], "share it"),
    (["web-{n}", "--range", "1-3", "--dhcp", "--keep-iso", "x.iso"], "overwrite"),
    (["web-{n}", "--range", "3-1", "--dhcp"], "backwards"),
    (["web_{n}", "--range", "1-2", "--dhcp"], "web_1"),
    (["web-{n}", "--range", "1-2", "--ip", "192.168.4.{n}99/24"], "web-2"),
])
def test_deploy_range_usage_errors(no_config, monkeypatch, capsys, argv, message):
    with pytest.raises(SystemExit) as info:
        run_cli(deploy_vm, [*argv, *DEPLOY], monkeypatch)
    assert info.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("argv, message", [
    (["-n", "web-{n}"], "--range was not given"),
    (["-n", "web01", "--range", "1-3"], "needs {n} in -n"),
    (["-n", "web-{n}", "--range", "1-3", "-m", "00:50:56:00:00:01"], "share it"),
    (["-n", "web-{n}", "--range", "1-2", "--iso", "missing-{n}.iso"], "web-1"),
])
def test_update_range_usage_errors(no_config, monkeypatch, capsys, argv, message):
    with pytest.raises(SystemExit) as info:
        run_cli(update_vm, [*argv, "-s", "esxi", "-u", "root"], monkeypatch)
    assert info.value.code == 2
    assert message in capsys.readouterr().err


def test_each_mac_is_drawn_excluding_the_ones_already_assigned(host, monkeypatch):
    """Random MACs rarely collide by chance, so pin the generator: it returns the
    first address not in ``existing``. Uncoordinated draws would all get :01."""
    pool = ["00:50:56:00:00:01", "00:50:56:00:00:02", "00:50:56:00:00:03"]
    monkeypatch.setattr(
        vmkit, "random_mac", lambda existing=None: next(m for m in pool if m not in (existing or set()))
    )
    _deploy()
    assert sorted(c["mac"] for c in host.clones) == pool
