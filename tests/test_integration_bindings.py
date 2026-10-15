import json
import os
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from test_app_deployments import ledger as ledger_fixture
from test_app_previews import install_without_docker
from test_app_registry import document, manifest, registration
from test_installer_recovery import installed
from test_managed_installer import helper as helper_fixture

from app.app_contracts import (
    ContractError,
    OperatorBindings,
    ReleaseManifest,
    Setting,
    parse_document,
)
from app.app_deployment_contracts import Receipt, fingerprint
from app.app_registry import AppCredential, ManagedApp
from app.app_runtime import runtime_spec
from app.config import InstanceFacts
from ops import installer_policy
from ops.installer_policy import HostPolicy, app_fingerprint
from ops.managed_installer import Installer

ledger = ledger_fixture
helper = helper_fixture

# app_fingerprint of policy_payload() computed at ecb3da3, before mounts existed.
HEAD_WISHLIST_FINGERPRINT = (
    "2726f0cdacb3ef4a790c32989062ef962b3541fca4fe69204c13dcc765d470e2"
)
LIBRARY_TARGET = "/integrations/library"
LIBRARY_ENV = "APP_INTEGRATION_LIBRARY_DIR=" + LIBRARY_TARGET


def app_entry(app_id: str, uid: int, port: int) -> dict[str, Any]:
    entry = registration(app_id, "/" + app_id)
    entry["source"]["repository"] = "example/" + app_id
    entry["source"]["image_repository"] = "ghcr.io/example/" + app_id
    entry["bindings"] = {
        "secret_files": [],
        "settings": [{"name": "WISHLIST_CURRENCY", "value": "SGD"}],
        "state_directory": f"/srv/apps/{app_id}/state",
        "backup_directory": f"/srv/backups/{app_id}",
    }
    entry["installation"] = {
        "owner": "platform",
        "host_port": port,
        "probe": {"service": app_id, "readiness": "v1-status"},
    }
    return {
        "registration": entry,
        "uid": uid,
        "gid": uid,
        "credential_file": f"/srv/credentials/{app_id}/credential",
        "compatibility_argv": ["python", "-m", app_id, "--check-state"],
        "state": {"sqlite_file": app_id + ".db"},
        "maintenance": [
            {
                "name": "refresh",
                "argv": ["python", "-m", app_id + ".refresh"],
                "interval_hours": 6,
                "timeout_seconds": 600,
            }
        ],
        "maintenance_schedules": [
            {"name": "refresh", "offset_minutes": 137, "jitter_seconds": 900}
        ],
        "backup_schedule": {
            "hour": 3,
            "minute": 41,
            "jitter_seconds": 600,
            "requires_mounts_for": ["/mnt/private backups"],
        },
    }


def policy_payload(*apps: dict[str, Any]) -> dict[str, Any]:
    return {
        "core_uid": 1001,
        "core_gid": 1001,
        "database": "/srv/core/core.sqlite",
        "spool": "/srv/spool",
        "journal": "/srv/journal",
        "scratch": "/srv/scratch",
        "units": "/etc/systemd/system",
        "python": "/usr/bin/python3",
        "environment": "production",
        "app_api_url": "http://host.docker.internal:8000/api/apps/v1",
        "timezone": "Asia/Singapore",
        "apps": list(apps) or [app_entry("wishlist", 1101, 8101)],
    }


def sorter(bindings=("library",), mounts=(("library", "/mnt/library"),)):
    entry = app_entry("sorter", 1103, 8103)
    entry["registration"]["bindings"]["integration_bindings"] = list(bindings)
    entry["mounts"] = [{"name": name, "source": source} for name, source in mounts]
    return entry


def parse(*apps):
    return HostPolicy.model_validate_json(json.dumps(policy_payload(*apps)))


def test_policy_mounts_exactly_the_registered_bindings():
    policy = parse(app_entry("wishlist", 1101, 8101), sorter())
    assert [mount.name for mount in policy.app("sorter").mounts] == ["library"]
    for refused in (
        sorter(mounts=(("photos", "/mnt/library"),)),
        sorter(mounts=()),
        sorter(bindings=(), mounts=(("library", "/mnt/library"),)),
        sorter(
            bindings=("library", "photos"),
            mounts=(("library", "/mnt/library"),),
        ),
    ):
        with pytest.raises(ValidationError, match="match the registered"):
            parse(refused)


