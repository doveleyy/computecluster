import hashlib
import json
import sqlite3
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_app_platform import assertion, bearer, managed, request
from test_app_platform import platform as platform_fixture
from test_app_registry import document, manifest, registration

from app import migrations
from app.accounts import AccountStore, UserCreate, encode_session
from app.app_assets import ComputeError
from app.app_compute import AppCompute
from app.app_contracts import ContractError
from app.app_origin import AppOrigin, AppReplayConflict
from app.app_registry import AppRegistry
from app.app_workloads import MAX_INPUT_BYTES, WorkloadStore
from app.artifacts import job_artifact_relative_path
from app.database import Database
from app.identity import ADMIN_USER_ID
from app.main import create_app
from app.repository import JobRepository
from contracts.models import (
    BatchResult,
    JobCompletion,
    JobGroupRead,
    JobRead,
    JobStatus,
    JobType,
    SleepParameters,
    WorkerCapacityUpdate,
    WorkerClaim,
    WorkerHeartbeat,
)

identity_platform = platform_fixture


@pytest.fixture
def atomic_store(tmp_path):
    database = Database(tmp_path / "core.db")
    database.initialize()
    registry = AppRegistry(database)
    registry.register(document(registration()))
    receipt = registry.ingest_release("notes", document(manifest()))
    user = AccountStore(database).create(
        UserCreate(username="alice", password="long password")
    )
    installation = uuid4()
    reservation = uuid4()
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO app_staging_reservations VALUES (?, ?, ?, ?)",
            (str(reservation), "notes", str(user.id), "[]"),
        )
        connection.execute(
            "INSERT INTO app_workloads VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(installation),
                "notes",
                receipt.manifest_sha256,
                "report",
                str(uuid4()),
                "[]",
            ),
        )
    yield (
        database,
        AppOrigin(
            "notes", user.id, installation, "retry-key", "a" * 64, "{}", reservation
        ),
    )
    database.close()


def native_group():
    now = datetime.now(UTC)
    group_id = uuid4()
    tasks = [
        JobRead(
            id=uuid4(),
            type=JobType.SLEEP,
            parameters=SleepParameters(seconds=1),
            group_id=group_id,
            task_id=str(index),
            task_index=index,
            status=JobStatus.QUEUED,
            created_at=now,
            updated_at=now,
        )
        for index in range(2)
    ]
    return JobGroupRead(
        id=group_id,
        name="atomic",
        status=JobStatus.QUEUED,
        created_at=now,
        updated_at=now,
        tasks=tasks,
    )


def test_native_children_and_app_origin_rollback_together(atomic_store):
    database, origin = atomic_store
    with database.connect() as connection:
        connection.execute(
            "CREATE TRIGGER fail_origin BEFORE INSERT ON app_runs BEGIN "
            "SELECT RAISE(ABORT, 'injected failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        JobRepository(database).add_group(
            native_group(), "{}", None, str(origin.owner_user_id), origin
        )
    with database.connect() as connection:
        assert [
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("job_groups", "jobs", "app_runs")
        ] == [0, 0, 0]
        connection.execute("DROP TRIGGER fail_origin")
    stored, _ = JobRepository(database).add_group(
        native_group(), "{}", None, str(origin.owner_user_id), origin
    )
    assert len(stored.tasks) == 2


def test_concurrent_app_replays_create_one_native_group(atomic_store):
    database, origin = atomic_store
    barrier = threading.Barrier(2)

    def insert():
        independent = Database(database.path)
        attempt = replace(origin, reservation_id=uuid4())
        temporary = database.path.parent / f"{attempt.reservation_id}.zip"
        try:
            temporary.write_bytes(b"attempt project")
            with independent.connect() as connection:
                connection.execute(
                    "INSERT INTO app_staging_reservations VALUES (?, ?, ?, ?)",
                    (
                        str(attempt.reservation_id),
                        attempt.app_id,
                        str(attempt.owner_user_id),
                        json.dumps([str(temporary)]),
                    ),
                )
            barrier.wait()
            return (
                JobRepository(independent)
                .add_group(
                    native_group(), "{}", None, str(origin.owner_user_id), attempt
                )[0]
                .id
            )
        finally:
            independent.close()

    with ThreadPoolExecutor(2) as pool:
        ids = list(pool.map(lambda _: insert(), range(2)))
    assert ids[0] == ids[1]
    assert len(list(database.path.parent.glob("*.zip"))) == 1
    with database.connect() as connection:
        assert [
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("job_groups", "jobs", "app_runs")
        ] == [1, 2, 1]
        assert (
            connection.execute("SELECT idempotency_key FROM job_groups").fetchone()[0]
            is None
        )
    changed = AppOrigin(
        origin.app_id,
        origin.owner_user_id,
        origin.installation_id,
        origin.key,
        "b" * 64,
        "{}",
        uuid4(),
    )
    with pytest.raises(AppReplayConflict):
        JobRepository(database).add_group(
            native_group(), "{}", None, str(origin.owner_user_id), changed
        )


