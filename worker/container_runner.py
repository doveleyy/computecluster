from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

import httpx

from contracts.models import (
    BatchParameters,
    BatchResult,
    FailureKind,
    PythonBatchParameters,
    PythonBatchResult,
)
from worker.data_plane import (
    DatasetPolicyError,
    WorkerWorkspace,
    materialize_batch_input,
    materialize_dataset,
    materialize_project,
    prune_cache,
    record_cache_use,
)


class BatchExecutionFailure(RuntimeError):
    def __init__(self, failure_kind: FailureKind, message: str) -> None:
        super().__init__(message)
        self.failure_kind = failure_kind


class BatchTimeoutError(BatchExecutionFailure, TimeoutError):
    pass


class BatchCancellationError(BatchExecutionFailure):
    pass


def _link_or_copy(source: Path, destination: Path) -> None:
    """Stage a cached file without duplicating its bytes when possible."""
    try:
        os.link(source, destination)
    except OSError:
        # The cache and run directory may be on different filesystems, or the
        # worker filesystem may not support hard links.
        shutil.copy2(source, destination)


def run_batch(
    job_id: UUID,
    worker_id: str,
    parameters: BatchParameters,
    workspace: WorkerWorkspace,
    cancellation_event: threading.Event | None = None,
    job_name: str | None = None,
    attempt: int = 1,
) -> BatchResult:
    if workspace.docker_executable is None:
        raise RuntimeError("Docker runtime is unavailable")
    if parameters.runtime != "scientific-python:1":
        raise RuntimeError(f"unsupported runtime {parameters.runtime!r}")

    run_directory = workspace.root / "runs" / str(job_id)
    project_directory = run_directory / "project"
    input_directory = run_directory / "input"
    output_directory = workspace.root / "artifacts" / str(job_id)
    if run_directory.exists():
        shutil.rmtree(run_directory)
    if output_directory.exists():
        shutil.rmtree(output_directory)
    output_directory.mkdir(parents=True)
    output_directory.chmod(0o777)
    materialize_project(
        parameters.project,
        workspace,
        project_directory,
        cancellation_event=cancellation_event,
    )
    entrypoint = project_directory / parameters.entrypoint
    if not entrypoint.is_file():
        raise DatasetPolicyError("batch entrypoint is missing after project extraction")
    input_directory.mkdir(parents=True)
    for name, reference in parameters.inputs.items():
        source = materialize_batch_input(
            reference, workspace, cancellation_event=cancellation_event
        )
        _link_or_copy(source, input_directory / name)

    container_name = f"home-platform-{str(job_id)[:12]}-{uuid4().hex[:6]}"
    memory = f"{parameters.memory_mb}m"
    threads = max(1, int(parameters.cpu_limit))
    environment = {
        **parameters.environment,
        "HOME_PLATFORM_PROJECT_DIR": "/workspace/project",
        "HOME_PLATFORM_INPUT_DIR": "/workspace/input",
        "HOME_PLATFORM_OUTPUT_DIR": "/workspace/output",
        "HOME_PLATFORM_TMP_DIR": "/tmp",
        "HOME_PLATFORM_JOB_ID": str(job_id),
        "HOME_PLATFORM_JOB_NAME": job_name or str(job_id),
        "HOME_PLATFORM_WORKER_ID": worker_id,
        "HOME_PLATFORM_ATTEMPT": str(attempt),
        "HOME_PLATFORM_ARRAY_INDEX": str(parameters.array_index),
        "HOME_PLATFORM_CPU_LIMIT": str(parameters.cpu_limit),
        "HOME_PLATFORM_MEMORY_MB": str(parameters.memory_mb),
        "HOME_PLATFORM_TIMEOUT_SECONDS": str(parameters.timeout_seconds),
        **{
            key: str(threads)
            for key in (
                "OMP_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
                "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS",
            )
        },
    }
    environment_arguments = [
        argument
        for key, value in environment.items()
        for argument in ("--env", f"{key}={value}")
    ]
    command = [
        workspace.docker_executable,
        "run",
        "--name",
        container_name,
        "--label",
        f"home-platform.job-id={job_id}",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--cpus",
        str(parameters.cpu_limit),
        "--memory",
        memory,
        "--memory-swap",
        memory,
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "--mount",
        f"type=bind,source={project_directory.resolve()},target=/workspace/project,readonly",
        "--mount",
        f"type=bind,source={input_directory.resolve()},target=/workspace/input,readonly",
        "--mount",
        f"type=bind,source={output_directory.resolve()},target=/workspace/output",
        *environment_arguments,
        workspace.container_image,
        "bash",
        f"/workspace/project/{parameters.entrypoint}",
    ]
    try:
        completed = _run_container(
            command,
            parameters.timeout_seconds,
            cancellation_event,
            workspace.docker_executable,
            container_name,
        )
    except BatchCancellationError:
        raise
    except subprocess.TimeoutExpired as error:
        _remove_container(workspace.docker_executable, container_name)
        raise BatchTimeoutError(
            FailureKind.TIMED_OUT,
            f"container exceeded {parameters.timeout_seconds} second timeout",
        ) from error
    except (OSError, subprocess.SubprocessError) as error:
        _remove_container(workspace.docker_executable, container_name)
        raise BatchExecutionFailure(
            FailureKind.INFRASTRUCTURE_ERROR,
            f"container runtime failed to execute the job: {error}",
        ) from error

    stdout = completed.stdout[-8000:]
    stderr = completed.stderr[-8000:]
    try:
        if completed.returncode != 0:
            if _container_was_oom_killed(workspace.docker_executable, container_name):
                raise BatchExecutionFailure(
                    FailureKind.MEMORY_LIMIT_EXCEEDED,
                    f"container exceeded {parameters.memory_mb} MiB memory limit",
                )
            detail = (stderr or stdout or "no diagnostic output")[-1000:]
            failure_kind = (
                FailureKind.INFRASTRUCTURE_ERROR
                if completed.returncode in {125, 126, 127}
                else FailureKind.EXECUTION_ERROR
            )
            raise BatchExecutionFailure(
                failure_kind,
                f"batch container exited with code {completed.returncode}: {detail}",
            )
        output_files = sorted(
            path.name
            for path in output_directory.iterdir()
            if path.is_file() and len(path.name) <= 200
        )[:100]
        return BatchResult(
            project_sha256=parameters.project.sha256,
            exit_code=0,
            stdout=stdout,
            stderr=stderr,
            output_files=output_files,
            artifact_uri=f"worker://{worker_id}/{job_id}/",
        )
    finally:
        _remove_container(workspace.docker_executable, container_name)


