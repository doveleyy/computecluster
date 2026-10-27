import base64
import http.server
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_app_deployments import complete_rehearsal, push, request
from test_app_platform import managed
from test_app_registry import document, registration
from test_managed_installer import helper as helper_fixture
from test_managed_installer import installed_by_root
from test_managed_installer import ledger as ledger_fixture

from app.accounts import AccountStore
from app.app_contracts import ContractError, Maintenance
from app.app_deployment_contracts import (
    KINDS,
    InstalledSnapshot,
    InstallerCapabilities,
    OperationHint,
    Receipt,
    RetiredSnapshot,
    fingerprint,
)
from app.app_deployments import (
    ApplicationRetired,
    ReconciliationPending,
    accepted,
    eligible,
    pending_change,
    read_authority,
    read_installed_authority,
    registration_for,
    retire_blocker,
    retirement,
    update_registration,
)
from app.app_deployments import installed as accepted_install
from app.app_gateway import AppGateway
from app.app_identity import AppIdentity, Refusal
from app.app_previews import CHECKS, PreviewResult
from app.app_publication import sha256
from app.app_registry import AppCredential, ManagedApp
from app.app_runtime import docker_run_argv, runtime_spec
from app.app_spool import publish, read_owned
from app.config import AppPlatformSettings, InstanceFacts
from app.identity import ADMIN_USER_ID
from ops import managed_installer
from ops.installer_policy import HostPolicy, app_fingerprint, maintenance_calendar
from ops.managed_installer import (
    Container,
    Installer,
    application_ready,
    capture,
    response_json,
)

helper = helper_fixture
ledger = ledger_fixture


def switch_request(ledger, operation):
    publish(
        ledger.spool / "receipts" / f"{operation.operation_id}.switch.json",
        OperationHint(operation_id=operation.operation_id).model_dump_json().encode(),
    )
    ledger.service.reconcile()


def deploy_request(ledger):
    rehearsal = request(ledger)
    complete_rehearsal(ledger, rehearsal)
    return request(ledger, "deploy", rehearsal_id=rehearsal.operation_id)


def install(ledger):
    operation = deploy_request(ledger)
    with ledger.database.connect() as connection:
        authority = read_authority(connection, operation.operation_id, "a" * 64)
    switch_request(ledger, operation)
    with ledger.database.connect() as connection:
        release = eligible(connection, authority.registration, ledger.digest)
    snapshot = InstalledSnapshot(
        operation_id=operation.operation_id,
        generation=1,
        selection=operation.approval.selection,
        registration=authority.registration,
        image=release.manifest.image,
        image_id="sha256:" + "b" * 64,
    )
    receipt = Receipt(
        operation_id=operation.operation_id,
        selection=operation.approval.selection,
        state="succeeded",
        installed=snapshot,
        authorization_committed=True,
    )
    publish(
        ledger.spool / "receipts" / f"{operation.operation_id}.json",
        receipt.model_dump_json().encode(),
    )
    ledger.service.reconcile()
    return snapshot


def gateway(ledger):
    application = FastAPI()
    application.add_middleware(AppGateway)
    application.state.app_registry = ledger.registry
    application.state.app_identity = SimpleNamespace(
        admit=lambda *_: Refusal(403, "Fixture denial")
    )
    return TestClient(application)


def test_switching_closes_gateway_only_after_core_acknowledgement(ledger):
    operation = deploy_request(ledger)
    with gateway(ledger) as client:
        assert client.get("/apps/notes/").status_code == 403
        switch_request(ledger, operation)
        assert client.get("/apps/notes/").status_code == 503
    with ledger.database.connect() as connection:
        assert read_authority(connection, operation.operation_id, "a" * 64)


def test_restart_with_revoked_authority_keeps_gateway_blocked(
    helper, ledger, monkeypatch
):
    installer, operation, _authority, _app, _release, events = helper
    operation = operation.model_copy(update={"kind": "deploy"})
    with ledger.database.connect() as connection:
        connection.execute(
            "UPDATE app_operations SET kind = 'deploy', operation_json = ?",
            (operation.model_dump_json(),),
        )
        connection.execute("UPDATE users SET session_version = 2 WHERE role = 'ADMIN'")
    switch_request(ledger, operation)
    publish(installer.journal / f"{operation.operation_id}.switch.json", b"{}")

    def refuse(*_):
        with ledger.database.connect() as connection:
            read_authority(connection, operation.operation_id, "a" * 64)
        pytest.fail("Revoked authority must be refused")

    monkeypatch.setattr(installer, "authority", refuse)
    monkeypatch.setattr(
        installer, "child", lambda *_: operation.model_dump_json().encode()
    )
    installer.execute(operation.operation_id)
    ledger.service.reconcile()
    assert events == []
    with gateway(ledger) as client:
        assert client.get("/apps/notes/").status_code == 503
    with ledger.database.connect() as connection:
        assert (
            connection.execute("SELECT state FROM app_operations").fetchone()[0]
            == "blocked"
        )


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE users SET session_version = 2 WHERE role = 'ADMIN'",
        "UPDATE app_credentials SET revoked_at = CURRENT_TIMESTAMP",
    ],
)
def test_completed_effect_is_consumed_after_revocation(ledger, change):
    operation = deploy_request(ledger)
    with ledger.database.connect() as connection:
        authority = read_authority(connection, operation.operation_id, "a" * 64)
    switch_request(ledger, operation)
    from app.app_deployments import eligible

    with ledger.database.connect() as connection:
        release = eligible(connection, authority.registration, ledger.digest)
        connection.execute(change)
    snapshot = InstalledSnapshot(
        operation_id=operation.operation_id,
        generation=1,
        selection=operation.approval.selection,
        registration=authority.registration,
        image=release.manifest.image,
        image_id="sha256:" + "b" * 64,
    )
    receipt = Receipt(
        operation_id=operation.operation_id,
        selection=operation.approval.selection,
        state="succeeded",
        installed=snapshot,
        authorization_committed=True,
    )
    publish(
        ledger.spool / "receipts" / f"{operation.operation_id}.json",
        receipt.model_dump_json().encode(),
    )
    ledger.service.reconcile()
    ledger.service.reconcile()
    with ledger.database.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM app_installed_history").fetchone()[
                0
            ]
            == 1
        )
        assert (
            connection.execute(
                "SELECT state FROM app_operations WHERE kind='deploy'"
            ).fetchone()[0]
            == "succeeded"
        )
        if "credentials" in change:
            with pytest.raises(ContractError, match="credential"):
                read_installed_authority(connection, "notes")
            assert ledger.registry.active_credential("notes") is None
        else:
            assert read_installed_authority(connection, "notes").previous == snapshot


