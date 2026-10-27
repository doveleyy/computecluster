"""Root retires an app and restores it without touching its neighbours.

Every test runs the helper against a two-app policy: ``notes`` is retired or
restored, ``habits`` stands in for the app the owner uses daily. Its units,
journal, backups and policy fingerprint must come out byte-identical.
"""

import hashlib
import json
import os
import stat
from pathlib import Path
from uuid import uuid4

import pytest
from test_app_deployments import ledger as ledger_fixture
from test_installer_recovery import container
from test_managed_installer import helper as helper_fixture
from test_managed_installer import installed_by_root

from app.app_contracts import ContractError
from app.app_deployment_contracts import (
    ArchiveRecord,
    InstalledSnapshot,
    Receipt,
    RetiredSnapshot,
    fingerprint,
)
from app.app_deployments import ReconciliationPending
from app.app_spool import publish
from ops import managed_installer
from ops.installer_child import DEFERRED
from ops.installer_policy import HostPolicy, app_fingerprint
from ops.managed_installer import BackupFacts, HostOperationFailed, Installer

ledger = ledger_fixture
helper = helper_fixture
IMAGE_ID = "sha256:" + "b" * 64


@pytest.fixture
def pair(helper, tmp_path):
    """The helper's installer with a Habits-shaped second app in its policy."""
    installer, operation, authority, _, release, events = helper
    policy = json.loads(installer.policy.model_dump_json())
    habits = json.loads(json.dumps(policy["apps"][0]))
    habits["registration"]["app_id"] = "habits"
    habits["registration"]["route"] = "/apps/habits"
    habits["registration"]["installation"]["host_port"] += 1
    habits["registration"]["installation"]["probe"]["service"] = "habits"
    habits["registration"]["bindings"]["state_directory"] = str(
        tmp_path / "habits-state"
    )
    habits["registration"]["bindings"]["backup_directory"] = str(
        tmp_path / "backups-habits"
    )
    habits["uid"] = habits["gid"] = 1003
    habits["credential_file"] = str(tmp_path / "habits-credentials/habits")
    policy["apps"].append(habits)
    installer.policy = HostPolicy.model_validate_json(json.dumps(policy))
    installer.digest = fingerprint(installer.policy)
    for directory in ("backups", "backups-habits", "units", "state", "habits-state"):
        (tmp_path / directory).mkdir(exist_ok=True)
    (tmp_path / "state" / ".notes.db").write_bytes(b"live notes database")
    return installer, operation, authority, installer.policy.apps[0], release, events


def previous_of(operation, app, release):
    return InstalledSnapshot(
        operation_id=uuid4(),
        generation=1,
        selection=operation.approval.selection.model_copy(update={"generation": 0}),
        registration=app.registration,
        image=release.image,
        image_id=IMAGE_ID,
    )


def approved(operation, kind, generation):
    selection = operation.approval.selection.model_copy(
        update={"generation": generation}
    )
    return operation.model_copy(
        update={
            "kind": kind,
            "approval": operation.approval.model_copy(update={"selection": selection}),
        }
    )


def arm(pair, monkeypatch, kind, current):
    """Point the helper at ``kind`` approved against the journaled ``current``."""
    installer, operation, authority, app, release, events = pair
    operation = approved(operation, kind, current.generation)
    authority = authority.model_copy(
        update={"operation": operation, "previous": current}
    )
    installed_by_root(installer, current)
    publish(installer.journal / (release.archive_sha256 + ".zip"), release.archive)
    monkeypatch.setattr(installer, "authority", lambda *_: (authority, app, release))
    monkeypatch.setattr(
        installer,
        "child",
        lambda *args: b"switching"
        if args[2] == "state"
        else operation.model_dump_json().encode(),
    )
    monkeypatch.setattr(installer, "local_image", lambda *_: IMAGE_ID)
    for name in ("stop_timers", "stop_supervisor"):
        monkeypatch.setattr(installer, name, lambda *_, name=name: events.append(name))
    monkeypatch.setattr(installer, "remove", lambda *_: events.append("remove"))
    monkeypatch.setattr(installer, "probe", lambda *_: events.append("probe"))
    monkeypatch.setattr(
        installer,
        "start",
        lambda *args, **kwargs: events.append(
            ("start", kwargs.get("installed_operation_id"))
        ),
    )
    monkeypatch.setattr(
        installer, "lifecycle", lambda _, flag: events.append(("lifecycle", flag))
    )
    return installer, operation, authority, app, release, events


