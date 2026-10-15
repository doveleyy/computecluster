import contextlib
import hashlib
import io
import json
import os
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_managed_installer import helper as helper_fixture
from test_managed_installer import ledger as ledger_fixture

from app.app_contracts import ContractError
from app.app_deployment_contracts import Receipt, fingerprint
from ops import installer_child, managed_installer
from ops.installer_policy import HostPolicy, app_fingerprint
from ops.managed_installer import Installer

ledger = ledger_fixture
helper = helper_fixture


def existing_app(helper, tmp_path, monkeypatch):
    installer, operation, authority, app, release, events = helper
    directory = tmp_path / "existing"
    directory.mkdir()
    app = app.model_copy(
        update={
            "registration": app.registration.model_copy(
                update={
                    "bindings": app.registration.bindings.model_copy(
                        update={"state_directory": str(directory)}
                    )
                }
            )
        }
    )
    monkeypatch.setattr(installer, "authority", lambda *_: (authority, app, release))
    original_lstat = Path.lstat

    def app_owner(path):
        info = original_lstat(path)
        if path == directory or path == directory / app.state.sqlite_file:
            fields = list(info)
            fields[4] = app.uid
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", app_owner)
    return installer, operation, authority, app, release, events, directory


def test_first_install_backs_up_existing_state_without_managed_history(
    helper, tmp_path, monkeypatch
):
    installer, operation, _, app, _, _, directory = existing_app(
        helper, tmp_path, monkeypatch
    )
    path = directory / app.state.sqlite_file
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE entries(value)")
        connection.execute("INSERT INTO entries VALUES ('kept')")
    checked = []
    monkeypatch.setattr(
        managed_installer,
        "root_path",
        lambda *a, **k: checked.append((a, k)),
        raising=False,
    )
    inputs = []
    monkeypatch.setattr(
        installer,
        "backup",
        lambda a, f, d: (
            inputs.append(path.read_bytes()),
            d.write_bytes(path.read_bytes()),
        )[1],
    )
    installer.execute(operation.operation_id)
    assert inputs == [path.read_bytes()]
    assert checked == [((directory,), {"leaf_uid": app.uid})]
    receipt = Receipt.model_validate_json(
        (installer.journal / f"{operation.operation_id}.receipt.json").read_bytes()
    )
    assert receipt.state == "succeeded"


@pytest.mark.parametrize("changed", [False, True])
def test_first_install_checks_full_restored_records_before_and_after_startup(
    helper, tmp_path, monkeypatch, changed
):
    installer, _, authority, app, release, _ = helper
    backup = tmp_path / "existing.sqlite"
    with sqlite3.connect(backup) as connection:
        connection.execute("CREATE TABLE entries(owner, value)")
        connection.execute("INSERT INTO entries VALUES ('stable-owner', 'kept')")
    before = backup.read_bytes()
    monkeypatch.setattr(managed_installer.os, "chown", lambda *_: None)
    monkeypatch.setattr(managed_installer.os, "fchown", lambda *_: None)
    monkeypatch.setattr(installer, "remove", lambda *_: None)
    monkeypatch.setattr(installer, "rehearsal_ready", lambda *_: None)
    probes = []

    def semantic(authority, app, release, image, directory):
        with sqlite3.connect(
            (directory / app.state.sqlite_file).as_uri() + "?mode=ro", uri=True
        ) as connection:
            digest = hashlib.sha256(
                "\n".join(connection.iterdump()).encode()
            ).hexdigest()
        probes.append(digest)
        return digest

    def start(*args, rehearsal, **kwargs):
        if changed:
            with sqlite3.connect(rehearsal / app.state.sqlite_file) as connection:
                connection.execute("UPDATE entries SET value='lost'")
        return "candidate"

    monkeypatch.setattr(installer, "start", start)
    monkeypatch.setattr(installer, "semantic_state", semantic)
    monkeypatch.setattr(installer, "backup", lambda *a, **k: None)
    if changed:
        with pytest.raises(ContractError, match="semantic"):
            Installer.rehearse(
                installer, authority, app, release, "sha256:" + "b" * 64, backup
            )
    else:
        result = Installer.rehearse(
            installer, authority, app, release, "sha256:" + "b" * 64, backup
        )
        assert result.previous_image_id is None
        assert result.backup_sha256 == hashlib.sha256(before).hexdigest()
        assert result.checks["semantic_records"]
    assert len(probes) == 2
    assert backup.read_bytes() == before