def test_native_app_submission_requires_owned_reservation(atomic_store):
    database, origin = atomic_store
    with pytest.raises(ValueError, match="staging reservation"):
        JobRepository(database).add_group(
            native_group(),
            "{}",
            None,
            str(origin.owner_user_id),
            replace(origin, reservation_id=uuid4()),
        )
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM job_groups").fetchone()[0] == 0


SCRIPT = """#!/usr/bin/env bash
#HP --version 1
#HP --name report
#HP --runtime scientific-python:1
#HP --cpus 0.5
#HP --memory-mb 256
#HP --time-limit 00:01:00
#HP --array 1-2
#HP --input data
echo report > "$HOME_PLATFORM_OUTPUT_DIR/report.txt"
"""


@pytest.fixture
def api(identity_platform, tmp_path):
    platform = identity_platform
    definition = managed(state=str(tmp_path / "state"))
    definition["capabilities"].append("files")
    with platform.database.connect() as connection:
        connection.execute(
            "UPDATE app_registrations SET definition_json = ? WHERE app_id = 'notes'",
            (json.dumps(definition),),
        )
    archive = tmp_path / "report.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("submit.hp", SCRIPT)
    release = manifest()
    release["capabilities"].append("files")
    release["workloads"][0].update(
        sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), entrypoint="submit.hp"
    )
    receipt = platform.registry.ingest_release("notes", document(release))
    workloads = WorkloadStore(platform.database)
    installed = workloads.install("notes", receipt.manifest_sha256, "report", archive)
    app = create_app(platform.database.path)
    with TestClient(app) as client:
        app.state.app_identity = platform.identity
        app.state.settings = replace(
            app.state.settings,
            upload_directory=tmp_path / "uploads",
            artifact_directory=tmp_path / "results",
            storage_directory=tmp_path / "storage",
            artifact_requires_mount=False,
            artifact_owner_scoped=True,
            member_storage_user_ids=frozenset({platform.user.id}),
        )
        service = app.state.job_service
        service.heartbeat(
            WorkerHeartbeat(worker_id="disposable", supported_types=[JobType.BATCH])
        )
        service.set_worker_capacity(
            "disposable", WorkerCapacityUpdate(max_job_cpu=1, max_job_memory_mb=512)
        )
        service.set_worker_enabled("disposable", True)
        headers = {
            "Authorization": bearer(platform),
            "X-Platform-User-Context": assertion(platform),
        }
        yield SimpleNamespace(
            client=client,
            app=app,
            headers=headers,
            platform=platform,
            installed=installed,
            workloads=workloads,
            archive=archive,
            release=release,
            tmp=tmp_path,
        )


def test_workloads_name_active_installed_release_with_retained_history(api):
    url = "/api/apps/v1/workloads"
    assert api.client.get(url, headers=api.headers).json() == {
        "active_release_sha256": None,
        "items": [api.installed.public()],
    }

    newer = json.loads(json.dumps(api.release))
    newer["source_commit"] = "d" * 40
    receipt = api.platform.registry.ingest_release("notes", document(newer))
    retained = api.workloads.install(
        "notes", receipt.manifest_sha256, "report", api.archive
    )

    newest_candidate = json.loads(json.dumps(newer))
    newest_candidate["source_commit"] = "e" * 40
    api.platform.registry.ingest_release("notes", document(newest_candidate))

    registration = api.platform.registry.managed_app("notes").registration
    with api.platform.database.connect() as connection:
        for generation, environment, digest in (
            (1, "acceptance", api.installed.release_sha256),
            (2, "acceptance", retained.release_sha256),
            (3, "other", "f" * 64),
        ):
            operation_id = str(uuid4())
            connection.execute(
                "INSERT INTO app_operations(operation_id,request_id,app_id,"
                "environment,kind,state,operation_json) "
                "VALUES (?,?,'notes',?,'deploy','succeeded','{}')",
                (operation_id, str(uuid4()), environment),
            )
            connection.execute(
                "INSERT INTO app_installed_history VALUES (?,?,?,?,?)",
                (
                    operation_id,
                    "notes",
                    environment,
                    generation,
                    json.dumps(
                        {
                            "operation_id": operation_id,
                            "generation": generation,
                            "selection": {
                                "app_id": "notes",
                                "environment": environment,
                                "manifest_sha256": digest,
                                "archive_sha256": "a" * 64,
                                "registration_sha256": "b" * 64,
                                "policy_sha256": "c" * 64,
                                "credential_id": "d" * 32,
                                "generation": generation - 1,
                                "preview_id": str(uuid4()),
                            },
                            "registration": registration.model_dump(mode="json"),
                            "image": api.installed.release.image,
                            "image_id": "sha256:" + "b" * 64,
                        }
                    ),
                ),
            )

    result = api.client.get(url, headers=api.headers)
    assert result.status_code == 200
    assert result.json() == {
        "active_release_sha256": retained.release_sha256,
        "items": sorted(
            [api.installed.public(), retained.public()], key=lambda item: item["id"]
        ),
    }


