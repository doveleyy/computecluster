import contextlib
import hashlib
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from contracts.models import (
    FailureKind,
    PythonBatchParameters,
    UploadedDatasetReference,
    UploadedScriptReference,
)
from worker.container_runner import (
    BatchCancellationError,
    BatchExecutionFailure,
    materialize_script,
    run_python_batch,
)
from worker.data_plane import WorkerWorkspace


class ResponseStream:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    def __enter__(self) -> httpx.Response:
        return self.response

    def __exit__(self, *_: object) -> None:
        self.response.close()


def batch_parameters(script: bytes, dataset: bytes) -> PythonBatchParameters:
    return PythonBatchParameters(
        script=UploadedScriptReference(
            upload_id=uuid4(),
            sha256=hashlib.sha256(script).hexdigest(),
            size_bytes=len(script),
        ),
        dataset=UploadedDatasetReference(
            upload_id=uuid4(),
            sha256=hashlib.sha256(dataset).hexdigest(),
            size_bytes=len(dataset),
        ),
        timeout_seconds=30,
        cpu_limit=1.5,
        memory_mb=1024,
    )


def workspace(tmp_path: Path) -> WorkerWorkspace:
    return WorkerWorkspace(
        root=tmp_path / "worker-data",
        allowed_dataset_hosts=frozenset(),
        max_dataset_bytes=1024 * 1024,
        control_plane_url="http://pi.local:8000",
        api_token="worker-secret",
        docker_executable="docker",
        container_image="home-platform-ml:0.1",
    )


def test_python_batch_uses_isolated_limited_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = b"print('trained')\n"
    dataset = b"feature,target\n1,0\n"
    parameters = batch_parameters(script, dataset)
    source_script = tmp_path / "source.py"
    source_dataset = tmp_path / "source.csv"
    source_script.write_bytes(script)
    source_dataset.write_bytes(dataset)
    commands: list[list[str]] = []

    monkeypatch.setattr(
        "worker.container_runner.materialize_script",
        lambda _parameters, _workspace, **_kwargs: source_script,
    )
    monkeypatch.setattr(
        "worker.container_runner.materialize_dataset",
        lambda _reference, _workspace: source_dataset,
    )

    class Completed:
        returncode = 0
        stdout = "trained\n"
        stderr = ""

    def run(command: list[str], **_kwargs: object) -> Completed:
        commands.append(command)
        return Completed()

    monkeypatch.setattr("worker.container_runner.subprocess.run", run)
    job_id = uuid4()
    result = run_python_batch(
        job_id, "windows-primary", parameters, workspace(tmp_path)
    )

    command = commands[0]
    assert command[:2] == ["docker", "run"]
    assert "--rm" not in command
    assert command[command.index("--network") : command.index("--network") + 2] == [
        "--network",
        "none",
    ]
    assert "--read-only" in command
    assert "no-new-privileges" in command
    assert command[command.index("--cpus") + 1] == "1.5"
    assert command[command.index("--memory") + 1] == "1024m"
    assert "HOME_PLATFORM_INPUT_DIR=/workspace/input" in command
    assert "HOME_PLATFORM_DATASET=/workspace/input/dataset.csv" in command
    assert "worker-secret" not in command
    assert result.stdout == "trained\n"
    assert result.artifact_uri == f"worker://windows-primary/{job_id}/"