@pytest.mark.parametrize("boundary", ["file-sync", "rename", "directory-sync"])
def test_spool_process_exit_at_commit_boundaries_is_replayable(tmp_path, boundary):
    target = tmp_path / "record.json"
    script = """
import os, stat, sys
from pathlib import Path
from app.app_spool import publish
boundary = sys.argv[2]
fsync, rename = os.fsync, os.rename
def sync(fd):
    fsync(fd)
    kind = 'directory-sync' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file-sync'
    if kind == boundary:
        os._exit(9)
def commit(*args):
    rename(*args)
    if boundary == 'rename':
        os._exit(9)
os.fsync, os.rename = sync, commit
publish(Path(sys.argv[1]), b'complete')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(target), boundary], check=False
    )
    assert result.returncode == 9
    if target.exists():
        assert target.stat().st_nlink == 1
        assert read_owned(target, os.geteuid()) == b"complete"
    publish(target, b"complete")
    assert read_owned(target, os.geteuid()) == b"complete"
    with pytest.raises(ContractError, match="conflicts"):
        publish(target, b"different")


@pytest.mark.parametrize("stream", [1, 2])
def test_subprocess_output_is_terminated_while_streaming(stream):
    script = f"import os\nwhile True: os.write({stream}, b'x' * 65536)"
    with pytest.raises(ContractError, match="output exceeds"):
        capture((sys.executable, "-c", script), timeout=3, limit=65536)


def test_readiness_response_stops_reading_at_limit():
    class Body(httpx.SyncByteStream):
        consumed = 0

        def __iter__(self):
            for _ in range(100):
                self.consumed += 1
                yield b"x" * 4096

    body = Body()
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))
        ) as client,
        pytest.raises(ContractError, match="response exceeds"),
    ):
        response_json(client, "http://fixture/ready")
    assert body.consumed == 17


def probe_against(helper, monkeypatch, status, body, headers=None, service="notes"):
    installer, _, authority, app, release, _ = helper
    resources = app.registration.bindings.resources
    running = Container.model_validate(
        dict(
            Image="sha256:" + "b" * 64,
            Config=dict(Labels={}),
            HostConfig=dict(
                Memory=resources.memory_bytes,
                MemorySwap=resources.memory_bytes,
                PidsLimit=resources.pids_limit,
                NanoCpus=int(resources.cpu_limit * 1_000_000_000),
            ),
            State=dict(Running=True),
        )
    )

    def handler(request):
        if request.url.path == release.ready_path:
            return httpx.Response(
                status, stream=httpx.ByteStream(body), headers=headers
            )
        if request.url.path == release.version_path:
            version = json.dumps({"service": service, "version": "1"}).encode()
            return httpx.Response(200, stream=httpx.ByteStream(version))
        return httpx.Response(401)

    client = httpx.Client
    monkeypatch.setattr(installer, "owned", lambda *_: running)
    monkeypatch.setattr(managed_installer.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        managed_installer.httpx,
        "Client",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )
    installer.probe(authority, app, release, "sha256:" + "b" * 64)


@pytest.mark.parametrize(
    "body",
    [b"ok", b"", b'{"status": "starting"}', b'{"database": "connected"}'],
)
def test_v1_status_probe_accepts_any_body_with_200(helper, monkeypatch, body):
    assert helper[3].installation.probe.readiness == "v1-status"
    probe_against(helper, monkeypatch, 200, body)


@pytest.mark.parametrize(
    ("status", "body", "headers"),
    [
        (503, b'{"status": "ready"}', None),
        (200, b"ok", {"Content-Encoding": "gzip"}),
        (200, b"x" * 65537, None),
    ],
)
def test_v1_status_probe_refuses_non_200_encoded_or_oversized(
    helper, monkeypatch, status, body, headers
):
    with pytest.raises(ContractError, match="readiness failed"):
        probe_against(helper, monkeypatch, status, body, headers)


def test_v1_status_probe_still_requires_the_version_service(helper, monkeypatch):
    with pytest.raises(ContractError, match="readiness failed"):
        probe_against(helper, monkeypatch, 200, b"ok", service="other")


@pytest.mark.parametrize(
    ("readiness", "body", "ready"),
    [
        ("v1-status", b"starting up", True),
        ("v1-status", b'{"status": "starting"}', True),
        ("legacy-json", b'{"status": "ready"}', True),
        ("legacy-json", b'{"status": "starting"}', False),
    ],
)
def test_readiness_mode_decides_whether_the_body_is_parsed(readiness, body, ready):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=httpx.ByteStream(body))
        )
    ) as client:
        assert application_ready(client, "http://fixture/ready", readiness) is ready


def test_legacy_json_readiness_refuses_a_plain_text_body():
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=httpx.ByteStream(b"ok"))
            )
        ) as client,
        pytest.raises(ValueError),
    ):
        application_ready(client, "http://fixture/ready", "legacy-json")


def serve_ready(status, body):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize(
    ("readiness", "status", "body", "passes"),
    [
        ("v1-status", 200, b"ok", True),
        ("v1-status", 200, b'{"status": "starting"}', True),
        ("v1-status", 503, b'{"status": "ready"}', False),
        ("v1-status", 200, b"x" * 65537, False),
        ("legacy-json", 200, b'{"status": "ready"}', True),
        ("legacy-json", 200, b'{"status": "starting"}', False),
        ("legacy-json", 200, b"ok", False),
    ],
)
def test_rehearsal_probe_honours_the_policy_readiness_mode(
    helper, monkeypatch, readiness, status, body, passes
):
    installer, _, _, app, release, _ = helper
    probe = app.installation.probe.model_copy(update={"readiness": readiness})
    registration = app.registration.model_copy(
        update={"installation": app.installation.model_copy(update={"probe": probe})}
    )
    app = app.model_copy(update={"registration": registration})
    server = serve_ready(status, body)
    release = SimpleNamespace(port=server.server_port, ready_path="/ready")
    monkeypatch.setattr(
        installer,
        "owned",
        lambda *_: Container.model_validate(
            dict(
                Image="sha256:" + "b" * 64,
                Config=dict(Labels={}),
                HostConfig=dict(Memory=1, MemorySwap=1, PidsLimit=1, NanoCpus=1),
                State=dict(Running=True, Pid=7),
            )
        ),
    )
    monkeypatch.setattr(managed_installer.time, "sleep", lambda _: None)
    outcome = []

    def command(*argv, **_):
        if not outcome:
            local = argv[argv.index("--") + 1 :]
            try:
                capture((sys.executable, *local[1:]), timeout=5, limit=65536)
                outcome.append(None)
            except ContractError as error:
                outcome.append(error)
        if outcome[0] is not None:
            raise outcome[0]
        return ""

    monkeypatch.setattr(installer, "command", command)
    try:
        if passes:
            installer.rehearsal_ready("hp-rehearsal-notes", app, release)
        else:
            with pytest.raises(ContractError, match="Rehearsal readiness failed"):
                installer.rehearsal_ready("hp-rehearsal-notes", app, release)
    finally:
        server.shutdown()


def test_policy_replacement_is_detected_by_same_helper(helper, monkeypatch):
    installer, *_ = helper
    current = installer.policy
    monkeypatch.setattr(managed_installer, "load_policy", lambda _: current)
    installer.current_policy()
    current = current.model_copy(update={"timezone": "Europe/London"})
    with pytest.raises(ContractError, match="policy changed"):
        installer.current_policy()


def test_lifecycle_names_do_not_overlap_and_recovery_removes_candidate_timers(
    helper, monkeypatch
):
    installer, _, _, app, _, _ = helper
    units = Path(installer.policy.units)
    units.mkdir()
    enabled = set()
    fail = None

    def command(*args, **kwargs):
        nonlocal fail
        if args[1] == "enable":
            if args[-1] == fail:
                fail = None
                raise ContractError("Injected second timer failure")
            enabled.add(args[-1])
        elif args[1] == "disable":
            enabled.discard(args[-1])
        return ""

    monkeypatch.setattr(installer, "command", command)
    other = app.model_copy(
        update={
            "registration": app.registration.model_copy(
                update={"app_id": "notes-backup"}
            )
        }
    )
    Installer.lifecycle(installer, other, True)
    Installer.lifecycle(installer, app, True)
    before = set(enabled)
    candidate = app.model_copy(
        update={
            "maintenance": (
                Maintenance(
                    name="backup",
                    argv=("check",),
                    interval_hours=1,
                    timeout_seconds=5,
                ),
                Maintenance(
                    name="second",
                    argv=("check",),
                    interval_hours=1,
                    timeout_seconds=5,
                ),
            )
        }
    )
    fail = "hp-managed-notes--maintenance-second.timer"
    with pytest.raises(ContractError):
        Installer.lifecycle(installer, candidate, True)
    assert "hp-managed-notes--maintenance-backup.timer" in enabled
    Installer.lifecycle(installer, app, True)
    assert enabled == before
    assert not (units / "hp-managed-notes--maintenance-backup.timer").exists()
    assert (units / "hp-managed-notes-backup--app.service").exists()
    assert "--run notes" in (units / "hp-managed-notes--app.service").read_text()
    assert "docker start" not in (units / "hp-managed-notes--app.service").read_text()


def test_maintenance_timer_is_a_persistent_calendar_timer(helper, monkeypatch):
    installer, _, _, app, _, _ = helper
    units = Path(installer.policy.units)
    units.mkdir()
    monkeypatch.setattr(installer, "command", lambda *args, **kwargs: "")
    Installer.lifecycle(installer, app, True)
    timer = (units / "hp-managed-notes--maintenance-cleanup.timer").read_text()
    assert timer == (
        "# Managed app notes\n[Timer]\nOnCalendar=*-*-* 02/6:04:00\nPersistent=true\n"
        "[Install]\nWantedBy=timers.target\n"
    ), "changing the offset derivation reschedules every installed task"


@pytest.mark.parametrize(
    ("hours", "pattern"),
    [
        (1, r"\*-\*-\* 00/1:[0-5]\d:00"),
        (3, r"\*-\*-\* 0[0-2]/3:[0-5]\d:00"),
        (6, r"\*-\*-\* 0[0-5]/6:[0-5]\d:00"),
        (12, r"\*-\*-\* (0\d|1[01])/12:[0-5]\d:00"),
        (24, r"\*-\*-\* ([01]\d|2[0-3]):[0-5]\d:00"),
        (168, r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \*-\*-\* ([01]\d|2[0-3]):[0-5]\d:00"),
    ],
)
def test_maintenance_calendar_steps_hours_within_a_day_and_days_within_a_week(
    hours, pattern
):
    item = Maintenance(
        name="cleanup", argv=("x",), interval_hours=hours, timeout_seconds=5
    )
    assert re.fullmatch(pattern, maintenance_calendar("notes", item))


def test_maintenance_offset_is_stable_across_processes_and_separates_tasks():
    item = Maintenance(name="cleanup", argv=("x",), interval_hours=6, timeout_seconds=5)
    other = item.model_copy(update={"name": "second"})
    script = (
        "from app.app_contracts import Maintenance\n"
        "from ops.installer_policy import maintenance_calendar\n"
        "print(maintenance_calendar('notes', Maintenance(name='cleanup', argv=('x',),"
        " interval_hours=6, timeout_seconds=5)), end='')"
    )
    printed = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    ).stdout
    assert maintenance_calendar("notes", item) == printed
    assert maintenance_calendar("notes", item) != maintenance_calendar("notes", other)
    assert maintenance_calendar("notes", item) != maintenance_calendar("other", item)


def test_policy_refuses_a_maintenance_interval_without_a_calendar_form(helper):
    installer, *_ = helper
    payload = installer.policy.model_dump(mode="json")
    payload["apps"][0]["maintenance"][0]["interval_hours"] = 5
    with pytest.raises(ValidationError, match="one of 1, 2, 3, 4, 6, 8, 12, 24, 168"):
        HostPolicy.model_validate_json(json.dumps(payload))


def installed(helper, monkeypatch, policy_sha256=None):
    installer, operation, authority, app, release, events = helper
    selection = operation.approval.selection.model_copy(
        update={
            "policy_sha256": policy_sha256
            or app_fingerprint(installer.policy, "notes"),
            "registration_sha256": fingerprint(app.registration),
        }
    )
    current = InstalledSnapshot(
        operation_id=operation.operation_id,
        generation=1,
        selection=selection,
        registration=app.registration,
        image=release.image,
        image_id="sha256:" + "b" * 64,
    )
    authority = authority.model_copy(
        update={
            "registration": app.registration,
            "previous": current,
            "credential_path": app.credential_file,
        }
    )
    installed_by_root(installer, current)
    publish(installer.journal / (release.archive_sha256 + ".zip"), release.archive)
    monkeypatch.setattr(
        installer, "child", lambda *_: authority.model_dump_json().encode()
    )
    monkeypatch.setattr(managed_installer, "load_policy", lambda _: installer.policy)
    monkeypatch.setattr(installer, "credential_snapshot", lambda *_: None)
    monkeypatch.setattr(installer, "limits", lambda: None)
    monkeypatch.setattr(installer, "probe", lambda *_: events.append("probe"))
    monkeypatch.setattr(installer, "remove", lambda *_: events.append("remove"))
    monkeypatch.setattr(
        installer, "start", lambda *args, **kwargs: events.append("start")
    )
    monkeypatch.setattr(
        installer, "command", lambda *args, **kwargs: events.append(args)
    )
    return current


def container(current, image=None, operation=None, running=True):
    return Container.model_validate(
        dict(
            Image=image or current.image_id,
            Config=dict(
                Labels={
                    "home-platform.operation": operation or str(current.operation_id)
                }
            ),
            HostConfig=dict(Memory=0, MemorySwap=0, PidsLimit=0, NanoCpus=0),
            State=dict(Running=running),
        )
    )


@pytest.mark.parametrize(
    "mismatch", [None, "image", "operation", "credential", "limits", "ready"]
)
def test_boot_reconciles_accepted_generation_before_restart(
    helper, monkeypatch, mismatch
):
    installer, _, _, _, _, events = helper
    current = installed(helper, monkeypatch)
    actual = container(
        current,
        image="wrong" if mismatch == "image" else None,
        operation="wrong" if mismatch == "operation" else None,
    )
    monkeypatch.setattr(installer, "owned", lambda *_: actual)

    def credential(*_):
        if mismatch == "credential":
            raise ContractError("Credential changed")

    def limits():
        if mismatch == "limits":
            raise ContractError("Missing memory enforcement")

    def probe(*_):
        if mismatch == "ready":
            raise ContractError("Readiness failed")
        events.append("probe")

    monkeypatch.setattr(installer, "credential_snapshot", credential)
    monkeypatch.setattr(installer, "limits", limits)
    monkeypatch.setattr(installer, "probe", probe)
    if mismatch:
        with pytest.raises(ContractError):
            installer.boot("notes")
        if mismatch == "ready":
            assert events[-1] == "remove"
        else:
            assert "start" not in events
            assert ("docker", "stop", "hp-app-notes") not in events
    else:
        installer.boot("notes")
        assert events == ["probe"]


def test_boot_adopts_exact_running_container_without_second_start(helper, monkeypatch):
    installer, _, _, _, _, events = helper
    current = installed(helper, monkeypatch)
    running = container(current)
    monkeypatch.setattr(installer, "owned", lambda *_: running)
    installer.boot("notes")
    assert events == ["probe"]
    stopped = container(current, running=False)
    monkeypatch.setattr(installer, "owned", lambda *_: stopped)
    installer.boot("notes")
    assert events[1:] == [
        ("docker", "stop", "hp-app-notes"),
        "remove",
        "start",
        "probe",
    ]


@pytest.mark.parametrize("present", [True, False])
def test_boot_confirms_local_image_before_stopping_and_pulls_only_when_absent(
    helper, monkeypatch, present
):
    installer, _, _, _, release, events = helper
    current = installed(helper, monkeypatch)
    monkeypatch.delattr(installer, "image")
    monkeypatch.setattr(
        installer, "owned", lambda *_: container(current, running=False)
    )
    inspected = {"Id": current.image_id, "RepoDigests": [release.image]}

    def command(*args, **kwargs):
        events.append(args)
        if args[1:3] == ("image", "inspect"):
            if present or ("docker", "pull", args[3]) in events:
                return json.dumps([inspected])
            raise ContractError("No such image")
        return ""

    monkeypatch.setattr(installer, "command", command)
    installer.boot("notes")
    pulls = [item for item in events if item[:2] == ("docker", "pull")]
    assert len(pulls) == (0 if present else 1)
    stop = events.index(("docker", "stop", "hp-app-notes"))
    inspect = events.index(("docker", "image", "inspect", release.image))
    assert inspect < stop
    assert events[-3:] == ["remove", "start", "probe"]


def test_boot_refuses_only_when_the_registry_is_needed_and_unreachable(
    helper, monkeypatch
):
    installer, _, _, _, _, events = helper
    current = installed(helper, monkeypatch)
    monkeypatch.delattr(installer, "image")
    monkeypatch.setattr(
        installer, "owned", lambda *_: container(current, running=False)
    )

    def command(*args, **kwargs):
        events.append(args)
        raise ContractError("Host operation failed")

    monkeypatch.setattr(installer, "command", command)
    with pytest.raises(ContractError):
        installer.boot("notes")
    assert ("docker", "stop", "hp-app-notes") not in events
    assert "remove" not in events


def test_boot_survives_policy_edits_outside_the_app(helper, tmp_path, monkeypatch):
    installer, _, _, app, _, events = helper
    original = installer.policy
    other = app.registration.model_dump(mode="json")
    other.update(app_id="other", route="/apps/other")
    other["installation"]["host_port"] += 1
    other["installation"]["probe"]["service"] = "other"
    other["bindings"].update(
        state_directory=str(tmp_path / "other-state"),
        backup_directory=str(tmp_path / "other-backups"),
    )
    edited = HostPolicy.model_validate_json(
        json.dumps(
            {
                **original.model_dump(mode="json"),
                "scratch": str(tmp_path / "other-scratch"),
                "apps": [
                    *original.model_dump(mode="json")["apps"],
                    dict(
                        registration=other,
                        uid=1003,
                        gid=1003,
                        credential_file=str(tmp_path / "other-credentials/other"),
                        compatibility_argv=["check"],
                    ),
                ],
            }
        )
    )
    assert fingerprint(edited) != fingerprint(original)
    installer.policy = edited
    installer.digest = fingerprint(edited)
    current = installed(helper, monkeypatch, app_fingerprint(original, "notes"))
    monkeypatch.setattr(installer, "owned", lambda *_: container(current))
    installer.boot("notes")
    assert events == ["probe"]
    affecting = edited.model_dump(mode="json")
    affecting["apps"][0]["uid"] = 1004
    installer.policy = HostPolicy.model_validate_json(json.dumps(affecting))
    installer.digest = fingerprint(installer.policy)
    with pytest.raises(ContractError, match="differs from host policy"):
        installer.boot("notes")
    assert events == ["probe"]


def test_policy_change_during_rehearsal_prevents_switch(helper, monkeypatch):
    installer, operation, authority, app, release, events = helper
    operation = operation.model_copy(update={"kind": "deploy"})
    authority = authority.model_copy(update={"operation": operation})
    monkeypatch.setattr(
        installer,
        "child",
        lambda *args: b"switching"
        if args[2] == "state"
        else operation.model_dump_json().encode(),
    )
    current = installer.policy
    monkeypatch.setattr(managed_installer, "load_policy", lambda _: current)

    def check(*_):
        installer.current_policy()
        return authority, app, release

    rehearsal = installer.rehearse

    def prepare(*args):
        nonlocal current
        result = rehearsal(*args)
        current = current.model_copy(update={"timezone": "Europe/London"})
        return result

    monkeypatch.setattr(installer, "authority", check)
    monkeypatch.setattr(installer, "rehearse", prepare)
    monkeypatch.setattr(
        installer, "stop_timers", lambda *_: pytest.fail("Must not stop old code")
    )
    installer.execute(operation.operation_id)
    receipt = Receipt.model_validate_json(
        (installer.journal / f"{operation.operation_id}.receipt.json").read_bytes()
    )
    assert receipt.state == "failed"
    assert events == ["limits"]


def test_managed_restart_has_one_owner_without_changing_legacy_runtime(
    helper, monkeypatch
):
    installer, operation, authority, app, release, _ = helper
    spec = runtime_spec(
        ManagedApp(app.registration, app.registration.installation),
        AppCredential(
            operation.approval.selection.credential_id,
            "notes",
            "test",
            Path(app.credential_file),
        ),
        InstanceFacts("test", authority.app_api_url, "UTC"),
        release.port,
    )
    legacy = docker_run_argv(spec, release.image, (app.uid, app.gid))
    assert legacy[legacy.index("--restart") + 1] == "unless-stopped"
    monkeypatch.setattr(installer, "spec", lambda *_: spec)
    commands = []
    monkeypatch.setattr(
        installer, "command", lambda *args, **kwargs: commands.append(args)
    )
    installer.start(authority, app, release, release.image)
    assert commands[0][commands[0].index("--restart") + 1] == "no"
    installer.start(
        authority,
        app,
        release,
        release.image,
        rehearsal=Path("/fixture/state"),
    )
    assert commands[1][commands[1].index("--name") + 1] == "hp-rehearsal-notes"


def test_helper_waits_for_durable_switch_ack_before_host_effects(helper, monkeypatch):
    installer, operation, authority, app, release, events = helper
    operation = operation.model_copy(update={"kind": "deploy"})
    authority = authority.model_copy(update={"operation": operation})
    monkeypatch.setattr(installer, "authority", lambda *_: (authority, app, release))
    monkeypatch.setattr(
        installer,
        "child",
        lambda *args: b"queued"
        if args[2] == "state"
        else operation.model_dump_json().encode(),
    )
    monkeypatch.setattr(managed_installer.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        installer,
        "owned",
        lambda *_: pytest.fail("No effects before core acknowledges switch"),
    )
    installer.execute(operation.operation_id)
    assert events == ["limits"]
    assert not (installer.journal / f"{operation.operation_id}.switch.json").exists()
    assert not (installer.journal / f"{operation.operation_id}.receipt.json").exists()


@pytest.mark.parametrize("adopt", [True, False])
def test_blocked_first_install_recovers_through_an_authorized_operation(
    helper, ledger, monkeypatch, adopt
):
    installer, rehearsal, _, app, release, events = helper
    complete_rehearsal(ledger, rehearsal)
    deploy = request(ledger, "deploy", rehearsal_id=rehearsal.operation_id)
    switch_request(ledger, deploy)
    blocked = Receipt(
        operation_id=deploy.operation_id,
        selection=deploy.approval.selection,
        state="blocked",
        error="recovery_required",
    )
    for path in (
        installer.journal / f"{deploy.operation_id}.receipt.json",
        ledger.spool / "receipts" / f"{deploy.operation_id}.json",
    ):
        publish(path, blocked.model_dump_json().encode())
    ledger.service.reconcile()
    with gateway(ledger) as client:
        assert client.get("/apps/notes/").status_code == 503
    with pytest.raises(ContractError, match="authority"):
        request(ledger, "recover", preview_id=None, session_version=2)
    with pytest.raises(ContractError, match="pending"):
        request(ledger, "deploy", rehearsal_id=deploy.approval.rehearsal_id)
    with pytest.raises(ContractError, match="blocked"):
        request(ledger, "recover", preview_id=None, digest="0" * 64)
    recover = request(ledger, "recover", preview_id=None)
    assert recover.recovers == deploy.operation_id
    with gateway(ledger) as client:
        assert client.get("/apps/notes/").status_code == 503
    with ledger.database.connect() as connection:
        authority = read_authority(connection, recover.operation_id, "a" * 64)
    app = app.model_copy(update={"registration": authority.registration})
    monkeypatch.setattr(installer, "authority", lambda *_: (authority, app, release))
    monkeypatch.setattr(
        installer,
        "child",
        lambda *args: b"switching"
        if args[2] == "state"
        else recover.model_dump_json().encode(),
    )
    candidate = Container.model_validate(
        dict(
            Image="sha256:" + "b" * 64,
            Config=dict(Labels={"home-platform.operation": str(deploy.operation_id)}),
            HostConfig=dict(Memory=1, MemorySwap=1, PidsLimit=1, NanoCpus=1),
            State=dict(Running=adopt),
        )
    )
    monkeypatch.setattr(installer, "owned", lambda *_: candidate)
    monkeypatch.setattr(installer, "probe", lambda *_: events.append("probe"))
    monkeypatch.setattr(installer, "remove", lambda *_: events.append("remove"))
    monkeypatch.setattr(installer, "stop_timers", lambda *_: events.append("timers"))
    monkeypatch.setattr(
        installer, "start", lambda *_, **__: pytest.fail("nothing to restore")
    )
    installer.execute(recover.operation_id)
    ledger.service.reconcile()
    with ledger.database.connect() as connection:
        states = dict(
            connection.execute("SELECT kind, state FROM app_operations").fetchall()
        )
        assert states["recover"] == "succeeded"
        if adopt:
            assert states["deploy"] == "succeeded"
            assert events == ["limits", "probe", "lifecycle"]
            installed = read_installed_authority(connection, "notes").previous
            assert installed is not None
            assert installed.operation_id == deploy.operation_id
            assert installed.generation == 1
        else:
            assert states["deploy"] == "failed"
            assert events == ["limits", "timers", "remove", "lifecycle"]
            with pytest.raises(ContractError, match="no accepted"):
                read_installed_authority(connection, "notes")
    with gateway(ledger) as client:
        assert client.get("/apps/notes/").status_code == 403
    if not adopt:
        request(ledger, "deploy", rehearsal_id=deploy.approval.rehearsal_id)


def test_failed_recovery_keeps_the_block_and_the_unknown_container(
    helper, ledger, monkeypatch
):
    installer, rehearsal, _, app, release, _ = helper
    complete_rehearsal(ledger, rehearsal)
    deploy = request(ledger, "deploy", rehearsal_id=rehearsal.operation_id)
    switch_request(ledger, deploy)
    blocked = Receipt(
        operation_id=deploy.operation_id,
        selection=deploy.approval.selection,
        state="blocked",
        error="recovery_required",
    )
    publish(
        installer.journal / f"{deploy.operation_id}.receipt.json",
        blocked.model_dump_json().encode(),
    )
    publish(
        ledger.spool / "receipts" / f"{deploy.operation_id}.json",
        blocked.model_dump_json().encode(),
    )
    ledger.service.reconcile()
    recover = request(ledger, "recover", preview_id=None)
    with ledger.database.connect() as connection:
        authority = read_authority(connection, recover.operation_id, "a" * 64)
    monkeypatch.setattr(installer, "authority", lambda *_: (authority, app, release))
    monkeypatch.setattr(
        installer,
        "child",
        lambda *args: b"switching"
        if args[2] == "state"
        else recover.model_dump_json().encode(),
    )

    def unknown(*_):
        raise ContractError("Container ownership is unknown")

    monkeypatch.setattr(installer, "owned", unknown)
    monkeypatch.setattr(installer, "remove", lambda *_: pytest.fail("must not remove"))
    installer.execute(recover.operation_id)
    ledger.service.reconcile()
    with ledger.database.connect() as connection:
        states = dict(
            connection.execute("SELECT kind, state FROM app_operations").fetchall()
        )
    assert states == {"rehearse": "succeeded", "deploy": "blocked", "recover": "failed"}
    with gateway(ledger) as client:
        assert client.get("/apps/notes/").status_code == 503
    assert request(ledger, "recover", preview_id=None).recovers == deploy.operation_id


def test_run_once_trigger_units_are_generated_once_and_retry_pending_requests(
    helper, monkeypatch
):
    installer, *_ = helper
    units = Path(installer.policy.units)
    units.mkdir()
    commands = []
    monkeypatch.setattr(
        installer, "command", lambda *args, **kwargs: commands.append(args)
    )
    installer.trigger()
    path_unit = (units / "hp-managed-installer.path").read_text()
    assert f"PathChanged={installer.policy.spool}/requests" in path_unit
    assert "Unit=hp-managed-installer.service" in path_unit
    timer = (units / "hp-managed-installer.timer").read_text()
    assert "OnUnitActiveSec=5min" in timer and "OnBootSec=" in timer
    service = (units / "hp-managed-installer.service").read_text()
    assert "-I -m ops.managed_installer" in service
    assert (
        "--policy" in service and "--run" not in service and "--backup" not in service
    )
    assert commands == [
        ("systemctl", "daemon-reload"),
        ("systemctl", "enable", "--now", "hp-managed-installer.path"),
        ("systemctl", "enable", "--now", "hp-managed-installer.timer"),
    ]
    commands.clear()
    installer.trigger()
    assert ("systemctl", "daemon-reload") not in commands


def test_execute_failure_after_candidate_timers_restores_previous_lifecycle(
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
    Path(installer.policy.units).mkdir()
    (tmp_path / "backups").mkdir()
    enabled = set()

    def command(*args, **kwargs):
        if args[1] == "enable":
            enabled.add(args[-1])
        elif args[1] == "disable":
            enabled.discard(args[-1])
        return ""

    monkeypatch.setattr(installer, "command", command)
    monkeypatch.delattr(installer, "lifecycle")
    installer.lifecycle(app.model_copy(update={"maintenance": ()}), True)
    before = set(enabled)
    cleanup = "hp-managed-notes--maintenance-cleanup.timer"
    failed = []

    def check(*_):
        if cleanup in enabled and not failed:
            failed.append(True)
            raise ContractError("Administrator session revoked after candidate timers")
        return authority, app, release

    monkeypatch.setattr(installer, "authority", check)
    monkeypatch.setattr(
        installer,
        "child",
        lambda *args: b"switching"
        if args[2] == "state"
        else operation.model_dump_json().encode(),
    )
    monkeypatch.setattr(
        installer,
        "backup",
        lambda app, f, destination, **_: destination.write_bytes(b"x"),
    )
    monkeypatch.setattr(installer, "owned", lambda *_: None)
    monkeypatch.setattr(installer, "remove", lambda *_: events.append("remove"))
    monkeypatch.setattr(
        installer,
        "start",
        lambda *args, **kwargs: events.append(
            ("start", kwargs.get("installed_operation_id"))
        ),
    )
    monkeypatch.setattr(installer, "probe", lambda *_: None)
    installer.execute(operation.operation_id)
    receipt = Receipt.model_validate_json(
        (installer.journal / f"{operation.operation_id}.receipt.json").read_bytes()
    )
    assert receipt.state == "recovered"
    assert failed
    assert events[-2:] == ["remove", ("start", previous.operation_id)]
    assert installer.current(app) == previous
    assert enabled == before | {cleanup}, "restored lifecycle follows the host policy"
    units = Path(installer.policy.units)
    assert (units / cleanup).exists()
    assert (units / "hp-managed-notes--app.service").exists()


def test_boot_checks_resource_limits_before_any_pull(helper, monkeypatch):
    installer, _, _, _, _, events = helper
    installed(helper, monkeypatch)
    monkeypatch.delattr(installer, "image")
    monkeypatch.setattr(installer, "owned", lambda *_: None)

    def limits():
        raise ContractError("Required resource controllers are unavailable")

    monkeypatch.setattr(installer, "limits", limits)
    with pytest.raises(ContractError, match="resource controllers"):
        installer.boot("notes")
    assert [item for item in events if item[:1] == ("docker",)] == []


def test_trigger_reloads_systemd_after_a_crash_between_unit_replace_and_reload(
    helper, monkeypatch
):
    installer, *_ = helper
    units = Path(installer.policy.units)
    units.mkdir()

    def crash_before_reload(*args, **kwargs):
        if args[1] == "daemon-reload":
            raise KeyboardInterrupt

    monkeypatch.setattr(installer, "command", crash_before_reload)
    with pytest.raises(KeyboardInterrupt):
        installer.trigger()
    assert (units / "hp-managed-installer.service").exists()

    commands = []
    monkeypatch.setattr(
        installer, "command", lambda *args, **kwargs: commands.append(args)
    )
    installer.trigger()
    assert commands[0] == ("systemctl", "daemon-reload")
    commands.clear()
    installer.trigger()
    assert ("systemctl", "daemon-reload") not in commands


def test_installed_release_cannot_be_rejected(ledger):
    install(ledger)
    with pytest.raises(ContractError, match="installed"):
        ledger.service.reject("notes", ledger.digest, UUID(ADMIN_USER_ID), 1)


def test_managed_app_joins_navigation_only_with_an_accepted_install(
    ledger, monkeypatch
):
    peer = registration("legacy-peer", "/legacy-peer")
    peer["source"] = {"kind": "legacy"}
    ledger.registry.register(document(peer))

    def keys():
        return [app.key for app in ledger.registry.navigation()]

    assert "notes" in keys(), "previews serve a managed app without an install"
    monkeypatch.setenv("HOME_PLATFORM_INSTALLER_POLICY", "/etc/home-platform/x.json")
    assert "notes" not in keys()
    assert "legacy-peer" in keys()
    with gateway(ledger) as client:
        assert client.get("/apps/notes/").status_code == 503
    install(ledger)
    assert keys().count("notes") == 1


def test_managed_app_is_monitored_only_with_an_accepted_install(ledger, monkeypatch):
    def keys():
        return [target.key for target in ledger.registry.monitoring()]

    assert "notes" in keys(), "previews monitor a managed app without an install"
    monkeypatch.setenv("HOME_PLATFORM_INSTALLER_POLICY", "/etc/home-platform/x.json")
    assert "notes" not in keys()
    install(ledger)
    assert keys().count("notes") == 1


MiB = 1024 * 1024


def accept(ledger, operation, generation):
    with ledger.database.connect() as connection:
        authority = read_authority(connection, operation.operation_id, "a" * 64)
        release = eligible(
            connection,
            authority.registration,
            operation.approval.selection.manifest_sha256,
        )
    switch_request(ledger, operation)
    snapshot = InstalledSnapshot(
        operation_id=operation.operation_id,
        generation=generation,
        selection=operation.approval.selection,
        registration=authority.registration,
        image=release.manifest.image,
        image_id="sha256:" + "b" * 64,
    )
    publish(
        ledger.spool / "receipts" / f"{operation.operation_id}.json",
        Receipt(
            operation_id=operation.operation_id,
            selection=operation.approval.selection,
            state="succeeded",
            installed=snapshot,
            authorization_committed=True,
        )
        .model_dump_json()
        .encode(),
    )
    ledger.service.reconcile()
    return snapshot


def settings_change(registration, memory=512 * MiB, **edits):
    value = json.loads(registration.model_dump_json())
    value["bindings"]["resources"]["memory_bytes"] = memory
    value["bindings"]["settings"].append({"name": "NOTES_PAGE_SIZE", "value": "50"})
    for path, item in edits.items():
        *parents, leaf = path.split("__")
        target = value
        for key in parents:
            target = target[key]
        target[leaf] = item
    return json.dumps(value).encode()


def boot_view(ledger):
    with ledger.database.connect() as connection:
        return (
            registration_for(connection, "notes"),
            read_installed_authority(connection, "notes"),
            accepted_install(connection, "notes", "test"),
            [app.key for app in ledger.registry.navigation()],
            ledger.registry.managed_app("notes"),
        )


def test_installed_settings_change_is_proposed_and_leaves_boot_authority(ledger):
    install(ledger)
    before = boot_view(ledger)
    assert update_registration(ledger.database, settings_change(before[0])) == (
        "proposed"
    )
    assert boot_view(ledger) == before
    with ledger.database.connect() as connection:
        change = pending_change(connection, "notes", "test")
    assert change.registration.bindings.resources.memory_bytes == 512 * MiB
    assert before[2].registration.bindings.resources.memory_bytes == 256 * MiB
    assert update_registration(ledger.database, settings_change(before[0])) == (
        "unchanged"
    )
    replaced = settings_change(before[0], memory=384 * MiB)
    assert update_registration(ledger.database, replaced) == "proposed"
    with ledger.database.connect() as connection:
        change = pending_change(connection, "notes", "test")
        assert change.registration.bindings.resources.memory_bytes == 384 * MiB
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM app_registration_proposals"
            ).fetchone()[0]
            == 2
        )
    unchanged = before[0].model_dump_json().encode()
    assert update_registration(ledger.database, unchanged) == "withdrawn"
    with ledger.database.connect() as connection:
        assert pending_change(connection, "notes", "test") is None
        connection.execute(
            "INSERT INTO app_registration_rejections(proposal_id, administrator_id) "
            "SELECT MIN(proposal_id), 'x' FROM app_registration_proposals"
        )
        for statement in (
            "UPDATE app_registration_proposals SET base_generation = 9",
            "DELETE FROM app_registration_proposals",
            "INSERT OR REPLACE INTO app_registration_proposals SELECT * FROM "
            "app_registration_proposals LIMIT 1",
            "UPDATE app_registration_rejections SET administrator_id = 'y'",
            "DELETE FROM app_registration_rejections",
            "INSERT OR REPLACE INTO app_registration_rejections SELECT * FROM "
            "app_registration_rejections",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(statement)
    assert boot_view(ledger) == before


def test_approved_settings_change_redeploys_the_installed_release(ledger):
    first = install(ledger)
    update_registration(ledger.database, settings_change(first.registration))
    rehearsal = request(ledger)
    selection = rehearsal.approval.selection
    assert selection.manifest_sha256 == first.selection.manifest_sha256
    assert selection.generation == 1
    with ledger.database.connect() as connection:
        proposal = pending_change(connection, "notes", "test").registration
    assert selection.registration_sha256 == fingerprint(proposal)
    complete_rehearsal(ledger, rehearsal)
    deploy = request(ledger, "deploy", rehearsal_id=rehearsal.operation_id)
    assert boot_view(ledger)[0] == first.registration
    second = accept(ledger, deploy, 2)
    current, authority, snapshot, *_ = boot_view(ledger)
    assert snapshot == second
    assert snapshot.registration.bindings.resources.memory_bytes == 512 * MiB
    assert current == authority.registration == proposal
    with ledger.database.connect() as connection:
        assert pending_change(connection, "notes", "test") is None

    rollback = request(ledger, "rollback", preview_id=first.selection.preview_id)
    assert rollback.approval.selection.registration_sha256 == fingerprint(
        first.registration
    )
    third = accept(ledger, rollback, 3)
    assert third.registration == first.registration
    current, authority, *_ = boot_view(ledger)
    assert current == authority.registration == first.registration
    with ledger.database.connect() as connection:
        assert pending_change(connection, "notes", "test") is None


def test_settings_change_after_approval_refuses_the_stale_deploy(ledger):
    first = install(ledger)
    update_registration(ledger.database, settings_change(first.registration))
    rehearsal = request(ledger)
    with pytest.raises(ContractError, match="pending"):
        update_registration(
            ledger.database, settings_change(first.registration, memory=1024 * MiB)
        )
    complete_rehearsal(ledger, rehearsal)
    update_registration(
        ledger.database, settings_change(first.registration, memory=1024 * MiB)
    )
    with pytest.raises(ContractError, match="rehearsal"):
        request(ledger, "deploy", rehearsal_id=rehearsal.operation_id)


@pytest.mark.parametrize(
    "edit",
    [
        {"access": "administrator"},
        {"capabilities": ["identity"]},
        {"source__repository": "example/other"},
        {"bindings__state_directory": "/private/other-state"},
        {"route": "/apps/renamed"},
        {"installation__host_port": 19555},
    ],
)
def test_installed_app_refuses_changes_outside_bindings_and_presentation(ledger, edit):
    first = install(ledger)
    with pytest.raises(ContractError):
        update_registration(
            ledger.database, settings_change(first.registration, **edit)
        )
    with ledger.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM app_registration_proposals"
            ).fetchone()[0]
            == 0
        )


def test_settings_change_that_breaks_the_installed_release_is_refused(ledger):
    first = install(ledger)
    with pytest.raises(ContractError, match="secret"):
        update_registration(
            ledger.database,
            settings_change(first.registration, bindings__secret_files=[]),
        )


def test_rejecting_a_settings_change_discards_only_that_change(ledger):
    first = install(ledger)
    update_registration(ledger.database, settings_change(first.registration))
    with ledger.database.connect() as connection:
        change = pending_change(connection, "notes", "test")
    admin = UUID(ADMIN_USER_ID)
    with pytest.raises(ContractError, match="replaced"):
        ledger.service.reject("notes", ledger.digest, admin, 1)
    with pytest.raises(ContractError, match="replaced"):
        ledger.service.reject("notes", ledger.digest, admin, 1, "0" * 64)
    ledger.service.reject(
        "notes", ledger.digest, admin, 1, fingerprint(change.registration)
    )
    with ledger.database.connect() as connection:
        assert pending_change(connection, "notes", "test") is None
        assert (
            connection.execute("SELECT COUNT(*) FROM app_rejections").fetchone()[0] == 0
        )
        assert accepted_install(connection, "notes", "test") == first
    with pytest.raises(ContractError, match="installed"):
        ledger.service.reject("notes", ledger.digest, admin, 1)


def test_blocked_settings_deploy_recovers_once_the_policy_is_reverted(
    helper, ledger, monkeypatch
):
    installer, rehearsal, _, app, release, events = helper
    complete_rehearsal(ledger, rehearsal)
    first = accept(
        ledger, request(ledger, "deploy", rehearsal_id=rehearsal.operation_id), 1
    )
    installed_by_root(installer, first)
    publish(installer.journal / f"{release.archive_sha256}.zip", release.archive)
    update_registration(ledger.database, settings_change(first.registration))
    proposal_rehearsal = request(ledger)
    complete_rehearsal(ledger, proposal_rehearsal)
    deploy = request(ledger, "deploy", rehearsal_id=proposal_rehearsal.operation_id)
    switch_request(ledger, deploy)
    blocked = Receipt(
        operation_id=deploy.operation_id,
        selection=deploy.approval.selection,
        state="blocked",
        error="recovery_required",
    )
    for path in (
        installer.journal / f"{deploy.operation_id}.receipt.json",
        ledger.spool / "receipts" / f"{deploy.operation_id}.json",
    ):
        publish(path, blocked.model_dump_json().encode())
    ledger.service.reconcile()

    recover = request(ledger, "recover", preview_id=None)
    assert recover.approval.selection.registration_sha256 == fingerprint(
        first.registration
    ), "recovery stayed pinned to the unaccepted proposal"
    with ledger.database.connect() as connection:
        authority = read_authority(connection, recover.operation_id, "a" * 64)
    reverted = app.model_copy(update={"registration": first.registration})
    monkeypatch.setattr(
        installer, "authority", lambda *_: (authority, reverted, release)
    )
    monkeypatch.setattr(
        installer,
        "child",
        lambda *args: b"switching"
        if args[2] == "state"
        else recover.model_dump_json().encode(),
    )
    switched = container(first, operation=str(deploy.operation_id))
    monkeypatch.setattr(installer, "owned", lambda *_: switched)
    monkeypatch.setattr(installer, "local_image", lambda *_: first.image_id)
    monkeypatch.setattr(installer, "stop_timers", lambda *_: None)
    monkeypatch.setattr(installer, "remove", lambda *_: events.append("remove"))
    started = []
    monkeypatch.setattr(
        installer,
        "start",
        lambda _a, started_app, *_, **kw: started.append(
            (kw["installed_operation_id"], started_app.registration)
        ),
    )
    monkeypatch.setattr(installer, "probe", lambda *_: events.append("probe"))
    installer.execute(recover.operation_id)
    ledger.service.reconcile()

    assert started == [(first.operation_id, first.registration)]
    with ledger.database.connect() as connection:
        states = dict(
            connection.execute("SELECT kind, state FROM app_operations").fetchall()
        )
        proposal = pending_change(connection, "notes", "test")
    assert (states["recover"], states["deploy"]) == ("succeeded", "recovered")
    row, authority, snapshot, *_ = boot_view(ledger)
    assert snapshot == first == installer.current(app)
    assert row == authority.registration == snapshot.registration
    assert proposal is not None
    assert (
        fingerprint(proposal.registration)
        == deploy.approval.selection.registration_sha256
    )
    assert (
        update_registration(
            ledger.database, first.registration.model_dump_json().encode()
        )
        == "withdrawn"
    )


def propose_release_with_secret(ledger, monkeypatch, base, secret_path):
    """Publish a release needing ``extra-key`` and propose the binding for it."""
    digest = push(
        ledger,
        monkeypatch,
        secrets=["provider-key", "extra-key"],
        source_commit="c" * 40,
    )
    with ledger.database.connect() as connection:
        archive = connection.execute(
            "SELECT archive FROM app_eligible_releases WHERE manifest_sha256 = ?",
            (digest,),
        ).fetchone()[0]
        preview = PreviewResult(
            attempt_id=uuid4(),
            app_id="notes",
            manifest_sha256=digest,
            bundle_sha256=sha256(archive),
            state="ready",
            image_id="sha256:" + "b" * 64,
            preview_url="https://preview.example/notes/",
            checks=dict.fromkeys(CHECKS, True),
        )
        connection.execute("DELETE FROM app_preview_attempts")
        connection.execute(
            "INSERT INTO app_preview_attempts VALUES (?,?,?,?,?,?)",
            (
                str(preview.attempt_id),
                "notes",
                digest,
                sha256(archive),
                "ready",
                preview.model_dump_json(),
            ),
        )
    value = json.loads(base.model_dump_json())
    value["bindings"]["secret_files"].append(
        {"name": "extra-key", "file_path": str(secret_path)}
    )
    assert update_registration(ledger.database, json.dumps(value).encode()) == (
        "proposed"
    )
    with ledger.database.connect() as connection:
        shown = fingerprint(pending_change(connection, "notes", "test").registration)
    rehearsal = request(
        ledger, digest=digest, preview_id=preview.attempt_id, registration_sha256=shown
    )
    complete_rehearsal(ledger, rehearsal)
    return request(
        ledger,
        "deploy",
        digest=digest,
        preview_id=preview.attempt_id,
        rehearsal_id=rehearsal.operation_id,
        registration_sha256=shown,
    )


def block(ledger, operation, journal=None):
    switch_request(ledger, operation)
    blocked = Receipt(
        operation_id=operation.operation_id,
        selection=operation.approval.selection,
        state="blocked",
        error="recovery_required",
    )
    for path in (
        *([journal / f"{operation.operation_id}.receipt.json"] if journal else []),
        ledger.spool / "receipts" / f"{operation.operation_id}.json",
    ):
        publish(path, blocked.model_dump_json().encode())
    ledger.service.reconcile()


def test_blocked_release_that_adds_a_secret_recovers_once_the_policy_is_reverted(
    helper, ledger, monkeypatch, tmp_path
):
    installer, rehearsal, _, app, release, events = helper
    complete_rehearsal(ledger, rehearsal)
    first = accept(
        ledger, request(ledger, "deploy", rehearsal_id=rehearsal.operation_id), 1
    )
    installed_by_root(installer, first)
    publish(installer.journal / f"{release.archive_sha256}.zip", release.archive)
    deploy = propose_release_with_secret(
        ledger, monkeypatch, first.registration, tmp_path / "extra/key"
    )
    block(ledger, deploy, installer.journal)

    recover = request(
        ledger, "recover", digest=deploy.approval.selection.manifest_sha256
    )
    assert recover.approval.selection.registration_sha256 == fingerprint(
        first.registration
    )
    with ledger.database.connect() as connection:
        authority = read_authority(connection, recover.operation_id, "a" * 64)
    assert authority.registration == first.registration
    reverted = app.model_copy(update={"registration": first.registration})
    newer = installer.release(
        reverted,
        base64.b64decode(authority.archive_base64),
        authority.artifact_id,
        recover.approval.selection.archive_sha256,
    )
    monkeypatch.setattr(installer, "authority", lambda *_: (authority, reverted, newer))
    monkeypatch.setattr(
        installer,
        "child",
        lambda *args: b"switching"
        if args[2] == "state"
        else recover.model_dump_json().encode(),
    )
    switched = container(first, operation=str(deploy.operation_id))
    monkeypatch.setattr(installer, "owned", lambda *_: switched)
    monkeypatch.setattr(installer, "local_image", lambda *_: first.image_id)
    monkeypatch.setattr(installer, "stop_timers", lambda *_: None)
    monkeypatch.setattr(installer, "remove", lambda *_: events.append("remove"))
    started = []
    monkeypatch.setattr(
        installer,
        "start",
        lambda _a, started_app, old, *_, **kw: started.append(
            (kw["installed_operation_id"], started_app.registration, old.image)
        ),
    )
    monkeypatch.setattr(installer, "probe", lambda *_: events.append("probe"))
    installer.execute(recover.operation_id)
    ledger.service.reconcile()

    assert started == [(first.operation_id, first.registration, release.image)]
    with ledger.database.connect() as connection:
        states = dict(
            connection.execute("SELECT kind, state FROM app_operations").fetchall()
        )
    assert (states["recover"], states["deploy"]) == ("succeeded", "recovered")
    row, authority, snapshot, *_ = boot_view(ledger)
    assert snapshot == first == installer.current(app)
    assert row == authority.registration == snapshot.registration


def test_blocked_rollback_to_a_release_needing_a_secret_is_recoverable(
    ledger, monkeypatch, tmp_path
):
    first = install(ledger)
    second = accept(
        ledger,
        propose_release_with_secret(
            ledger, monkeypatch, first.registration, tmp_path / "extra/key"
        ),
        2,
    )
    assert "extra-key" in {
        item.name for item in second.registration.bindings.secret_files
    }
    third = accept(ledger, request(ledger, "rollback"), 3)
    assert third.registration == first.registration
    again = request(ledger, "rollback", digest=second.selection.manifest_sha256)
    assert again.approval.selection.registration_sha256 == fingerprint(
        second.registration
    )
    block(ledger, again)

    recover = request(ledger, "recover", digest=second.selection.manifest_sha256)
    assert recover.approval.selection.registration_sha256 == fingerprint(
        first.registration
    )
    with ledger.database.connect() as connection:
        assert (
            read_authority(connection, recover.operation_id, "a" * 64).registration
            == first.registration
        )
    switch_request(ledger, recover)
    publish(
        ledger.spool / "receipts" / f"{recover.operation_id}.json",
        Receipt(
            operation_id=recover.operation_id,
            selection=recover.approval.selection,
            state="succeeded",
            authorization_committed=True,
        )
        .model_dump_json()
        .encode(),
    )
    ledger.service.reconcile()
    with ledger.database.connect() as connection:
        states = dict(
            connection.execute(
                "SELECT operation_id, state FROM app_operations"
            ).fetchall()
        )
    assert (states[str(recover.operation_id)], states[str(again.operation_id)]) == (
        "succeeded",
        "recovered",
    )
    row, authority, snapshot, *_ = boot_view(ledger)
    assert snapshot == third
    assert row == authority.registration == first.registration


def test_root_never_boots_a_registration_core_has_not_accepted(helper, monkeypatch):
    installer, _, _, app, _, events = helper
    edited = app.model_copy(
        update={
            "registration": app.registration.model_validate_json(
                settings_change(app.registration)
            )
        }
    )
    policy = installer.policy.model_copy(update={"apps": (edited,)})
    installed(helper, monkeypatch, app_fingerprint(policy, "notes"))
    installer.policy, installer.digest = policy, fingerprint(policy)
    with pytest.raises(ContractError, match="differs from host policy"):
        installer.boot("notes")
    assert "start" not in events and "remove" not in events


def test_recover_retry_with_the_same_request_id_returns_the_original(ledger):
    deploy = deploy_request(ledger)
    switch_request(ledger, deploy)
    blocked = Receipt(
        operation_id=deploy.operation_id,
        selection=deploy.approval.selection,
        state="blocked",
        error="recovery_required",
    )
    publish(
        ledger.spool / "receipts" / f"{deploy.operation_id}.json",
        blocked.model_dump_json().encode(),
    )
    ledger.service.reconcile()
    recover = request(ledger, "recover", preview_id=None)
    assert (
        request(ledger, "recover", preview_id=None, request_id=recover.request_id)
        == recover
    )
    switch_request(ledger, recover)
    publish(
        ledger.spool / "receipts" / f"{recover.operation_id}.json",
        Receipt(
            operation_id=recover.operation_id,
            selection=recover.approval.selection,
            state="succeeded",
            authorization_committed=True,
        )
        .model_dump_json()
        .encode(),
    )
    ledger.service.reconcile()
    with ledger.database.connect() as connection:
        states = dict(
            connection.execute("SELECT kind, state FROM app_operations").fetchall()
        )
    assert states["recover"] == "succeeded" and states["deploy"] == "failed"
    assert (
        request(ledger, "recover", preview_id=None, request_id=recover.request_id)
        == recover
    )


def test_container_swap_is_capped_at_memory_and_probed(helper, monkeypatch):
    installer, operation, authority, app, release, _ = helper
    spec = runtime_spec(
        ManagedApp(app.registration, app.registration.installation),
        AppCredential(
            operation.approval.selection.credential_id,
            "notes",
            "test",
            Path(app.credential_file),
        ),
        InstanceFacts("test", authority.app_api_url, "UTC"),
        release.port,
    )
    argv = docker_run_argv(spec, release.image, (app.uid, app.gid))
    resources = app.registration.bindings.resources
    assert argv[argv.index("--memory") + 1] == str(resources.memory_bytes)
    assert argv[argv.index("--memory-swap") + 1] == str(resources.memory_bytes)
    unlimited_swap = Container.model_validate(
        dict(
            Image="sha256:" + "b" * 64,
            Config=dict(Labels={}),
            HostConfig=dict(
                Memory=resources.memory_bytes,
                MemorySwap=-1,
                PidsLimit=resources.pids_limit,
                NanoCpus=int(resources.cpu_limit * 1_000_000_000),
            ),
            State=dict(Running=True),
        )
    )
    monkeypatch.setattr(installer, "owned", lambda *_: unlimited_swap)
    monkeypatch.setattr(
        managed_installer,
        "response_body",
        lambda *_: pytest.fail("limits are verified before readiness"),
    )
    with pytest.raises(ContractError, match="limits"):
        installer.probe(authority, app, release, "sha256:" + "b" * 64)


def test_rehearse_and_deploy_name_the_settings_change_they_approve(ledger):
    first = install(ledger)
    with pytest.raises(ContractError, match="settings change"):
        request(ledger, registration_sha256=fingerprint(first.registration))
    update_registration(ledger.database, settings_change(first.registration))
    with ledger.database.connect() as connection:
        shown = fingerprint(pending_change(connection, "notes", "test").registration)
    for claimed in (None, fingerprint(first.registration)):
        with pytest.raises(ContractError, match="settings change"):
            request(ledger, registration_sha256=claimed)
    rehearsal = request(ledger, registration_sha256=shown)
    assert rehearsal.approval.selection.registration_sha256 == shown
    retry = dict(request_id=rehearsal.request_id, registration_sha256=None)
    with pytest.raises(ContractError, match="already used"):
        request(ledger, **retry)
    assert request(ledger, **{**retry, "registration_sha256": shown}) == rehearsal
    complete_rehearsal(ledger, rehearsal)
    deploy = dict(kind="deploy", rehearsal_id=rehearsal.operation_id)
    with pytest.raises(ContractError, match="settings change"):
        request(ledger, **deploy, registration_sha256=None)
    assert (
        request(
            ledger, **deploy, registration_sha256=shown
        ).approval.selection.registration_sha256
        == shown
    )


def log_options(argv):
    return [argv[i + 1] for i, item in enumerate(argv) if item == "--log-opt"]


def test_managed_containers_choose_their_log_driver_independently_of_the_host(
    helper, monkeypatch
):
    installer, operation, authority, app, release, _ = helper
    spec = runtime_spec(
        ManagedApp(app.registration, app.registration.installation),
        AppCredential(
            operation.approval.selection.credential_id,
            "notes",
            "test",
            Path(app.credential_file),
        ),
        InstanceFacts("test", authority.app_api_url, "UTC"),
        release.port,
    )
    monkeypatch.setattr(installer, "spec", lambda *_: spec)
    commands = []
    monkeypatch.setattr(
        installer, "command", lambda *args, **kwargs: commands.append(args)
    )
    installer.start(authority, app, release, release.image)
    installer.start(
        authority, app, release, release.image, rehearsal=Path("/fixture/state")
    )
    # A host daemon.json defaulting to the local driver compresses rotated logs,
    # which Docker refuses with a single log file unless the run says otherwise.
    for argv in commands:
        assert argv[argv.index("--log-driver") + 1] == "local"
        assert log_options(argv) == ["max-size=1m", "max-file=1", "compress=false"]


def advertise(ledger, kinds=tuple(KINDS)):
    publish(
        ledger.spool / "receipts" / "installer.json",
        InstallerCapabilities(kinds=kinds).model_dump_json().encode(),
    )


def retire_request(ledger, current):
    """Request a retirement as the card would: the installed digest and preview."""
    advertise(ledger)
    return request(
        ledger,
        "retire",
        digest=current.selection.manifest_sha256,
        preview_id=current.selection.preview_id,
    )


def retired_receipt(operation, last_installed, **changes):
    selection = operation.approval.selection
    retired = RetiredSnapshot(
        operation_id=operation.operation_id,
        generation=selection.generation + 1,
        selection=selection,
        registration=last_installed.registration,
        last_installed=last_installed,
        archive=None,
    )
    return Receipt(
        operation_id=operation.operation_id,
        selection=selection,
        state="succeeded",
        retired=retired.model_copy(update=changes),
        authorization_committed=True,
    )


def retire(ledger, current=None):
    current = current or install(ledger)
    operation = retire_request(ledger, current)
    switch_request(ledger, operation)
    publish(
        ledger.spool / "receipts" / f"{operation.operation_id}.json",
        retired_receipt(operation, current).model_dump_json().encode(),
    )
    ledger.service.reconcile()
    return current, operation


def served(ledger, monkeypatch):
    monkeypatch.setenv("HOME_PLATFORM_INSTALLER_POLICY", "/etc/home-platform/x.json")
    return {
        "navigation": [app.key for app in ledger.registry.navigation()],
        "monitoring": [target.key for target in ledger.registry.monitoring()],
    }


def test_retire_selection_pins_the_installed_generation(ledger, monkeypatch):
    current = install(ledger)
    newer = push(ledger, monkeypatch, source_commit="c" * 40)
    advertise(ledger)
    with pytest.raises(ContractError, match="Retire target changed"):
        request(ledger, "retire", digest=newer)
    with pytest.raises(ContractError, match="Retire target changed"):
        request(ledger, "retire", preview_id=uuid4())
    operation = retire_request(ledger, current)
    selected = operation.approval.selection
    assert selected.generation == 1
    assert selected.manifest_sha256 == current.selection.manifest_sha256
    assert selected.preview_id == current.selection.preview_id
    assert selected.registration_sha256 == fingerprint(current.registration)
    with ledger.database.connect() as connection:
        authority = read_authority(connection, operation.operation_id, "a" * 64)
    assert authority.previous == current
    assert authority.registration == current.registration


def test_retire_refuses_while_compute_runs_are_queued_or_running(ledger):
    current = install(ledger)
    raw = sqlite3.connect(ledger.database.path)
    with raw:
        for index, status in enumerate(("QUEUED", "RUNNING", "COMPLETED")):
            raw.execute(
                "INSERT INTO jobs(id,type,parameters_json,status,created_at,"
                "updated_at,group_id,owner_user_id) VALUES "
                "(?,'app','{}',?,'t','t',?,?)",
                (f"job-{index}", status, f"group-{index}", ADMIN_USER_ID),
            )
            raw.execute(
                "INSERT INTO app_runs(group_id,app_id,owner_user_id,installation_id,"
                "submission_key,payload_sha256,inputs_json) VALUES "
                "(?,'notes','owner','workload',?,'p','[]')",
                (f"group-{index}", f"key-{index}"),
            )
    raw.close()
    with ledger.database.connect() as connection:
        assert retire_blocker(connection, "notes") == (
            "2 compute runs still queued or running"
        )
    with pytest.raises(ContractError, match="2 compute runs still queued"):
        retire_request(ledger, current)
    assert not list((ledger.spool / "requests").iterdir())


def test_retire_receipt_appends_a_retired_generation_and_installed_becomes_none(
    ledger, monkeypatch
):
    current, operation = retire(ledger)
    with ledger.database.connect() as connection:
        states = dict(connection.execute("SELECT kind, state FROM app_operations"))
        assert states["retire"] == "succeeded"
        assert accepted_install(connection, "notes", "test") is None
        assert accepted_install(connection, "notes", "test", 1) is None
        latest = accepted(connection, "notes", "test")
        assert isinstance(latest, RetiredSnapshot)
        assert latest.generation == 2 and latest.last_installed == current
        assert retirement(connection, "notes", "test") == latest
        assert pending_change(connection, "notes", "test") is None
        with pytest.raises(ApplicationRetired, match="retired") as refused:
            read_installed_authority(connection, "notes")
        assert isinstance(refused.value, ReconciliationPending)
    assert not (ledger.spool / "requests" / f"{operation.operation_id}.json").exists()
    with gateway(ledger) as client:
        response = client.get("/apps/notes/")
    assert response.status_code == 503
    assert "retired" in response.text
    assert ledger.registry.retirement("notes") is not None
    assert "notes" not in served(ledger, monkeypatch)["navigation"]
    assert "notes" not in served(ledger, monkeypatch)["monitoring"]
    with pytest.raises(ContractError, match="Nothing is installed to retire"):
        retire_request(ledger, current)


def test_a_reconciler_that_loses_the_race_to_accept_a_retirement_records_no_error(
    ledger, monkeypatch
):
    """Catches the losing reconciler checking the consumed retire receipt
    against live installed state, which the winner's retirement already
    removed, and stamping a permanent reconcile error on the succeeded
    retirement."""
    current = install(ledger)
    operation = retire_request(ledger, current)
    switch_request(ledger, operation)
    publish(
        ledger.spool / "receipts" / f"{operation.operation_id}.json",
        retired_receipt(operation, current).model_dump_json().encode(),
    )
    service = ledger.service
    unraced = service.reconcile_operation

    def lose_the_race(stale):
        monkeypatch.setattr(service, "reconcile_operation", unraced)
        service.reconcile()
        unraced(stale)

    monkeypatch.setattr(service, "reconcile_operation", lose_the_race)
    service.reconcile()
    with ledger.database.connect() as connection:
        assert tuple(
            connection.execute(
                "SELECT state, reconcile_error FROM app_operations "
                "WHERE kind = 'retire'"
            ).fetchone()
        ) == ("succeeded", None)
        assert isinstance(accepted(connection, "notes", "test"), RetiredSnapshot)


def test_retired_receipt_must_name_the_row_it_retires(ledger):
    current = install(ledger)
    operation = retire_request(ledger, current)
    switch_request(ledger, operation)
    stale = current.model_copy(update={"operation_id": uuid4()})
    publish(
        ledger.spool / "receipts" / f"{operation.operation_id}.json",
        retired_receipt(operation, stale).model_dump_json().encode(),
    )
    ledger.service.reconcile()
    with ledger.database.connect() as connection:
        state, error = connection.execute(
            "SELECT state, reconcile_error FROM app_operations WHERE kind = 'retire'"
        ).fetchone()
        assert (state, error) == ("switching", "Installer retirement is incomplete")
        assert accepted_install(connection, "notes", "test") == current


def test_gateway_closes_while_a_retirement_switches(ledger):
    current = install(ledger)
    operation = retire_request(ledger, current)
    with gateway(ledger) as client:
        assert client.get("/apps/notes/").status_code == 403
    switch_request(ledger, operation)
    with gateway(ledger) as client:
        response = client.get("/apps/notes/")
    assert response.status_code == 503
    assert "pending" in response.text


def test_restore_deploy_selects_the_retired_generation_and_lands_at_n_plus_two(
    ledger, monkeypatch
):
    current, _ = retire(ledger)
    rehearsal = request(ledger)
    assert rehearsal.approval.selection.generation == 2
    complete_rehearsal(ledger, rehearsal)
    deploy = request(ledger, "deploy", rehearsal_id=rehearsal.operation_id)
    with ledger.database.connect() as connection:
        authority = read_authority(connection, deploy.operation_id, "a" * 64)
    assert isinstance(authority.previous, RetiredSnapshot)
    assert authority.previous.last_installed == current
    restored = accept(ledger, deploy, 3)
    with ledger.database.connect() as connection:
        assert accepted_install(connection, "notes", "test") == restored
        assert accepted_install(connection, "notes", "test", 1) is None, (
            "a rollback was offered across the retirement"
        )
        assert retirement(connection, "notes", "test") is None
        assert read_installed_authority(connection, "notes").previous == restored
    with gateway(ledger) as client:
        assert client.get("/apps/notes/").status_code == 403
    assert served(ledger, monkeypatch)["navigation"].count("notes") == 1


@pytest.mark.parametrize(
    "edits", [{}, {"bindings__state_directory": "/private/other-state"}]
)
def test_registration_is_kept_while_retired_so_a_restore_binds_the_kept_data(
    ledger, edits
):
    """Catches a registry update replacing a retired app's registration, which
    skips the installed-identity guard, lets a restore bind a state directory
    other than the kept one, and makes the retired card's restore, which shows
    no settings change, fail as a replaced change."""
    current, _ = retire(ledger)
    changed = settings_change(current.registration, **edits)
    with pytest.raises(ContractError, match="Restore the app before changing"):
        update_registration(ledger.database, changed)
    with ledger.database.connect() as connection:
        assert registration_for(connection, "notes") == current.registration
        assert not connection.execute(
            "SELECT 1 FROM app_registration_proposals"
        ).fetchone()
    rehearsal = request(ledger)
    assert rehearsal.approval.selection.registration_sha256 == fingerprint(
        current.registration
    )


def test_one_retirement_leaves_other_apps_served_and_their_history_untouched(
    ledger, monkeypatch, tmp_path
):
    current = install(ledger)
    peer = managed("habits", "/apps/habits", state=str(tmp_path / "habits-state"))
    ledger.registry.register(document(peer))
    habits_operation = uuid4()
    with ledger.database.connect() as connection:
        registration = registration_for(connection, "habits")
        habits = current.model_copy(
            update={
                "operation_id": habits_operation,
                "registration": registration,
                "selection": current.selection.model_copy(
                    update={
                        "app_id": "habits",
                        "registration_sha256": fingerprint(registration),
                    }
                ),
            }
        )
        connection.execute(
            "INSERT INTO app_operations(operation_id,request_id,app_id,environment,"
            "kind,state,operation_json) VALUES "
            "(?,?,'habits','test','deploy','succeeded','{}')",
            (str(habits_operation), str(uuid4())),
        )
        connection.execute(
            "INSERT INTO app_installed_history VALUES (?,'habits','test',1,?)",
            (str(habits_operation), habits.model_dump_json()),
        )

    def habits_rows():
        with ledger.database.connect() as connection:
            return connection.execute(
                "SELECT * FROM app_installed_history WHERE app_id = 'habits'"
            ).fetchall()

    before = habits_rows()
    assert ledger.registry._accepted_installs() == {"notes", "habits"}
    operation = retire_request(ledger, current)
    switch_request(ledger, operation)
    publish(
        ledger.spool / "receipts" / f"{operation.operation_id}.json",
        retired_receipt(operation, current).model_dump_json().encode(),
    )
    ledger.service.reconcile()
    assert ledger.registry._accepted_installs() == {"habits"}
    assert habits_rows() == before
    assert ledger.registry.retirement("habits") is None
    keys = served(ledger, monkeypatch)["navigation"]
    assert "habits" in keys and "notes" not in keys
    with ledger.database.connect() as connection:
        assert accepted_install(connection, "habits", "test") == habits


def test_retired_credential_is_refused_as_retired_not_as_missing(ledger):
    identity = AppIdentity(
        AccountStore(ledger.database),
        ledger.registry,
        AppPlatformSettings(
            InstanceFacts("test", "http://127.0.0.1:1/api/apps/v1", "UTC"), b"c" * 32
        ),
    )
    credential = ledger.registry.active_credential("notes")
    bearer = "Bearer " + credential.file_path.read_text().strip()
    current = install(ledger)
    assert identity.verify(bearer, None) == Refusal(
        401, "Missing or invalid user context"
    )
    retire(ledger, current)
    assert identity.verify(bearer, None) == Refusal(403, "Application is retired")
    assert identity.verify("Bearer " + "0" * 64, None).status == 401