@pytest.mark.parametrize(
    "kind", ["missing", "symlink", "directory", "corrupt", "unreadable", "unsafe"]
)
def test_first_install_distinguishes_missing_state_from_refused_state(
    helper, tmp_path, monkeypatch, kind
):
    installer, operation, _, app, _, _, directory = existing_app(
        helper, tmp_path, monkeypatch
    )
    path = directory / app.state.sqlite_file
    if kind == "symlink":
        path.symlink_to(tmp_path / "missing-target")
    elif kind == "directory":
        path.mkdir()
    elif kind != "missing":
        path.write_bytes(b"invalid SQLite")

    def check_path(*args, **kwargs):
        if kind == "unsafe":
            raise ContractError("Host path ownership is unsafe")

    monkeypatch.setattr(managed_installer, "root_path", check_path)
    monkeypatch.setattr(managed_installer.os, "chown", lambda *_: None)
    monkeypatch.setattr(managed_installer.os, "fchown", lambda *_: None)
    original_child = installer.child

    def child(uid, gid, command, source, target):
        if command != "backup":
            return original_child(uid, gid, command, source, target)
        if kind == "unreadable":
            raise PermissionError("Application UID cannot read state")
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                installer_child.backup(Path(source), Path(target))
        except sqlite3.Error as error:
            raise ContractError("Host backup refused invalid state") from error
        return output.getvalue().encode()

    monkeypatch.setattr(installer, "child", child)
    reached = []
    rehearsal = installer.rehearse
    monkeypatch.setattr(
        installer,
        "rehearse",
        lambda *args: (reached.append(args[-1]), rehearsal(*args))[1],
    )
    installer.execute(operation.operation_id)
    receipt = Receipt.model_validate_json(
        (installer.journal / f"{operation.operation_id}.receipt.json").read_bytes()
    )
    assert receipt.state == ("succeeded" if kind == "missing" else "failed")
    assert reached == ([None] if kind == "missing" else [])
    assert not list(Path(installer.policy.scratch).iterdir())


@pytest.mark.parametrize("disappears", [False, True])
def test_first_install_takes_fresh_pre_switch_backup_or_refuses_missing_state(
    helper, tmp_path, monkeypatch, disappears
):
    installer, original, authority, app, release, events, directory = existing_app(
        helper, tmp_path, monkeypatch
    )
    path = directory / app.state.sqlite_file
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE entries(value)")
        connection.execute("INSERT INTO entries VALUES ('before-rehearsal')")
    operation = original.model_copy(update={"kind": "deploy"})
    authority = authority.model_copy(update={"operation": operation})
    monkeypatch.setattr(installer, "authority", lambda *_: (authority, app, release))
    monkeypatch.setattr(managed_installer, "root_path", lambda *a, **k: None)
    monkeypatch.setattr(managed_installer.os, "chown", lambda *_: None)
    monkeypatch.setattr(managed_installer.os, "fchown", lambda *_: None)
    backups = tmp_path / "backups"
    backups.mkdir()
    taken = []

    def child(uid, gid, kind, source, target):
        if kind == "operation":
            return operation.model_dump_json().encode()
        if kind == "state":
            return b"switching"
        assert kind == "backup"
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                installer_child.backup(Path(source), Path(target))
        except sqlite3.Error as error:
            raise ContractError("Host backup failed") from error
        with sqlite3.connect(target) as connection:
            taken.append(connection.execute("SELECT value FROM entries").fetchall())
        return output.getvalue().encode()

    monkeypatch.setattr(installer, "child", child)
    rehearsal = installer.rehearse

    def rehearse(*args):
        result = rehearsal(*args)
        if disappears:
            path.unlink()
        else:
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE entries SET value='after-rehearsal'")
        return result

    monkeypatch.setattr(installer, "rehearse", rehearse)
    monkeypatch.setattr(installer, "owned", lambda *_: None)
    monkeypatch.setattr(installer, "stop_timers", lambda *_: events.append("stop"))
    monkeypatch.setattr(installer, "stop_supervisor", lambda *_: None)
    for name in ("remove", "start", "probe"):
        monkeypatch.setattr(installer, name, lambda *a, **k: None)
    installer.execute(operation.operation_id)
    receipt = Receipt.model_validate_json(
        (installer.journal / f"{operation.operation_id}.receipt.json").read_bytes()
    )
    assert receipt.state == ("failed" if disappears else "succeeded")
    assert taken[:2] == [[("before-rehearsal",)], [("before-rehearsal",)]]
    if disappears:
        assert "stop" not in events
        assert not list(backups.iterdir())
    else:
        assert taken[2:] == [[("after-rehearsal",)], [("after-rehearsal",)]]
        copy = backups / f"pre-switch-g1-{operation.operation_id}.sqlite"
        with sqlite3.connect(copy) as connection:
            assert connection.execute("SELECT value FROM entries").fetchall() == [
                ("after-rehearsal",)
            ]


def wishlist_policy(installer):
    payload = installer.policy.model_dump(mode="json")
    app = payload["apps"][0]
    app["maintenance"][0]["timeout_seconds"] = 600
    app["maintenance_schedules"] = [
        {"name": "cleanup", "offset_minutes": 137, "jitter_seconds": 900}
    ]
    app["backup_schedule"] = {
        "hour": 3,
        "minute": 41,
        "jitter_seconds": 600,
        "requires_mounts_for": ["/mnt/private backups"],
    }
    return payload