def recording_backup(events, content_sha256="c" * 64):
    def backup(app, sqlite_file, destination, **_):
        events.append(("backup", destination.name))
        destination.write_bytes(b"final state")
        os.chmod(destination, 0o600)
        return BackupFacts("f" * 64, content_sha256, ("entries",), 11)

    return backup


def receipt_of(installer, operation):
    return Receipt.model_validate_json(
        (installer.journal / f"{operation.operation_id}.receipt.json").read_bytes()
    )


def retired_of(previous, operation, archive=None):
    return RetiredSnapshot(
        operation_id=operation.operation_id,
        generation=2,
        selection=operation.approval.selection,
        registration=previous.registration,
        last_installed=previous,
        archive=archive,
    )


def test_retire_stops_then_archives_then_removes_units_and_journals_retired_current(
    pair, monkeypatch
):
    _, operation, _, app, release, _ = pair
    previous = previous_of(operation, app, release)
    installer, operation, _, app, _, events = arm(pair, monkeypatch, "retire", previous)
    monkeypatch.setattr(installer, "owned", lambda *_: container(previous))
    monkeypatch.setattr(installer, "backup", recording_backup(events))
    installer.execute(operation.operation_id)
    archive_name = f"retired-g2-{operation.operation_id}.sqlite"
    assert events == [
        "limits",
        "stop_timers",
        "stop_supervisor",
        "remove",
        ("backup", archive_name),
        ("lifecycle", False),
    ], "the archive must follow the stop and precede the unit removal"
    receipt = receipt_of(installer, operation)
    assert receipt.state == "succeeded" and receipt.authorization_committed
    assert receipt.installed is None
    assert receipt.retired == retired_of(
        previous,
        operation,
        ArchiveRecord(
            file_name=archive_name,
            sha256="f" * 64,
            content_sha256="c" * 64,
            tables=("entries",),
            size_bytes=11,
        ),
    )
    assert installer.current(app) == receipt.retired
    assert installer.running(app) is None
    assert not (installer.journal / f"{operation.operation_id}.switch.json").exists()
    spool = Path(installer.policy.spool) / "receipts" / f"{operation.operation_id}.json"
    assert Receipt.model_validate_json(spool.read_bytes()) == receipt
    assert (Path(app.registration.bindings.state_directory) / ".notes.db").exists()


def test_retire_refuses_a_container_it_does_not_recognise(pair, monkeypatch):
    _, operation, _, app, release, _ = pair
    previous = previous_of(operation, app, release)
    installer, operation, _, app, _, events = arm(pair, monkeypatch, "retire", previous)
    stranger = container(previous, operation="0" * 36)
    monkeypatch.setattr(installer, "owned", lambda *_: stranger)
    monkeypatch.setattr(installer, "backup", recording_backup(events))
    installer.execute(operation.operation_id)
    receipt = receipt_of(installer, operation)
    assert (receipt.state, receipt.error) == ("blocked", "recovery_required")
    assert events == ["limits"], "a contradictory container must stay untouched"
    assert installer.current(app) == previous


def test_retire_archive_is_root_private_verified_and_never_pruned(
    pair, monkeypatch, tmp_path
):
    installer, operation, _, app, _, _ = pair
    monkeypatch.setattr(managed_installer.os, "chown", lambda *_: None)
    monkeypatch.setattr(managed_installer.os, "fchown", lambda *_: None)
    calls = []

    def child(uid, gid, kind, source, target):
        calls.append((kind, Path(source).name))
        Path(target).write_bytes(b"sqlite copy")
        return json.dumps(
            {
                "sha256": hashlib.sha256(b"sqlite copy").hexdigest(),
                "tables": ["entries"],
                "content_sha256": "c" * 64,
            }
        ).encode()

    monkeypatch.setattr(installer, "child", child)
    first = installer.retirement_archive(app, ".notes.db", 2, operation)
    archive = tmp_path / "backups" / first.file_name
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert [kind for kind, _ in calls] == ["backup", "backup"], (
        "the archive skipped the disposable restore"
    )
    assert calls[0][1] == ".notes.db"
    assert first == ArchiveRecord(
        file_name=f"retired-g2-{operation.operation_id}.sqlite",
        sha256=hashlib.sha256(b"sqlite copy").hexdigest(),
        content_sha256="c" * 64,
        tables=("entries",),
        size_bytes=len(b"sqlite copy"),
    )
    stale = tmp_path / "backups" / ("pre-switch-g1-" + "0" * 36 + ".sqlite")
    stale.write_bytes(b"x")
    installer.prune_backups(app, 99)
    assert archive.exists(), "pruning pre-switch backups removed the archive"
    assert installer.pre_switch_backups(app) == []
    assert list((tmp_path / "backups").glob("daily-*.sqlite")) == []
    retry = approved(operation, "retire", 1).model_copy(
        update={"operation_id": uuid4()}
    )
    second = installer.retirement_archive(app, ".notes.db", 2, retry)
    names = sorted(path.name for path in (tmp_path / "backups").glob("retired-*"))
    assert names == [second.file_name], "a retry left the superseded archive behind"


