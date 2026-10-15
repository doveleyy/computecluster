import hashlib
import json
import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.app_registry import AppRegistry, EnvironmentMismatch
from app.app_workloads import WorkloadStore
from app.config import InstanceFacts
from app.database import Database
from app.repository import JobRepository
from ops.sandbox.seed import (
    PERSONAS,
    SandboxRefusal,
    plan_from_environment,
    resolve_manifest,
    seed,
)

TOKEN = "t" * 40
SUBMIT = (
    "#!/usr/bin/env bash\n#HP --version 1\n#HP --name report\n"
    "#HP --runtime scientific-python:1\n#HP --cpus 0.5\n#HP --memory-mb 256\n"
    "#HP --time-limit 00:01:00\n#HP --input data\n#HP --array 1-1\n"
    'python "$HOME_PLATFORM_PROJECT_DIR/report.py"\n'
)


def template(**overrides):
    document = {
        "contract_version": 1,
        "app_id": "example-reports",
        "source_commit": "${SOURCE_COMMIT}",
        "image": "${IMAGE}",
        "runtime": {
            "port": 8080,
            "health_path": "/health",
            "ready_path": "/ready",
            "version_path": "/version",
        },
        "capabilities": ["identity", "jobs", "files"],
        "workloads": [
            {
                "id": "csv-summary",
                "asset": "csv-summary.zip",
                "sha256": "${SHA256}",
                "entrypoint": "submit.hp",
                "runtime": "scientific-python:1",
            }
        ],
    }
    document.update(overrides)
    return json.dumps(document, indent=2).encode()


