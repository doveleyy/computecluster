import argparse
import csv
import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Protocol

import httpx
import psutil

from contracts.models import (
    BatchParameters,
    FailureKind,
    GpuMetrics,
    JobRead,
    JobResult,
    JobType,
    PythonBatchParameters,
    SleepParameters,
    SleepResult,
    WorkerHeartbeatResponse,
    WorkerMetrics,
)
from contracts.tokens import load_api_token
from worker.container_runner import BatchExecutionFailure, run_batch, run_python_batch
from worker.data_plane import WorkerWorkspace


@dataclass(frozen=True)
class WorkerSettings:
    api_url: str
    api_token: str
    worker_id: str
    poll_seconds: float
    heartbeat_seconds: float
    data_directory: Path
    allowed_dataset_hosts: frozenset[str]
    max_dataset_bytes: int
    max_cache_bytes: int
    docker_executable: str | None
    container_image: str


class WorkerAPI(Protocol):
    worker_id: str

    def claim(self) -> JobRead | None: ...

    def heartbeat(self, job: JobRead | None = None) -> bool: ...

    def complete(self, job: JobRead, result: JobResult) -> JobRead: ...

    def fail(
        self,
        job: JobRead,
        error: str,
        failure_kind: FailureKind = FailureKind.EXECUTION_ERROR,
    ) -> JobRead: ...

    def upload_artifact(self, job: JobRead, path: Path) -> None: ...