def test_file_listing_preserves_file_size_and_nullable_directory_size(api):
    home = api.tmp / "storage" / "users" / str(api.platform.user.id)
    home.mkdir(parents=True)
    (home / "data.csv").write_bytes(b"x,y\n")
    (home / "Reports").mkdir()

    response = api.client.get("/api/apps/v1/files?path=Home", headers=api.headers)
    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "name": "Reports",
                "path": "Home/Reports",
                "kind": "directory",
                "size_bytes": None,
            },
            {
                "name": "data.csv",
                "path": "Home/data.csv",
                "kind": "file",
                "size_bytes": 4,
            },
        ]
    }


def post_upload(api, content=b"input bytes", headers=None):
    return api.client.post(
        f"/api/apps/v1/workloads/{api.installed.id}/uploads",
        headers={
            **(headers or api.headers),
            "Content-Type": "application/octet-stream",
        },
        content=content,
    )


def upload(api, content=b"input bytes", headers=None):
    response = post_upload(api, content, headers)
    assert response.status_code == 201, response.text
    return response.json()


def submit(api, item, key="run-key", headers=None, **changes):
    body = {
        "installation_id": str(api.installed.id),
        "inputs": {"data": item["id"]},
        **changes,
    }
    return api.client.post(
        "/api/apps/v1/runs",
        headers={**(headers or api.headers), "Idempotency-Key": key},
        json=body,
    )


def member_headers(api, name):
    user = api.platform.accounts.create(
        UserCreate(username=name, password="long test password")
    )
    login = name + "@example.test"
    api.platform.accounts.link_external_identity(user.id, "tailscale", login, name)
    token = api.platform.identity.admit(
        api.platform.registry.managed_app("notes"), request(login=login)
    )
    return {**api.headers, "X-Platform-User-Context": token}


def test_http_retention_cancellation_and_replay_after_cleanup(api):
    item = upload(api)
    created = submit(api, item)
    assert created.status_code == 201, created.text
    run = created.json()
    assert run["status"] == "QUEUED"
    assert run["tasks"] == [
        {"index": 0, "status": "QUEUED"},
        {"index": 1, "status": "QUEUED"},
    ]
    url = f"/api/apps/v1/runs/{run['id']}"
    cancelled = api.client.post(url + "/cancel", headers=api.headers)
    assert cancelled.json()["status"] == "CANCELLED"
    assert (
        api.client.post(url + "/cancel", headers=api.headers).json() == cancelled.json()
    )
    assert not list((api.tmp / "uploads/projects").glob("*"))
    assert not list((api.tmp / "uploads/inputs").glob("*"))
    assert (api.workloads.root / "inputs" / item["id"]).read_bytes() == b"input bytes"
    assert (api.workloads.root / "workloads" / str(api.installed.object_key)).is_file()
    api.app.state.job_service.set_worker_capacity(
        "disposable", WorkerCapacityUpdate(max_job_cpu=0.25, max_job_memory_mb=256)
    )
    assert submit(api, item).status_code == 200
    assert submit(api, {"id": str(uuid4())}).status_code == 409
    assert api.client.get(url + "/results", headers=api.headers).status_code == 409
    with api.platform.database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM app_runs").fetchone()[0] == 1
        value = dict(connection.execute("SELECT * FROM app_runs").fetchone())
    assert json.loads(value["inputs_json"])["data"]["sha256"] == item["sha256"]
    assert "assertion" not in json.dumps(value)


def test_queued_staging_cap_replay_and_terminal_slot_reuse(api, monkeypatch):
    monkeypatch.setattr("app.app_compute.MAX_OWNER_OUTSTANDING_RUNS", 4)
    item = upload(api)
    runs = [submit(api, item, key=f"queued-{index}") for index in range(4)]
    assert [response.status_code for response in runs] == [201] * 4
    files = set((api.tmp / "uploads").rglob("*"))
    assert submit(api, item, key="overflow").status_code == 409
    assert submit(api, item, key="queued-0").status_code == 200
    assert set((api.tmp / "uploads").rglob("*")) == files
    cancelled = api.client.post(
        f"/api/apps/v1/runs/{runs[0].json()['id']}/cancel", headers=api.headers
    )
    assert cancelled.json()["status"] == "CANCELLED"
    assert submit(api, item, key="after-cancel").status_code == 201
    assert submit(api, item, key="overflow").status_code == 409
    with api.platform.database.connect() as connection:
        connection.execute(
            "UPDATE jobs SET status = 'COMPLETED' WHERE group_id = ?",
            (runs[1].json()["id"],),
        )
    assert submit(api, item, key="after-complete").status_code == 201