@pytest.fixture
def root(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    with zipfile.ZipFile(app_dir / "csv-summary.zip", "w") as archive:
        archive.writestr("submit.hp", SUBMIT)
        archive.writestr("report.py", "print('ok')\n")
    (tmp_path / "manifest.json").write_bytes(template())
    return tmp_path


def environment(**extra):
    return {"HP_SANDBOX_TOKEN": TOKEN, "PATH": "/usr/bin", **extra}


def test_template_markers_resolve_to_synthetic_values_and_archive_digest(root):
    archive_digest = hashlib.sha256(
        (root / "app" / "csv-summary.zip").read_bytes()
    ).hexdigest()
    resolved = json.loads(resolve_manifest(template(), root / "app"))
    assert resolved["source_commit"] == "0" * 40
    assert resolved["image"] == "ghcr.io/sandbox/example-reports@sha256:" + "0" * 64
    assert resolved["workloads"][0]["sha256"] == archive_digest
    final = template(
        source_commit="a" * 40,
        image="ghcr.io/x/y@sha256:" + "b" * 64,
        workloads=[{**json.loads(template())["workloads"][0], "sha256": "c" * 64}],
    )
    assert resolve_manifest(final, root / "app") == final


def test_sandbox_refuses_core_and_fixture_configuration(root):
    for name in (
        "HOME_PLATFORM_STORAGE_DIR",
        "HOME_PLATFORM_DB_PATH",
        "HP_FIXTURE_USER",
        "APP_ALLOW_DEV_IDENTITY",
        "HOME_PLATFORM_DEV_USER",
        "PREVIEW_APP_HOST",
    ):
        with pytest.raises(SandboxRefusal, match=name):
            plan_from_environment(environment(**{name: "x"}), root)
    with pytest.raises(SandboxRefusal, match="HP_SANDBOX_TOKEN"):
        plan_from_environment(environment(HP_SANDBOX_TOKEN="short"), root)
    with pytest.raises(SandboxRefusal, match="HP_SANDBOX_APP_PORT"):
        plan_from_environment(environment(HP_SANDBOX_APP_PORT="0"), root)
    with pytest.raises(SandboxRefusal, match=r"manifest\.json"):
        plan_from_environment(environment(), root / "missing")


PUBLISHED = template(
    source_commit="a" * 40,
    image="ghcr.io/x/y@sha256:" + "b" * 64,
    workloads=[{**json.loads(template())["workloads"][0], "sha256": "c" * 64}],
)


@pytest.mark.parametrize("manifest", [template(), PUBLISHED], ids=["template", "final"])
def test_sandbox_names_a_missing_workload_archive(root, manifest):
    (root / "manifest.json").write_bytes(manifest)
    (root / "app" / "csv-summary.zip").unlink()
    with pytest.raises(SandboxRefusal, match=r"csv-summary\.zip is missing; package"):
        plan_from_environment(environment(), root)


@pytest.mark.skipif(os.getuid() == 0, reason="root reads any file")
@pytest.mark.parametrize("name", ["manifest.json", "app/csv-summary.zip"])
def test_sandbox_names_the_reader_uid_for_an_unreadable_mount(root, name):
    (root / name).chmod(0o200)
    reader = rf"not readable by the sandbox user \(uid {os.getuid()}\)"
    with pytest.raises(SandboxRefusal, match=reader):
        plan_from_environment(environment(), root)


def test_seed_registers_installs_issues_and_converges_on_restart(root):
    plan = plan_from_environment(environment(), root)
    first = seed(plan)
    assert first["registration"] == "recorded"
    assert first["credential"] == "issued"
    assert set(first["users"]) == {persona.login for persona in PERSONAS}
    credential = plan.credential_file.read_text()
    assert credential.startswith("hpa1.")
    api_token = (plan.state_dir / "api-token").read_text()
    assert len(api_token.strip()) >= 32 and api_token != credential
    assert (plan.state_dir / "ingress-token").read_text() == TOKEN + "\n"
    assert plan.app.route == "/apps/example-reports"

    second = seed(plan)
    assert second["registration"] == "unchanged"
    assert second["credential"] == "retained"
    assert second["release_sha256"] == first["release_sha256"]
    assert second["installed_workloads"] == first["installed_workloads"]
    assert second["users"] == first["users"]
    assert plan.credential_file.read_text() == credential

    plan.credential_file.unlink()
    assert seed(plan)["credential"] == "rotated"
    assert plan.credential_file.read_text() != credential

    database = Database(plan.database_path)
    try:
        registry = AppRegistry(database)
        instance = registry.instance()
        assert instance is not None and instance.environment == "sandbox"
        assert registry.verify_credential(plan.credential_file.read_text().strip())
        assert not registry.verify_credential(credential.strip())
        installed = WorkloadStore(database).list("example-reports")
        assert [item.workload.id for item in installed] == ["csv-summary"]
        workers = JobRepository(database).list_workers(datetime.now(UTC))
        assert [
            (w.id, w.enabled, w.max_job_cpu, w.max_job_memory_mb) for w in workers
        ] == [("sandbox-worker", True, 1.0, 512)]
        with pytest.raises(EnvironmentMismatch):
            registry.publish_instance(
                type(instance)("production", instance.app_api_url, "UTC")
            )
    finally:
        database.close()


def test_seed_refuses_foreign_database_without_migrating(root):
    plan = plan_from_environment(environment(), root)
    plan.state_dir.mkdir(parents=True)
    database = Database(plan.database_path)
    try:
        database.initialize()
        AppRegistry(database).publish_instance(
            InstanceFacts("production", "http://elsewhere:8000/api/apps/v1", "UTC")
        )
    finally:
        database.close()
    before = plan.database_path.read_bytes()

    with pytest.raises(SandboxRefusal, match="different platform instance"):
        seed(plan)

    assert plan.database_path.read_bytes() == before
    assert not (plan.state_dir / "storage").exists()


def test_seed_keeps_disabled_persona_disabled_and_storage_per_user(root):
    plan = plan_from_environment(environment(), root)
    users = seed(plan)["users"]
    storage = plan.state_dir / "storage"
    for persona in PERSONAS:
        sample = storage / "users" / users[persona.login] / "Inputs" / "sample.csv"
        assert sample.is_file()
    database = Database(plan.database_path)
    try:
        from app.accounts import AccountStore

        accounts = AccountStore(database)
        disabled = {
            item.username: item.disabled for item in accounts.list() if item.username
        }
    finally:
        database.close()
    assert disabled["dana"] is True
    assert disabled["alex"] is False and disabled["sam"] is False
    assert (
        (storage / "shared" / "Inputs" / "shared.csv")
        .read_bytes()
        .startswith(b"name,score")
    )
    assert isinstance(plan.database_path, Path)