def test_retire_archive_failure_restarts_the_app_and_reports_backup_failed(
    pair, monkeypatch
):
    _, operation, _, app, release, _ = pair
    previous = previous_of(operation, app, release)
    installer, operation, _, app, _, events = arm(pair, monkeypatch, "retire", previous)
    monkeypatch.setattr(installer, "owned", lambda *_: None)

    def failing_backup(*_, **__):
        raise ContractError("Disposable restore failed")

    monkeypatch.setattr(installer, "backup", failing_backup)
    installer.execute(operation.operation_id)
    receipt = receipt_of(installer, operation)
    assert (receipt.state, receipt.error) == ("recovered", "backup_failed")
    assert receipt.retired is None
    assert installer.current(app) == previous
    assert events[-3:] == [
        ("start", previous.operation_id),
        "probe",
        ("lifecycle", True),
    ]
    assert ("lifecycle", False) not in events
    assert not list((Path(installer.policy.spool) / "receipts").glob("retired-*"))


BOUNDARIES = {
    "before-stop": ("stop_timers", None),
    "before-remove": ("remove", None),
    "before-archive": ("backup", None),
    "before-units": ("lifecycle", None),
    "before-journal-receipt": ("commit", None),
    "before-current-record": (None, "replace_file"),
    "before-spool-receipt": (None, "publish"),
}


@pytest.mark.parametrize("boundary", sorted(BOUNDARIES))
def test_retire_replays_from_every_crash_boundary(pair, monkeypatch, boundary):
    _, operation, _, app, release, _ = pair
    previous = previous_of(operation, app, release)
    installer, operation, _, app, _, events = arm(pair, monkeypatch, "retire", previous)
    monkeypatch.setattr(installer, "owned", lambda *_: None)
    monkeypatch.setattr(installer, "backup", recording_backup(events))
    method, function = BOUNDARIES[boundary]
    crashed = []

    def crash_once(original, *, when=lambda *a, **k: True):
        def wrapper(*args, **kwargs):
            if not crashed and when(*args, **kwargs):
                crashed.append(boundary)
                raise RuntimeError("process exit")
            return original(*args, **kwargs)

        return wrapper

    if method is not None:
        monkeypatch.setattr(installer, method, crash_once(getattr(installer, method)))
    elif function == "publish":
        spool_receipt = (
            Path(installer.policy.spool) / "receipts" / f"{operation.operation_id}.json"
        )
        monkeypatch.setattr(
            managed_installer,
            "publish",
            crash_once(
                managed_installer.publish, when=lambda p, *_: p == spool_receipt
            ),
        )
    else:
        monkeypatch.setattr(
            managed_installer,
            "replace_file",
            crash_once(managed_installer.replace_file),
        )
    with pytest.raises(RuntimeError, match="process exit"):
        installer.execute(operation.operation_id)
    assert crashed == [boundary]
    installer.execute(operation.operation_id)
    receipt = receipt_of(installer, operation)
    assert receipt.state == "succeeded" and receipt.retired is not None
    assert installer.current(app) == receipt.retired
    spool = Path(installer.policy.spool) / "receipts" / f"{operation.operation_id}.json"
    assert Receipt.model_validate_json(spool.read_bytes()) == receipt
    assert not (installer.journal / f"{operation.operation_id}.switch.json").exists()
    archives = list(Path(app.registration.bindings.backup_directory).glob("retired-*"))
    assert [path.name for path in archives] == [receipt.retired.archive.file_name]
    assert all(not isinstance(e, tuple) or e[0] != "start" for e in events), (
        "a replay restarted the app it was retiring"
    )
    assert events.count(("lifecycle", False)) >= 1
    assert ("lifecycle", True) not in events
    installer.execute(operation.operation_id)
    assert installer.current(app) == receipt.retired