def test_policy_refuses_duplicate_mount_names():
    entry = sorter(mounts=(("library", "/mnt/library"), ("library", "/mnt/other")))
    with pytest.raises(ValidationError, match="Duplicate entries"):
        parse(entry)


@pytest.mark.parametrize(
    "source",
    [
        "/srv/apps/sorter",
        "/srv/apps/sorter/state/library",
        "/srv/backups/wishlist",
        "/srv/journal/library",
        "/srv/credentials/sorter",
        "/srv/core",
        "/srv",
    ],
)
def test_policy_refuses_a_mount_source_that_aliases_or_nests_a_root(source):
    with pytest.raises(ValidationError, match="must not alias or nest"):
        parse(app_entry("wishlist", 1101, 8101), sorter(mounts=(("library", source),)))


def test_policy_refuses_a_mount_source_unsafe_for_a_unit_file():
    for source in ("/mnt/lib\trary", "/mnt/lib,rary", "relative/library"):
        with pytest.raises(ValidationError):
            parse(sorter(mounts=(("library", source),)))


def test_fingerprint_without_mounts_is_unchanged_and_mounts_participate():
    policy = parse()
    assert app_fingerprint(policy, "wishlist") == HEAD_WISHLIST_FINGERPRINT
    first = app_fingerprint(parse(sorter()), "sorter")
    moved = parse(sorter(mounts=(("library", "/mnt/other"),)))
    assert first != app_fingerprint(moved, "sorter")


# app_fingerprint and registration fingerprint of the entry below, computed at
# 12e963e. Accepted selections and journals store these, so they must not move.
UNORDERED_SORTER_FINGERPRINTS = (
    "813327d3fe68169f3767f67cb5f6e6148acb04a2c01135e0210a640d59e8db43",
    "446b56276d7d451f9be9b7686bb34cb931250de36d055451473624c8ce1a24e2",
)


def test_fingerprints_keep_the_stored_order_of_name_keyed_bindings():
    entry = sorter(
        bindings=("photos", "library"),
        mounts=(("photos", "/mnt/photos"), ("library", "/mnt/library")),
    )
    bindings = entry["registration"]["bindings"]
    bindings["settings"] = [
        {"name": "ZED", "value": "1"},
        {"name": "ALPHA", "value": "2"},
    ]
    bindings["secret_files"] = [
        {"name": "provider-key", "file_path": "/private/sorter-provider/key"},
        {"name": "aaa-extra", "file_path": "/private/sorter-extra/key"},
    ]
    policy = parse(entry)
    registration = policy.app("sorter").registration
    assert (
        app_fingerprint(policy, "sorter"),
        fingerprint(registration),
    ) == UNORDERED_SORTER_FINGERPRINTS


def test_a_manifest_cannot_request_an_integration_binding():
    payload = {**manifest(), "integration_bindings": ["library"]}
    with pytest.raises(ContractError, match="integration_bindings"):
        parse_document(ReleaseManifest, document(payload))


def test_settings_cannot_impersonate_an_integration_variable():
    with pytest.raises(ValidationError, match="Reserved setting name"):
        OperatorBindings(
            settings=(Setting(name="APP_INTEGRATION_LIBRARY_DIR", value="/x"),)
        )


def bind(helper, tmp_path, *, owner=None, library=True):
    installer, operation, authority, _, release, events = helper
    source = tmp_path / "library"
    if library:
        source.mkdir(exist_ok=True)
    archive = tmp_path / "archive"
    archive.mkdir(exist_ok=True)
    payload = installer.policy.model_dump(mode="json")
    entry = payload["apps"][0]
    entry["registration"]["bindings"]["integration_bindings"] = [
        "library",
        "photo-archive",
    ]
    entry["mounts"] = [
        {"name": "library", "source": str(source)},
        {"name": "photo-archive", "source": str(archive), "read_only": True},
    ]
    entry["uid"] = entry["gid"] = owner or os.getuid()
    policy = HostPolicy.model_validate_json(json.dumps(payload))
    installer.policy = policy
    installer.digest = fingerprint(policy)
    bound = policy.apps[0]
    return (installer, operation, authority, bound, release, events), source