def test_concurrent_admission_reserves_before_any_copy(api, monkeypatch):
    monkeypatch.setattr("app.app_compute.MAX_OWNER_OUTSTANDING_RUNS", 4)
    item = upload(api)
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    copies = 0
    original = WorkloadStore.copy_project

    def blocked_copy(*args, **kwargs):
        nonlocal copies
        with lock:
            copies += 1
            if copies == 4:
                entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(WorkloadStore, "copy_project", blocked_copy)
    with ThreadPoolExecutor(8) as pool:
        pending = [pool.submit(submit, api, item, f"race-{i}") for i in range(8)]
        try:
            assert entered.wait(10)
            with api.platform.database.connect() as connection:
                assert (
                    connection.execute(
                        "SELECT count(*) FROM app_staging_reservations"
                    ).fetchone()[0]
                    == 4
                )
            assert copies == 4
            assert not list((api.tmp / "uploads").rglob("*.zip"))
        finally:
            release.set()
        statuses = [future.result().status_code for future in pending]
    assert sorted(statuses) == [201] * 4 + [409] * 4
    with api.platform.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM app_staging_reservations"
            ).fetchone()[0]
            == 0
        )


def test_failed_staging_releases_slot_and_partial_copies(api, monkeypatch):
    item = upload(api)
    original = WorkloadStore.copy_project

    def fail_copy(store, installed, path, project_id):
        path.write_bytes(b"interrupted copy")
        raise OSError("injected copy failure")

    monkeypatch.setattr(WorkloadStore, "copy_project", fail_copy)
    assert submit(api, item).status_code == 409
    assert not list((api.tmp / "uploads").rglob("*.zip"))
    with api.platform.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM app_staging_reservations"
            ).fetchone()[0]
            == 0
        )
    monkeypatch.setattr(WorkloadStore, "copy_project", original)
    assert submit(api, item).status_code == 201


def test_native_commit_failure_releases_staging(api):
    item = upload(api)
    with api.platform.database.connect() as connection:
        connection.execute(
            "CREATE TRIGGER fail_staged_origin BEFORE INSERT ON app_runs BEGIN "
            "SELECT RAISE(ABORT, 'injected failure'); END"
        )
    assert submit(api, item).status_code == 503
    assert not [path for path in (api.tmp / "uploads").rglob("*") if path.is_file()]
    with api.platform.database.connect() as connection:
        assert [
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("app_staging_reservations", "app_runs", "job_groups")
        ] == [0, 0, 0]
        connection.execute("DROP TRIGGER fail_staged_origin")
    assert submit(api, item).status_code == 201


def test_restart_removes_only_interrupted_staging(api):
    from app.app_compute import reconcile_app_inputs

    item = upload(api)
    assert submit(api, item).status_code == 201
    committed = {
        path: path.read_bytes()
        for path in (api.tmp / "uploads").rglob("*")
        if path.is_file()
    }
    interrupted = api.tmp / "uploads" / "inputs" / f"{uuid4()}.input"
    interrupted.write_bytes(b"partial input")
    with api.platform.database.connect() as connection:
        connection.execute(
            "INSERT INTO app_staging_reservations VALUES (?, ?, ?, ?)",
            (str(uuid4()), "notes", "owner", json.dumps([str(interrupted)])),
        )
    independent = Database(api.platform.database.path)
    try:
        for _ in range(2):
            reconcile_app_inputs(independent)
        assert not interrupted.exists()
        assert all(path.read_bytes() == data for path, data in committed.items())
        assert (
            api.workloads.root / "inputs" / item["id"]
        ).read_bytes() == b"input bytes"
        with independent.connect() as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM app_staging_reservations"
                ).fetchone()[0]
                == 0
            )
    finally:
        independent.close()


def test_aggregate_run_inputs_include_repeated_ids(api, monkeypatch):
    import app.app_compute as compute

    item = upload(api, b"sixsix")
    installed = api.installed
    with api.platform.database.connect() as connection:
        connection.execute("DROP TRIGGER app_workloads_no_update")
        connection.execute(
            "UPDATE app_workloads SET inputs_json = ? WHERE id = ?",
            (json.dumps(["data", "again"]), str(installed.id)),
        )
    monkeypatch.setattr(compute, "MAX_OWNER_INPUT_BYTES", 10)
    response = submit(api, item, inputs={"data": item["id"], "again": item["id"]})
    assert response.status_code == 409
    assert response.json()["detail"] == "Run input staging quota exceeded"
    assert not list((api.tmp / "uploads").rglob("*.zip"))