def retired_pair(pair, monkeypatch, kind="deploy", archive_content="c" * 64):
    _, operation, _, app, release, _ = pair
    previous = previous_of(operation, app, release)
    retired = retired_of(
        previous,
        approved(operation, "retire", 1),
        archive_content
        and ArchiveRecord(
            file_name=f"retired-g2-{uuid4()}.sqlite",
            sha256="f" * 64,
            content_sha256=archive_content,
            tables=("entries",),
            size_bytes=11,
        ),
    )
    installer, operation, authority, app, release, events = arm(
        pair, monkeypatch, kind, retired
    )
    monkeypatch.setattr(installer, "existing_state", lambda *_: True)
    monkeypatch.setattr(installer, "backup", recording_backup(events))
    return installer, operation, authority, app, release, events, retired


def test_restore_rehearses_against_the_kept_state_with_the_retired_image(
    pair, monkeypatch, tmp_path
):
    installer, _, authority, app, release, _, retired = retired_pair(pair, monkeypatch)
    monkeypatch.setattr(managed_installer.os, "chown", lambda *_: None)
    monkeypatch.setattr(managed_installer.os, "fchown", lambda *_: None)
    pulled = []
    monkeypatch.setattr(
        installer, "image", lambda image, **_: pulled.append(image) or IMAGE_ID
    )
    probed = []
    monkeypatch.setattr(
        installer,
        "semantic_state",
        lambda _a, _app, probed_release, *_: probed.append(probed_release.image)
        or "records",
    )
    monkeypatch.setattr(installer, "rehearsal_ready", lambda *_: None)
    backup = tmp_path / "input.sqlite"
    backup.write_bytes(b"kept data")
    rehearsal = Installer.rehearse(installer, authority, app, release, IMAGE_ID, backup)
    assert pulled == [retired.last_installed.image], (
        "the rehearsal did not read the data with the image that last wrote it"
    )
    assert probed == [release.image, release.image, release.image]
    assert rehearsal.previous_image_id == IMAGE_ID
    assert rehearsal.checks["previous_reads_current"] is True
    assert rehearsal.passed()
    assert rehearsal.backup_sha256 == hashlib.sha256(b"kept data").hexdigest()


def test_container_present_while_retired_blocks_the_restore(pair, monkeypatch):
    installer, operation, _, app, _, events, retired = retired_pair(pair, monkeypatch)
    monkeypatch.setattr(
        installer, "owned", lambda *_: container(retired.last_installed)
    )
    installer.execute(operation.operation_id)
    receipt = receipt_of(installer, operation)
    assert (receipt.state, receipt.error) == ("blocked", "recovery_required")
    assert "remove" not in events
    assert not any(isinstance(e, tuple) and e[0] == "start" for e in events)
    assert not (installer.journal / f"{operation.operation_id}.switch.json").exists()
    assert installer.current(app) == retired


def test_failed_restore_recovers_to_retired_with_data_intact(pair, monkeypatch):
    installer, operation, _, app, _, events, retired = retired_pair(pair, monkeypatch)
    monkeypatch.setattr(installer, "owned", lambda *_: None)

    def probe(*_):
        raise ContractError("new image failed readiness")

    monkeypatch.setattr(installer, "probe", probe)
    state = Path(app.registration.bindings.state_directory) / ".notes.db"
    before = state.read_bytes()
    installer.execute(operation.operation_id)
    receipt = receipt_of(installer, operation)
    assert (receipt.state, receipt.error) == ("recovered", "installation_failed")
    assert receipt.installed is None and receipt.retired is None
    assert installer.current(app) == retired, "a failed restore moved the journal"
    assert state.read_bytes() == before
    marker = events.index(("backup", f"pre-switch-g3-{operation.operation_id}.sqlite"))
    assert events[marker + 1 :] == [
        "stop_timers",
        "stop_supervisor",
        "remove",
        ("start", None),
        "stop_timers",
        "remove",
        ("lifecycle", False),
    ], "the fallback must tear down, never restart the retired release"
    assert ("start", retired.last_installed.operation_id) not in events


@pytest.mark.parametrize("defect", ["missing", "changed"])
def test_restore_refuses_missing_or_changed_state_before_switching(
    pair, monkeypatch, defect
):
    installer, operation, _, app, _, events, retired = retired_pair(
        pair, monkeypatch, archive_content="d" * 64 if defect == "changed" else "c" * 64
    )
    if defect == "missing":
        monkeypatch.setattr(installer, "existing_state", lambda *_: False)
    installer.execute(operation.operation_id)
    receipt = receipt_of(installer, operation)
    assert (receipt.state, receipt.error) == ("failed", "state_" + defect)
    assert not (installer.journal / f"{operation.operation_id}.switch.json").exists()
    assert "remove" not in events and "stop_timers" not in events
    assert installer.current(app) == retired


