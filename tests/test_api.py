import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.artifacts import run_directory_name
from app.identity import ADMIN_USER_ID
from app.main import create_app


def create_sleep_job(
    client: TestClient, seconds: int = 1, name: str | None = None
) -> dict:
    response = client.post(
        "/jobs",
        json={"name": name, "type": "sleep", "parameters": {"seconds": seconds}},
    )
    assert response.status_code == 201
    return response.json()


def claim_job(client: TestClient, worker_id: str = "mac-one") -> dict | None:
    response = client.post(
        "/workers/claim",
        json={"worker_id": worker_id, "supported_types": ["sleep"]},
    )
    assert response.status_code == 200
    return response.json()


def enable_worker(
    client: TestClient,
    worker_id: str = "mac-one",
    supported_types: list[str] | None = None,
) -> None:
    """Register a worker and turn its scheduling on.

    Workers register with scheduling disabled, so any test that expects a
    claim to succeed has to enable the worker first.
    """
    register_worker_capacity(client, worker_id, supported_types)
    update = client.patch(f"/workers/{worker_id}", json={"enabled": True})
    assert update.status_code == 200


def register_worker_capacity(
    client: TestClient,
    worker_id: str,
    supported_types: list[str] | None = None,
    *,
    max_job_cpu: float = 4,
    max_job_memory_mb: int = 4096,
) -> None:
    registration = client.post(
        "/workers/heartbeat",
        json={
            "worker_id": worker_id,
            "supported_types": supported_types or ["sleep"],
        },
    )
    assert registration.status_code == 200
    assert registration.json() == {"cancellation_requested": False}
    capacity = client.put(
        f"/workers/{worker_id}/capacity",
        json={
            "max_job_cpu": max_job_cpu,
            "max_job_memory_mb": max_job_memory_mb,
        },
    )
    assert capacity.status_code == 200


def test_liveness_and_readiness(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        health = client.get("/health")
        ready = client.get("/ready")

    assert health.json() == {"status": "healthy"}
    assert ready.json() == {"status": "ready"}


def test_readiness_reports_database_failure(tmp_path: Path) -> None:
    class UnavailableService:
        def ping(self) -> None:
            raise sqlite3.OperationalError("database unavailable")

    app = create_app(tmp_path / "jobs.db")
    with TestClient(app) as client:
        app.state.job_service = UnavailableService()
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "Database is unavailable"}