def test_root_schedule_preserves_calendar_phase_jitter_timeout_and_mount_dependency(
    helper, monkeypatch
):
    installer, _, _, _, _, _ = helper
    policy = HostPolicy.model_validate_json(json.dumps(wishlist_policy(installer)))
    app = policy.apps[0]
    units = Path(policy.units)
    units.mkdir()
    monkeypatch.setattr(installer, "command", lambda *a, **k: "")
    Installer.lifecycle(installer, app, True)
    refresh = "hp-managed-notes--maintenance-cleanup"
    timer = (units / (refresh + ".timer")).read_text()
    assert timer == (
        "# Managed app notes\n[Timer]\nOnCalendar=*-*-* 02/6:17:00\n"
        "Persistent=true\nRandomizedDelaySec=900\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    assert "TimeoutStartSec=600\n" in (units / (refresh + ".service")).read_text()
    assert (units / "hp-managed-notes--backup.timer").read_text() == (
        "# Managed app notes\n[Timer]\nOnCalendar=*-*-* 03:41:00\n"
        "Persistent=true\nRandomizedDelaySec=600\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    service = (units / "hp-managed-notes--backup.service").read_text()
    assert '[Unit]\nRequiresMountsFor="/mnt/private backups"\n[Service]\n' in service


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown-task",
        "duplicate-task",
        "offset",
        "negative-offset",
        "jitter",
        "short-jitter",
        "hour",
        "minute",
        "backup-jitter",
        "mount-relative",
        "mount-newline",
        "mount-tab",
        "duplicate-mount",
        "missing-state",
        "string-directive",
    ],
)
def test_root_policy_refuses_invalid_or_injected_schedule(helper, mutation):
    installer, *_ = helper
    payload = wishlist_policy(installer)
    app = payload["apps"][0]
    schedule = app["maintenance_schedules"][0]
    backup = app["backup_schedule"]
    if mutation == "unknown-task":
        schedule["name"] = "other"
    elif mutation == "duplicate-task":
        app["maintenance_schedules"].append(schedule.copy())
    elif mutation == "offset":
        schedule["offset_minutes"] = 360
    elif mutation == "negative-offset":
        schedule["offset_minutes"] = -1
    elif mutation == "jitter":
        schedule["jitter_seconds"] = 3601
    elif mutation == "short-jitter":
        app["maintenance"][0]["interval_hours"] = 1
        schedule.update(offset_minutes=0, jitter_seconds=3600)
    elif mutation == "hour":
        backup["hour"] = 24
    elif mutation == "minute":
        backup["minute"] = 60
    elif mutation == "backup-jitter":
        backup["jitter_seconds"] = -1
    elif mutation.startswith("mount-"):
        backup["requires_mounts_for"] = [
            {
                "mount-relative": "relative/path",
                "mount-newline": "/mnt/backups\nExecStart=bad",
                "mount-tab": "/mnt/backups\tbad",
            }[mutation]
        ]
    elif mutation == "duplicate-mount":
        backup["requires_mounts_for"] *= 2
    elif mutation == "missing-state":
        app["state"] = None
    else:
        schedule["OnCalendar"] = "arbitrary systemd directive"
    with pytest.raises(ValidationError):
        HostPolicy.model_validate_json(json.dumps(payload))


def test_mount_dependency_quotes_literal_characters_without_expanding_them(
    helper, monkeypatch
):
    installer, *_ = helper
    payload = wishlist_policy(installer)
    payload["apps"][0]["backup_schedule"]["requires_mounts_for"] = [
        '/mnt/a "quoted"\\folder%value$'
    ]
    policy = HostPolicy.model_validate_json(json.dumps(payload))
    units = Path(policy.units)
    units.mkdir()
    monkeypatch.setattr(installer, "command", lambda *a, **k: "")
    Installer.lifecycle(installer, policy.apps[0], True)
    assert (
        'RequiresMountsFor="/mnt/a \\"quoted\\"\\\\folder%%value$"\n'
        in (units / "hp-managed-notes--backup.service").read_text()
    )


def test_unconfigured_schedule_preserves_existing_approval_fingerprint(helper):
    installer, *_ = helper
    policy = installer.policy
    shared = policy.model_dump(
        mode="json", exclude={"apps", "spool", "scratch", "registry_credentials"}
    )
    old_app = policy.apps[0].model_dump(
        mode="json", exclude={"maintenance_schedules", "backup_schedule", "mounts"}
    )
    assert app_fingerprint(policy, "notes") == fingerprint({**shared, "app": old_app})
    changed = HostPolicy.model_validate_json(json.dumps(wishlist_policy(installer)))
    assert app_fingerprint(changed, "notes") != app_fingerprint(policy, "notes")