@pytest.mark.parametrize("kept", [False, True])
def test_restore_of_an_app_retired_without_an_archive_follows_its_existing_state(
    pair, monkeypatch, kept
):
    """Catches a restore refused as state_missing, or its pre-switch backup
    failing, because the policy now declares a state file that an app retired
    without one never had."""
    installer, operation, *_, events, _ = retired_pair(
        pair, monkeypatch, archive_content=None
    )
    monkeypatch.setattr(installer, "existing_state", lambda *_: kept)
    monkeypatch.setattr(installer, "owned", lambda *_: None)
    installer.execute(operation.operation_id)
    receipt = receipt_of(installer, operation)
    assert (receipt.state, receipt.error) == ("succeeded", None)
    backups = [event[1] for event in events if event[0] == "backup"]
    assert backups == (
        ["input.sqlite", f"pre-switch-g3-{operation.operation_id}.sqlite"]
        if kept
        else []
    )


def test_boot_and_daily_backup_refuse_a_retired_app(pair, monkeypatch):
    installer, _, authority, app, _, events, _ = retired_pair(pair, monkeypatch)
    monkeypatch.setattr(managed_installer, "load_policy", lambda _: installer.policy)
    with pytest.raises(ContractError, match="retired"):
        installer.installed(authority, app)

    def deferred_child(*_):
        raise HostOperationFailed(DEFERRED)

    monkeypatch.setattr(installer, "child", deferred_child)
    monkeypatch.setattr(installer, "command", lambda *a, **k: events.append(a))
    with pytest.raises(ReconciliationPending):
        installer.boot("notes")
    with pytest.raises(HostOperationFailed):
        installer.daily_backup("notes")
    assert events == [], "a retired app reached docker or took a backup"


def test_retiring_one_app_leaves_other_units_fingerprints_and_backups_untouched(
    pair, monkeypatch, tmp_path
):
    _, operation, _, app, release, _ = pair
    previous = previous_of(operation, app, release)
    installer, operation, _, app, _, events = arm(pair, monkeypatch, "retire", previous)
    habits = installer.policy.apps[1]
    units = tmp_path / "units"
    habits_unit = units / "hp-managed-habits--app.service"
    habits_unit.write_text("# Managed app habits\n[Service]\nExecStart=/bin/true\n")
    habits_timer = units / "hp-managed-habits--backup.timer"
    habits_timer.write_text("# Managed app habits\n[Timer]\nOnCalendar=daily\n")
    notes_unit = units / "hp-managed-notes--app.service"
    notes_unit.write_text("# Managed app notes\n[Service]\nExecStart=/bin/true\n")
    habits_current = previous.model_copy(
        update={
            "registration": habits.registration,
            "selection": previous.selection.model_copy(update={"app_id": "habits"}),
        }
    )
    installed_by_root(installer, habits_current)
    habits_backup = tmp_path / "backups-habits" / "daily-20261027-010000.sqlite"
    habits_backup.write_bytes(b"habits backup")
    fingerprints = {
        app_id: app_fingerprint(installer.policy, app_id) for app_id in ("habits",)
    }

    def snapshot():
        return {
            "units": {
                path.name: path.read_bytes()
                for path in units.iterdir()
                if "habits" in path.name
            },
            "journal": installer.current(habits),
            "backups": {
                path.name: path.read_bytes()
                for path in (tmp_path / "backups-habits").iterdir()
            },
        }

    before = snapshot()
    commands = []
    monkeypatch.setattr(
        installer, "command", lambda *args, **kwargs: commands.append(args)
    )
    monkeypatch.setattr(installer, "lifecycle", Installer.lifecycle.__get__(installer))
    monkeypatch.setattr(
        installer, "stop_timers", Installer.stop_timers.__get__(installer)
    )
    monkeypatch.setattr(installer, "owned", lambda *_: None)
    monkeypatch.setattr(installer, "backup", recording_backup(events))
    installer.execute(operation.operation_id)
    assert receipt_of(installer, operation).state == "succeeded"
    assert not notes_unit.exists(), "the retired app kept its unit"
    assert snapshot() == before
    assert all("habits" not in " ".join(argv) for argv in commands)
    assert ("systemctl", "disable", "--now", notes_unit.name) in commands
    assert {
        app_id: app_fingerprint(installer.policy, app_id) for app_id in ("habits",)
    } == fingerprints
    assert installer.running(habits) == habits_current