def test_create_and_get_job(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created = create_sleep_job(client, seconds=2, name="  Nightly check  ")
        response = client.get(f"/jobs/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created
    assert created["name"] == "Nightly check"


def test_job_name_is_optional_validated_and_persists(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.db"
    with TestClient(create_app(database_path)) as client:
        unnamed = create_sleep_job(client)
        named = create_sleep_job(client, name="Iris summary / run 02")
        empty = client.post(
            "/jobs",
            json={"name": "   ", "type": "sleep", "parameters": {"seconds": 1}},
        )
        control = client.post(
            "/jobs",
            json={
                "name": "bad\nname",
                "type": "sleep",
                "parameters": {"seconds": 1},
            },
        )
    with TestClient(create_app(database_path)) as restarted:
        preserved = restarted.get(f"/jobs/{named['id']}")

    assert unnamed["name"] is None
    assert named["name"] == "Iris summary / run 02"
    assert empty.status_code == 422
    assert control.status_code == 422
    assert preserved.json()["name"] == "Iris summary / run 02"


def test_completed_job_survives_application_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.db"
    with TestClient(create_app(database_path)) as first_client:
        created = create_sleep_job(first_client)
        enable_worker(first_client)
        claimed = claim_job(first_client)
        assert claimed is not None
        completed = first_client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "mac-one",
                "lease_token": claimed["lease_token"],
                "result": {"slept_seconds": 1},
            },
        )
        assert completed.status_code == 200

    with TestClient(create_app(database_path)) as restarted_client:
        response = restarted_client.get(f"/jobs/{created['id']}")

    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"
    assert response.json()["result"] == {"slept_seconds": 1}


def test_validation_and_missing_job_responses(tmp_path: Path) -> None:
    missing_job_id = uuid4()
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        invalid = client.post(
            "/jobs",
            json={"type": "sleep", "parameters": {"seconds": 301}},
        )
        missing = client.get(f"/jobs/{missing_job_id}")
        malformed = client.get("/jobs/not-a-uuid")

    assert invalid.status_code == 422
    assert missing.status_code == 404
    assert malformed.status_code == 422


def test_job_can_target_a_registered_worker(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        missing = client.post(
            "/jobs",
            json={
                "type": "sleep",
                "target_worker_id": "not-registered",
                "parameters": {"seconds": 1},
            },
        )
        enable_worker(client, "windows-primary")
        targeted = client.post(
            "/jobs",
            json={
                "type": "sleep",
                "target_worker_id": "windows-primary",
                "parameters": {"seconds": 1},
            },
        )
        wrong_claim = claim_job(client, "mac-primary")
        selected_claim = claim_job(client, "windows-primary")

    assert missing.status_code == 404
    assert targeted.status_code == 201
    assert targeted.json()["target_worker_id"] == "windows-primary"
    assert wrong_claim is None
    assert selected_claim is not None
    assert selected_claim["id"] == targeted.json()["id"]


def test_worker_capacity_is_validated_and_enforced(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        client.post(
            "/workers/heartbeat",
            json={
                "worker_id": "small-worker",
                "supported_types": ["python_batch"],
            },
        )
        configured = client.put(
            "/workers/small-worker/capacity",
            json={"max_job_cpu": 2, "max_job_memory_mb": 2048},
        )
        invalid = client.put(
            "/workers/small-worker/capacity",
            json={"max_job_cpu": 0, "max_job_memory_mb": 100},
        )
        missing = client.put(
            "/workers/absent/capacity",
            json={"max_job_cpu": 2, "max_job_memory_mb": 2048},
        )
        rejected = client.post(
            "/jobs",
            json={
                "type": "python_batch",
                "target_worker_id": "small-worker",
                "parameters": {
                    "script": {
                        "upload_id": str(uuid4()),
                        "sha256": "a" * 64,
                        "size_bytes": 10,
                    },
                    "dataset": {
                        "upload_id": str(uuid4()),
                        "sha256": "b" * 64,
                        "size_bytes": 10,
                    },
                    "cpu_limit": 4,
                    "memory_mb": 4096,
                },
            },
        )

    assert configured.status_code == 200
    assert configured.json()["max_job_cpu"] == 2
    assert configured.json()["max_job_memory_mb"] == 2048
    assert invalid.status_code == 422
    assert missing.status_code == 404
    assert rejected.status_code == 422
    assert "not configured to accept" in rejected.json()["detail"]


def test_python_batch_accepts_seven_day_timeout_and_rejects_longer(
    tmp_path: Path,
) -> None:
    parameters = {
        "script": {
            "upload_id": str(uuid4()),
            "sha256": "a" * 64,
            "size_bytes": 10,
        },
        "dataset": {
            "upload_id": str(uuid4()),
            "sha256": "b" * 64,
            "size_bytes": 10,
        },
        "cpu_limit": 1,
        "memory_mb": 1024,
        "timeout_seconds": 7 * 24 * 3600,
    }
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        register_worker_capacity(
            client,
            "batch-worker",
            supported_types=["python_batch"],
        )
        accepted = client.post(
            "/jobs",
            json={"type": "python_batch", "parameters": parameters},
        )
        rejected = client.post(
            "/jobs",
            json={
                "type": "python_batch",
                "parameters": {**parameters, "timeout_seconds": 7 * 24 * 3600 + 1},
            },
        )

    assert accepted.status_code == 201
    assert accepted.json()["parameters"]["timeout_seconds"] == 604800
    assert rejected.status_code == 422


def test_idempotency_key_returns_same_job_and_rejects_different_request(
    tmp_path: Path,
) -> None:
    headers = {"Idempotency-Key": "upload-batch-42"}
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        first = client.post(
            "/jobs",
            headers=headers,
            json={"type": "sleep", "parameters": {"seconds": 1}},
        )
        repeated = client.post(
            "/jobs",
            headers=headers,
            json={"type": "sleep", "parameters": {"seconds": 1}},
        )
        conflicting = client.post(
            "/jobs",
            headers=headers,
            json={"type": "sleep", "parameters": {"seconds": 2}},
        )
        renamed = client.post(
            "/jobs",
            headers=headers,
            json={
                "name": "different run",
                "type": "sleep",
                "parameters": {"seconds": 1},
            },
        )
        jobs = client.get("/jobs")

    assert first.status_code == 201
    assert repeated.status_code == 201
    assert repeated.json()["id"] == first.json()["id"]
    assert conflicting.status_code == 409
    assert renamed.status_code == 409
    assert len(jobs.json()) == 1


def test_one_group_expands_to_four_independently_claimed_tasks(
    tmp_path: Path,
) -> None:
    payload = {
        "name": "four-task queue test",
        "tasks": [
            {
                "task_id": f"task-{index + 1}",
                "job": {
                    "type": "sleep",
                    "parameters": {"seconds": 120},
                },
            }
            for index in range(4)
        ],
    }
    headers = {"Idempotency-Key": "four-task-acceptance"}
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created = client.post("/job-groups", headers=headers, json=payload)
        repeated = client.post("/job-groups", headers=headers, json=payload)
        conflicting = client.post(
            "/job-groups",
            headers=headers,
            json={**payload, "name": "different"},
        )
        enable_worker(client, "worker-a")
        enable_worker(client, "worker-b")

        first_a = claim_job(client, "worker-a")
        first_b = claim_job(client, "worker-b")
        assert first_a is not None
        assert first_b is not None
        assert first_a["id"] != first_b["id"]
        running = client.get(f"/job-groups/{created.json()['id']}")

        for worker_id, claimed in (("worker-a", first_a), ("worker-b", first_b)):
            completed = client.post(
                f"/jobs/{claimed['id']}/complete",
                json={
                    "worker_id": worker_id,
                    "lease_token": claimed["lease_token"],
                    "result": {"slept_seconds": 120},
                },
            )
            assert completed.status_code == 200

        second_a = claim_job(client, "worker-a")
        second_b = claim_job(client, "worker-b")
        assert second_a is not None
        assert second_b is not None
        claimed_ids = {first_a["id"], first_b["id"], second_a["id"], second_b["id"]}
        listed = client.get("/job-groups")

    body = created.json()
    assert created.status_code == 201
    assert repeated.status_code == 201
    assert repeated.json()["id"] == body["id"]
    assert conflicting.status_code == 409
    assert body["status"] == "QUEUED"
    assert [task["task_id"] for task in body["tasks"]] == [
        "task-1",
        "task-2",
        "task-3",
        "task-4",
    ]
    assert all(task["group_id"] == body["id"] for task in body["tasks"])
    assert running.json()["status"] == "RUNNING"
    assert len(claimed_ids) == 4
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == body["id"]


def test_job_group_rejects_duplicate_task_ids(tmp_path: Path) -> None:
    task = {
        "task_id": "same",
        "job": {"type": "sleep", "parameters": {"seconds": 1}},
    }
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        response = client.post(
            "/job-groups",
            json={"name": "invalid group", "tasks": [task, task]},
        )

    assert response.status_code == 422


def test_token_protects_client_and_worker_operations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        missing_token = client.get("/jobs")
        wrong_token = client.get("/jobs", headers={"X-API-Token": "wrong-secret"})
        accepted = client.get("/jobs", headers={"X-API-Token": "test-secret"})
        worker_without_token = client.post(
            "/workers/claim",
            json={"worker_id": "mac-one", "supported_types": ["sleep"]},
        )

    assert missing_token.status_code == 401
    assert wrong_token.status_code == 401
    assert accepted.status_code == 200
    assert worker_without_token.status_code == 401


def test_health_does_not_require_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        response = client.get("/health")
    assert response.status_code == 200


def test_token_can_be_loaded_from_file(tmp_path: Path, monkeypatch) -> None:
    token_file = tmp_path / "api-token"
    token_file.write_text("file-secret\n")
    monkeypatch.delenv("HOME_PLATFORM_API_TOKEN", raising=False)
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN_FILE", str(token_file))
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        response = client.get("/jobs", headers={"X-API-Token": "file-secret"})
    assert response.status_code == 200


def test_explicit_empty_token_file_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    token_file = tmp_path / "api-token"
    token_file.write_text("")
    monkeypatch.delenv("HOME_PLATFORM_API_TOKEN", raising=False)
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN_FILE", str(token_file))

    with pytest.raises(RuntimeError, match="required but empty"):
        create_app(tmp_path / "jobs.db")


def test_worker_claims_and_completes_oldest_job(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        first = create_sleep_job(client, seconds=2)
        create_sleep_job(client, seconds=3)
        enable_worker(client)
        running = claim_job(client)

        assert running is not None
        assert running["id"] == first["id"]
        assert running["status"] == "RUNNING"
        assert running["worker_id"] == "mac-one"
        assert running["started_at"] is not None

        completion = client.post(
            f"/jobs/{first['id']}/complete",
            json={
                "worker_id": "mac-one",
                "lease_token": running["lease_token"],
                "result": {"slept_seconds": 2},
            },
        )

    assert completion.status_code == 200
    assert completion.json()["status"] == "COMPLETED"
    assert completion.json()["result"] == {"slept_seconds": 2}


def test_empty_queue_returns_null_claim(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        enable_worker(client)
        response = client.post(
            "/workers/claim",
            json={"worker_id": "mac-one", "supported_types": ["sleep"]},
        )
    assert response.status_code == 200
    assert response.json() is None


def test_retired_dataset_script_cannot_be_submitted(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created_response = client.post(
            "/jobs",
            json={
                "type": "dataset_script",
                "parameters": {
                    "script": "csv_summary",
                    "dataset": {
                        "url": "https://datasets.example/input.csv",
                        "sha256": "a" * 64,
                        "size_bytes": 100,
                    },
                    "timeout_seconds": 60,
                },
            },
        )

    assert created_response.status_code == 422


def test_python_batch_contract_claim_and_completion(tmp_path: Path) -> None:
    script_id = uuid4()
    dataset_id = uuid4()
    sha256 = "a" * 64
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        register_worker_capacity(client, "windows-primary", ["python_batch"])
        created_response = client.post(
            "/jobs",
            json={
                "name": "Train tiny model",
                "type": "python_batch",
                "parameters": {
                    "script": {
                        "upload_id": str(script_id),
                        "sha256": sha256,
                        "size_bytes": 100,
                    },
                    "dataset": {
                        "upload_id": str(dataset_id),
                        "sha256": sha256,
                        "size_bytes": 200,
                    },
                    "timeout_seconds": 600,
                    "cpu_limit": 2,
                    "memory_mb": 2048,
                },
            },
        )
        created = created_response.json()
        enable_worker(client, "windows-primary", ["python_batch"])
        claimed_response = client.post(
            "/workers/claim",
            json={
                "worker_id": "windows-primary",
                "supported_types": ["python_batch"],
            },
        )
        claimed = claimed_response.json()
        completed = client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "windows-primary",
                "lease_token": claimed["lease_token"],
                "result": {
                    "script_sha256": sha256,
                    "dataset_sha256": sha256,
                    "exit_code": 0,
                    "stdout": "accuracy=0.95\n",
                    "stderr": "",
                    "output_files": ["model.joblib", "metrics.json"],
                    "artifact_uri": f"worker://windows-primary/{created['id']}/",
                },
            },
        )

    assert created_response.status_code == 201
    assert claimed_response.status_code == 200
    assert claimed["id"] == created["id"]
    assert completed.status_code == 200
    assert completed.json()["result"]["output_files"] == [
        "model.joblib",
        "metrics.json",
    ]


def test_batch_contract_rejects_unsafe_url_and_wrong_result(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        unsafe = client.post(
            "/jobs",
            json={
                "type": "python_batch",
                "parameters": {
                    "script": {
                        "upload_id": str(uuid4()),
                        "sha256": "b" * 64,
                        "size_bytes": 10,
                    },
                    "dataset": {
                        "url": "http://127.0.0.1/private.csv",
                        "sha256": "a" * 64,
                        "size_bytes": 100,
                    },
                },
            },
        )
        created = create_sleep_job(client)
        enable_worker(client)
        claimed = claim_job(client)
        assert claimed is not None
        mismatch = client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "mac-one",
                "lease_token": claimed["lease_token"],
                "result": {
                    "script": "csv_summary",
                    "dataset_sha256": "a" * 64,
                    "dataset_bytes": 100,
                    "rows": 2,
                    "columns": [],
                    "artifact_uri": f"worker://mac-one/{created['id']}/summary.json",
                },
            },
        )

    assert unsafe.status_code == 422
    assert mismatch.status_code == 409


def test_disabled_worker_stays_connected_but_cannot_claim(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        registered = client.post(
            "/workers/heartbeat",
            json={"worker_id": "mac-one", "supported_types": ["sleep"]},
        )
        registered_enabled = client.get("/workers").json()[0]["enabled"]
        enabled = client.patch("/workers/mac-one", json={"enabled": True})
        disabled = client.patch("/workers/mac-one", json={"enabled": False})
        created = create_sleep_job(client)
        blocked_claim = claim_job(client)
        heartbeat = client.post(
            "/workers/heartbeat",
            json={"worker_id": "mac-one", "supported_types": ["sleep"]},
        )
        workers = client.get("/workers")
        reenabled = client.patch("/workers/mac-one", json={"enabled": True})
        accepted_claim = claim_job(client)

    assert registered.status_code == 200
    assert registered_enabled is False
    assert enabled.json()["enabled"] is True
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert blocked_claim is None
    assert heartbeat.status_code == 200
    assert workers.json()[0]["enabled"] is False
    assert workers.json()[0]["state"] == "ONLINE"
    assert reenabled.json()["enabled"] is True
    assert accepted_claim is not None
    assert accepted_claim["id"] == created["id"]


def test_disabling_busy_worker_does_not_cancel_its_job(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created = create_sleep_job(client)
        enable_worker(client)
        claimed = claim_job(client)
        assert claimed is not None
        disabled = client.patch("/workers/mac-one", json={"enabled": False})
        heartbeat = client.post(
            "/workers/heartbeat",
            json={
                "worker_id": "mac-one",
                "supported_types": ["sleep"],
                "current_job_id": created["id"],
                "lease_token": claimed["lease_token"],
            },
        )
        completed = client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "mac-one",
                "lease_token": claimed["lease_token"],
                "result": {"slept_seconds": 1},
            },
        )

    assert disabled.json()["enabled"] is False
    assert disabled.json()["state"] == "BUSY"
    assert heartbeat.status_code == 200
    assert completed.status_code == 200
    assert completed.json()["status"] == "COMPLETED"


def test_updating_unknown_or_invalid_worker_is_rejected(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        unknown = client.patch("/workers/not-registered", json={"enabled": False})
        invalid_id = client.patch("/workers/not%20valid", json={"enabled": False})
        invalid_body = client.patch("/workers/not-registered", json={"enabled": "no"})

    assert unknown.status_code == 404
    assert invalid_id.status_code == 422
    assert invalid_body.status_code == 422


def test_only_owner_can_finish_running_job(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created = create_sleep_job(client)
        enable_worker(client, "mac-owner")
        claimed = claim_job(client, "mac-owner")
        assert claimed is not None
        wrong_worker = client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "mac-impostor",
                "lease_token": claimed["lease_token"],
                "result": {"slept_seconds": 1},
            },
        )
        completed = client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "mac-owner",
                "lease_token": claimed["lease_token"],
                "result": {"slept_seconds": 1},
            },
        )
        duplicate = client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "mac-owner",
                "lease_token": claimed["lease_token"],
                "result": {"slept_seconds": 1},
            },
        )

    assert wrong_worker.status_code == 409
    assert completed.status_code == 200
    assert duplicate.status_code == 409