def run_python_batch(
    job_id: UUID,
    worker_id: str,
    parameters: PythonBatchParameters,
    workspace: WorkerWorkspace,
    cancellation_event: threading.Event | None = None,
    job_name: str | None = None,
) -> PythonBatchResult:
    if workspace.docker_executable is None:
        raise RuntimeError("Docker runtime is unavailable")
    if cancellation_event is None:
        dataset_path = materialize_dataset(parameters.dataset, workspace)
        script_path = materialize_script(
            parameters, workspace, protected_cache_entries=frozenset({dataset_path})
        )
    else:
        dataset_path = materialize_dataset(
            parameters.dataset, workspace, cancellation_event=cancellation_event
        )
        script_path = materialize_script(
            parameters,
            workspace,
            cancellation_event=cancellation_event,
            protected_cache_entries=frozenset({dataset_path}),
        )

    run_directory = workspace.root / "runs" / str(job_id)
    input_directory = run_directory / "input"
    output_directory = workspace.root / "artifacts" / str(job_id)
    if run_directory.exists():
        shutil.rmtree(run_directory)
    if output_directory.exists():
        shutil.rmtree(output_directory)
    input_directory.mkdir(parents=True)
    output_directory.mkdir(parents=True)
    output_directory.chmod(0o777)
    _link_or_copy(script_path, input_directory / "job.py")
    _link_or_copy(dataset_path, input_directory / "dataset.csv")

    container_name = f"home-platform-{str(job_id)[:12]}-{uuid4().hex[:6]}"
    memory = f"{parameters.memory_mb}m"

    # Match library thread pools to the CPU quota.
    #
    # `--cpus` caps how much CPU time the container may consume, but it does not
    # change how many cores the container *sees*. joblib reads the cgroup quota
    # and behaves, but native BLAS libraries do not: OpenBLAS starts one thread
    # per host core regardless. Under `--cpus 1` on an 8-core host that means 8
    # threads contending for one core's worth of quota — measured at roughly
    # 3.5x slower than the same work with one thread, for identical CPU budget.
    #
    # This matters most for exactly the case the limit exists to serve: running
    # something deliberately slowly to keep a laptop cool.
    threads = max(1, int(parameters.cpu_limit))
    thread_environment = [
        argument
        for variable in (
            "OMP_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
            "VECLIB_MAXIMUM_THREADS",
        )
        for argument in ("--env", f"{variable}={threads}")
    ]
    command = [
        workspace.docker_executable,
        "run",
        "--name",
        container_name,
        "--label",
        f"home-platform.job-id={job_id}",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--cpus",
        str(parameters.cpu_limit),
        "--memory",
        memory,
        "--memory-swap",
        memory,
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "--mount",
        f"type=bind,source={input_directory.resolve()},target=/workspace/input,readonly",
        "--mount",
        f"type=bind,source={output_directory.resolve()},target=/workspace/output",
        "--env",
        "HOME_PLATFORM_DATASET=/workspace/input/dataset.csv",
        "--env",
        "HOME_PLATFORM_INPUT_DIR=/workspace/input",
        "--env",
        "HOME_PLATFORM_OUTPUT_DIR=/workspace/output",
        "--env",
        f"HOME_PLATFORM_JOB_ID={job_id}",
        "--env",
        f"HOME_PLATFORM_JOB_NAME={job_name or job_id}",
        # Advertised so a script can size its own parallelism to the quota
        # rather than to the host, e.g. GridSearchCV(n_jobs=...).
        "--env",
        f"HOME_PLATFORM_CPU_LIMIT={parameters.cpu_limit}",
        *thread_environment,
        workspace.container_image,
        "python",
        "-I",
        "/workspace/input/job.py",
    ]
    logging.info(
        "job=%s container=%s image=%s cpus=%s memory_mb=%s",
        job_id,
        container_name,
        workspace.container_image,
        parameters.cpu_limit,
        parameters.memory_mb,
    )
    try:
        completed = _run_container(
            command,
            parameters.timeout_seconds,
            cancellation_event,
            workspace.docker_executable,
            container_name,
        )
    except BatchCancellationError:
        raise
    except subprocess.TimeoutExpired as error:
        _remove_container(workspace.docker_executable, container_name)
        raise BatchTimeoutError(
            FailureKind.TIMED_OUT,
            f"container exceeded {parameters.timeout_seconds} second timeout",
        ) from error
    except (OSError, subprocess.SubprocessError) as error:
        _remove_container(workspace.docker_executable, container_name)
        raise BatchExecutionFailure(
            FailureKind.INFRASTRUCTURE_ERROR,
            f"container runtime failed to execute the job: {error}",
        ) from error

    stdout = completed.stdout[-8000:]
    stderr = completed.stderr[-8000:]
    try:
        if completed.returncode != 0:
            if _container_was_oom_killed(workspace.docker_executable, container_name):
                raise BatchExecutionFailure(
                    FailureKind.MEMORY_LIMIT_EXCEEDED,
                    f"container exceeded {parameters.memory_mb} MiB memory limit",
                )
            detail = (stderr or stdout or "no diagnostic output")[-1000:]
            failure_kind = (
                FailureKind.INFRASTRUCTURE_ERROR
                if completed.returncode in {125, 126, 127}
                else FailureKind.EXECUTION_ERROR
            )
            raise BatchExecutionFailure(
                failure_kind,
                f"batch container exited with code {completed.returncode}: {detail}",
            )
        output_files = sorted(
            path.name
            for path in output_directory.iterdir()
            if path.is_file() and len(path.name) <= 200
        )[:100]
        return PythonBatchResult(
            script_sha256=parameters.script.sha256,
            dataset_sha256=parameters.dataset.sha256,
            exit_code=0,
            stdout=stdout,
            stderr=stderr,
            output_files=output_files,
            artifact_uri=f"worker://{worker_id}/{job_id}/",
        )
    finally:
        _remove_container(workspace.docker_executable, container_name)