def test_python_batch_timeout_force_removes_only_its_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = b"while True: pass\n"
    dataset = b"x\n1\n"
    parameters = batch_parameters(script, dataset)
    source_script = tmp_path / "source.py"
    source_dataset = tmp_path / "source.csv"
    source_script.write_bytes(script)
    source_dataset.write_bytes(dataset)
    commands: list[list[str]] = []

    monkeypatch.setattr(
        "worker.container_runner.materialize_script",
        lambda _parameters, _workspace, **_kwargs: source_script,
    )
    monkeypatch.setattr(
        "worker.container_runner.materialize_dataset",
        lambda _reference, _workspace: source_dataset,
    )

    def run(command: list[str], **_kwargs: object) -> object:
        commands.append(command)
        if command[1] == "run":
            raise subprocess.TimeoutExpired(command, 30)
        return object()

    monkeypatch.setattr("worker.container_runner.subprocess.run", run)

    with pytest.raises(TimeoutError, match="30 second timeout"):
        run_python_batch(uuid4(), "windows-primary", parameters, workspace(tmp_path))

    container_name = commands[0][commands[0].index("--name") + 1]
    assert commands[1] == ["docker", "rm", "-f", container_name]


def test_python_batch_cancellation_force_removes_its_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = b"while True: pass\n"
    dataset = b"x\n1\n"
    parameters = batch_parameters(script, dataset)
    source_script = tmp_path / "source.py"
    source_dataset = tmp_path / "source.csv"
    source_script.write_bytes(script)
    source_dataset.write_bytes(dataset)
    commands: list[list[str]] = []
    running = threading.Event()
    removed = threading.Event()
    cancellation = threading.Event()

    monkeypatch.setattr(
        "worker.container_runner.materialize_script",
        lambda *_args, **_kwargs: source_script,
    )
    monkeypatch.setattr(
        "worker.container_runner.materialize_dataset",
        lambda *_args, **_kwargs: source_dataset,
    )

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        if command[1] == "run":
            running.set()
            assert removed.wait(2)
            return SimpleNamespace(returncode=137, stdout="", stderr="")
        if command[1] == "rm":
            removed.set()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("worker.container_runner.subprocess.run", run)

    def request_cancellation() -> None:
        assert running.wait(2)
        cancellation.set()

    trigger = threading.Thread(target=request_cancellation)
    trigger.start()
    with pytest.raises(BatchCancellationError) as raised:
        run_python_batch(
            uuid4(),
            "windows-primary",
            parameters,
            workspace(tmp_path),
            cancellation_event=cancellation,
        )
    trigger.join()

    assert raised.value.failure_kind is FailureKind.CANCELLED_BY_USER
    assert any(command[1:3] == ["rm", "-f"] for command in commands)