def test_http_owner_app_version_scope_and_unknown_fields(api):
    item = upload(api)
    created = submit(api, item)
    assert created.status_code == 201
    run_id = created.json()["id"]
    other = api.platform.accounts.create(
        UserCreate(username="bob", password="long test password")
    )
    api.platform.accounts.link_external_identity(
        other.id, "tailscale", "bob@example.test", "Bob"
    )
    token = api.platform.identity.admit(
        api.platform.registry.managed_app("notes"), request(login="bob@example.test")
    )
    foreign = {**api.headers, "X-Platform-User-Context": token}
    assert (
        api.client.get(f"/api/apps/v1/runs/{run_id}", headers=foreign).status_code
        == 404
    )
    assert (
        api.client.post(
            f"/api/apps/v1/runs/{run_id}/cancel", headers=foreign
        ).status_code
        == 404
    )
    assert api.client.get("/api/apps/v1/runs", headers=foreign).json() == {"items": []}
    assert (
        api.client.post(
            "/api/apps/v1/runs",
            headers={**foreign, "Idempotency-Key": "foreign"},
            json={
                "installation_id": str(api.installed.id),
                "inputs": {"data": item["id"]},
            },
        ).status_code
        == 404
    )
    assert submit(api, item, key="bad", owner=str(other.id)).status_code == 422
    changed = {**api.release, "source_commit": "c" * 40}
    receipt = api.platform.registry.ingest_release("notes", document(changed))
    new = api.workloads.install("notes", receipt.manifest_sha256, "report", api.archive)
    assert new.id != api.installed.id
    assert (
        submit(api, item, key="wrong-version", installation_id=str(new.id)).status_code
        == 404
    )
    denied = api.client.get(f"/api/apps/v1/runs/{run_id}")
    assert denied.status_code == 401
    assert denied.headers["cache-control"] == "no-store"
    assert (
        api.client.get("/api/apps/v1/runs?limit=101", headers=api.headers).status_code
        == 422
    )


def test_http_logical_snapshot_rechecks_entitlement_and_fingerprint(api):
    home = api.tmp / "storage" / "users" / str(api.platform.user.id)
    home.mkdir(parents=True)
    source = home / "data.csv"
    source.write_bytes(b"a,b\n1,2\n")
    path = f"/api/apps/v1/workloads/{api.installed.id}/file-handles"
    chosen = api.client.post(path, headers=api.headers, json={"path": "Home/data.csv"})
    assert chosen.status_code == 201, chosen.text
    api.app.state.settings = replace(
        api.app.state.settings, member_storage_user_ids=frozenset()
    )
    assert submit(api, chosen.json(), key="denied").status_code == 403
    api.app.state.settings = replace(
        api.app.state.settings,
        member_storage_user_ids=frozenset({api.platform.user.id}),
    )
    source.write_bytes(b"a,b\n3,4\n")
    assert submit(api, chosen.json(), key="changed").status_code == 409
    chosen = api.client.post(
        path, headers=api.headers, json={"path": "Home/data.csv"}
    ).json()
    created = submit(api, chosen, key="snapshot")
    assert created.status_code == 201
    source.write_bytes(b"later bytes")
    group = api.app.state.job_service.get_group(UUID(created.json()["id"]))
    reference = group.tasks[0].parameters.inputs["data"]
    assert (
        api.tmp / "uploads/inputs" / f"{reference.upload_id}.input"
    ).read_bytes() == b"a,b\n3,4\n"
    source.unlink()
    source.symlink_to(api.archive)
    assert (
        api.client.post(
            path, headers=api.headers, json={"path": "Home/data.csv"}
        ).status_code
        == 404
    )
    assert (
        api.client.post(
            path, headers=api.headers, json={"path": "Home/../secret"}
        ).status_code
        == 422
    )


def complete_run(api, run_id):
    service = api.app.state.job_service
    group = service.get_group(UUID(run_id))
    for task in group.tasks:
        claimed = service.claim(
            WorkerClaim(worker_id="disposable", supported_types=[JobType.BATCH])
        )
        assert claimed is not None
        directory = (
            api.tmp
            / "results"
            / str(api.platform.user.id)
            / job_artifact_relative_path(claimed, group_name=group.name)
        )
        directory.mkdir(parents=True)
        (directory / "report.txt").write_bytes(b"complete report")
        result = BatchResult(
            project_sha256=api.installed.workload.sha256,
            exit_code=0,
            stdout="private logs",
            stderr="",
            output_files=["report.txt"],
            artifact_uri=f"worker://disposable/{task.id}/",
        )
        service.complete(
            claimed.id,
            JobCompletion(
                worker_id="disposable", lease_token=claimed.lease_token, result=result
            ),
        )
    return group