def _run_container(
    command: list[str],
    timeout_seconds: int,
    cancellation_event: threading.Event | None,
    executable: str,
    container_name: str,
) -> subprocess.CompletedProcess[str]:
    if cancellation_event is None:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )

    result: list[subprocess.CompletedProcess[str]] = []
    errors: list[BaseException] = []

    def wait_for_container() -> None:
        try:
            result.append(
                subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            )
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=wait_for_container, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout_seconds
    while thread.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _remove_container(executable, container_name)
            thread.join(timeout=15)
            raise subprocess.TimeoutExpired(command, timeout_seconds)
        if cancellation_event.wait(min(0.25, remaining)):
            _remove_container(executable, container_name)
            thread.join(timeout=15)
            raise BatchCancellationError(
                FailureKind.CANCELLED_BY_USER, "container cancelled by user"
            )
    if errors:
        error = errors[0]
        if isinstance(error, Exception):
            raise error
        raise RuntimeError("container runner terminated unexpectedly")
    if not result:
        raise RuntimeError("container runner returned no result")
    return result[0]


def _container_was_oom_killed(executable: str, container_name: str) -> bool:
    try:
        inspected = subprocess.run(
            [executable, "inspect", "--format", "{{json .State}}", container_name],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if inspected.returncode != 0:
            return False
        state = json.loads(inspected.stdout)
        return state.get("OOMKilled") is True
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, AttributeError):
        logging.exception("could not inspect failed container=%s", container_name)
        return False