def test_python_batch_reports_memory_limit_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = b"values = bytearray(2_000_000_000)\n"
    dataset = b"x\n1\n"
    parameters = batch_parameters(script, dataset)
    source_script = tmp_path / "source.py"
    source_dataset = tmp_path / "source.csv"
    source_script.write_bytes(script)
    source_dataset.write_bytes(dataset)
    commands: list[list[str]] = []

    monkeypatch.setattr(
        "worker.container_runner.materialize_script",
        lambda _parameters, _workspace, **_kwargs: source_script,
    )
    monkeypatch.setattr(
        "worker.container_runner.materialize_dataset",
        lambda _reference, _workspace: source_dataset,
    )

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        if command[1] == "run":
            return SimpleNamespace(returncode=137, stdout="", stderr="")
        if command[1] == "inspect":
            return SimpleNamespace(returncode=0, stdout='{"OOMKilled":true}', stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("worker.container_runner.subprocess.run", run)

    with pytest.raises(BatchExecutionFailure) as raised:
        run_python_batch(uuid4(), "windows-primary", parameters, workspace(tmp_path))

    assert raised.value.failure_kind is FailureKind.MEMORY_LIMIT_EXCEEDED
    assert "1024 MiB memory limit" in str(raised.value)
    assert [command[1] for command in commands] == ["run", "inspect", "rm"]


def test_uploaded_script_is_downloaded_with_token_and_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"print('hello')\n"
    parameters = batch_parameters(content, b"x\n1\n")
    request_details: dict[str, object] = {}

    def stream(*args: object, **kwargs: object) -> ResponseStream:
        request_details["url"] = args[1]
        request_details["headers"] = kwargs["headers"]
        return ResponseStream(
            httpx.Response(
                200,
                content=content,
                request=httpx.Request("GET", str(args[1])),
            )
        )

    monkeypatch.setattr("worker.container_runner.httpx.stream", stream)
    path = materialize_script(parameters, workspace(tmp_path))

    assert path.read_bytes() == content
    assert request_details["url"] == (
        f"http://pi.local:8000/scripts/uploads/{parameters.script.upload_id}"
    )
    assert request_details["headers"] == {
        "Accept-Encoding": "identity",
        "X-API-Token": "worker-secret",
    }


def test_thread_pools_are_matched_to_the_cpu_quota(tmp_path: Path, monkeypatch) -> None:
    """`--cpus` caps CPU time but not the core count the container sees.

    OpenBLAS is not cgroup-aware and starts one thread per host core, so a
    throttled job would oversubscribe its own quota and run several times slower
    for the same CPU budget. The launcher pins the thread pools instead.
    """
    recorded: list[list[str]] = []

    def capture(command, **kwargs):
        recorded.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("worker.container_runner.subprocess.run", capture)
    monkeypatch.setattr(
        "worker.container_runner.materialize_dataset", lambda *a: tmp_path / "d.csv"
    )
    monkeypatch.setattr(
        "worker.container_runner.materialize_script",
        lambda *a, **kw: tmp_path / "s.py",
    )
    (tmp_path / "d.csv").write_text("a\n1\n")
    (tmp_path / "s.py").write_text("print(1)")

    workspace = WorkerWorkspace(
        root=tmp_path / "ws",
        allowed_dataset_hosts=frozenset(),
        max_dataset_bytes=1024,
        docker_executable="/usr/bin/docker",
    )
    job_id = uuid4()
    parameters = PythonBatchParameters(
        script=UploadedScriptReference(
            upload_id=uuid4(), sha256="a" * 64, size_bytes=8
        ),
        dataset=UploadedDatasetReference(
            upload_id=uuid4(), sha256="b" * 64, size_bytes=4
        ),
        cpu_limit=1.0,
        memory_mb=512,
    )
    with contextlib.suppress(Exception):
        run_python_batch(job_id, "mac-one", parameters, workspace)

    assert recorded, "docker was never invoked"
    command = recorded[0]
    for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        assert f"{variable}=1" in command, f"{variable} not pinned to the quota"
    assert "HOME_PLATFORM_CPU_LIMIT=1.0" in command
    assert "--cpus" in command and "1.0" in command


def test_fractional_cpu_limits_still_get_one_thread(
    tmp_path: Path, monkeypatch
) -> None:
    """0.5 CPUs must mean one thread, not zero."""
    recorded: list[list[str]] = []

    def capture(command, **kwargs):
        recorded.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("worker.container_runner.subprocess.run", capture)
    monkeypatch.setattr(
        "worker.container_runner.materialize_dataset", lambda *a: tmp_path / "d.csv"
    )
    monkeypatch.setattr(
        "worker.container_runner.materialize_script",
        lambda *a, **kw: tmp_path / "s.py",
    )
    (tmp_path / "d.csv").write_text("a\n1\n")
    (tmp_path / "s.py").write_text("print(1)")

    workspace = WorkerWorkspace(
        root=tmp_path / "ws2",
        allowed_dataset_hosts=frozenset(),
        max_dataset_bytes=1024,
        docker_executable="/usr/bin/docker",
    )
    parameters = PythonBatchParameters(
        script=UploadedScriptReference(
            upload_id=uuid4(), sha256="a" * 64, size_bytes=8
        ),
        dataset=UploadedDatasetReference(
            upload_id=uuid4(), sha256="b" * 64, size_bytes=4
        ),
        cpu_limit=0.5,
        memory_mb=256,
    )
    with contextlib.suppress(Exception):
        run_python_batch(uuid4(), "mac-one", parameters, workspace)

    assert "OMP_NUM_THREADS=1" in recorded[0]