def test_missing_job_cannot_be_finished(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        response = client.post(
            f"/jobs/{uuid4()}/fail",
            json={
                "worker_id": "mac-one",
                "lease_token": str(uuid4()),
                "error": "not found",
            },
        )
    assert response.status_code == 404


def test_worker_can_report_failure(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created = create_sleep_job(client)
        enable_worker(client)
        claimed = claim_job(client)
        assert claimed is not None
        response = client.post(
            f"/jobs/{created['id']}/fail",
            json={
                "worker_id": "mac-one",
                "lease_token": claimed["lease_token"],
                "failure_kind": "MEMORY_LIMIT_EXCEEDED",
                "error": "test failure",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "FAILED"
    assert response.json()["failure_kind"] == "MEMORY_LIMIT_EXCEEDED"
    assert response.json()["error"] == "test failure"


def test_queued_job_can_be_cancelled_immediately(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created = create_sleep_job(client, seconds=30)
        cancelled = client.post(f"/jobs/{created['id']}/cancel")
        repeated = client.post(f"/jobs/{created['id']}/cancel")

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "FAILED"
    assert cancelled.json()["failure_kind"] == "CANCELLED_BY_USER"
    assert cancelled.json()["cancellation_requested"] is True
    assert repeated.status_code == 409


def test_running_job_receives_cancellation_on_heartbeat(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created = create_sleep_job(client, seconds=30)
        enable_worker(client)
        claimed = claim_job(client)
        assert claimed is not None
        requested = client.post(f"/jobs/{created['id']}/cancel")
        heartbeat = client.post(
            "/workers/heartbeat",
            json={
                "worker_id": "mac-one",
                "supported_types": ["sleep"],
                "current_job_id": created["id"],
                "lease_token": claimed["lease_token"],
            },
        )
        completion = client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "mac-one",
                "lease_token": claimed["lease_token"],
                "result": {"slept_seconds": 30},
            },
        )
        acknowledged = client.post(
            f"/jobs/{created['id']}/fail",
            json={
                "worker_id": "mac-one",
                "lease_token": claimed["lease_token"],
                "failure_kind": "EXECUTION_ERROR",
                "error": "old worker supplied the wrong reason",
            },
        )

    assert requested.status_code == 200
    assert requested.json()["status"] == "RUNNING"
    assert requested.json()["cancellation_requested"] is True
    assert heartbeat.json() == {"cancellation_requested": True}
    assert completion.status_code == 409
    assert acknowledged.status_code == 200
    assert acknowledged.json()["status"] == "FAILED"
    assert acknowledged.json()["failure_kind"] == "CANCELLED_BY_USER"
    assert acknowledged.json()["error"] == "cancelled by user"


def test_heartbeat_registers_worker_and_renews_lease(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created = create_sleep_job(client)
        enable_worker(client)
        claimed = claim_job(client)
        assert claimed is not None
        response = client.post(
            "/workers/heartbeat",
            json={
                "worker_id": "mac-one",
                "supported_types": ["sleep"],
                "current_job_id": created["id"],
                "lease_token": claimed["lease_token"],
                "metrics": {
                    "platform": "TestOS",
                    "logical_cores": 8,
                    "cpu_percent": 25.5,
                    "memory_percent": 50.0,
                    "memory_available": 8000000000,
                    "memory_total": 16000000000,
                    "storage_percent": 75.0,
                    "storage_free": 25000000000,
                    "storage_total": 100000000000,
                    "temperature_c": 51.0,
                    "gpus": [
                        {
                            "name": "Test GPU",
                            "percent": 12.0,
                            "memory_used": 1000000000,
                            "memory_total": 8000000000,
                            "temperature_c": 48.0,
                        }
                    ],
                },
            },
        )
        workers = client.get("/workers")
        renewed = client.get(f"/jobs/{created['id']}")

    assert response.status_code == 200
    assert response.json() == {"cancellation_requested": False}
    assert workers.json()[0]["state"] == "BUSY"
    assert workers.json()[0]["current_job_id"] == created["id"]
    assert workers.json()[0]["metrics"]["cpu_percent"] == 25.5
    assert workers.json()[0]["metrics"]["gpus"][0]["name"] == "Test GPU"
    assert renewed.json()["lease_expires_at"] >= claimed["lease_expires_at"]


def test_old_lease_token_cannot_complete_job(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created = create_sleep_job(client)
        enable_worker(client)
        claimed = claim_job(client)
        assert claimed is not None
        rejected = client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "mac-one",
                "lease_token": str(uuid4()),
                "result": {"slept_seconds": 1},
            },
        )

    assert rejected.status_code == 409


def test_dashboard_requires_login_and_exposes_operational_data(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    monkeypatch.setenv("HOME_PLATFORM_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        page = client.get("/dashboard")
        operations_page = client.get("/dashboard/operations")
        jobs_page = client.get("/jobs-ui")
        submit_page = client.get("/jobs-ui/new")
        files_page = client.get("/jobs-ui/files")
        unauthenticated = client.get("/dashboard/api/system")
        unauthenticated_jobs = client.get("/jobs-ui/api/jobs")
        unauthenticated_groups = client.get("/jobs-ui/api/job-groups")
        unauthenticated_artifacts = client.get(f"/jobs-ui/api/jobs/{uuid4()}/artifacts")
        unauthenticated_storage_download = client.get(
            "/jobs-ui/api/storage/download", params={"path": "Shared/common.txt"}
        )
        unauthenticated_files = client.get("/jobs-ui/api/files")
        unauthenticated_file_download = client.get(
            "/jobs-ui/api/files/download", params={"path": "Artifacts/report.txt"}
        )
        unauthenticated_submit = client.post(
            "/jobs-ui/api/jobs",
            json={
                "name": "Family check",
                "type": "sleep",
                "parameters": {"seconds": 5},
            },
        )
        wrong = client.post("/dashboard/login", json={"token": "wrong"})
        registered = client.post(
            "/workers/heartbeat",
            headers={"X-API-Token": "test-secret"},
            json={"worker_id": "mac-one", "supported_types": ["sleep"]},
        )
        dashboard_update_without_session = client.patch(
            "/dashboard/api/workers/mac-one", json={"enabled": False}
        )
        api_update_without_token = client.patch(
            "/workers/mac-one", json={"enabled": False}
        )
        accepted = client.post("/dashboard/login", json={"token": "test-secret"})
        uploaded = client.post(
            "/jobs-ui/api/uploads",
            files={"file": ("family.csv", b"name,value\na,1\n", "text/csv")},
        )
        upload_body = uploaded.json()
        script_uploaded = client.post(
            "/jobs-ui/api/script-uploads",
            files={"file": ("train.py", b"print('ok')\n", "text/x-python")},
        )
        script_body = script_uploaded.json()
        unauthenticated_download = client.get(
            f"/datasets/uploads/{upload_body['upload_id']}"
        )
        downloaded = client.get(
            f"/datasets/uploads/{upload_body['upload_id']}",
            headers={"X-API-Token": "test-secret"},
        )
        script_downloaded = client.get(
            f"/scripts/uploads/{script_body['upload_id']}",
            headers={"X-API-Token": "test-secret"},
        )
        dashboard_update = client.patch(
            "/dashboard/api/workers/mac-one", json={"enabled": False}
        )
        metrics = client.get("/dashboard/api/system")
        services = client.get("/dashboard/api/services")
        jobs = client.get("/dashboard/api/jobs")
        portal_submit = client.post(
            "/jobs-ui/api/jobs",
            headers={"Idempotency-Key": "family-check-01"},
            json={
                "name": "Family check",
                "type": "sleep",
                "parameters": {"seconds": 5},
            },
        )
        artifact_directory = tmp_path / "artifacts" / portal_submit.json()["id"]
        artifact_directory.mkdir(parents=True)
        (artifact_directory / "metrics.json").write_text('{"accuracy": 0.95}\n')
        portal_artifacts = client.get(
            f"/jobs-ui/api/jobs/{portal_submit.json()['id']}/artifacts"
        )
        portal_artifact_download = client.get(
            f"/jobs-ui/api/jobs/{portal_submit.json()['id']}/artifacts/metrics.json"
        )
        portal_jobs = client.get("/jobs-ui/api/jobs")
        portal_groups = client.get("/jobs-ui/api/job-groups")
        portal_workers = client.get("/jobs-ui/api/workers")
        retired_job = client.post(
            "/jobs-ui/api/jobs",
            json={
                "name": "Uploaded family CSV",
                "type": "dataset_script",
                "parameters": {
                    "script": "csv_summary",
                    "dataset": upload_body,
                    "timeout_seconds": 60,
                },
            },
        )

    assert page.status_code == 200
    assert "Homelab Dashboard" in page.text
    assert "homelab dashboard" in page.text
    assert "viewport-fit=cover" in page.text
    assert 'href="/jobs-ui"' in page.text
    assert 'href="/jobs-ui/new"' in page.text
    assert 'aria-label="Home Platform"' in page.text
    assert "width:min(1240px,100%)" in page.text
    assert 'class="topbar-actions"' in page.text
    assert 'id="host" class="context-line"' in page.text
    jobs_card_position = page.text.index('class="service-card jobs"')
    jobs_link_position = page.text.index('class="service-link" href="/jobs-ui"')
    assert jobs_card_position < jobs_link_position
    assert operations_page.status_code == 200
    assert 'href="/dashboard/operations"' in page.text
    assert 'operationsView=location.pathname.endsWith("/operations")' in page.text
    assert 'id="members-section" class="section panel hidden"' in page.text
    assert 'id="reset-dialog"' in page.text
    assert 'actionMenu("Actions for "+user.username' in page.text
    assert 'user.role+" · storage "+user.id' in page.text
    assert 'state=status(user.disabled?"DISABLED":"ACTIVE")' in page.text
    assert 'status(user.disabled?"OFFLINE":"ONLINE")' not in page.text
    assert "body:not(.operations-view) .worker-control" in page.text
    assert 'primary.append(element("div","worker-meta",platform+ceiling))' in page.text
    assert 'worker.supported_types.join(" · ")' not in page.text
    assert 'state.classList.add("account-state")' in page.text
    assert (
        ".account-state { min-width:78px; min-height:30px; justify-content:center;"
        in page.text
    )
    assert "response.status===403" in page.text
    assert "ADMINISTRATOR SESSION REQUIRED" in page.text
    assert "Control plane</div>" not in page.text
    assert "Job database</div>" not in page.text
    assert '<div class="service-name">Jobs</div>' in page.text
    assert '<div class="metric-label">RAM</div>' in page.text
    assert '<div class="metric-label">Disk space</div>' in page.text
    assert '<div class="metric-label">Pi root</div>' not in page.text
    assert 'api("/dashboard/api/jobs")' not in page.text
    assert jobs_page.status_code == 200
    assert submit_page.status_code == 200
    assert files_page.status_code == 200
    assert 'href="/jobs-ui/files"' in page.text
    assert 'href="/jobs-ui/new"' in jobs_page.text
    assert 'href="/jobs-ui/files"' in jobs_page.text
    assert 'href="/dashboard/operations"' in jobs_page.text
    assert 'aria-label="Home Platform"' in jobs_page.text
    assert "width:min(1240px,100%)" in jobs_page.text
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in jobs_page.text
    assert 'id="target-worker-field"' in jobs_page.text
    assert 'class="topbar-actions"' in jobs_page.text
    assert 'id="account" class="context-line"' in jobs_page.text
    assert 'id="account-open"' in jobs_page.text
    assert 'id="account-dialog"' in jobs_page.text
    assert 'api("/jobs-ui/api/account/password"' in jobs_page.text
    assert 'submitView=location.pathname.endsWith("/new")' in jobs_page.text
    assert 'filesView=location.pathname.endsWith("/files")' in jobs_page.text
    assert 'id="submit-panel" class="panel submit-panel hidden"' in jobs_page.text
    assert 'id="queue-panel" class="panel queue-panel hidden"' in jobs_page.text
    assert 'id="files-panel" class="panel hidden"' in jobs_page.text
    assert 'actionMenu("Actions for "+entry.name,items)' in jobs_page.text
    assert "Submit a job" in jobs_page.text
    assert "Queue &amp; history" in jobs_page.text
    assert '"Idempotency-Key":submissionKey()' in jobs_page.text
    assert 'data-sort="name"' in jobs_page.text
    assert 'data-sort="id"' in jobs_page.text
    assert 'data-sort="created_at"' in jobs_page.text
    assert 'id="target-worker"' in jobs_page.text
    assert '<option value="604800">7 days</option>' in jobs_page.text
    assert 'api("/jobs-ui/api/workers")' in jobs_page.text
    assert 'api("/jobs-ui/api/job-groups")' in jobs_page.text
    assert 'element("div","group-block")' in jobs_page.text
    assert 'detail("Failure reason",job.failure_kind)' in jobs_page.text
    assert "smallest capable worker" in jobs_page.text
    assert "job ceiling" in page.text
    assert '"label","Artifacts"' in jobs_page.text
    assert '"DOWNLOAD"' in jobs_page.text
    assert "new URLSearchParams({path})" in jobs_page.text
    assert 'api("/jobs-ui/api/files?path="' in jobs_page.text
    assert 'fileMutation("/jobs-ui/api/files","DELETE"' in jobs_page.text
    assert "Artifacts contains this account's published job outputs" in jobs_page.text
    assert (
        "setInterval(refresh,submitView?30000:filesView?60000:10000)" in jobs_page.text
    )
    assert 'id="login-username"' in jobs_page.text
    assert 'id="login-password"' in jobs_page.text
    assert 'api("/jobs-ui/api/session")' in jobs_page.text
    assert 'api("/jobs-ui/api/workload-owners")' in jobs_page.text
    assert "/dashboard/api/system" not in jobs_page.text
    assert "/dashboard/api/workers" not in jobs_page.text
    assert "sort-button" not in page.text
    assert "Pi SSD Samba" not in page.text
    assert "Synology NAS" in page.text
    assert "Network storage" not in page.text
    assert 'id="synology-state"' not in page.text
    assert 'id="storage-service-state"' in page.text
    assert "CREATE MEMBER" in page.text
    assert "GPU thermal" in page.text
    assert "thermal-critical" in page.text
    assert "DEACTIVATE" in page.text
    assert "PI POWER" in page.text
    assert 'api("/dashboard/api/system/power"' in page.text
    assert "Type REBOOT to confirm" not in page.text
    assert "setInterval(refresh,15000)" in page.text
    assert 'method:"PATCH"' in page.text
    assert "<table" not in page.text
    assert unauthenticated.status_code == 401
    assert unauthenticated_jobs.status_code == 401
    assert unauthenticated_groups.status_code == 401
    assert unauthenticated_artifacts.status_code == 401
    assert unauthenticated_storage_download.status_code == 401
    assert unauthenticated_files.status_code == 401
    assert unauthenticated_file_download.status_code == 401
    assert unauthenticated_submit.status_code == 401
    assert wrong.status_code == 401
    assert registered.status_code == 200
    assert dashboard_update_without_session.status_code == 401
    assert api_update_without_token.status_code == 401
    assert accepted.status_code == 204
    assert uploaded.status_code == 201
    assert script_uploaded.status_code == 201
    assert upload_body["size_bytes"] == len(b"name,value\na,1\n")
    assert len(upload_body["sha256"]) == 64
    assert unauthenticated_download.status_code == 401
    assert downloaded.status_code == 200
    assert downloaded.content == b"name,value\na,1\n"
    assert script_downloaded.status_code == 200
    assert script_downloaded.content == b"print('ok')\n"
    assert dashboard_update.status_code == 200
    assert dashboard_update.json()["enabled"] is False
    assert metrics.status_code == 200
    assert {"cpu", "memory", "storage", "uptime_seconds"} <= metrics.json().keys()
    assert services.status_code == 200
    assert {
        "control_plane",
        "database",
        "synology_nas",
    } == services.json().keys()
    assert jobs.status_code == 200
    assert portal_submit.status_code == 201
    assert portal_submit.json()["name"] == "Family check"
    assert portal_submit.json()["status"] == "QUEUED"
    assert portal_jobs.status_code == 200
    assert portal_groups.status_code == 200
    assert portal_groups.json() == []
    assert portal_workers.status_code == 200
    assert portal_workers.json()[0]["id"] == "mac-one"
    assert portal_jobs.json()[0]["id"] == portal_submit.json()["id"]
    assert portal_artifacts.status_code == 200
    assert portal_artifacts.json() == [
        {
            "filename": "metrics.json",
            "size_bytes": 19,
            "sha256": hashlib.sha256(b'{"accuracy": 0.95}\n').hexdigest(),
        }
    ]
    assert portal_artifact_download.status_code == 200
    assert portal_artifact_download.content == b'{"accuracy": 0.95}\n'
    assert retired_job.status_code == 422
    assert 'value="dataset_script"' not in jobs_page.text


def test_dashboard_power_control_requires_confirmation_and_safe_idle_state(
    tmp_path: Path, monkeypatch
) -> None:
    power_directory = tmp_path / "power"
    power_directory.mkdir()
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    monkeypatch.setenv("HOME_PLATFORM_POWER_REQUEST_DIR", str(power_directory))

    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        unauthenticated = client.post(
            "/dashboard/api/system/power",
            json={"action": "reboot", "confirmation": "REBOOT"},
        )
        client.post("/dashboard/login", json={"token": "test-secret"})
        wrong_confirmation = client.post(
            "/dashboard/api/system/power",
            json={"action": "reboot", "confirmation": "reboot"},
        )
        registered = client.post(
            "/workers/heartbeat",
            headers={"X-API-Token": "test-secret"},
            json={"worker_id": "mac-one", "supported_types": ["sleep"]},
        )
        assert registered.status_code == 200
        enabled = client.patch(
            "/workers/mac-one",
            headers={"X-API-Token": "test-secret"},
            json={"enabled": True},
        )
        assert enabled.status_code == 200
        unsafe = client.post(
            "/dashboard/api/system/power",
            json={"action": "reboot", "confirmation": "REBOOT"},
        )
        disabled = client.patch(
            "/workers/mac-one",
            headers={"X-API-Token": "test-secret"},
            json={"enabled": False},
        )
        assert disabled.status_code == 200
        accepted = client.post(
            "/dashboard/api/system/power",
            json={"action": "reboot", "confirmation": "REBOOT"},
        )
        duplicate = client.post(
            "/dashboard/api/system/power",
            json={"action": "shutdown", "confirmation": "SHUTDOWN"},
        )

    assert unauthenticated.status_code == 401
    assert wrong_confirmation.status_code == 422
    assert unsafe.status_code == 409
    assert "Disable scheduling" in unsafe.json()["detail"]
    assert accepted.status_code == 202
    assert accepted.json()["action"] == "reboot"
    assert (power_directory / "reboot").read_text() == "reboot\n"
    assert duplicate.status_code == 409


def test_dashboard_power_control_rejects_running_job(
    tmp_path: Path, monkeypatch
) -> None:
    power_directory = tmp_path / "power"
    power_directory.mkdir()
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    monkeypatch.setenv("HOME_PLATFORM_POWER_REQUEST_DIR", str(power_directory))

    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        client.post("/dashboard/login", json={"token": "test-secret"})
        token_header = {"X-API-Token": "test-secret"}
        created_response = client.post(
            "/jobs-ui/api/jobs",
            json={"type": "sleep", "parameters": {"seconds": 1}},
        )
        assert created_response.status_code == 201
        created = created_response.json()
        registration = client.post(
            "/workers/heartbeat",
            headers=token_header,
            json={"worker_id": "mac-one", "supported_types": ["sleep"]},
        )
        assert registration.status_code == 200
        capacity = client.put(
            "/workers/mac-one/capacity",
            headers=token_header,
            json={"max_job_cpu": 4, "max_job_memory_mb": 4096},
        )
        assert capacity.status_code == 200
        enabled = client.patch(
            "/workers/mac-one", headers=token_header, json={"enabled": True}
        )
        assert enabled.status_code == 200
        claim_response = client.post(
            "/workers/claim",
            headers=token_header,
            json={"worker_id": "mac-one", "supported_types": ["sleep"]},
        )
        assert claim_response.status_code == 200
        claimed = claim_response.json()
        assert claimed is not None
        disabled = client.patch(
            "/workers/mac-one",
            headers=token_header,
            json={"enabled": False},
        )
        assert disabled.status_code == 200
        response = client.post(
            "/dashboard/api/system/power",
            json={"action": "shutdown", "confirmation": "SHUTDOWN"},
        )

    assert response.status_code == 409
    assert created["id"] in response.json()["detail"]
    assert list(power_directory.iterdir()) == []


def test_dashboard_upload_rejects_invalid_or_oversized_files(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    monkeypatch.setenv("HOME_PLATFORM_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("HOME_PLATFORM_MAX_UPLOAD_BYTES", "8")
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        client.post("/dashboard/login", json={"token": "test-secret"})
        wrong_type = client.post(
            "/jobs-ui/api/uploads",
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )
        oversized = client.post(
            "/jobs-ui/api/uploads",
            files={"file": ("large.csv", b"123456789", "text/csv")},
        )

    assert wrong_type.status_code == 415
    assert oversized.status_code == 413
    assert list((tmp_path / "uploads").iterdir()) == []


def test_cli_and_job_desk_adapters_share_submission_validation(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    payload = {
        "name": "same contract",
        "target_worker_id": "missing-worker",
        "type": "sleep",
        "parameters": {"seconds": 1},
    }

    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        api_response = client.post(
            "/jobs",
            headers={"X-API-Token": "test-secret"},
            json=payload,
        )
        assert (
            client.post("/dashboard/login", json={"token": "test-secret"}).status_code
            == 204
        )
        browser_response = client.post("/jobs-ui/api/jobs", json=payload)

    assert api_response.status_code == browser_response.status_code == 404
    assert api_response.json() == browser_response.json()


def test_dashboard_session_survives_application_restart(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    database_path = tmp_path / "jobs.db"
    with TestClient(create_app(database_path)) as first_client:
        login = first_client.post("/dashboard/login", json={"token": "test-secret"})
        session_cookie = first_client.cookies.get("home_platform_dashboard")

    assert login.status_code == 204
    assert session_cookie is not None

    with TestClient(create_app(database_path)) as restarted_client:
        restarted_client.cookies.set("home_platform_dashboard", session_cookie)
        metrics = restarted_client.get("/dashboard/api/system")

    assert metrics.status_code == 200


def test_member_password_change_and_admin_reset_revoke_sessions(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    original = "member password 1234"
    changed = "member changed password"
    reset = "administrator reset password"

    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        assert (
            client.post("/dashboard/login", json={"token": "test-secret"}).status_code
            == 204
        )
        member = client.post(
            "/dashboard/api/users",
            json={"username": "alice", "password": original, "role": "MEMBER"},
        ).json()
        assert client.post("/dashboard/logout").status_code == 204
        assert (
            client.post(
                "/dashboard/login",
                json={"username": "alice", "password": original},
            ).status_code
            == 204
        )
        original_cookie = client.cookies.get("home_platform_dashboard")
        wrong = client.post(
            "/jobs-ui/api/account/password",
            json={"current_password": "wrong", "new_password": changed},
        )
        updated = client.post(
            "/jobs-ui/api/account/password",
            json={"current_password": original, "new_password": changed},
        )
        assert wrong.status_code == 400
        assert updated.status_code == 204
        assert client.get("/jobs-ui/api/session").status_code == 401
        assert (
            client.post(
                "/dashboard/login",
                json={"username": "alice", "password": original},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/dashboard/login",
                json={"username": "alice", "password": changed},
            ).status_code
            == 204
        )
        changed_cookie = client.cookies.get("home_platform_dashboard")

        assert (
            client.post("/dashboard/login", json={"token": "test-secret"}).status_code
            == 204
        )
        admin_change = client.post(
            "/jobs-ui/api/account/password",
            json={"current_password": original, "new_password": changed},
        )
        reset_response = client.put(
            f"/dashboard/api/users/{member['id']}/password",
            json={"new_password": reset},
        )
        missing_reset = client.put(
            f"/dashboard/api/users/{uuid4()}/password",
            json={"new_password": reset},
        )
        assert admin_change.status_code == 403
        assert reset_response.status_code == 204
        assert missing_reset.status_code == 404

        assert original_cookie is not None
        assert changed_cookie is not None
        client.cookies.set("home_platform_dashboard", changed_cookie)
        assert client.get("/jobs-ui/api/session").status_code == 401
        assert (
            client.post(
                "/dashboard/login",
                json={"username": "alice", "password": reset},
            ).status_code
            == 204
        )


def test_tailscale_identity_self_link_and_internal_resolution(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    service_token = tmp_path / "service-identity-token"
    service_token.write_text("service-secret\n")
    monkeypatch.setenv("HOME_PLATFORM_SERVICE_IDENTITY_TOKEN_FILE", str(service_token))
    application = create_app(tmp_path / "jobs.db")

    with TestClient(application) as direct_client:
        assert (
            direct_client.post(
                "/dashboard/login", json={"token": "test-secret"}
            ).status_code
            == 204
        )
        refused = direct_client.post(
            "/jobs-ui/api/account/identities/tailscale",
            headers={"Tailscale-User-Login": "owner@example.test"},
        )
        assert refused.status_code == 400

    with TestClient(application, base_url="https://testserver") as client:
        assert (
            client.post("/dashboard/login", json={"token": "test-secret"}).status_code
            == 204
        )
        available = client.get(
            "/jobs-ui/api/account/identities/tailscale",
            headers={
                "Tailscale-User-Login": "owner@example.test",
                "Tailscale-User-Name": "Owner",
            },
        )
        linked = client.post(
            "/jobs-ui/api/account/identities/tailscale",
            headers={
                "Tailscale-User-Login": "owner@example.test",
                "Tailscale-User-Name": "Owner",
            },
        )
        unauthorized = client.post(
            "/internal/service-identities/resolve",
            json={"provider": "tailscale", "subject": "owner@example.test"},
        )
        resolved = client.post(
            "/internal/service-identities/resolve",
            headers={"X-Service-Identity-Token": "service-secret"},
            json={"provider": "tailscale", "subject": "owner@example.test"},
        )

    assert available.json()["request_subject"] == "owner@example.test"
    assert available.json()["linked"] is None
    assert linked.status_code == 200
    assert linked.json()["subject"] == "owner@example.test"
    assert unauthorized.status_code == 401
    assert resolved.status_code == 200
    assert resolved.json()["id"] == ADMIN_USER_ID


def test_member_sessions_enforce_job_upload_artifact_and_admin_boundaries(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    monkeypatch.setenv("HOME_PLATFORM_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_OWNER_SCOPED", "true")
    password = "member password 1234"

    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        assert (
            client.post("/dashboard/login", json={"token": "test-secret"}).status_code
            == 204
        )
        alice = client.post(
            "/dashboard/api/users",
            json={"username": "alice", "password": password, "role": "MEMBER"},
        )
        bob = client.post(
            "/dashboard/api/users",
            json={"username": "bob", "password": password, "role": "MEMBER"},
        )
        duplicate = client.post(
            "/dashboard/api/users",
            json={"username": "alice", "password": password, "role": "MEMBER"},
        )
        assert alice.status_code == bob.status_code == 201
        assert duplicate.status_code == 409

        client.post("/dashboard/logout")
        assert (
            client.post(
                "/dashboard/login", json={"username": "alice", "password": password}
            ).status_code
            == 204
        )
        alice_job = client.post(
            "/jobs-ui/api/jobs",
            headers={"Idempotency-Key": "same-browser-key"},
            json={"name": "Alice job", "type": "sleep", "parameters": {"seconds": 1}},
        )
        alice_script = client.post(
            "/jobs-ui/api/script-uploads",
            files={"file": ("train.py", b"print('alice')\n", "text/x-python")},
        )
        alice_dataset = client.post(
            "/jobs-ui/api/uploads",
            files={"file": ("data.csv", b"x\n1\n", "text/csv")},
        )
        assert alice_job.status_code == 201
        assert alice_script.status_code == alice_dataset.status_code == 201
        artifact_directory = (
            tmp_path / "artifacts" / alice.json()["id"] / alice_job.json()["id"]
        )
        artifact_directory.mkdir(parents=True)
        (artifact_directory / "result.txt").write_text("alice result\n")

        client.post("/dashboard/logout")
        assert (
            client.post(
                "/dashboard/login", json={"username": "bob", "password": password}
            ).status_code
            == 204
        )
        bob_cookie = client.cookies.get("home_platform_dashboard")
        bob_job = client.post(
            "/jobs-ui/api/jobs",
            headers={"Idempotency-Key": "same-browser-key"},
            json={"name": "Bob job", "type": "sleep", "parameters": {"seconds": 1}},
        )
        bob_jobs = client.get("/jobs-ui/api/jobs")
        foreign_cancel = client.post(
            f"/jobs-ui/api/jobs/{alice_job.json()['id']}/cancel"
        )
        foreign_artifacts = client.get(
            f"/jobs-ui/api/jobs/{alice_job.json()['id']}/artifacts"
        )
        foreign_upload = client.post(
            "/jobs-ui/api/jobs",
            json={
                "name": "stolen upload",
                "type": "python_batch",
                "parameters": {
                    "script": alice_script.json(),
                    "dataset": alice_dataset.json(),
                    "timeout_seconds": 60,
                    "cpu_limit": 1,
                    "memory_mb": 512,
                },
            },
        )
        member_dashboard = client.get("/dashboard/api/system")
        member_users = client.get("/dashboard/api/users")
        member_owners = client.get("/jobs-ui/api/workload-owners")
        member_storage = client.get("/jobs-ui/api/storage")

        assert bob_job.status_code == 201
        assert bob_job.json()["id"] != alice_job.json()["id"]
        assert [job["id"] for job in bob_jobs.json()] == [bob_job.json()["id"]]
        assert foreign_cancel.status_code == 404
        assert foreign_artifacts.status_code == 404
        assert foreign_upload.status_code == 404
        assert member_dashboard.status_code == 403
        assert member_users.status_code == 403
        assert member_owners.status_code == 403
        assert member_storage.status_code == 503

        assert (
            client.post("/dashboard/login", json={"token": "test-secret"}).status_code
            == 204
        )
        admin_jobs = client.get("/jobs-ui/api/jobs")
        admin_owners = client.get("/jobs-ui/api/workload-owners")
        own_artifacts = client.get(
            f"/jobs-ui/api/jobs/{alice_job.json()['id']}/artifacts"
        )
        disabled = client.patch(
            f"/dashboard/api/users/{bob.json()['id']}", json={"disabled": True}
        )
        assert {job["id"] for job in admin_jobs.json()} == {
            alice_job.json()["id"],
            bob_job.json()["id"],
        }
        assert admin_owners.json()["jobs"][alice_job.json()["id"]] == "alice"
        assert admin_owners.json()["jobs"][bob_job.json()["id"]] == "bob"
        assert own_artifacts.status_code == 200
        assert own_artifacts.json()[0]["filename"] == "result.txt"
        assert disabled.status_code == 200
        assert disabled.json()["disabled"] is True

        client.cookies.set("home_platform_dashboard", bob_cookie)
        assert client.get("/jobs-ui/api/jobs").status_code == 401


def test_member_storage_routes_expose_only_home_and_shared(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    monkeypatch.setenv("HOME_PLATFORM_MEMBER_STORAGE_ENABLED", "true")
    storage = tmp_path / "storage"
    uploads = tmp_path / "uploads"
    monkeypatch.setenv("HOME_PLATFORM_STORAGE_DIR", str(storage))
    monkeypatch.setenv("HOME_PLATFORM_UPLOAD_DIR", str(uploads))
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(artifacts))
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_OWNER_SCOPED", "true")
    password = "member password 1234"

    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        assert (
            client.post("/dashboard/login", json={"token": "test-secret"}).status_code
            == 204
        )
        alice = client.post(
            "/dashboard/api/users",
            json={"username": "alice", "password": password, "role": "MEMBER"},
        ).json()
        bob = client.post(
            "/dashboard/api/users",
            json={"username": "bob", "password": password, "role": "MEMBER"},
        ).json()
        alice_home = storage / "users" / alice["id"]
        bob_home = storage / "users" / bob["id"]
        shared = storage / "shared"
        (alice_home / "project").mkdir(parents=True)
        bob_home.mkdir(parents=True)
        shared.mkdir()
        (alice_home / "input.txt").write_text("alice")
        (alice_home / "project" / "submit.hp").write_text("#!/bin/sh\n")
        (bob_home / "secret.txt").write_text("bob")
        (shared / "common.txt").write_text("shared")

        # Artifacts are derived from job ownership, not from whatever happens to
        # sit in the artifact tree, so each run needs a real owned job behind it.
        def member_job(name: str) -> dict:
            created = client.post(
                "/jobs-ui/api/jobs",
                json={"name": name, "type": "sleep", "parameters": {"seconds": 1}},
                headers={"Idempotency-Key": f"key-{name.replace(' ', '-')}"},
            )
            assert created.status_code == 201, created.text
            return created.json()

        def publish(owner_id: str, job: dict, filename: str, body: str) -> Path:
            directory = (
                artifacts / owner_id / run_directory_name(job["name"], UUID(job["id"]))
            )
            directory.mkdir(parents=True, exist_ok=True)
            (directory / filename).write_text(body)
            return directory

        client.post("/dashboard/logout")
        assert (
            client.post(
                "/dashboard/login",
                json={"username": "alice", "password": password},
            ).status_code
            == 204
        )
        alice_model = member_job("model run")
        alice_old = member_job("old run")
        client.post("/dashboard/logout")
        assert (
            client.post(
                "/dashboard/login",
                json={"username": "bob", "password": password},
            ).status_code
            == 204
        )
        bob_private = member_job("private run")
        publish(alice["id"], alice_model, "metrics.json", '{"accuracy": 0.9}\n')
        publish(alice["id"], alice_old, "report.txt", "old")
        bob_results = publish(bob["id"], bob_private, "secret.txt", "bob result")
        model_run = run_directory_name(alice_model["name"], UUID(alice_model["id"]))
        old_run = run_directory_name(alice_old["name"], UUID(alice_old["id"]))

        client.post("/dashboard/logout")
        assert (
            client.post(
                "/dashboard/login",
                json={"username": "alice", "password": password},
            ).status_code
            == 204
        )
        session = client.get("/jobs-ui/api/session")
        root = client.get("/jobs-ui/api/storage")
        home = client.get("/jobs-ui/api/storage", params={"path": "Home"})
        reference = client.post(
            "/jobs-ui/api/storage/references", json={"path": "Home/input.txt"}
        )
        project = client.post(
            "/jobs-ui/api/storage/project-uploads",
            json={"path": "Home/project"},
        )
        escaped = client.get(
            "/jobs-ui/api/storage", params={"path": f"users/{bob['id']}"}
        )
        own_download = client.get(
            "/jobs-ui/api/storage/download", params={"path": "Home/input.txt"}
        )
        shared_download = client.get(
            "/jobs-ui/api/storage/download", params={"path": "Shared/common.txt"}
        )
        foreign_download = client.get(
            "/jobs-ui/api/storage/download",
            params={"path": f"users/{bob['id']}/secret.txt"},
        )
        directory_download = client.get(
            "/jobs-ui/api/storage/download", params={"path": "Home/project"}
        )
        files_root = client.get("/jobs-ui/api/files")
        results = client.get("/jobs-ui/api/files", params={"path": "Artifacts"})
        result_files = client.get(
            "/jobs-ui/api/files", params={"path": f"Artifacts/{model_run}"}
        )
        result_download = client.get(
            "/jobs-ui/api/files/download",
            params={"path": f"Artifacts/{model_run}/metrics.json"},
        )
        foreign_result = client.get(
            "/jobs-ui/api/files/download",
            params={"path": f"Artifacts/../{bob['id']}/private-run/secret.txt"},
        )
        deleted_result = client.request(
            "DELETE",
            "/jobs-ui/api/files",
            json={"path": f"Artifacts/{model_run}/metrics.json"},
        )
        cleared_results = client.request(
            "DELETE", "/jobs-ui/api/files", json={"path": "Artifacts"}
        )
        client.post("/dashboard/logout")
        assert (
            client.post(
                "/dashboard/login",
                json={"username": "bob", "password": password},
            ).status_code
            == 204
        )
        bob_root = client.get("/jobs-ui/api/storage")
        bob_home_listing = client.get("/jobs-ui/api/storage", params={"path": "Home"})
        bob_own_download = client.get(
            "/jobs-ui/api/storage/download", params={"path": "Home/secret.txt"}
        )
        bob_shared_download = client.get(
            "/jobs-ui/api/storage/download", params={"path": "Shared/common.txt"}
        )
        bob_results_listing = client.get(
            "/jobs-ui/api/files", params={"path": "Artifacts"}
        )

    assert session.status_code == 200
    assert session.json()["storage_enabled"] is True
    assert [entry["path"] for entry in root.json()["entries"]] == [
        "Home",
        "Shared",
    ]
    assert [entry["path"] for entry in home.json()["entries"]] == [
        "Home/project",
        "Home/input.txt",
    ]
    assert reference.status_code == 200
    assert reference.json()["path"] == f"users/{alice['id']}/input.txt"
    assert project.status_code == 201
    assert escaped.status_code == 422
    assert own_download.status_code == 200
    assert own_download.content == b"alice"
    assert "input.txt" in own_download.headers["content-disposition"]
    assert shared_download.status_code == 200
    assert shared_download.content == b"shared"
    assert foreign_download.status_code == 422
    assert directory_download.status_code == 422
    assert [entry["path"] for entry in files_root.json()["entries"]] == [
        "Home",
        "Shared",
        "Artifacts",
    ]
    assert [entry["path"] for entry in results.json()["entries"]] == [
        f"Artifacts/{model_run}",
        f"Artifacts/{old_run}",
    ]
    assert [entry["path"] for entry in result_files.json()["entries"]] == [
        f"Artifacts/{model_run}/metrics.json"
    ]
    assert result_download.content == b'{"accuracy": 0.9}\n'
    assert foreign_result.status_code == 422
    assert deleted_result.status_code == 200
    assert cleared_results.status_code == 200
    assert list((artifacts / alice["id"]).iterdir()) == []
    assert (bob_results / "secret.txt").exists()
    assert alice["id"] not in root.text
    assert alice["id"] not in home.text
    assert bob["id"] not in root.text
    assert bob["id"] not in home.text
    assert [entry["path"] for entry in bob_root.json()["entries"]] == [
        "Home",
        "Shared",
    ]
    assert [entry["path"] for entry in bob_home_listing.json()["entries"]] == [
        "Home/secret.txt"
    ]
    assert bob_own_download.content == b"bob"
    assert bob_shared_download.content == b"shared"
    assert [entry["path"] for entry in bob_results_listing.json()["entries"]] == [
        "Artifacts/" + run_directory_name(bob_private["name"], UUID(bob_private["id"]))
    ]
    assert alice["id"] not in bob_root.text
    assert alice["id"] not in bob_home_listing.text
    assert bob["id"] not in bob_root.text
    assert bob["id"] not in bob_home_listing.text


def test_results_are_owner_scoped_in_the_flat_artifact_layout(
    tmp_path: Path, monkeypatch
) -> None:
    """The live store is flat: every member's runs sit side by side.

    `HOME_PLATFORM_ARTIFACT_OWNER_SCOPED` is off in production, so there is no
    per-owner directory to hide behind. Artifacts must therefore be filtered by
    job ownership. An earlier implementation looked inside `artifacts/<member>/`,
    which does not exist in this layout — it showed nothing at all, and would
    have shown everything had that directory ever been created by other means.
    """
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(artifacts))
    monkeypatch.delenv("HOME_PLATFORM_ARTIFACT_OWNER_SCOPED", raising=False)
    password = "member password 1234"

    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        assert (
            client.post("/dashboard/login", json={"token": "test-secret"}).status_code
            == 204
        )
        for username in ("alice", "bob"):
            client.post(
                "/dashboard/api/users",
                json={"username": username, "password": password, "role": "MEMBER"},
            )

        def as_member(username: str) -> None:
            client.post("/dashboard/logout")
            assert (
                client.post(
                    "/dashboard/login",
                    json={"username": username, "password": password},
                ).status_code
                == 204
            )

        def own_run(username: str, name: str, body: str) -> str:
            as_member(username)
            created = client.post(
                "/jobs-ui/api/jobs",
                json={"name": name, "type": "sleep", "parameters": {"seconds": 1}},
                headers={"Idempotency-Key": f"key-{name}"},
            )
            assert created.status_code == 201, created.text
            job = created.json()
            run = run_directory_name(job["name"], UUID(job["id"]))
            # Flat: straight under the root, no owner directory between.
            (artifacts / run).mkdir(parents=True)
            (artifacts / run / "out.txt").write_text(body)
            return run

        alice_run = own_run("alice", "alice-run", "alice result")
        bob_run = own_run("bob", "bob-run", "bob result")
        assert {item.name for item in artifacts.iterdir()} == {alice_run, bob_run}

        as_member("alice")
        listing = client.get("/jobs-ui/api/files", params={"path": "Artifacts"})
        own = client.get(
            "/jobs-ui/api/files/download",
            params={"path": f"Artifacts/{alice_run}/out.txt"},
        )
        foreign_listing = client.get(
            "/jobs-ui/api/files", params={"path": f"Artifacts/{bob_run}"}
        )
        foreign_download = client.get(
            "/jobs-ui/api/files/download",
            params={"path": f"Artifacts/{bob_run}/out.txt"},
        )
        foreign_delete = client.request(
            "DELETE", "/jobs-ui/api/files", json={"path": f"Artifacts/{bob_run}"}
        )
        cleared = client.request(
            "DELETE", "/jobs-ui/api/files", json={"path": "Artifacts"}
        )

    # Alice sees her own run and no trace of Bob's, despite them being siblings.
    assert [entry["path"] for entry in listing.json()["entries"]] == [
        f"Artifacts/{alice_run}"
    ]
    assert own.status_code == 200
    assert own.content == b"alice result"
    assert foreign_listing.status_code == 422
    assert foreign_download.status_code == 422
    assert foreign_delete.status_code == 422
    # Clearing removes only what she owns.
    assert cleared.status_code == 200
    assert not (artifacts / alice_run).exists()
    assert (artifacts / bob_run / "out.txt").read_text() == "bob result"


def test_member_storage_pilot_allowlist_enables_only_provisioned_member(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    password = "member password 1234"

    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        assert (
            client.post("/dashboard/login", json={"token": "test-secret"}).status_code
            == 204
        )
        alice = client.post(
            "/dashboard/api/users",
            json={"username": "alice", "password": password, "role": "MEMBER"},
        ).json()
        client.post(
            "/dashboard/api/users",
            json={"username": "bob", "password": password, "role": "MEMBER"},
        )
        client.app.state.settings = replace(
            client.app.state.settings,
            member_storage_user_ids=frozenset({UUID(alice["id"])}),
        )

        client.post("/dashboard/logout")
        client.post(
            "/dashboard/login", json={"username": "alice", "password": password}
        )
        assert client.get("/jobs-ui/api/session").json()["storage_enabled"] is True
        assert client.get("/jobs-ui/api/storage").status_code == 200

        client.post("/dashboard/logout")
        client.post("/dashboard/login", json={"username": "bob", "password": password})
        assert client.get("/jobs-ui/api/session").json()["storage_enabled"] is False
        assert client.get("/jobs-ui/api/storage").status_code == 503
        assert [
            entry["path"]
            for entry in client.get("/jobs-ui/api/files").json()["entries"]
        ] == ["Artifacts"]
        assert (
            client.get("/jobs-ui/api/files", params={"path": "Home"}).status_code == 503
        )
        assert (
            client.get(
                "/jobs-ui/api/storage/download",
                params={"path": "Shared/common.txt"},
            ).status_code
            == 503
        )


def test_member_project_preview_and_submission_resolve_header_input_defaults(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    monkeypatch.setenv("HOME_PLATFORM_MEMBER_STORAGE_ENABLED", "true")
    storage = tmp_path / "storage"
    uploads = tmp_path / "uploads"
    monkeypatch.setenv("HOME_PLATFORM_STORAGE_DIR", str(storage))
    monkeypatch.setenv("HOME_PLATFORM_UPLOAD_DIR", str(uploads))
    password = "member password 1234"
    token_header = {"X-API-Token": "test-secret"}

    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        client.post(
            "/workers/heartbeat",
            headers=token_header,
            json={"worker_id": "worker-a", "supported_types": ["batch"]},
        )
        client.put(
            "/workers/worker-a/capacity",
            headers=token_header,
            json={"max_job_cpu": 2, "max_job_memory_mb": 1024},
        )
        client.post("/dashboard/login", json={"token": "test-secret"})
        alice = client.post(
            "/dashboard/api/users",
            json={"username": "alice", "password": password, "role": "MEMBER"},
        ).json()
        project = storage / "users" / alice["id"] / "Workspace" / "Projects" / "demo"
        inputs = storage / "users" / alice["id"] / "Workspace" / "Inputs"
        project.mkdir(parents=True)
        inputs.mkdir(parents=True)
        (inputs / "cohort.csv").write_text("sample,value\na,1\n")
        (project / "run.sh").write_text("#!/usr/bin/env bash\n")
        (project / "submit.hp").write_text(
            """#!/usr/bin/env bash
#HP --version 1
#HP --name "Resolved NAS input"
#HP --runtime scientific-python:1
#HP --cpus 1
#HP --memory-mb 512
#HP --time-limit 00:05:00
#HP --input dataset=Home/Workspace/Inputs/cohort.csv
#HP --array 1-2
echo "$HOME_PLATFORM_ARRAY_INDEX"
"""
        )

        client.post("/dashboard/logout")
        client.post(
            "/dashboard/login", json={"username": "alice", "password": password}
        )
        preview = client.post(
            "/jobs-ui/api/storage/project-preview",
            json={
                "path": "Home/Workspace/Projects/demo",
                "entrypoint": "submit.hp",
            },
        )
        packaged = client.post(
            "/jobs-ui/api/storage/project-uploads",
            json={"path": "Home/Workspace/Projects/demo"},
        )
        submitted = client.post(
            "/jobs-ui/api/batch-submissions",
            json={
                "project": packaged.json(),
                "entrypoint": "submit.hp",
                "inputs": {},
            },
        )

    assert preview.status_code == 200, preview.text
    assert preview.json()["name"] == "Resolved NAS input"
    assert preview.json()["files"] == ["run.sh", "submit.hp"]
    assert preview.json()["inputs"] == [
        {
            "name": "dataset",
            "default_path": "Home/Workspace/Inputs/cohort.csv",
        }
    ]
    assert packaged.status_code == 201
    assert submitted.status_code == 201, submitted.text
    source = submitted.json()["tasks"][0]["parameters"]["inputs"]["dataset"]
    assert source["path"] == f"users/{alice['id']}/Workspace/Inputs/cohort.csv"


def test_member_workspace_create_and_upload_are_scoped_and_non_overwriting(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    monkeypatch.setenv("HOME_PLATFORM_MEMBER_STORAGE_ENABLED", "true")
    monkeypatch.setenv("HOME_PLATFORM_MEMBER_WORKSPACE_ENABLED", "true")
    storage = tmp_path / "storage"
    monkeypatch.setenv("HOME_PLATFORM_STORAGE_DIR", str(storage))
    monkeypatch.setenv("HOME_PLATFORM_WORKSPACE_DIR", str(storage))
    monkeypatch.setenv("HOME_PLATFORM_MAX_WORKSPACE_UPLOAD_BYTES", "8")
    password = "member password 1234"

    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        client.post("/dashboard/login", json={"token": "test-secret"})
        alice = client.post(
            "/dashboard/api/users",
            json={"username": "alice", "password": password, "role": "MEMBER"},
        ).json()
        workspace = storage / "users" / alice["id"] / "Workspace"
        workspace.mkdir(parents=True)
        (storage / "shared").mkdir()
        client.post("/dashboard/logout")
        client.post(
            "/dashboard/login", json={"username": "alice", "password": password}
        )

        listing = client.get("/jobs-ui/api/storage", params={"path": "Home/Workspace"})
        created = client.post(
            "/jobs-ui/api/workspace/directories",
            json={"path": "Home/Workspace/Projects"},
        )
        uploaded = client.post(
            "/jobs-ui/api/workspace/uploads",
            data={"directory": "Home/Workspace/Projects"},
            files={"file": ("submit.hp", b"12345678")},
        )
        duplicate = client.post(
            "/jobs-ui/api/workspace/uploads",
            data={"directory": "Home/Workspace/Projects"},
            files={"file": ("submit.hp", b"changed")},
        )
        too_large = client.post(
            "/jobs-ui/api/workspace/uploads",
            data={"directory": "Home/Workspace/Projects"},
            files={"file": ("large.bin", b"123456789")},
        )
        escaped = client.post(
            "/jobs-ui/api/workspace/directories",
            json={"path": "Home/Workspace/../escape"},
        )
        original_after_duplicate = (workspace / "Projects" / "submit.hp").read_bytes()
        renamed = client.post(
            "/jobs-ui/api/files/move",
            json={
                "source": "Home/Workspace/Projects/submit.hp",
                "destination": "Home/Workspace/Projects/job.hp",
            },
        )
        copied = client.post(
            "/jobs-ui/api/files/copy",
            json={
                "source": "Home/Workspace/Projects/job.hp",
                "destination": "Home/Workspace/job-copy.hp",
            },
        )
        deleted = client.request(
            "DELETE",
            "/jobs-ui/api/files",
            json={"path": "Home/Workspace/job-copy.hp"},
        )
        protected_root = client.request(
            "DELETE",
            "/jobs-ui/api/files",
            json={"path": "Home/Workspace"},
        )
        shared_delete = client.request(
            "DELETE",
            "/jobs-ui/api/files",
            json={"path": "Shared/common.txt"},
        )

    assert listing.status_code == 200
    assert listing.json()["workspace_writable"] is True
    assert created.status_code == 201
    assert uploaded.status_code == 201
    assert uploaded.json()["path"] == "Home/Workspace/Projects/submit.hp"
    assert original_after_duplicate == b"12345678"
    assert duplicate.status_code in {409, 422}
    assert renamed.status_code == 200
    assert copied.status_code == 201
    assert copied.json()["copied_bytes"] == 8
    assert deleted.status_code == 200
    assert (workspace / "Projects" / "job.hp").read_bytes() == b"12345678"
    assert not (workspace / "job-copy.hp").exists()
    assert protected_root.status_code == 422
    assert shared_delete.status_code == 422
    assert too_large.status_code == 413
    assert not (workspace / "Projects" / "large.bin").exists()
    assert escaped.status_code == 422


def running_job_with_lease(
    client: TestClient, *, provision_artifacts: bool = True
) -> tuple[dict, dict]:
    """Create a job and claim it, returning (job, claim) with a live lease."""
    if (
        provision_artifacts
        and client.app.state.settings.artifact_owner_scoped
        and not client.app.state.settings.artifact_requires_mount
    ):
        (client.app.state.settings.artifact_directory / ADMIN_USER_ID).mkdir(
            parents=True, exist_ok=True
        )
    created = create_sleep_job(client)
    enable_worker(client)
    claimed = claim_job(client)
    assert claimed is not None
    return created, claimed


def test_artifacts_upload_list_and_download(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_OWNER_SCOPED", "true")
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created, claimed = running_job_with_lease(client)
        credentials = {
            "worker_id": "mac-one",
            "lease_token": claimed["lease_token"],
        }
        first = client.post(
            f"/jobs/{created['id']}/artifacts",
            data=credentials,
            files={"file": ("model.joblib", b"weights")},
        )
        second = client.post(
            f"/jobs/{created['id']}/artifacts",
            data=credentials,
            files={"file": ("metrics.json", b'{"accuracy": 1.0}')},
        )
        listing = client.get(f"/jobs/{created['id']}/artifacts")
        download = client.get(f"/jobs/{created['id']}/artifacts/model.joblib")
        resumed = client.get(
            f"/jobs/{created['id']}/artifacts/model.joblib",
            headers={"Range": "bytes=3-"},
        )
        missing = client.get(f"/jobs/{created['id']}/artifacts/absent.bin")

    assert first.status_code == 201
    assert first.json()["sha256"] == hashlib.sha256(b"weights").hexdigest()
    assert first.json()["size_bytes"] == 7
    assert second.status_code == 201
    assert [item["filename"] for item in listing.json()] == [
        "metrics.json",
        "model.joblib",
    ]
    assert listing.json()[1]["sha256"] == hashlib.sha256(b"weights").hexdigest()
    assert download.content == b"weights"
    assert resumed.status_code == 206
    assert resumed.content == b"ghts"
    assert resumed.headers["content-range"] == "bytes 3-6/7"
    assert missing.status_code == 404


def test_artifact_upload_requires_the_current_lease(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created, claimed = running_job_with_lease(client)
        payload = {"file": ("model.joblib", b"weights")}

        impostor = client.post(
            f"/jobs/{created['id']}/artifacts",
            data={"worker_id": "mac-impostor", "lease_token": claimed["lease_token"]},
            files=payload,
        )
        stale_token = client.post(
            f"/jobs/{created['id']}/artifacts",
            data={"worker_id": "mac-one", "lease_token": str(uuid4())},
            files=payload,
        )
        unknown_job = client.post(
            f"/jobs/{uuid4()}/artifacts",
            data={"worker_id": "mac-one", "lease_token": claimed["lease_token"]},
            files=payload,
        )
        # Finishing the job ends the lease, so publishing must stop working too.
        client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "mac-one",
                "lease_token": claimed["lease_token"],
                "result": {"slept_seconds": 1},
            },
        )
        after_completion = client.post(
            f"/jobs/{created['id']}/artifacts",
            data={"worker_id": "mac-one", "lease_token": claimed["lease_token"]},
            files=payload,
        )
        listing = client.get(f"/jobs/{created['id']}/artifacts")

    assert impostor.status_code == 409
    assert stale_token.status_code == 409
    assert unknown_job.status_code == 404
    assert after_completion.status_code == 409
    assert listing.json() == []


def test_artifact_names_cannot_escape_the_job_directory(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created, claimed = running_job_with_lease(client)
        credentials = {
            "worker_id": "mac-one",
            "lease_token": claimed["lease_token"],
        }
        rejected = [
            client.post(
                f"/jobs/{created['id']}/artifacts",
                data=credentials,
                files={"file": (name, b"x")},
            ).status_code
            for name in ("../escape.txt", "/etc/passwd", ".hidden", "", "a/b.txt")
        ]
        download_escape = client.get(
            f"/jobs/{created['id']}/artifacts/..%2F..%2Fetc%2Fpasswd"
        )

    assert rejected == [422, 422, 422, 422, 422]
    assert download_escape.status_code in {404, 422}
    assert not (tmp_path / "escape.txt").exists()


def test_artifact_size_limits_are_enforced(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("HOME_PLATFORM_MAX_ARTIFACT_BYTES", "16")
    monkeypatch.setenv("HOME_PLATFORM_MAX_JOB_ARTIFACT_BYTES", "24")
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created, claimed = running_job_with_lease(client)
        credentials = {
            "worker_id": "mac-one",
            "lease_token": claimed["lease_token"],
        }
        too_big = client.post(
            f"/jobs/{created['id']}/artifacts",
            data=credentials,
            files={"file": ("big.bin", b"x" * 32)},
        )
        accepted = client.post(
            f"/jobs/{created['id']}/artifacts",
            data=credentials,
            files={"file": ("ok.bin", b"x" * 16)},
        )
        over_job_budget = client.post(
            f"/jobs/{created['id']}/artifacts",
            data=credentials,
            files={"file": ("second.bin", b"x" * 16)},
        )

    assert too_big.status_code == 413
    assert accepted.status_code == 201
    assert over_job_budget.status_code == 413


def test_artifacts_refuse_to_write_when_storage_is_not_mounted(
    tmp_path: Path, monkeypatch
) -> None:
    # Pointing at "/" makes the device check see the root filesystem, which is
    # exactly the "SSD is absent, do not fill the boot disk" condition.
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", "/")
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_REQUIRE_MOUNT", "true")
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created, claimed = running_job_with_lease(client)
        refused = client.post(
            f"/jobs/{created['id']}/artifacts",
            data={"worker_id": "mac-one", "lease_token": claimed["lease_token"]},
            files={"file": ("model.joblib", b"weights")},
        )
        listing = client.get(f"/jobs/{created['id']}/artifacts")

    assert refused.status_code == 503
    assert listing.status_code == 503


def test_artifacts_refuse_unprovisioned_owner_directory(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(root))
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_OWNER_SCOPED", "true")
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created, claimed = running_job_with_lease(client, provision_artifacts=False)
        refused = client.post(
            f"/jobs/{created['id']}/artifacts",
            data={"worker_id": "mac-one", "lease_token": claimed["lease_token"]},
            files={"file": ("model.joblib", b"weights")},
        )

    assert refused.status_code == 503
    assert refused.json()["detail"] == (
        "Artifact storage is not provisioned for this job owner"
    )


def python_batch_job(client: TestClient, name: str = "batch") -> tuple[dict, dict]:
    """Submit a python_batch job the way the CLI does: upload, then reference."""
    register_worker_capacity(client, "mac-one", ["python_batch"])
    script = client.post(
        "/uploads/scripts", files={"file": ("train.py", b"print('hi')")}
    ).json()
    dataset = client.post(
        "/uploads/datasets", files={"file": ("data.csv", b"a,b\n1,2\n")}
    ).json()
    created = client.post(
        "/jobs",
        json={
            "name": name,
            "type": "python_batch",
            "parameters": {
                "script": script,
                "dataset": dataset,
                "timeout_seconds": 600,
                "cpu_limit": 2,
                "memory_mb": 2048,
            },
        },
    )
    assert created.status_code == 201
    return created.json(), {"script": script, "dataset": dataset}


def test_finishing_a_job_releases_its_staged_uploads(
    tmp_path: Path, monkeypatch
) -> None:
    uploads = tmp_path / "uploads"
    monkeypatch.setenv("HOME_PLATFORM_UPLOAD_DIR", str(uploads))
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created, refs = python_batch_job(client)
        script_file = uploads / "scripts" / f"{refs['script']['upload_id']}.py"
        dataset_file = uploads / f"{refs['dataset']['upload_id']}.csv"
        assert script_file.exists() and dataset_file.exists()

        enable_worker(client, "mac-one", ["python_batch"])
        claimed = client.post(
            "/workers/claim",
            json={"worker_id": "mac-one", "supported_types": ["python_batch"]},
        ).json()
        client.post(
            f"/jobs/{created['id']}/complete",
            json={
                "worker_id": "mac-one",
                "lease_token": claimed["lease_token"],
                "result": {
                    "script_sha256": refs["script"]["sha256"],
                    "dataset_sha256": refs["dataset"]["sha256"],
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "output_files": [],
                    "artifact_uri": f"worker://mac-one/{created['id']}/",
                },
            },
        )

    assert not script_file.exists(), "finished job left its script staged"
    assert not dataset_file.exists(), "finished job left its dataset staged"


def test_uploads_shared_with_an_unfinished_job_are_kept(
    tmp_path: Path, monkeypatch
) -> None:
    uploads = tmp_path / "uploads"
    monkeypatch.setenv("HOME_PLATFORM_UPLOAD_DIR", str(uploads))
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        first, refs = python_batch_job(client, "first")
        # A second job reusing the same uploads, left QUEUED.
        client.post(
            "/jobs",
            json={
                "name": "second",
                "type": "python_batch",
                "parameters": {
                    "script": refs["script"],
                    "dataset": refs["dataset"],
                    "timeout_seconds": 600,
                    "cpu_limit": 2,
                    "memory_mb": 2048,
                },
            },
        )
        enable_worker(client, "mac-one", ["python_batch"])
        claimed = client.post(
            "/workers/claim",
            json={"worker_id": "mac-one", "supported_types": ["python_batch"]},
        ).json()
        client.post(
            f"/jobs/{claimed['id']}/fail",
            json={
                "worker_id": "mac-one",
                "lease_token": claimed["lease_token"],
                "error": "boom",
            },
        )
        script_file = uploads / "scripts" / f"{refs['script']['upload_id']}.py"

    assert script_file.exists(), "deleted an upload another queued job still needs"
    assert first is not None


def test_artifacts_can_be_deleted_explicitly(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created, claimed = running_job_with_lease(client)
        credentials = {"worker_id": "mac-one", "lease_token": claimed["lease_token"]}
        for name in ("model.joblib", "metrics.json"):
            client.post(
                f"/jobs/{created['id']}/artifacts",
                data=credentials,
                files={"file": (name, b"payload")},
            )
        one = client.delete(f"/jobs/{created['id']}/artifacts/metrics.json")
        remaining = client.get(f"/jobs/{created['id']}/artifacts")
        everything = client.delete(f"/jobs/{created['id']}/artifacts")
        after = client.get(f"/jobs/{created['id']}/artifacts")
        again = client.delete(f"/jobs/{created['id']}/artifacts")
        # The job record itself must survive; only the bytes go.
        job = client.get(f"/jobs/{created['id']}")

    assert one.status_code == 200
    assert one.json()["deleted"] == ["metrics.json"]
    assert [item["filename"] for item in remaining.json()] == ["model.joblib"]
    assert everything.json()["deleted"] == ["model.joblib"]
    assert after.json() == []
    assert again.status_code == 404
    assert job.json()["status"] == "RUNNING"


def test_store_cap_evicts_the_least_recently_touched_job(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("HOME_PLATFORM_MAX_ARTIFACT_STORE_BYTES", "600")
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        first, first_claim = running_job_with_lease(client)
        client.post(
            f"/jobs/{first['id']}/artifacts",
            data={"worker_id": "mac-one", "lease_token": first_claim["lease_token"]},
            files={"file": ("old.bin", b"x" * 400)},
        )
        # Finish the first job so a second can be claimed by the same worker.
        client.post(
            f"/jobs/{first['id']}/complete",
            json={
                "worker_id": "mac-one",
                "lease_token": first_claim["lease_token"],
                "result": {"slept_seconds": 1},
            },
        )
        second = create_sleep_job(client)
        second_claim = claim_job(client)
        assert second_claim is not None
        pushed = client.post(
            f"/jobs/{second['id']}/artifacts",
            data={"worker_id": "mac-one", "lease_token": second_claim["lease_token"]},
            files={"file": ("new.bin", b"y" * 400)},
        )
        old = client.get(f"/jobs/{first['id']}/artifacts")
        new = client.get(f"/jobs/{second['id']}/artifacts")

    assert pushed.status_code == 201
    # Eviction reports the run directory it removed, which is derived from the
    # job's name rather than its UUID.
    assert pushed.json()["evicted_runs"] == [
        run_directory_name(None, UUID(first["id"]))
    ]
    assert old.json() == [], "the older job should have been evicted"
    assert [item["filename"] for item in new.json()] == ["new.bin"]


def test_results_are_published_under_a_directory_named_after_the_job(
    tmp_path: Path, monkeypatch
) -> None:
    """An owner browsing over SMB should recognise the run, not read UUIDs."""
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(artifacts))
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created = create_sleep_job(client, name="SVM model")
        enable_worker(client)
        claimed = claim_job(client)
        assert claimed is not None
        pushed = client.post(
            f"/jobs/{created['id']}/artifacts",
            data={"worker_id": "mac-one", "lease_token": claimed["lease_token"]},
            files={"file": ("metrics.json", b"{}")},
        )
        listed = client.get(f"/jobs/{created['id']}/artifacts")

    assert pushed.status_code == 201
    expected = artifacts / f"SVM-model-{UUID(created['id']).hex[:8]}"
    assert (expected / "metrics.json").is_file()
    # The job's UUID directory is not created alongside it.
    assert not (artifacts / created["id"]).exists()
    assert [item["filename"] for item in listed.json()] == ["metrics.json"]


def test_group_results_use_the_parent_name_even_when_child_names_differ(
    tmp_path: Path, monkeypatch
) -> None:
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(artifacts))
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created = client.post(
            "/job-groups",
            json={
                "name": "Shared experiment",
                "tasks": [
                    {
                        "task_id": "task-001",
                        "job": {
                            "name": "Different child label",
                            "type": "sleep",
                            "parameters": {"seconds": 1},
                        },
                    }
                ],
            },
        )
        assert created.status_code == 201
        enable_worker(client)
        claimed = claim_job(client)
        assert claimed is not None
        pushed = client.post(
            f"/jobs/{claimed['id']}/artifacts",
            data={"worker_id": "mac-one", "lease_token": claimed["lease_token"]},
            files={"file": ("result.txt", b"done")},
        )

    assert pushed.status_code == 201
    group_id = UUID(created.json()["id"])
    expected = artifacts / run_directory_name("Shared experiment", group_id)
    assert (expected / "task-001" / "result.txt").is_file()
    assert not (
        artifacts / run_directory_name("Different child label", group_id)
    ).exists()


def test_results_published_before_the_rename_stay_reachable(
    tmp_path: Path, monkeypatch
) -> None:
    """Changing the layout must not strand already-completed work."""
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(artifacts))
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        created = create_sleep_job(client, name="legacy run")
        # Stage results exactly as a pre-0.33.0 deployment left them.
        legacy = artifacts / created["id"]
        legacy.mkdir(parents=True)
        (legacy / "report.txt").write_text("published before the rename")

        listed = client.get(f"/jobs/{created['id']}/artifacts")
        downloaded = client.get(f"/jobs/{created['id']}/artifacts/report.txt")

    assert [item["filename"] for item in listed.json()] == ["report.txt"]
    assert downloaded.status_code == 200
    assert downloaded.text == "published before the rename"
