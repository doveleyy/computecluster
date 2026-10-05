import hashlib
import io
import os
import time
import zipfile
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from contracts.models import (
    DatasetReference,
    StorageInputReference,
    UploadedDatasetReference,
    UploadedProjectReference,
)
from worker.data_plane import (
    DatasetPolicyError,
    WorkerWorkspace,
    materialize_batch_input,
    materialize_dataset,
    materialize_project,
    prune_cache,
)

# Names that look relative under POSIX rules but escape, collide, or hit a
# device under Windows rules. The server and the worker must agree on them.
UNSAFE_PROJECT_ENTRY_NAMES = [
    "D:evil.dll",
    "C:x",
    "sub/D:x",
    "file.txt:stream",
    "back\\slash.txt",
    "\\\\?\\x",
    "/absolute",
    "//server/share/x",
    "../up",
    "a/../b",
    "a/.. /x",
    "trailing.",
    "trailing /x",
    "CON",
    "con.txt",
    "COM1",
    "lpt9.log",
]

SAFE_PROJECT_ENTRY_NAMES = ["submit.hp", "lib/", "lib/util.py", "data.v2.csv", "nul_ok"]


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


def test_cache_evicts_oldest_entry_and_preserves_linked_inputs(tmp_path: Path) -> None:
    worker_workspace = WorkerWorkspace(
        root=tmp_path / "worker-data",
        allowed_dataset_hosts=frozenset(),
        max_dataset_bytes=1024,
        max_cache_bytes=10,
    )
    cache = worker_workspace.root / "cache"
    inputs = worker_workspace.root / "inputs"
    run = worker_workspace.root / "runs" / "active"
    cache.mkdir(parents=True)
    inputs.mkdir(parents=True)
    run.mkdir(parents=True)
    oldest = cache / "oldest"
    linked = inputs / "linked"
    newest = cache / "newest"
    oldest.write_bytes(b"1111")
    linked.write_bytes(b"2222")
    newest.write_bytes(b"3333")
    (run / "linked").hardlink_to(linked)
    oldest.touch()
    linked.touch()
    newest.touch()
    # Make ordering deterministic without sleeping.
    oldest_stat = oldest.stat()
    newest_stat = newest.stat()
    oldest_time = min(oldest_stat.st_mtime_ns, newest_stat.st_mtime_ns) - 10_000
    newest_time = max(oldest_stat.st_mtime_ns, newest_stat.st_mtime_ns) + 10_000
    oldest.touch()
    import os

    os.utime(oldest, ns=(oldest_time, oldest_time))
    os.utime(newest, ns=(newest_time, newest_time))

    removed = prune_cache(worker_workspace, required_bytes=2)

    assert removed == (1, 4)
    assert not oldest.exists()
    assert linked.exists()
    assert newest.exists()


def test_cache_rejects_an_item_larger_than_its_ceiling(tmp_path: Path) -> None:
    worker_workspace = WorkerWorkspace(
        root=tmp_path,
        allowed_dataset_hosts=frozenset(),
        max_dataset_bytes=1024,
        max_cache_bytes=4,
    )

    with pytest.raises(DatasetPolicyError, match="requires 5 cache bytes"):
        prune_cache(worker_workspace, required_bytes=5)


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


def cached_project(
    tmp_path: Path, names: list[str]
) -> tuple[WorkerWorkspace, UploadedProjectReference]:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name in names:
            archive.writestr(name, b"" if name.endswith("/") else b"payload")
    content = output.getvalue()
    worker_workspace = WorkerWorkspace(
        root=tmp_path / "worker-data",
        allowed_dataset_hosts=frozenset(),
        max_dataset_bytes=1024 * 1024,
        control_plane_url="https://control.example",
        api_token="worker-secret",
    )
    digest = hashlib.sha256(content).hexdigest()
    projects = worker_workspace.root / "projects"
    projects.mkdir(parents=True)
    (projects / f"{digest}.zip").write_bytes(content)
    reference = UploadedProjectReference(
        upload_id=uuid4(), sha256=digest, size_bytes=len(content)
    )
    return worker_workspace, reference


@pytest.mark.parametrize("name", UNSAFE_PROJECT_ENTRY_NAMES)
def test_project_extraction_rejects_entries_unsafe_on_posix_or_windows(
    tmp_path: Path, name: str
) -> None:
    worker_workspace, reference = cached_project(tmp_path, [name, "submit.hp"])
    destination = worker_workspace.root / "runs" / "job" / "project"

    with pytest.raises(DatasetPolicyError, match="unsafe path"):
        materialize_project(reference, worker_workspace, destination)

    assert [item for item in tmp_path.rglob("*") if item.is_file()] == [
        worker_workspace.root / "projects" / f"{reference.sha256}.zip"
    ]


def test_project_extraction_writes_safe_entries_under_destination(
    tmp_path: Path,
) -> None:
    worker_workspace, reference = cached_project(tmp_path, SAFE_PROJECT_ENTRY_NAMES)
    destination = worker_workspace.root / "runs" / "job" / "project"

    materialize_project(reference, worker_workspace, destination)

    written = sorted(
        item.relative_to(destination).as_posix()
        for item in destination.rglob("*")
        if item.is_file()
    )
    assert written == ["data.v2.csv", "lib/util.py", "nul_ok", "submit.hp"]
    assert (destination / "lib").is_dir()


def test_prune_removes_stale_partial_downloads_and_keeps_live_ones(
    tmp_path: Path,
) -> None:
    worker_workspace = WorkerWorkspace(
        root=tmp_path / "worker-data",
        allowed_dataset_hosts=frozenset(),
        max_dataset_bytes=1024,
        max_cache_bytes=100,
    )
    cache = worker_workspace.root / "cache"
    cache.mkdir(parents=True)
    stale = cache / ".abandoned.1.part"
    live = cache / ".downloading.2.part"
    stale.write_bytes(b"123456")
    live.write_bytes(b"123456")
    hour_ago = int((time.time() - 3600) * 1_000_000_000)
    os.utime(stale, ns=(hour_ago, hour_ago))

    removed = prune_cache(worker_workspace)

    assert removed == (1, 6)
    assert not stale.exists()
    assert live.exists()