def test_live_container_gets_the_mounts_and_rehearsal_gets_neither(
    helper, tmp_path, monkeypatch
):
    (installer, _, authority, app, release, _), source = bind(helper, tmp_path)
    commands = []
    monkeypatch.setattr(installer, "command", lambda *argv, **_: commands.append(argv))
    monkeypatch.setattr(
        installer, "credential_snapshot", lambda *_: tmp_path / "credential"
    )
    Installer.start(installer, authority, app, release, release.image)
    live = commands[0]
    assert f"type=bind,source={source},target={LIBRARY_TARGET}" in live
    assert (
        f"type=bind,source={tmp_path / 'archive'},"
        "target=/integrations/photo-archive,readonly"
    ) in live
    assert LIBRARY_ENV in live
    assert "APP_INTEGRATION_PHOTO_ARCHIVE_DIR=/integrations/photo-archive" in live
    Installer.start(
        installer, authority, app, release, release.image, rehearsal=Path("/r/state")
    )
    rehearsal = commands[1]
    assert rehearsal[rehearsal.index("--name") + 1] == "hp-rehearsal-notes"
    assert not [part for part in rehearsal if "integration" in part.lower()]
    assert str(source) not in " ".join(rehearsal)


def test_runtime_spec_refuses_bindings_without_their_mounts(helper, tmp_path):
    (_, operation, authority, app, release, _), _ = bind(helper, tmp_path)
    with pytest.raises(ContractError, match="differ from the registered"):
        runtime_spec(
            ManagedApp(app.registration, app.installation),
            AppCredential(
                operation.approval.selection.credential_id,
                "notes",
                "test",
                Path(app.credential_file),
            ),
            InstanceFacts("test", authority.app_api_url, "UTC"),
            release.port,
        )


def test_app_unit_requires_the_mount_sources(helper, tmp_path, monkeypatch):
    (installer, _, _, app, _, _), source = bind(helper, tmp_path)
    units = Path(installer.policy.units)
    units.mkdir()
    monkeypatch.setattr(installer, "command", lambda *a, **k: "")
    Installer.lifecycle(installer, app, True)
    unit = (units / "hp-managed-notes--app.service").read_text()
    head, service = unit.split("[Service]")
    assert f'RequiresMountsFor="{source}" "{tmp_path / "archive"}"\n' in head
    assert "RequiresMountsFor" not in service


def test_unbound_app_unit_is_unchanged(helper, monkeypatch):
    installer, _, _, app, _, _ = helper
    units = Path(installer.policy.units)
    units.mkdir()
    monkeypatch.setattr(installer, "command", lambda *a, **k: "")
    Installer.lifecycle(installer, app, True)
    assert (units / "hp-managed-notes--app.service").read_text() == (
        "# Managed app notes\n[Unit]\nAfter=docker.service\n"
        "Requires=docker.service\n[Service]\nType=simple\nRestart=on-failure\n"
        "RestartSec=5\nKillMode=mixed\nTimeoutStopSec=30\n"
        'SuccessExitStatus=75\nExecStart="/usr/bin/python3" -I -m '
        'ops.managed_installer --policy "/etc/home-platform/installer.json" '
        "--run notes\n[Install]\nWantedBy=multi-user.target\n"
    )


def owned_source(source):
    entry = sorter(mounts=(("library", str(source)),))
    entry["uid"] = os.getuid()
    return parse(entry).app("sorter")


def test_integration_sources_refuse_a_symlinked_source(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "library").symlink_to(real)
    installer_policy.integration_sources(owned_source(real))
    with pytest.raises(ContractError, match="owned by the app"):
        installer_policy.integration_sources(owned_source(tmp_path / "library"))


def test_integration_sources_refuse_a_symlinked_ancestor(tmp_path):
    parent = tmp_path / "parent"
    (parent / "real").mkdir(parents=True)
    (tmp_path / "anc").symlink_to(parent)
    installer_policy.integration_sources(owned_source(parent / "real"))
    with pytest.raises(ContractError, match="through a symlink"):
        installer_policy.integration_sources(owned_source(tmp_path / "anc" / "real"))


def boot_with(helper, monkeypatch):
    installer, _, _, _, _, events = helper
    installed(helper, monkeypatch)
    monkeypatch.setattr(installer, "start", partial(Installer.start, installer))
    monkeypatch.setattr(installer, "owned", lambda *_: None)
    monkeypatch.setattr(installer, "credential_snapshot", lambda *_: Path("/c"))
    installer.boot("notes")
    return [event for event in events if event[:2] == ("docker", "run")]


