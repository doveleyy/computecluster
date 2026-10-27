import ctypes
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_app_deployments import request
from test_installer_recovery import container, installed
from test_managed_installer import helper as helper_fixture
from test_managed_installer import ledger as ledger_fixture

from app.app_contracts import ContractError
from app.app_deployments import ReconciliationPending, read_installed_authority
from ops import managed_installer
from ops.installer_child import DEFERRED
from ops.managed_installer import HostOperationFailed, capture

ledger = ledger_fixture
helper = helper_fixture

APP_UNIT = "hp-managed-notes--app.service"


def record(installer, monkeypatch):
    commands = []
    monkeypatch.setattr(
        installer, "command", lambda *args, **kwargs: commands.append(args) or ""
    )
    monkeypatch.setattr(installer, "remove", lambda *_: commands.append("remove"))
    return commands


def test_deploy_enables_the_app_unit_and_starts_it_once_the_core_acknowledges(
    helper, monkeypatch
):
    installer, operation, authority, app, release, _ = helper
    units = Path(installer.policy.units)
    units.mkdir()
    commands = record(installer, monkeypatch)
    monkeypatch.delattr(installer, "lifecycle")
    deploy = operation.model_copy(update={"kind": "deploy"})
    deployed = authority.model_copy(update={"operation": deploy})
    monkeypatch.setattr(installer, "authority", lambda *_: (deployed, app, release))

    def child(uid, gid, kind, source, target):
        if kind == "state":
            return b"switching"
        if kind == "installed":
            raise HostOperationFailed(DEFERRED)
        return deploy.model_dump_json().encode()

    monkeypatch.setattr(installer, "child", child)
    monkeypatch.setattr(installer, "owned", lambda *_: None)
    monkeypatch.setattr(managed_installer, "load_policy", lambda _: installer.policy)
    for name in ("stop_timers", "start", "probe"):
        monkeypatch.setattr(installer, name, lambda *a, **k: None)
    installer.execute(deploy.operation_id)

    unit = (units / APP_UNIT).read_text()
    assert "Restart=on-failure" in unit
    assert f"SuccessExitStatus={DEFERRED}" in unit
    assert ("systemctl", "enable", APP_UNIT) in commands
    started = [c for c in commands if APP_UNIT in c and ("--now" in c or "start" in c)]
    assert started == [], "the switch must not start the unit before core acknowledges"

    with pytest.raises(ReconciliationPending):
        installer.boot("notes")
    assert [c for c in commands if c[0] == "docker"] == []

    spool = Path(installer.policy.spool)
    name = f"{deploy.operation_id}.json"
    assert (spool / "receipts" / name).exists()
    assert (spool / "requests" / name).exists()
    commands.clear()
    installer.sweep_markers()
    assert commands == [], "the receipt is committed but core has not consumed it"
    (spool / "requests" / name).unlink()
    installer.sweep_markers()
    assert commands == [("systemctl", "start", "--no-block", APP_UNIT)]
    installer.sweep_markers()
    assert commands == [("systemctl", "start", "--no-block", APP_UNIT)]


def test_app_unit_stop_signals_only_the_supervisor_and_outlasts_docker_stop(
    helper, monkeypatch
):
    installer, _, _, app, _, _ = helper
    units = Path(installer.policy.units)
    units.mkdir()
    record(installer, monkeypatch)
    managed_installer.Installer.lifecycle(installer, app, True)
    text = (units / APP_UNIT).read_text()
    service = dict(
        line.split("=", 1)
        for line in text.split("[Service]\n", 1)[1].split("\n[", 1)[0].splitlines()
    )
    assert service.get("KillMode") == "mixed", (
        "a control-group SIGTERM also kills the stop handler's docker stop child"
    )
    assert int(service.get("TimeoutStopSec", "0")) > 10, (
        "systemd must not SIGKILL the handler inside docker stop's 10 s grace"
    )


