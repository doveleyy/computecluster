"""Root helper behaviour that only showed up on a real host.

Backup copies must open from a read-only mount, long waits must fit the
platform poller, the registry login must stay root-private and root-only, the
narrowed release is all root holds, and finished switch markers are removed.
"""

import contextlib
import io
import json
import os
import selectors
import sqlite3
from pathlib import Path

import pytest
from test_managed_installer import helper as helper_fixture
from test_managed_installer import ledger as ledger_fixture

from app.app_contracts import ContractError
from app.app_deployment_contracts import OperationHint, Receipt
from app.app_spool import publish
from ops import installer_child, installer_policy, managed_installer
from ops.installer_policy import HostPolicy, app_fingerprint
from ops.managed_installer import HostRelease, capture

ledger = ledger_fixture
helper = helper_fixture


def wal_database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE entries(value)")
    connection.execute("INSERT INTO entries VALUES ('kept')")
    connection.commit()
    connection.close()
    assert path.read_bytes()[18:20] == b"\x02\x02"
    return path


def in_process_child(uid, gid, kind, source, target):
    assert kind == "backup"
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        installer_child.backup(Path(source), Path(target))
    return output.getvalue().encode()


def test_backup_copies_of_a_wal_database_open_from_a_read_only_mount(
    helper, tmp_path, monkeypatch
):
    installer, _, _, app, _, _ = helper
    monkeypatch.setattr(managed_installer.os, "chown", lambda *_: None)
    monkeypatch.setattr(managed_installer.os, "fchown", lambda *_: None)
    monkeypatch.setattr(installer, "child", in_process_child)
    live = wal_database(tmp_path / "live" / "notes.db")
    (tmp_path / "backups").mkdir()
    destination = tmp_path / "backups" / "pre-switch-g2-test.sqlite"
    digest = installer.backup(app, "notes.db", destination, source=live)
    payload = destination.read_bytes()
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "notes.db").write_bytes(payload)
    os.chmod(mount / "notes.db", 0o600)
    os.chmod(mount, 0o500)
    try:
        for uri, kwargs in (
            (str(mount / "notes.db"), {}),
            (f"file:{mount / 'notes.db'}?mode=ro", {"uri": True}),
        ):
            connection = sqlite3.connect(uri, **kwargs)
            try:
                assert connection.execute("PRAGMA integrity_check").fetchone() == (
                    "ok",
                )
                assert connection.execute("SELECT value FROM entries").fetchall() == [
                    ("kept",)
                ]
            finally:
                connection.close()
        assert sorted(p.name for p in mount.iterdir()) == ["notes.db"]
    finally:
        os.chmod(mount, 0o700)
    assert payload[18:20] == b"\x01\x01", "copy still carries the WAL header"
    assert sorted(p.name for p in destination.parent.iterdir()) == [destination.name]
    assert not list(Path(installer.policy.scratch).iterdir())
    assert len(digest) == 64


def test_capture_waits_in_slices_that_fit_an_epoll_timeout(monkeypatch):
    year = 365 * 24 * 3600
    waits = []
    base = selectors.DefaultSelector

    class EpollLike(base):
        def select(self, timeout=None):
            waits.append(timeout)
            if timeout is not None and int(timeout * 1000) > 2**31 - 1:
                raise OverflowError("timeout is too large")
            return super().select(timeout)

    monkeypatch.setattr(managed_installer.selectors, "DefaultSelector", EpollLike)
    assert capture(("sh", "-c", "echo ok"), timeout=year, limit=1024) == b"ok\n"
    from ops.managed_installer import SELECT_INTERVAL, wait_interval

    assert waits and max(waits) <= SELECT_INTERVAL
    assert wait_interval(year) * 1000 <= 2**31 - 1


def registry_login(tmp_path):
    directory = tmp_path / "registry"
    directory.mkdir(mode=0o700)
    (directory / "config.json").write_bytes(b'{"auths": {}}')
    os.chmod(directory / "config.json", 0o600)
    return directory


def test_root_registry_login_is_private_and_used_only_by_root_pulls(
    helper, tmp_path, monkeypatch
):
    from ops.installer_policy import registry_credentials

    installer, operation, *_ = helper
    login = registry_login(tmp_path)
    before = installer.policy
    policy = HostPolicy.model_validate_json(
        json.dumps(
            {**before.model_dump(mode="json"), "registry_credentials": str(login)}
        )
    )
    installer.policy = policy
    monkeypatch.setattr(installer_policy, "root_path", lambda *_, **__: None)
    image = "ghcr.io/example/notes@sha256:" + "b" * 64
    commands = []

    def command(*argv, **_):
        commands.append(argv)
        return json.dumps([{"RepoDigests": [image], "Id": "sha256:" + "c" * 64}])

    monkeypatch.setattr(installer, "command", command)
    assert managed_installer.Installer.image(installer, image) == "sha256:" + "c" * 64
    assert commands == [
        ("docker", "--config", str(login), "pull", image),
        ("docker", "image", "inspect", image),
    ]
    captured = []
    monkeypatch.setattr(
        managed_installer, "capture", lambda argv, **_: captured.append(argv) or b""
    )
    managed_installer.Installer.child(
        installer, 1001, 1001, "authority", policy.database, str(operation.operation_id)
    )
    assert captured and all(str(login) not in part for part in captured[0])
    assert app_fingerprint(policy, "notes") == app_fingerprint(before, "notes")
    assert registry_credentials(before) == ()
    os.chmod(login / "config.json", 0o640)
    with pytest.raises(ContractError, match="private to root"):
        managed_installer.Installer.image(installer, image)
    os.chmod(login / "config.json", 0o600)
    os.chmod(login, 0o750)
    with pytest.raises(ContractError, match="private to root"):
        managed_installer.Installer.image(installer, image)
    assert len(commands) == 2, "a refused login still reached docker"


def test_verified_release_is_narrowed_before_root_holds_it(helper):
    installer, _, _, app, release, _ = helper
    verified = installer.verified(app, release.archive, 1, release.archive_sha256)
    assert isinstance(verified, HostRelease)
    assert verified == release


def test_switch_markers_are_removed_once_the_operation_is_finished(helper):
    installer, operation, _, _, _, _ = helper
    name = str(operation.operation_id)
    spool = Path(installer.policy.spool)
    journal_marker = installer.journal / (name + ".switch.json")
    spool_marker = spool / "receipts" / (name + ".switch.json")
    hint = OperationHint(operation_id=operation.operation_id).model_dump_json().encode()
    publish(journal_marker, b"{}")
    publish(spool_marker, hint)
    publish(spool / "requests" / (name + ".json"), hint)
    installer.sweep_markers()
    assert spool_marker.exists(), "no receipt yet: the switch intent must stay"
    receipt = Receipt(
        operation_id=operation.operation_id,
        selection=operation.approval.selection,
        state="failed",
        error="installation_failed",
    )
    installer.commit(receipt)
    assert (installer.journal / (name + ".receipt.json")).exists()
    assert (spool / "receipts" / (name + ".json")).exists()
    assert not journal_marker.exists()
    installer.sweep_markers()
    assert spool_marker.exists(), "core has not consumed the receipt yet"
    (spool / "requests" / (name + ".json")).unlink()
    installer.sweep_markers()
    assert not spool_marker.exists()
    installer.commit(receipt)
    installer.sweep_markers()
    assert not journal_marker.exists() and not spool_marker.exists()