def test_http_results_require_exact_complete_publication_and_current_digest(api):
    run_id = submit(api, upload(api)).json()["id"]
    url = f"/api/apps/v1/runs/{run_id}"
    assert api.client.get(url + "/results", headers=api.headers).status_code == 409
    group = complete_run(api, run_id)
    status = api.client.get(url, headers=api.headers)
    assert status.json()["status"] == "COMPLETED"
    assert "private logs" not in status.text and "worker" not in status.text
    response = api.client.get(url + "/results", headers=api.headers)
    assert response.status_code == 200, response.text
    outputs = response.json()["outputs"]
    assert len(outputs) == 2
    downloaded = api.client.get(outputs[0]["download_url"], headers=api.headers)
    assert downloaded.content == b"complete report"
    directory = (
        api.tmp
        / "results"
        / str(api.platform.user.id)
        / job_artifact_relative_path(group.tasks[0], group_name=group.name)
    )
    (directory / "report.txt").write_bytes(b"changed report")
    assert (
        api.client.get(outputs[0]["download_url"], headers=api.headers).status_code
        == 409
    )
    (directory / "extra.txt").write_bytes(b"extra")
    assert api.client.get(url + "/results", headers=api.headers).status_code == 409
    assert (
        api.client.get(outputs[1]["download_url"], headers=api.headers).status_code
        == 409
    )
    assert api.client.get(url, headers=api.headers).json()["status"] == "COMPLETED"


def test_http_limits_grants_and_installer_retry(api):
    assert (
        api.workloads.install(
            "notes", api.installed.release_sha256, "report", api.archive
        ).id
        == api.installed.id
    )
    assert api.client.get("/api/apps/v1/workloads", headers=api.headers).json()[
        "items"
    ] == [api.installed.public()]
    response = api.client.post(
        f"/api/apps/v1/workloads/{api.installed.id}/uploads",
        headers={**api.headers, "Content-Type": "application/octet-stream"},
        content=b"x" * (MAX_INPUT_BYTES + 1),
    )
    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"
    assert not list((api.workloads.root / "inputs").glob("*"))
    with api.platform.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM app_input_reservations"
            ).fetchone()[0]
            == 0
        )
    item = upload(api)
    created = submit(api, item)
    definition = managed(state=str(api.tmp / "state"))
    definition["capabilities"] = ["identity"]
    with api.platform.database.connect() as connection:
        connection.execute(
            "UPDATE app_registrations SET definition_json = ? WHERE app_id = 'notes'",
            (json.dumps(definition),),
        )
    assert submit(api, item).status_code == 403
    assert (
        api.client.get(
            f"/api/apps/v1/runs/{created.json()['id']}", headers=api.headers
        ).status_code
        == 403
    )


@pytest.mark.parametrize("mode", ["worker", "duplicate", "symlink", "unsafe"])
def test_installer_rejects_unsafe_archive_even_with_matching_digest(api, mode):
    archive = api.tmp / f"{mode}.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr(
            "submit.hp",
            SCRIPT.replace("#HP --array", "#HP --worker private-worker\n#HP --array")
            if mode == "worker"
            else SCRIPT,
        )
        if mode == "duplicate":
            with pytest.warns(UserWarning):
                package.writestr("submit.hp", SCRIPT)
        if mode == "unsafe":
            package.writestr("../bad", b"bad")
        if mode == "symlink":
            entry = zipfile.ZipInfo("link")
            entry.external_attr = 0o120777 << 16
            package.writestr(entry, "/etc/passwd")
    release = json.loads(json.dumps(api.release))
    release["workloads"][0]["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    receipt = api.platform.registry.ingest_release("notes", document(release))
    with pytest.raises(ContractError):
        api.workloads.install("notes", receipt.manifest_sha256, "report", archive)
    assert len(api.workloads.list("notes")) == 1


def test_administrator_binding_cannot_read_another_app_run(api):
    created = submit(api, upload(api))
    admin = api.platform.accounts.get_identity(UUID(ADMIN_USER_ID))
    version = api.platform.accounts.session_version(admin.id)
    cookie = encode_session(admin, "admin-key", int(time.time()) + 1000, version)
    headers = {
        "Authorization": bearer(api.platform, "admin-notes"),
        "X-Platform-User-Context": assertion(api.platform, "admin-notes", cookie),
    }
    context = api.platform.identity.verify(
        headers["Authorization"], headers["X-Platform-User-Context"]
    )
    assert context.administrator.account_id == admin.id
    assert context.user.id == api.platform.user.id
    run = f"/api/apps/v1/runs/{created.json()['id']}"
    for suffix in ("", "/results"):
        assert api.client.get(run + suffix, headers=headers).status_code == 404
    assert api.client.post(run + "/cancel", headers=headers).status_code == 404


def test_quota_reservations_are_atomic_between_connections(api, monkeypatch):
    monkeypatch.setattr("app.app_compute.MAX_OWNER_INPUT_BYTES", MAX_INPUT_BYTES)
    context = api.platform.identity.verify(
        api.headers["Authorization"], api.headers["X-Platform-User-Context"]
    )
    barrier = threading.Barrier(2)

    def reserve():
        domain = AppCompute(api.app.state.job_service, api.app.state.settings)
        try:
            barrier.wait()
            return domain.reserve_input(context, api.installed.id)
        except ComputeError as error:
            return error.status
        finally:
            domain.database.close()

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(lambda _: reserve(), range(2)))
    assert sum(isinstance(item, UUID) for item in outcomes) == 1
    assert outcomes.count(413) == 1
    with api.platform.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT sum(size_bytes) FROM app_input_reservations"
            ).fetchone()[0]
            == MAX_INPUT_BYTES
        )