def test_switch_stops_the_supervisor_before_replacing_its_container(
    helper, tmp_path, monkeypatch
):
    installer, operation, authority, app, release, _ = helper
    current = installed(helper, monkeypatch)
    units = Path(installer.policy.units)
    units.mkdir()
    (units / APP_UNIT).write_text("# Managed app notes\n[Service]\n")
    (tmp_path / "backups").mkdir()
    commands = record(installer, monkeypatch)
    monkeypatch.delattr(installer, "lifecycle")
    deploy = operation.model_copy(update={"kind": "deploy", "operation_id": uuid4()})
    deployed = authority.model_copy(update={"operation": deploy, "previous": current})
    monkeypatch.setattr(installer, "authority", lambda *_: (deployed, app, release))
    monkeypatch.setattr(installer, "owned", lambda *_: container(current))
    monkeypatch.setattr(
        installer, "backup", lambda app, f, d, **_: d.write_bytes(b"copy")
    )
    monkeypatch.setattr(
        installer,
        "child",
        lambda *a: b"switching"
        if a[2] == "state"
        else deploy.model_dump_json().encode(),
    )
    installer.execute(deploy.operation_id)
    assert commands.index(("systemctl", "stop", APP_UNIT)) < commands.index("remove")


def test_restore_previous_stops_the_supervisor_before_replacing_its_container(
    helper, monkeypatch
):
    installer, _, authority, app, _, _ = helper
    current = installed(helper, monkeypatch)
    units = Path(installer.policy.units)
    units.mkdir()
    (units / APP_UNIT).write_text("# Managed app notes\n[Service]\n")
    commands = record(installer, monkeypatch)
    monkeypatch.setattr(installer, "lifecycle", lambda *_: None)
    monkeypatch.setattr(installer, "local_image", lambda *_: current.image_id)
    installer.restore_previous(authority, app)
    assert commands.index(("systemctl", "stop", APP_UNIT)) < commands.index("remove")


def test_boot_refusals_other_than_pending_reconciliation_stay_failures(
    helper, monkeypatch
):
    installer, *_ = helper
    installed(helper, monkeypatch)

    def child(*args):
        raise HostOperationFailed(1)

    monkeypatch.setattr(installer, "child", child)
    with pytest.raises(ContractError) as refused:
        installer.boot("notes")
    assert not isinstance(refused.value, ReconciliationPending)


def test_capture_reports_the_child_exit_status():
    with pytest.raises(HostOperationFailed) as failed:
        capture(("sh", "-c", f"exit {DEFERRED}"), timeout=5, limit=64)
    assert failed.value.returncode == DEFERRED


def test_child_exits_with_the_deferred_status_while_an_operation_switches(
    ledger, tmp_path, monkeypatch
):
    request(ledger)
    with ledger.database.connect() as connection:
        connection.execute("UPDATE app_operations SET state = 'switching'")
    with ledger.database.connect() as connection, pytest.raises(ReconciliationPending):
        read_installed_authority(connection, "notes")
    monkeypatch.setattr(
        ctypes, "CDLL", lambda name: SimpleNamespace(prctl=lambda *a: 0)
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["installer_child", "installed", str(tmp_path / "core.sqlite"), "notes"],
    )
    with pytest.raises(SystemExit) as exit_:
        runpy.run_module("ops.installer_child", run_name="__main__")
    assert exit_.value.code == DEFERRED


def test_run_maps_a_deferred_boot_to_the_unit_success_status(tmp_path, monkeypatch):
    class Stub:
        def __init__(self, *args):
            pass

        def sweep_scratch(self):
            pass

        def sweep_markers(self):
            pass

        def advertise(self):
            pass

        def boot(self, app_id):
            raise ReconciliationPending("Installation requires reconciliation")

    monkeypatch.setattr(
        managed_installer,
        "load_policy",
        lambda _: SimpleNamespace(journal=str(tmp_path)),
    )
    monkeypatch.setattr(managed_installer, "validate_paths", lambda _: None)
    monkeypatch.setattr(managed_installer, "Installer", Stub)
    monkeypatch.setattr(
        sys, "argv", ["managed_installer", "--policy", str(tmp_path), "--run", "notes"]
    )
    with pytest.raises(SystemExit) as exit_:
        managed_installer.main()
    assert exit_.value.code == DEFERRED == 75
