import hashlib
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from contracts.models import (
    DatasetReference,
    StorageInputReference,
    UploadedDatasetReference,
)
from worker.data_plane import (
    DatasetPolicyError,
    WorkerWorkspace,
    materialize_batch_input,
    materialize_dataset,
)


class ResponseStream:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    def __enter__(self) -> httpx.Response:
        return self.response

    def __exit__(self, *_: object) -> None:
        self.response.close()


def reference_for(content: bytes) -> DatasetReference:
    return DatasetReference(
        url="https://datasets.example/input.csv",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )


def workspace(tmp_path: Path) -> WorkerWorkspace:
    return WorkerWorkspace(
        root=tmp_path / "worker-data",
        allowed_dataset_hosts=frozenset({"datasets.example"}),
        max_dataset_bytes=1024 * 1024,
    )


def test_dataset_download_is_verified_and_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"name,value\nalpha,1\nbeta,2\n"
    calls = 0

    def stream(*args: object, **kwargs: object) -> ResponseStream:
        nonlocal calls
        calls += 1
        return ResponseStream(
            httpx.Response(
                200,
                content=content,
                headers={"Content-Length": str(len(content))},
                request=httpx.Request("GET", "https://datasets.example/input.csv"),
            )
        )

    monkeypatch.setattr("worker.data_plane.httpx.stream", stream)
    reference = reference_for(content)

    first = materialize_dataset(reference, workspace(tmp_path))
    second = materialize_dataset(reference, workspace(tmp_path))

    assert first == second
    assert first.read_bytes() == content
    assert calls == 1


def test_dataset_download_rejects_wrong_hash_and_removes_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = b"expected"
    received = b"tampered"

    def stream(*args: object, **kwargs: object) -> ResponseStream:
        return ResponseStream(
            httpx.Response(
                200,
                content=received,
                headers={"Content-Length": str(len(received))},
                request=httpx.Request("GET", "https://datasets.example/input.csv"),
            )
        )

    monkeypatch.setattr("worker.data_plane.httpx.stream", stream)
    reference = DatasetReference(
        url="https://datasets.example/input.csv",
        sha256=hashlib.sha256(expected).hexdigest(),
        size_bytes=len(received),
    )

    with pytest.raises(DatasetPolicyError, match="SHA-256"):
        materialize_dataset(reference, workspace(tmp_path))

    assert list((tmp_path / "worker-data/cache").iterdir()) == []


def test_uploaded_dataset_is_fetched_from_control_plane_with_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"name,value\nalpha,1\n"
    upload_id = uuid4()
    reference = UploadedDatasetReference(
        upload_id=upload_id,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )
    worker_workspace = WorkerWorkspace(
        root=tmp_path / "worker-data",
        allowed_dataset_hosts=frozenset(),
        max_dataset_bytes=1024,
        control_plane_url="http://pi.local:8000",
        api_token="worker-secret",
    )
    request_details: dict[str, object] = {}

    def stream(*args: object, **kwargs: object) -> ResponseStream:
        request_details["url"] = args[1]
        request_details["headers"] = kwargs["headers"]
        return ResponseStream(
            httpx.Response(
                200,
                content=content,
                headers={"Content-Length": str(len(content))},
                request=httpx.Request("GET", str(args[1])),
            )
        )

    monkeypatch.setattr("worker.data_plane.httpx.stream", stream)

    materialized = materialize_dataset(reference, worker_workspace)

    assert materialized.read_bytes() == content
    assert request_details["url"] == (
        f"http://pi.local:8000/datasets/uploads/{upload_id}"
    )
    assert request_details["headers"] == {
        "Accept-Encoding": "identity",
        "X-API-Token": "worker-secret",
    }


def test_storage_input_is_fetched_by_logical_path_and_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"large input bytes"
    reference = StorageInputReference(
        storage_id="home-storage",
        path="inputs/cohort.bin",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )
    worker_workspace = WorkerWorkspace(
        root=tmp_path / "worker-data",
        allowed_dataset_hosts=frozenset(),
        max_dataset_bytes=1024,
        control_plane_url="https://control.example",
        api_token="worker-secret",
    )
    requested: dict[str, object] = {}

    def stream(*args: object, **kwargs: object) -> ResponseStream:
        requested["url"] = args[1]
        requested["headers"] = kwargs["headers"]
        return ResponseStream(
            httpx.Response(
                200,
                content=content,
                headers={"Content-Length": str(len(content))},
                request=httpx.Request("GET", str(args[1])),
            )
        )

    monkeypatch.setattr("worker.data_plane.httpx.stream", stream)

    materialized = materialize_batch_input(reference, worker_workspace)

    assert materialized.read_bytes() == content
    assert requested["url"] == (
        "https://control.example/storage/files/inputs/cohort.bin"
    )
    assert requested["headers"] == {
        "Accept-Encoding": "identity",
        "X-API-Token": "worker-secret",
    }


def test_python_dataset_can_use_the_same_storage_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"feature,target\n1,0\n"
    reference = StorageInputReference(
        storage_id="home-storage",
        path="users/member/Workspace/Inputs/training.csv",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )
    worker_workspace = WorkerWorkspace(
        root=tmp_path / "worker-data",
        allowed_dataset_hosts=frozenset(),
        max_dataset_bytes=1024,
        control_plane_url="https://control.example",
        api_token="worker-secret",
    )

    def stream(*args: object, **kwargs: object) -> ResponseStream:
        return ResponseStream(
            httpx.Response(
                200,
                content=content,
                headers={"Content-Length": str(len(content))},
                request=httpx.Request("GET", str(args[1])),
            )
        )

    monkeypatch.setattr("worker.data_plane.httpx.stream", stream)

    materialized = materialize_dataset(reference, worker_workspace)

    assert materialized.read_bytes() == content