def test_app_provenance_rejects_replace_update_and_delete(api):
    item = upload(api)
    assert submit(api, item).status_code == 201
    with api.platform.database.connect() as connection:
        for table in ("app_workloads", "app_inputs", "app_runs"):
            for sql in (
                f"DELETE FROM {table}",
                f"UPDATE {table} SET app_id = 'changed'",
                f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}",
            ):
                with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                    connection.execute(sql)
            assert (
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
            )


def test_migration_19_failure_rolls_back_all_tables_and_retries(tmp_path, monkeypatch):
    database = Database(tmp_path / "migration.db")
    current = migrations.MIGRATIONS
    with monkeypatch.context() as old:
        old.setattr(migrations, "MIGRATIONS", current[:18])
        database.initialize()

    class FailRuns:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement, *args):
            if "CREATE TABLE app_runs" in statement:
                raise sqlite3.OperationalError("injected schema failure")
            return self.connection.execute(statement, *args)

    with monkeypatch.context() as failing:
        failing.setattr(
            migrations,
            "MIGRATIONS",
            (
                *current[:18],
                (
                    19,
                    lambda connection: migrations.add_app_compute(FailRuns(connection)),
                ),
            ),
        )
        with pytest.raises(sqlite3.OperationalError, match="injected schema"):
            database.initialize()
    with database.connect() as connection:
        assert (
            connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[
                0
            ]
            == 18
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'app_workloads'"
            ).fetchone()
            is None
        )
    database.initialize()
    with database.connect() as connection:
        assert (
            connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[
                0
            ]
            == migrations.MIGRATIONS[-1][0]
        )
    database.close()


def test_failed_input_directory_creation_releases_quota(api, monkeypatch):
    directory = api.workloads.root / "inputs"
    original = Path.mkdir

    def fail_directory(path, *args, **kwargs):
        if path == directory:
            raise PermissionError("injected directory failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_directory)
    response = api.client.post(
        f"/api/apps/v1/workloads/{api.installed.id}/uploads",
        headers={**api.headers, "Content-Type": "application/octet-stream"},
        content=b"data",
    )
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    with api.platform.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM app_input_reservations"
            ).fetchone()[0]
            == 0
        )


def test_startup_reconciles_interrupted_uploads_and_preserves_committed_inputs(
    atomic_store,
):
    database, origin = atomic_store
    directory = database.path.parent / "app-compute" / "inputs"
    directory.mkdir(parents=True)
    committed, interrupted, orphan = uuid4(), uuid4(), uuid4()
    (directory / str(committed)).write_bytes(b"committed bytes")
    (directory / str(interrupted)).write_bytes(b"partial bytes")
    (directory / str(orphan)).write_bytes(b"orphan bytes")
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO app_inputs VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL)",
            (
                str(committed),
                origin.app_id,
                str(origin.owner_user_id),
                str(origin.installation_id),
                str(committed),
                15,
                hashlib.sha256(b"committed bytes").hexdigest(),
            ),
        )
        connection.execute(
            "INSERT INTO app_input_reservations VALUES (?, ?, ?, ?)",
            (
                str(interrupted),
                origin.app_id,
                str(origin.owner_user_id),
                MAX_INPUT_BYTES,
            ),
        )
    database.close()
    for _ in range(2):
        with TestClient(create_app(database.path)) as client:
            assert client.get("/health").status_code == 200
            assert (directory / str(committed)).read_bytes() == b"committed bytes"
            assert not (directory / str(interrupted)).exists()
            assert not (directory / str(orphan)).exists()
            with database.connect() as connection:
                assert (
                    connection.execute(
                        "SELECT count(*) FROM app_input_reservations"
                    ).fetchone()[0]
                    == 0
                )
                assert connection.execute("SELECT id FROM app_inputs").fetchone()[
                    0
                ] == str(committed)