def _remove_container(executable: str, container_name: str) -> None:
    try:
        subprocess.run(
            [executable, "rm", "-f", container_name],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logging.exception("could not remove container=%s", container_name)


def materialize_script(
    parameters: PythonBatchParameters,
    workspace: WorkerWorkspace,
    cancellation_event: threading.Event | None = None,
    protected_cache_entries: frozenset[Path] = frozenset(),
) -> Path:
    if cancellation_event is not None and cancellation_event.is_set():
        raise RuntimeError("job cancelled while preparing its script")
    if workspace.control_plane_url is None or workspace.api_token is None:
        raise DatasetPolicyError("uploaded script access is not configured")
    reference = parameters.script
    cache_directory = workspace.root / "scripts"
    cache_directory.mkdir(parents=True, exist_ok=True)
    target = cache_directory / f"{reference.sha256}.py"
    if target.is_file() and _matches(target, reference.sha256, reference.size_bytes):
        record_cache_use(target)
        return target
    target.unlink(missing_ok=True)
    prune_cache(
        workspace,
        required_bytes=reference.size_bytes,
        protected=protected_cache_entries,
    )
    temporary = cache_directory / f".{reference.sha256}.{uuid4().hex}.part"
    url = (
        f"{workspace.control_plane_url.rstrip('/')}/scripts/uploads/"
        f"{reference.upload_id}"
    )
    digest = hashlib.sha256()
    received = 0
    try:
        with httpx.stream(
            "GET",
            url,
            headers={
                "Accept-Encoding": "identity",
                "X-API-Token": workspace.api_token,
            },
            follow_redirects=False,
            timeout=httpx.Timeout(30, read=120),
        ) as response:
            response.raise_for_status()
            with temporary.open("xb") as output:
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    if cancellation_event is not None and cancellation_event.is_set():
                        raise RuntimeError("job cancelled while downloading its script")
                    received += len(chunk)
                    if received > reference.size_bytes:
                        raise DatasetPolicyError("script exceeds declared size")
                    digest.update(chunk)
                    output.write(chunk)
        if received != reference.size_bytes:
            raise DatasetPolicyError("script size does not match declaration")
        if digest.hexdigest() != reference.sha256:
            raise DatasetPolicyError("script SHA-256 does not match declaration")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _matches(path: Path, sha256: str, size_bytes: int) -> bool:
    if path.stat().st_size != size_bytes:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == sha256
