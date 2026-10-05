"""Pin the HTTP status and detail each service error maps to.

Every mapped error type is exercised on at least one token route and one
Job Desk route, so moving the mapping cannot silently change a response.
"""

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.test_api import create_sleep_job, register_worker_capacity

MEMBER_PASSWORD = "member password 1234"


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    storage = tmp_path / "storage"
    (storage / "shared").mkdir(parents=True)
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "test-secret")
    monkeypatch.setenv("HOME_PLATFORM_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("HOME_PLATFORM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("HOME_PLATFORM_STORAGE_DIR", str(storage))
    monkeypatch.setenv("HOME_PLATFORM_MEMBER_STORAGE_ENABLED", "true")
    with TestClient(
        create_app(tmp_path / "jobs.db"), headers={"X-API-Token": "test-secret"}
    ) as client:
        yield client


def login_member(client: TestClient, username: str = "alice") -> dict:
    assert client.post("/dashboard/login", json={"token": "test-secret"}).status_code
    created = client.post(
        "/dashboard/api/users",
        json={"username": username, "password": MEMBER_PASSWORD, "role": "MEMBER"},
    )
    assert created.status_code == 201
    client.post("/dashboard/logout")
    login = client.post(
        "/dashboard/login", json={"username": username, "password": MEMBER_PASSWORD}
    )
    assert login.status_code == 204
    return created.json()


def sleep_job(name: str, worker: str | None = None) -> dict:
    job = {"name": name, "type": "sleep", "parameters": {"seconds": 1}}
    if worker is not None:
        job["target_worker_id"] = worker
    return job


def group(name: str, job: dict) -> dict:
    return {"name": name, "tasks": [{"task_id": "t1", "job": job}]}


def batch_job(script: dict, dataset: dict, cpu_limit: int) -> dict:
    return {
        "name": "heavy",
        "type": "python_batch",
        "parameters": {
            "script": script,
            "dataset": dataset,
            "timeout_seconds": 600,
            "cpu_limit": cpu_limit,
            "memory_mb": 2048,
        },
    }


def test_unknown_target_worker_maps_to_404_with_its_id(client: TestClient) -> None:
    job = client.post("/jobs", json=sleep_job("a", "ghost"))
    grouped = client.post("/job-groups", json=group("g", sleep_job("a", "ghost")))
    assert (job.status_code, job.json()["detail"]) == (404, "ghost")
    assert (grouped.status_code, grouped.json()["detail"]) == (404, "ghost")

    login_member(client)
    portal_job = client.post("/jobs-ui/api/jobs", json=sleep_job("a", "ghost"))
    portal_group = client.post(
        "/jobs-ui/api/job-groups", json=group("g", sleep_job("a", "ghost"))
    )
    assert (portal_job.status_code, portal_job.json()["detail"]) == (404, "ghost")
    assert (portal_group.status_code, portal_group.json()["detail"]) == (404, "ghost")


def test_worker_update_routes_keep_their_own_not_found_detail(
    client: TestClient,
) -> None:
    detail = "Worker with ID ghost not found"
    token_update = client.patch("/workers/ghost", json={"enabled": True})
    token_capacity = client.put(
        "/workers/ghost/capacity", json={"max_job_cpu": 1, "max_job_memory_mb": 512}
    )
    assert (token_update.status_code, token_update.json()["detail"]) == (404, detail)
    assert (token_capacity.status_code, token_capacity.json()["detail"]) == (
        404,
        detail,
    )
    assert client.post("/dashboard/login", json={"token": "test-secret"}).status_code
    admin_update = client.patch("/dashboard/api/workers/ghost", json={"enabled": True})
    admin_capacity = client.put(
        "/dashboard/api/workers/ghost/capacity",
        json={"max_job_cpu": 1, "max_job_memory_mb": 512},
    )
    assert (admin_update.status_code, admin_update.json()["detail"]) == (404, detail)
    assert (admin_capacity.status_code, admin_capacity.json()["detail"]) == (
        404,
        detail,
    )


def test_scheduling_capacity_maps_to_422_with_service_message(
    client: TestClient,
) -> None:
    register_worker_capacity(client, "mac-one", ["python_batch"], max_job_cpu=1)
    script = client.post(
        "/uploads/scripts", files={"file": ("train.py", b"print('hi')")}
    ).json()
    dataset = client.post(
        "/uploads/datasets", files={"file": ("data.csv", b"a,b\n1,2\n")}
    ).json()
    detail = (
        "No registered worker is configured to accept a job requesting "
        "2 CPU and 2048 MiB RAM"
    )
    job = client.post("/jobs", json=batch_job(script, dataset, 2))
    grouped = client.post("/job-groups", json=group("g", batch_job(script, dataset, 2)))
    assert (job.status_code, job.json()["detail"]) == (422, detail)
    assert (grouped.status_code, grouped.json()["detail"]) == (422, detail)

    login_member(client)
    member_script = client.post(
        "/jobs-ui/api/script-uploads",
        files={"file": ("train.py", b"print('hi')", "text/x-python")},
    ).json()
    member_dataset = client.post(
        "/jobs-ui/api/uploads", files={"file": ("data.csv", b"a\n1\n", "text/csv")}
    ).json()
    portal_job = client.post(
        "/jobs-ui/api/jobs", json=batch_job(member_script, member_dataset, 2)
    )
    portal_group = client.post(
        "/jobs-ui/api/job-groups",
        json=group("g", batch_job(member_script, member_dataset, 2)),
    )
    assert (portal_job.status_code, portal_job.json()["detail"]) == (422, detail)
    assert (portal_group.status_code, portal_group.json()["detail"]) == (422, detail)


def test_idempotency_conflicts_map_to_409(client: TestClient) -> None:
    headers = {"Idempotency-Key": "pin-key"}
    assert client.post("/jobs", headers=headers, json=sleep_job("a")).status_code == 201
    job = client.post("/jobs", headers=headers, json=sleep_job("b"))
    assert (
        client.post(
            "/job-groups", headers=headers, json=group("g", sleep_job("a"))
        ).status_code
        == 201
    )
    grouped = client.post(
        "/job-groups", headers=headers, json=group("h", sleep_job("a"))
    )
    assert (job.status_code, job.json()["detail"]) == (
        409,
        "Idempotency key was already used for a different request",
    )
    assert (grouped.status_code, grouped.json()["detail"]) == (
        409,
        "Idempotency key was already used for a different group request",
    )

    login_member(client)
    assert (
        client.post(
            "/jobs-ui/api/jobs", headers=headers, json=sleep_job("a")
        ).status_code
        == 201
    )
    portal_job = client.post("/jobs-ui/api/jobs", headers=headers, json=sleep_job("b"))
    assert (
        client.post(
            "/jobs-ui/api/job-groups", headers=headers, json=group("g", sleep_job("a"))
        ).status_code
        == 201
    )
    portal_group = client.post(
        "/jobs-ui/api/job-groups", headers=headers, json=group("h", sleep_job("a"))
    )
    assert (portal_job.status_code, portal_job.json()["detail"]) == (
        409,
        "Idempotency key was already used for a different request",
    )
    assert (portal_group.status_code, portal_group.json()["detail"]) == (
        409,
        "Idempotency key was already used for a different group request",
    )


def test_storage_policy_errors_map_to_422_with_policy_message(
    client: TestClient,
) -> None:
    listing = client.get("/storage", params={"path": "../outside"})
    reference = client.post("/storage/references", json={"path": "../outside"})
    assert (listing.status_code, listing.json()["detail"]) == (
        422,
        "storage path must stay inside HomeStorage",
    )
    assert (reference.status_code, reference.json()["detail"]) == (
        422,
        "storage path must stay inside HomeStorage",
    )

    login_member(client)
    member_listing = client.get("/jobs-ui/api/storage", params={"path": "Elsewhere"})
    member_download = client.get(
        "/jobs-ui/api/storage/download", params={"path": "Elsewhere/x"}
    )
    member_reference = client.post(
        "/jobs-ui/api/storage/references", json={"path": "Elsewhere/x"}
    )
    files = client.get("/jobs-ui/api/files", params={"path": "Elsewhere"})
    assert (member_listing.status_code, member_listing.json()["detail"]) == (
        422,
        "member storage paths must start with Home or Shared",
    )
    assert (member_download.status_code, member_download.json()["detail"]) == (
        422,
        "member storage paths must start with Home or Shared",
    )
    assert (member_reference.status_code, member_reference.json()["detail"]) == (
        422,
        "member storage paths must start with Home or Shared",
    )
    assert (files.status_code, files.json()["detail"]) == (
        422,
        "choose Home, Shared or Artifacts",
    )


def test_batch_script_errors_map_to_422_with_parser_message(
    client: TestClient,
) -> None:
    bad_archive = client.post(
        "/uploads/projects", files={"file": ("p.zip", b"not a zip", "application/zip")}
    )
    assert (bad_archive.status_code, bad_archive.json()["detail"]) == (
        422,
        "project must be a valid ZIP archive",
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("submit.hp", "echo not a batch header\n")
    project = client.post(
        "/uploads/projects",
        files={"file": ("p.zip", buffer.getvalue(), "application/zip")},
    ).json()
    submission = client.post("/batch-submissions", json={"project": project})
    assert (submission.status_code, submission.json()["detail"]) == (
        422,
        "line 1 must be exactly #!/usr/bin/env bash",
    )

    login_member(client)
    member_bad = client.post(
        "/jobs-ui/api/project-uploads",
        files={"file": ("p.zip", b"not a zip", "application/zip")},
    )
    assert (member_bad.status_code, member_bad.json()["detail"]) == (
        422,
        "project must be a valid ZIP archive",
    )
    member_project = client.post(
        "/jobs-ui/api/project-uploads",
        files={"file": ("p.zip", buffer.getvalue(), "application/zip")},
    ).json()
    member_submission = client.post(
        "/jobs-ui/api/batch-submissions", json={"project": member_project}
    )
    assert (member_submission.status_code, member_submission.json()["detail"]) == (
        422,
        "line 1 must be exactly #!/usr/bin/env bash",
    )


def test_job_transition_errors_map_to_409_with_service_message(
    client: TestClient,
) -> None:
    created = create_sleep_job(client)
    assert client.post(f"/jobs/{created['id']}/cancel").status_code == 200
    repeated = client.post(f"/jobs/{created['id']}/cancel")
    assert (repeated.status_code, repeated.json()["detail"]) == (
        409,
        f"Job {created['id']} is already terminal with status FAILED",
    )
    heartbeat = client.post(
        "/workers/heartbeat",
        json={
            "worker_id": "mac-one",
            "supported_types": ["sleep"],
            "current_job_id": created["id"],
            "lease_token": "00000000-0000-0000-0000-000000000000",
        },
    )
    assert (heartbeat.status_code, heartbeat.json()["detail"]) == (
        409,
        "The job lease is no longer valid",
    )
    artifact = client.post(
        f"/jobs/{created['id']}/artifacts",
        data={
            "worker_id": "mac-one",
            "lease_token": "00000000-0000-0000-0000-000000000000",
        },
        files={"file": ("out.txt", b"x")},
    )
    assert (artifact.status_code, artifact.json()["detail"]) == (
        409,
        f"Job {created['id']} is FAILED; worker 'mac-one' does not hold its "
        "current lease",
    )

    login_member(client)
    portal = client.post("/jobs-ui/api/jobs", json=sleep_job("mine")).json()
    assert client.post(f"/jobs-ui/api/jobs/{portal['id']}/cancel").status_code == 200
    portal_repeat = client.post(f"/jobs-ui/api/jobs/{portal['id']}/cancel")
    assert (portal_repeat.status_code, portal_repeat.json()["detail"]) == (
        409,
        f"Job {portal['id']} is already terminal with status FAILED",
    )