def test_one_owner_cannot_take_every_run_slot(api):
    item = upload(api)
    statuses = [submit(api, item, key=f"own-{index}").status_code for index in range(3)]
    assert statuses == [201, 201, 409]
    bob = member_headers(api, "bob")
    theirs = upload(api, headers=bob)
    assert [
        submit(api, theirs, key=f"bob-{index}", headers=bob).status_code
        for index in range(3)
    ] == [201, 201, 409]
    carol = member_headers(api, "carol")
    assert submit(api, upload(api, headers=carol), headers=carol).status_code == 409


def test_logical_handles_charge_no_bytes_and_are_count_bounded(api, monkeypatch):
    monkeypatch.setattr("app.app_compute.MAX_OWNER_INPUT_BYTES", MAX_INPUT_BYTES)
    home = api.tmp / "storage" / "users" / str(api.platform.user.id)
    home.mkdir(parents=True)
    (home / "data.csv").write_bytes(b"a,b\n1,2\n")
    path = f"/api/apps/v1/workloads/{api.installed.id}/file-handles"
    responses = [
        api.client.post(path, headers=api.headers, json={"path": "Home/data.csv"})
        for _ in range(101)
    ]
    assert [response.status_code for response in responses] == [201] * 100 + [413]
    upload(api)
    handle = responses[0].json()["id"]
    deleted = api.client.delete(f"/api/apps/v1/inputs/{handle}", headers=api.headers)
    assert deleted.status_code == 204
    again = api.client.post(path, headers=api.headers, json={"path": "Home/data.csv"})
    assert again.status_code == 201


def test_owner_deletes_upload_bytes_and_keeps_tombstoned_provenance(api, monkeypatch):
    from app.app_compute import reconcile_app_inputs

    monkeypatch.setattr("app.app_compute.MAX_OWNER_INPUT_BYTES", MAX_INPUT_BYTES)
    item = upload(api)
    assert submit(api, item).status_code == 201
    staged = sorted((api.tmp / "uploads" / "inputs").iterdir())
    assert post_upload(api).status_code == 413
    url = f"/api/apps/v1/inputs/{item['id']}"
    assert api.client.delete(url, headers=member_headers(api, "bob")).status_code == 404
    deleted = api.client.delete(url, headers=api.headers)
    assert deleted.status_code == 204
    assert deleted.headers["Cache-Control"] == "no-store"
    stored = api.workloads.root / "inputs" / item["id"]
    assert not stored.exists()
    assert sorted((api.tmp / "uploads" / "inputs").iterdir()) == staged
    assert api.client.delete(url, headers=api.headers).status_code == 204
    assert submit(api, item, key="after-delete").status_code == 404
    assert submit(api, item).status_code == 200
    upload(api, b"replacement")
    stored.write_bytes(b"left by a crash")
    reconcile_app_inputs(api.platform.database)
    assert not stored.exists()
    with api.platform.database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM app_inputs WHERE id = ?", (item["id"],)
        ).fetchone()
        assert row["deleted_at"] is not None and row["sha256"] == item["sha256"]
        for sql in (
            "UPDATE app_inputs SET deleted_at = NULL WHERE id = ?",
            "UPDATE app_inputs SET deleted_at = 'later' WHERE id = ?",
            "DELETE FROM app_inputs WHERE id = ?",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(sql, (item["id"],))
        live = connection.execute(
            "SELECT id FROM app_inputs WHERE deleted_at IS NULL"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE app_inputs SET deleted_at = 'now', size_bytes = 1 WHERE id = ?",
                (live,),
            )


def test_downloads_hash_only_the_requested_output_after_one_manifest(api, monkeypatch):
    from app import app_results

    run_id = submit(api, upload(api)).json()["id"]
    group = complete_run(api, run_id)
    hashed = []
    original = app_results.copy_digest

    def counted(*args, **kwargs):
        hashed.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(app_results, "copy_digest", counted)
    url = f"/api/apps/v1/runs/{run_id}/results"
    outputs = api.client.get(url, headers=api.headers).json()["outputs"]
    assert len(outputs) == 2 and len(hashed) == 2
    for _ in range(3):
        response = api.client.get(outputs[0]["download_url"], headers=api.headers)
        assert response.content == b"complete report"
    assert len(hashed) == 5
    directory = (
        api.tmp
        / "results"
        / str(api.platform.user.id)
        / job_artifact_relative_path(group.tasks[1], group_name=group.name)
    )
    (directory / "report.txt").write_bytes(b"replaced report")
    assert api.client.get(url, headers=api.headers).json()["outputs"] == outputs
    assert len(hashed) == 5
    assert (
        api.client.get(outputs[1]["download_url"], headers=api.headers).status_code
        == 409
    )
