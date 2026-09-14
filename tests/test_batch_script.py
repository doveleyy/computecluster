import hashlib
import io
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.batch_script import BatchScriptError, parse_batch_script
from app.main import create_app
from contracts.models import (
    BatchParameters,
    UploadedInputReference,
    UploadedProjectReference,
)
from worker.container_runner import run_batch
from worker.data_plane import WorkerWorkspace

SCRIPT = """#!/usr/bin/env bash
#HP --version 1
#HP --name "four experiments"
#HP --runtime scientific-python:1
#HP --cpus 1.5
#HP --memory-mb 1024
#HP --time-limit 02:03:04
#HP --input cohort.csv
#HP --env MODEL=svm
#HP --array 3-6

set -euo pipefail
bash "$HOME_PLATFORM_PROJECT_DIR/run-one.sh" "$HOME_PLATFORM_ARRAY_INDEX"
"""


def project_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("submit.hp", SCRIPT)
        archive.writestr(
            "run-one.sh",
            '#!/usr/bin/env bash\necho "$1" > "$HOME_PLATFORM_OUTPUT_DIR/index.txt"\n',
        )
    return output.getvalue()


def test_parser_reads_numeric_array_and_resources() -> None:
    parsed = parse_batch_script(SCRIPT)

    assert (parsed.array_start, parsed.array_end) == (3, 6)
    assert parsed.timeout_seconds == 2 * 3600 + 3 * 60 + 4
    assert parsed.environment == {"MODEL": "svm"}
    assert [(item.name, item.default_path) for item in parsed.inputs] == [
        ("cohort.csv", None)
    ]


def test_parser_accepts_safe_default_storage_input_paths() -> None:
    parsed = parse_batch_script(
        SCRIPT.replace(
            "#HP --input cohort.csv",
            '#HP --input "cohort.csv=Home/Inputs/cohort one.csv"',
        )
    )

    assert [(item.name, item.default_path) for item in parsed.inputs] == [
        ("cohort.csv", "Home/Inputs/cohort one.csv")
    ]


@pytest.mark.parametrize(
    "value",
    [
        "dataset=/etc/passwd",
        "dataset=../secret.csv",
        "dataset=users/someone/secret.csv",
        "dataset=Home",
        r"dataset=Home\\secret.csv",
    ],
)
def test_parser_rejects_unsafe_default_storage_input_paths(value: str) -> None:
    with pytest.raises(BatchScriptError, match=r"Home/\.\.\. or Shared/\.\.\."):
        parse_batch_script(SCRIPT.replace("cohort.csv", value))


def test_parser_rejects_manifest_style_array() -> None:
    with pytest.raises(BatchScriptError, match="START-END"):
        parse_batch_script(SCRIPT.replace("#HP --array 3-6", "#HP --array tasks.yaml"))


def test_api_upload_compiles_one_group_with_numeric_children(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        client.post(
            "/workers/heartbeat",
            json={"worker_id": "worker-a", "supported_types": ["batch"]},
        )
        client.put(
            "/workers/worker-a/capacity",
            json={"max_job_cpu": 2, "max_job_memory_mb": 2048},
        )
        archive = project_zip()
        uploaded = client.post(
            "/uploads/projects",
            files={"file": ("project.zip", archive, "application/zip")},
        )
        assert uploaded.status_code == 201
        input_upload = client.post(
            "/uploads/inputs",
            files={"file": ("anything.parquet", b"input bytes")},
        )
        assert input_upload.status_code == 201

        created = client.post(
            "/batch-submissions",
            json={
                "project": uploaded.json(),
                "entrypoint": "submit.hp",
                "inputs": {"cohort.csv": input_upload.json()},
            },
        )
        client.patch("/workers/worker-a", json={"enabled": True})
        claimed = client.post(
            "/workers/claim",
            json={"worker_id": "worker-a", "supported_types": ["batch"]},
        )

    assert created.status_code == 201, created.text
    group = created.json()
    assert group["name"] == "four experiments"
    assert [task["task_id"] for task in group["tasks"]] == ["3", "4", "5", "6"]
    assert [task["parameters"]["array_index"] for task in group["tasks"]] == [
        3,
        4,
        5,
        6,
    ]
    assert all(task["type"] == "batch" for task in group["tasks"])
    assert all("cohort.csv" in task["parameters"]["inputs"] for task in group["tasks"])
    assert claimed.status_code == 200
    assert claimed.json()["parameters"]["array_index"] == 3


def test_batch_runner_passes_index_to_isolated_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = project_zip()
    parameters = BatchParameters(
        project=UploadedProjectReference(
            upload_id=uuid4(),
            sha256=hashlib.sha256(archive).hexdigest(),
            size_bytes=len(archive),
        ),
        entrypoint="submit.hp",
        runtime="scientific-python:1",
        timeout_seconds=30,
        cpu_limit=1,
        memory_mb=512,
        array_index=7,
        environment={"MODEL": "svm"},
        inputs={
            "cohort.csv": UploadedInputReference(
                upload_id=uuid4(),
                sha256=hashlib.sha256(b"input").hexdigest(),
                size_bytes=5,
            )
        },
    )
    workspace = WorkerWorkspace(
        root=tmp_path / "worker",
        allowed_dataset_hosts=frozenset(),
        max_dataset_bytes=1024,
        control_plane_url="http://control-plane",
        api_token="secret",
        docker_executable="docker",
    )
    commands: list[list[str]] = []

    def materialize(_reference, _workspace, destination, cancellation_event=None):
        destination.mkdir(parents=True)
        (destination / "submit.hp").write_text(SCRIPT)

    source_input = tmp_path / "source.input"
    source_input.write_bytes(b"input")

    def run(command, *_args, **_kwargs):
        commands.append(command)
        return type(
            "Completed", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""}
        )()

    monkeypatch.setattr("worker.container_runner.materialize_project", materialize)
    monkeypatch.setattr(
        "worker.container_runner.materialize_batch_input",
        lambda *_args, **_kwargs: source_input,
    )
    monkeypatch.setattr("worker.container_runner._run_container", run)
    monkeypatch.setattr("worker.container_runner._remove_container", lambda *_: None)

    result = run_batch(uuid4(), "worker-a", parameters, workspace)

    command = commands[0]
    staged_input = next((workspace.root / "runs").glob("*/input/cohort.csv"))
    assert staged_input.stat().st_ino == source_input.stat().st_ino
    assert "HOME_PLATFORM_ARRAY_INDEX=7" in command
    assert "MODEL=svm" in command
    assert "HOME_PLATFORM_INPUT_DIR=/workspace/input" in command
    assert command[-2:] == ["bash", "/workspace/project/submit.hp"]
    assert "secret" not in command
    assert result.stdout == "ok\n"