@pytest.mark.parametrize(
    "fault, message",
    [("foreign", "owned by the app"), ("missing", "source is missing")],
)
def test_boot_refuses_an_unowned_or_missing_mount_source(
    helper, tmp_path, monkeypatch, fault, message
):
    bound, _ = bind(
        helper,
        tmp_path,
        owner=os.getuid() + 1 if fault == "foreign" else None,
        library=fault != "missing",
    )
    with pytest.raises(ContractError, match=message):
        boot_with(bound, monkeypatch)
    assert not [e for e in bound[-1] if e[:2] == ("docker", "run")]


def test_boot_starts_with_an_owned_mount_source(helper, tmp_path, monkeypatch):
    bound, source = bind(helper, tmp_path)
    (run,) = boot_with(bound, monkeypatch)
    assert f"type=bind,source={source},target={LIBRARY_TARGET}" in run


def test_validate_paths_never_touches_mount_sources(helper, tmp_path, monkeypatch):
    (installer, *_), _ = bind(helper, tmp_path, library=False)
    touched = []
    monkeypatch.setattr(
        installer_policy, "root_path", lambda path, **_: touched.append(str(path))
    )
    monkeypatch.setattr(installer_policy, "read_owned", lambda *_, **__: b"")
    monkeypatch.setattr(installer_policy.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        installer_policy,
        "sys",
        SimpleNamespace(flags=SimpleNamespace(isolated=1), path=[], modules={}),
    )
    backups = Path(str(installer.policy.apps[0].registration.bindings.backup_directory))
    backups.mkdir(mode=0o700)
    installer_policy.validate_paths(installer.policy)
    assert touched
    assert not [path for path in touched if "library" in path or "archive" in path]


def test_deploy_refuses_a_missing_source_before_the_switch(
    helper, tmp_path, monkeypatch
):
    bound, _ = bind(helper, tmp_path, library=False)
    installer, operation, authority, app, release, _ = bound
    deploy = operation.model_copy(update={"kind": "deploy"})
    deployed = authority.model_copy(update={"operation": deploy})
    monkeypatch.setattr(installer, "authority", lambda *_: (deployed, app, release))
    monkeypatch.setattr(
        installer, "child", lambda *_: deploy.model_dump_json().encode()
    )
    monkeypatch.setattr(
        installer, "admitted", lambda *_: pytest.fail("switch began without a mount")
    )
    installer.execute(deploy.operation_id)
    receipt = Receipt.model_validate_json(
        (installer.journal / f"{deploy.operation_id}.receipt.json").read_bytes()
    )
    assert (receipt.state, receipt.error) == ("failed", "installation_failed")


def app_run(installed_preview):
    return next(
        command for command in installed_preview.commands if "APP_ID=notes" in command
    )


def test_preview_copies_the_fixture_into_each_attempt(tmp_path, monkeypatch):
    fixture = tmp_path / "fixture-library"
    (fixture / "dump").mkdir(parents=True)
    (fixture / "dump" / "a.txt").write_text("original")
    first = install_without_docker(
        tmp_path, monkeypatch, integrations={"library": str(fixture)}
    )
    second = install_without_docker(
        tmp_path, monkeypatch, integrations={"library": str(fixture)}
    )
    copies = []
    for attempt in (first, second):
        copy = attempt.runtime.root / str(attempt.request.attempt_id) / "integrations"
        copy = copy / "library"
        command = app_run(attempt)
        assert f"type=bind,source={copy},target={LIBRARY_TARGET}" in command
        assert LIBRARY_ENV in command
        assert str(fixture) not in " ".join(command)
        copies.append(copy)
        attempt.database.close()
    (copies[0] / "dump" / "a.txt").write_text("sorted by the first attempt")
    assert (copies[1] / "dump" / "a.txt").read_text() == "original"
    assert (fixture / "dump" / "a.txt").read_text() == "original"


def test_preview_refuses_a_missing_fixture_directory(tmp_path, monkeypatch):
    with pytest.raises(ContractError, match="integration directory is missing"):
        install_without_docker(
            tmp_path, monkeypatch, integrations={"library": str(tmp_path / "absent")}
        )
    assert list((tmp_path / "guest").iterdir()) == []