class ControlPlaneClient:
    def __init__(self, settings: WorkerSettings) -> None:
        self.worker_id = settings.worker_id
        self.http = httpx.Client(
            base_url=settings.api_url.rstrip("/"),
            headers={"X-API-Token": settings.api_token},
            timeout=10,
        )
        self._settings = settings
        self._container_checked_at = 0.0
        self._container_ready = False
        self.supported_types = [JobType.SLEEP]
        self._refresh_container_capability(force=True)
        self.metrics = SystemMetricsSampler()

    def close(self) -> None:
        self.http.close()

    def claim(self) -> JobRead | None:
        self._refresh_container_capability()
        response = self.http.post(
            "/workers/claim",
            json={
                "worker_id": self.worker_id,
                "supported_types": [
                    job_type.value for job_type in self.supported_types
                ],
                "metrics": self.metrics.sample().model_dump(mode="json"),
            },
        )
        response.raise_for_status()
        body = response.json()
        return JobRead.model_validate(body) if body is not None else None

    def complete(self, job: JobRead, result: JobResult) -> JobRead:
        if job.lease_token is None:
            raise ValueError("claimed job does not contain a lease token")
        response = self.http.post(
            f"/jobs/{job.id}/complete",
            json={
                "worker_id": self.worker_id,
                "lease_token": str(job.lease_token),
                "result": result.model_dump(mode="json"),
            },
        )
        response.raise_for_status()
        return JobRead.model_validate(response.json())

    def fail(
        self,
        job: JobRead,
        error: str,
        failure_kind: FailureKind = FailureKind.EXECUTION_ERROR,
    ) -> JobRead:
        if job.lease_token is None:
            raise ValueError("claimed job does not contain a lease token")
        response = self.http.post(
            f"/jobs/{job.id}/fail",
            json={
                "worker_id": self.worker_id,
                "lease_token": str(job.lease_token),
                "failure_kind": failure_kind.value,
                "error": error[:1000],
            },
        )
        response.raise_for_status()
        return JobRead.model_validate(response.json())

    def upload_artifact(self, job: JobRead, path: Path) -> None:
        if job.lease_token is None:
            raise ValueError("claimed job does not contain a lease token")
        with path.open("rb") as handle:
            response = self.http.post(
                f"/jobs/{job.id}/artifacts",
                data={
                    "worker_id": self.worker_id,
                    "lease_token": str(job.lease_token),
                },
                files={"file": (path.name, handle)},
                timeout=httpx.Timeout(30, write=300, read=120),
            )
        response.raise_for_status()

    def heartbeat(self, job: JobRead | None = None) -> bool:
        self._refresh_container_capability()
        body = {
            "worker_id": self.worker_id,
            "supported_types": [job_type.value for job_type in self.supported_types],
            "current_job_id": str(job.id) if job is not None else None,
            "lease_token": str(job.lease_token) if job is not None else None,
            "metrics": self.metrics.sample().model_dump(mode="json"),
        }
        response = self.http.post("/workers/heartbeat", json=body)
        response.raise_for_status()
        return WorkerHeartbeatResponse.model_validate(
            response.json()
        ).cancellation_requested

    def _refresh_container_capability(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._container_checked_at < 60:
            return
        ready = _container_runtime_ready(
            self._settings.docker_executable,
            self._settings.container_image,
        )
        self._container_checked_at = now
        if ready == self._container_ready and not force:
            return
        self._container_ready = ready
        self.supported_types = [
            job_type
            for job_type in self.supported_types
            if job_type not in {JobType.PYTHON_BATCH, JobType.BATCH}
        ]
        if ready:
            self.supported_types.extend([JobType.PYTHON_BATCH, JobType.BATCH])
        logging.info(
            "container capability ready=%s image=%s",
            ready,
            self._settings.container_image,
        )


class SystemMetricsSampler:
    def __init__(self, cache_seconds: float = 5) -> None:
        self.cache_seconds = cache_seconds
        self._sampled_at = 0.0
        self._cached: WorkerMetrics | None = None
        self._lock = threading.Lock()
        self._gpu_inventory = _gpu_inventory()
        psutil.cpu_percent(interval=None)

    def sample(self) -> WorkerMetrics:
        with self._lock:
            now = time.monotonic()
            if self._cached is not None and now - self._sampled_at < self.cache_seconds:
                return self._cached
            memory = psutil.virtual_memory()
            storage = psutil.disk_usage(Path.home().anchor or "/")
            libre_cpu_temperature, libre_gpu_temperature = (
                _libre_hardware_temperatures()
            )
            gpu_metrics = _nvidia_metrics() or self._gpu_inventory
            if libre_gpu_temperature is not None and gpu_metrics:
                gpu_metrics = [
                    gpu_metrics[0].model_copy(
                        update={"temperature_c": libre_gpu_temperature}
                    ),
                    *gpu_metrics[1:],
                ]
            native_cpu_temperature = _cpu_temperature()
            self._cached = WorkerMetrics(
                platform=platform.system(),
                logical_cores=psutil.cpu_count(logical=True) or 1,
                cpu_percent=psutil.cpu_percent(interval=None),
                memory_percent=memory.percent,
                memory_available=memory.available,
                memory_total=memory.total,
                storage_percent=storage.percent,
                storage_free=storage.free,
                storage_total=storage.total,
                temperature_c=(
                    native_cpu_temperature
                    if native_cpu_temperature is not None
                    else libre_cpu_temperature
                ),
                gpus=gpu_metrics,
            )
            self._sampled_at = now
            return self._cached


def _cpu_temperature() -> float | None:
    sensors = getattr(psutil, "sensors_temperatures", None)
    if sensors is None:
        return None
    try:
        readings = sensors()
    except (OSError, RuntimeError):
        return None
    values = [
        reading.current
        for group in readings.values()
        for reading in group
        if reading.current is not None
    ]
    return max(values) if values else None


def _libre_hardware_temperatures() -> tuple[float | None, float | None]:
    if platform.system() != "Windows":
        return None, None
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if executable is None:
        return None, None
    command = [
        executable,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Get-CimInstance -Namespace root/LibreHardwareMonitor -ClassName Sensor "
        "-ErrorAction SilentlyContinue | Where-Object SensorType -eq Temperature "
        "| Select-Object Identifier,Name,Value | ConvertTo-Json -Compress",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=True,
            text=True,
            timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        decoded = json.loads(completed.stdout or "[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None, None
    readings = decoded if isinstance(decoded, list) else [decoded]
    cpu_values: list[float] = []
    gpu_values: list[float] = []
    for reading in readings:
        if not isinstance(reading, dict):
            continue
        identifier = str(reading.get("Identifier", "")).lower()
        try:
            value = float(reading["Value"])
        except (KeyError, TypeError, ValueError):
            continue
        if "cpu" in identifier:
            cpu_values.append(value)
        elif "gpu" in identifier:
            gpu_values.append(value)
    return (
        max(cpu_values) if cpu_values else None,
        max(gpu_values) if gpu_values else None,
    )


def _optional_number(value: str) -> float | None:
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in {"n/a", "[not supported]"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _nvidia_metrics() -> list[GpuMetrics]:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return []
    command = [
        executable,
        "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []

    gpus = []
    for row in csv.reader(completed.stdout.splitlines(), skipinitialspace=True):
        if len(row) != 5:
            continue
        percent = _optional_number(row[1])
        memory_used = _optional_number(row[2])
        memory_total = _optional_number(row[3])
        temperature = _optional_number(row[4])
        gpus.append(
            GpuMetrics(
                name=row[0].strip(),
                percent=percent,
                memory_used=(
                    round(memory_used * 1024 * 1024)
                    if memory_used is not None
                    else None
                ),
                memory_total=(
                    round(memory_total * 1024 * 1024)
                    if memory_total is not None
                    else None
                ),
                temperature_c=temperature,
            )
        )
    return gpus


def _gpu_inventory() -> list[GpuMetrics]:
    system = platform.system()
    if system == "Windows":
        executable = shutil.which("powershell.exe") or shutil.which("powershell")
        if executable is None:
            return []
        command = [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-CimInstance Win32_VideoController | ForEach-Object Name",
        ]
    elif system == "Darwin":
        executable = shutil.which("system_profiler")
        if executable is None:
            return []
        command = [executable, "SPDisplaysDataType", "-json"]
    else:
        return []

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []

    if system == "Windows":
        return [
            GpuMetrics(name=line.strip())
            for line in completed.stdout.splitlines()
            if line.strip()
        ]
    try:
        displays = json.loads(completed.stdout).get("SPDisplaysDataType", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    return [
        GpuMetrics(name=str(display["sppci_model"]))
        for display in displays
        if display.get("sppci_model")
    ]


def _docker_executable() -> str | None:
    discovered = shutil.which("docker.exe") or shutil.which("docker")
    if discovered is not None:
        return discovered
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Programs/Docker/Docker/resources/bin/docker.exe",
        Path(os.environ.get("PROGRAMFILES", ""))
        / "Docker/Docker/resources/bin/docker.exe",
    ]
    return next((str(path) for path in candidates if path.is_file()), None)


def _container_runtime_ready(executable: str | None, image: str) -> bool:
    if executable is None:
        return False
    try:
        completed = subprocess.run(
            [executable, "image", "inspect", image],
            capture_output=True,
            check=False,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def load_settings(
    poll_seconds: float | None = None,
    worker_id: str | None = None,
    api_url: str | None = None,
    heartbeat_seconds: float | None = None,
    token_file: Path | None = None,
    data_directory: Path | None = None,
    allowed_dataset_hosts: list[str] | None = None,
    max_dataset_bytes: int | None = None,
    max_cache_bytes: int | None = None,
) -> WorkerSettings:
    resolved_token_file = token_file or Path(
        os.environ.get(
            "HOME_PLATFORM_API_TOKEN_FILE",
            Path.home() / ".config/home-platform/api-token",
        )
    )
    api_token = load_api_token(
        default_file=resolved_token_file,
        explicit_file=token_file,
        required=True,
    )
    assert api_token is not None

    hostname = re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname())
    resolved_worker_id = worker_id or os.environ.get(
        "HOME_PLATFORM_WORKER_ID", f"worker-{hostname}"
    )
    if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", resolved_worker_id) is None:
        raise ValueError(
            "worker ID must contain 1-64 letters, numbers, '.', '_', or '-'"
        )
    resolved_poll_seconds = (
        poll_seconds
        if poll_seconds is not None
        else float(os.environ.get("HOME_PLATFORM_POLL_SECONDS", "5"))
    )
    if resolved_poll_seconds <= 0:
        raise ValueError("poll interval must be greater than zero")
    resolved_heartbeat_seconds = (
        heartbeat_seconds
        if heartbeat_seconds is not None
        else float(os.environ.get("HOME_PLATFORM_HEARTBEAT_SECONDS", "5"))
    )
    if resolved_heartbeat_seconds <= 0:
        raise ValueError("heartbeat interval must be greater than zero")
    resolved_api_url = api_url or os.environ.get(
        "HOME_PLATFORM_API_URL", "http://raspberrypi.local:8000"
    )
    parsed_url = httpx.URL(resolved_api_url)
    if parsed_url.scheme not in {"http", "https"} or parsed_url.host is None:
        raise ValueError("control-plane URL must be an absolute HTTP(S) URL")
    resolved_data_directory = data_directory or Path(
        os.environ.get(
            "HOME_PLATFORM_WORKER_DATA_DIR",
            Path.home() / ".local/share/home-platform-worker",
        )
    )
    configured_hosts = (
        allowed_dataset_hosts
        if allowed_dataset_hosts is not None
        else os.environ.get("HOME_PLATFORM_DATASET_ALLOWED_HOSTS", "").split(",")
    )
    resolved_hosts = frozenset(
        host.strip().lower() for host in configured_hosts if host.strip()
    )
    if any(re.fullmatch(r"[A-Za-z0-9.-]+", host) is None for host in resolved_hosts):
        raise ValueError("dataset allowlist contains an invalid hostname")
    resolved_max_dataset_bytes = (
        max_dataset_bytes
        if max_dataset_bytes is not None
        else int(os.environ.get("HOME_PLATFORM_MAX_DATASET_BYTES", str(10 * 1024**3)))
    )
    if resolved_max_dataset_bytes <= 0:
        raise ValueError("maximum dataset bytes must be greater than zero")
    resolved_max_cache_bytes = (
        max_cache_bytes
        if max_cache_bytes is not None
        else int(os.environ.get("HOME_PLATFORM_MAX_CACHE_BYTES", str(20 * 1024**3)))
    )
    if resolved_max_cache_bytes <= 0:
        raise ValueError("maximum cache bytes must be greater than zero")
    container_image = os.environ.get(
        "HOME_PLATFORM_CONTAINER_IMAGE", "home-platform-ml:0.1"
    )
    docker_executable = _docker_executable()
    return WorkerSettings(
        api_url=resolved_api_url,
        api_token=api_token,
        worker_id=resolved_worker_id,
        poll_seconds=resolved_poll_seconds,
        heartbeat_seconds=resolved_heartbeat_seconds,
        data_directory=resolved_data_directory,
        allowed_dataset_hosts=resolved_hosts,
        max_dataset_bytes=resolved_max_dataset_bytes,
        max_cache_bytes=resolved_max_cache_bytes,
        docker_executable=docker_executable,
        container_image=container_image,
    )


def execute(
    job: JobRead,
    workspace: WorkerWorkspace | None = None,
    worker_id: str = "worker",
    cancellation_event: threading.Event | None = None,
) -> JobResult:
    if job.type is JobType.SLEEP:
        if not isinstance(job.parameters, SleepParameters):
            raise ValueError("sleep job has invalid parameters")
        seconds = job.parameters.seconds
        logging.info("job=%s sleeping seconds=%s", job.id, seconds)
        if cancellation_event is not None and cancellation_event.wait(seconds):
            raise BatchExecutionFailure(
                FailureKind.CANCELLED_BY_USER, "job cancelled by user"
            )
        if cancellation_event is None:
            time.sleep(seconds)
        return SleepResult(slept_seconds=seconds)
    if job.type is JobType.PYTHON_BATCH:
        if not isinstance(job.parameters, PythonBatchParameters):
            raise ValueError("Python batch job has invalid parameters")
        if workspace is None:
            raise ValueError("Python batch execution requires a worker workspace")
        return run_python_batch(
            job.id,
            worker_id,
            job.parameters,
            workspace,
            cancellation_event=cancellation_event,
            job_name=job.name,
        )
    if job.type is JobType.BATCH:
        if not isinstance(job.parameters, BatchParameters):
            raise ValueError("batch job has invalid parameters")
        if workspace is None:
            raise ValueError("batch execution requires a worker workspace")
        return run_batch(
            job.id,
            worker_id,
            job.parameters,
            workspace,
            cancellation_event=cancellation_event,
            job_name=job.name,
            attempt=job.attempt,
        )
    raise ValueError(f"Unsupported job type: {job.type}")


class LeaseKeeper:
    def __init__(self, client: WorkerAPI, job: JobRead, interval: float) -> None:
        self.client = client
        self.job = job
        self.interval = interval
        self.stop_event = threading.Event()
        self.cancellation_event = threading.Event()
        self.lost = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "LeaseKeeper":
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop_event.set()
        self.thread.join()

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                if self.client.heartbeat(self.job):
                    self.cancellation_event.set()
            except (httpx.HTTPError, ValueError):
                logging.exception("job=%s lease heartbeat failed", self.job.id)
                self.lost = True
                return


def publish_artifacts(
    client: WorkerAPI,
    job: JobRead,
    workspace: WorkerWorkspace | None,
    attempts: int = 3,
) -> int:
    """Upload this job's output files to the control plane.

    Called while the lease keeper is still running, deliberately. A large
    artifact can take longer to upload than the lease interval, and if nothing
    were renewing the lease the control plane would requeue the job midway
    through it succeeding.

    A failure here fails the job. A COMPLETED job whose results silently went
    nowhere is worse than a visible failure, and the files remain on this
    worker either way.
    """
    if workspace is None:
        return 0
    directory = workspace.root / "artifacts" / str(job.id)
    if not directory.is_dir():
        return 0

    published = 0
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        for attempt in range(1, attempts + 1):
            try:
                client.upload_artifact(job, path)
                published += 1
                break
            except Exception:
                if attempt == attempts:
                    logging.error(
                        "job=%s artifact=%s upload failed after %d attempts",
                        job.id,
                        path.name,
                        attempts,
                    )
                    raise
                logging.warning(
                    "job=%s artifact=%s upload attempt %d failed; retrying",
                    job.id,
                    path.name,
                    attempt,
                )
                time.sleep(2 * attempt)
    if published:
        logging.info("job=%s artifacts published=%d", job.id, published)
        # Only reached if every upload succeeded — a failure raises above. Once
        # the control plane holds the results, this copy is pure duplication,
        # and keeping it means every laptop slowly accumulates everything it has
        # ever produced. The per-job input directory goes too; it is just copies
        # of the cached script and dataset.
        for stale in (directory, workspace.root / "runs" / str(job.id)):
            shutil.rmtree(stale, ignore_errors=True)
        logging.info("job=%s worker copies removed", job.id)
    return published


def discard_job_files(job: JobRead, workspace: WorkerWorkspace | None) -> None:
    """Remove incomplete outputs when an operator deliberately cancels a job."""
    if workspace is None:
        return
    for path in (
        workspace.root / "artifacts" / str(job.id),
        workspace.root / "runs" / str(job.id),
    ):
        shutil.rmtree(path, ignore_errors=True)


MAX_IDLE_POLL_SECONDS = 30.0


def next_idle_interval(current: float, base: float) -> float:
    """Back off while nothing is queued, capped so pickup stays predictable.

    A worker that just finished a job is likely to be offered another, so the
    caller resets to `base` on every claim. A worker that has been idle for
    minutes — including a scheduling-disabled one, which can never claim — is
    asking a question whose answer has been "no" for a while, so it asks less
    often. The cap bounds worst-case pickup latency for the first job after a
    quiet period.
    """
    return min(current * 2, max(base, MAX_IDLE_POLL_SECONDS))


def run_once(
    client: WorkerAPI,
    heartbeat_seconds: float = 5,
    workspace: WorkerWorkspace | None = None,
) -> bool:
    job = client.claim()
    if job is None:
        return False

    if job.lease_token is None:
        raise ValueError("control plane returned a job without a lease token")

    logging.info("job=%s claimed worker=%s", job.id, client.worker_id)
    execution_error: Exception | None = None
    result: JobResult | None = None
    with LeaseKeeper(client, job, heartbeat_seconds) as lease:
        try:
            result = execute(
                job,
                workspace,
                client.worker_id,
                cancellation_event=lease.cancellation_event,
            )
            if lease.cancellation_event.is_set():
                raise BatchExecutionFailure(
                    FailureKind.CANCELLED_BY_USER, "job cancelled by user"
                )
            # Inside the lease keeper on purpose — see publish_artifacts.
            publish_artifacts(client, job, workspace)
        except Exception as error:
            logging.exception("job=%s execution failed", job.id)
            execution_error = error

    if lease.lost:
        logging.error("job=%s result discarded because lease was lost", job.id)
        return True

    if lease.cancellation_event.is_set():
        discard_job_files(job, workspace)

    try:
        if execution_error is not None:
            failure_kind = (
                FailureKind.CANCELLED_BY_USER
                if lease.cancellation_event.is_set()
                else execution_error.failure_kind
                if isinstance(execution_error, BatchExecutionFailure)
                else FailureKind.EXECUTION_ERROR
            )
            client.fail(
                job,
                f"{type(execution_error).__name__}: {execution_error}",
                failure_kind,
            )
        else:
            assert result is not None
            completed = client.complete(job, result)
            logging.info("job=%s status=%s", completed.id, completed.status)
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 409:
            raise
        logging.warning("job=%s completion rejected because lease was lost", job.id)
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Home Platform compute worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="process at most one job, then exit",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        help="seconds between empty queue or connection retries",
    )
    parser.add_argument("--worker-id", help="stable worker identity")
    parser.add_argument("--url", help="control-plane base URL")
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        help="seconds between lease renewals",
    )
    parser.add_argument("--log-file", type=Path, help="rotating worker log file")
    parser.add_argument(
        "--token-file",
        type=Path,
        help="path to the control-plane API token",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="worker-local dataset cache and artifact directory",
    )
    parser.add_argument(
        "--dataset-host",
        action="append",
        dest="dataset_hosts",
        help="approved HTTPS dataset hostname; may be repeated",
    )
    parser.add_argument(
        "--max-dataset-bytes",
        type=int,
        help="maximum declared size accepted by this worker",
    )
    parser.add_argument(
        "--max-cache-bytes",
        type=int,
        help="combined ceiling for reusable dataset, input, and project caches",
    )
    return parser


def configure_logging(log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_file,
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )
    # httpx logs every successful request at INFO. An idle worker makes the
    # same authenticated claim repeatedly, so those routine 200 lines can grow
    # by tens of megabytes per day without adding operational information.
    # Keep warnings and failures while leaving job lifecycle logs at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    args = build_parser().parse_args()
    configure_logging(args.log_file)
    settings = load_settings(
        args.poll_seconds,
        worker_id=args.worker_id,
        api_url=args.url,
        heartbeat_seconds=args.heartbeat_seconds,
        token_file=args.token_file,
        data_directory=args.data_dir,
        allowed_dataset_hosts=args.dataset_hosts,
        max_dataset_bytes=args.max_dataset_bytes,
        max_cache_bytes=args.max_cache_bytes,
    )
    client = ControlPlaneClient(settings)
    workspace = WorkerWorkspace(
        root=settings.data_directory,
        allowed_dataset_hosts=settings.allowed_dataset_hosts,
        max_dataset_bytes=settings.max_dataset_bytes,
        max_cache_bytes=settings.max_cache_bytes,
        control_plane_url=settings.api_url,
        api_token=settings.api_token,
        docker_executable=settings.docker_executable,
        container_image=settings.container_image,
    )
    logging.info(
        "worker=%s control_plane=%s starting",
        settings.worker_id,
        settings.api_url,
    )

    try:
        if args.once:
            run_once(client, settings.heartbeat_seconds, workspace)
            return

        idle_seconds = settings.poll_seconds
        while True:
            try:
                processed_job = run_once(client, settings.heartbeat_seconds, workspace)
            except httpx.HTTPStatusError as error:
                if error.response.status_code < 500:
                    raise
                logging.warning("control plane server error: %s", error)
                processed_job = False
            except httpx.RequestError as error:
                logging.warning("control plane unavailable: %s", error)
                processed_job = False
            if processed_job:
                idle_seconds = settings.poll_seconds
            else:
                time.sleep(idle_seconds)
                idle_seconds = next_idle_interval(idle_seconds, settings.poll_seconds)
    finally:
        client.close()


if __name__ == "__main__":
    main()
