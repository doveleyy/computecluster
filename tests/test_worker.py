import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

import pytest

from contracts.models import (
    FailureKind,
    JobRead,
    JobResult,
    JobStatus,
    JobType,
    SleepParameters,
    SleepResult,
)
from worker.container_runner import BatchExecutionFailure
from worker.data_plane import WorkerWorkspace
from worker.main import (
    MAX_IDLE_POLL_SECONDS,
    _libre_hardware_temperatures,
    configure_logging,
    execute,
    load_settings,
    next_idle_interval,
    publish_artifacts,
    run_once,
)


def running_job(seconds: int = 1) -> JobRead:
    now = datetime.now(UTC)
    return JobRead(
        id=uuid4(),
        type=JobType.SLEEP,
        parameters=SleepParameters(seconds=seconds),
        status=JobStatus.RUNNING,
        created_at=now,
        updated_at=now,
        worker_id="mac-one",
        started_at=now,
        lease_token=uuid4(),
        lease_expires_at=now,
    )


class FakeWorkerAPI:
    worker_id = "mac-one"

    def __init__(self, job: JobRead | None) -> None:
        self.job = job
        self.completed_result: JobResult | None = None
        self.failure: str | None = None
        self.failure_kind: FailureKind | None = None
        self.heartbeats: list[JobRead | None] = []
        self.uploaded: list[str] = []
        self.cancel_on_heartbeat = False

    def claim(self) -> JobRead | None:
        return self.job

    def complete(self, job: JobRead, result: JobResult) -> JobRead:
        self.completed_result = result
        return job.model_copy(update={"status": JobStatus.COMPLETED, "result": result})

    def fail(
        self,
        job: JobRead,
        error: str,
        failure_kind: FailureKind = FailureKind.EXECUTION_ERROR,
    ) -> JobRead:
        self.failure = error
        self.failure_kind = failure_kind
        return job.model_copy(
            update={
                "status": JobStatus.FAILED,
                "error": error,
                "failure_kind": failure_kind,
            }
        )

    def heartbeat(self, job: JobRead | None = None) -> bool:
        self.heartbeats.append(job)
        return self.cancel_on_heartbeat

    def upload_artifact(self, job: JobRead, path: Path) -> None:
        self.uploaded.append(path.name)


def test_sleep_executor_uses_validated_duration(monkeypatch) -> None:
    slept_for: list[int] = []
    monkeypatch.setattr("worker.main.time.sleep", slept_for.append)

    result = execute(running_job(seconds=3))

    assert result.model_dump() == {"slept_seconds": 3}
    assert slept_for == [3]


def test_worker_reports_success(monkeypatch) -> None:
    client = FakeWorkerAPI(running_job(seconds=2))
    monkeypatch.setattr(
        "worker.main.execute",
        lambda job, workspace, worker_id, cancellation_event: SleepResult(
            slept_seconds=2
        ),
    )

    assert run_once(client) is True
    assert client.completed_result == SleepResult(slept_seconds=2)
    assert client.failure is None


def test_worker_does_nothing_when_queue_is_empty() -> None:
    client = FakeWorkerAPI(None)

    assert run_once(client) is False
    assert client.completed_result is None
    assert client.failure is None
    # The claim request already carries idle liveness and metrics. Heartbeats
    # are reserved for active lease renewal and cancellation.
    assert client.heartbeats == []


def test_worker_reports_execution_failure(monkeypatch) -> None:
    client = FakeWorkerAPI(running_job())

    def fail_execution(
        job: JobRead,
        workspace: object,
        worker_id: str,
        cancellation_event: object,
    ) -> SleepResult:
        raise RuntimeError(f"cannot execute {job.id}")

    monkeypatch.setattr("worker.main.execute", fail_execution)

    assert run_once(client) is True
    assert client.completed_result is None
    assert client.failure is not None
    assert client.failure.startswith("RuntimeError: cannot execute")
    assert client.failure_kind is FailureKind.EXECUTION_ERROR


def test_worker_preserves_structured_batch_failure(monkeypatch) -> None:
    client = FakeWorkerAPI(running_job())

    def exceed_memory(
        job: JobRead,
        workspace: object,
        worker_id: str,
        cancellation_event: object,
    ) -> SleepResult:
        raise BatchExecutionFailure(
            FailureKind.MEMORY_LIMIT_EXCEEDED, "container exceeded memory limit"
        )

    monkeypatch.setattr("worker.main.execute", exceed_memory)

    assert run_once(client) is True
    assert client.failure_kind is FailureKind.MEMORY_LIMIT_EXCEEDED
    assert client.failure is not None
    assert "exceeded memory limit" in client.failure


def test_worker_stops_and_acknowledges_user_cancellation(tmp_path: Path) -> None:
    job = running_job(seconds=30)
    client = FakeWorkerAPI(job)
    client.cancel_on_heartbeat = True
    workspace = artifact_workspace(tmp_path, job, ["partial.txt"])
    run_directory = tmp_path / "runs" / str(job.id)
    run_directory.mkdir(parents=True)

    assert run_once(client, heartbeat_seconds=0.01, workspace=workspace) is True

    assert client.completed_result is None
    assert client.failure_kind is FailureKind.CANCELLED_BY_USER
    assert not (tmp_path / "artifacts" / str(job.id)).exists()
    assert not run_directory.exists()


def test_worker_settings_reject_invalid_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    token_file = tmp_path / "api-token"
    token_file.write_text("test-secret")
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("HOME_PLATFORM_WORKER_ID", "spaces are invalid")

    with pytest.raises(ValueError, match="worker ID"):
        load_settings()

    monkeypatch.setenv("HOME_PLATFORM_WORKER_ID", "mac-one")
    with pytest.raises(ValueError, match="poll interval"):
        load_settings(poll_seconds=0)


def test_worker_settings_accept_cli_overrides(tmp_path: Path, monkeypatch) -> None:
    environment_token = tmp_path / "environment-token"
    environment_token.write_text("wrong-secret")
    explicit_token = tmp_path / "explicit-token"
    explicit_token.write_text("test-secret")
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN_FILE", str(environment_token))

    settings = load_settings(
        poll_seconds=3,
        worker_id="windows-primary",
        api_url="http://raspberrypi.local:8000",
        heartbeat_seconds=4,
        token_file=explicit_token,
    )

    assert settings.api_token == "test-secret"
    assert settings.worker_id == "windows-primary"
    assert settings.api_url == "http://raspberrypi.local:8000"
    assert settings.poll_seconds == 3
    assert settings.heartbeat_seconds == 4


def test_worker_logging_suppresses_successful_http_noise_and_rotates(
    tmp_path: Path, monkeypatch
) -> None:
    configured: list[dict[str, object]] = []
    monkeypatch.setattr(
        "worker.main.logging.basicConfig", lambda **values: configured.append(values)
    )
    httpx_logger = logging.getLogger("httpx")
    previous_level = httpx_logger.level
    try:
        configure_logging(tmp_path / "worker.log")
        assert httpx_logger.level == logging.WARNING
        handlers = configured[0]["handlers"]
        assert isinstance(handlers, list)
        rotating = next(
            item for item in handlers if isinstance(item, RotatingFileHandler)
        )
        assert rotating.maxBytes == 5 * 1024 * 1024
        assert rotating.backupCount == 3
        rotating.close()
    finally:
        httpx_logger.setLevel(previous_level)


def test_libre_hardware_monitor_temperature_parsing(monkeypatch) -> None:
    monkeypatch.setattr("worker.main.platform.system", lambda: "Windows")
    monkeypatch.setattr("worker.main.shutil.which", lambda name: "powershell.exe")

    class Completed:
        stdout = (
            '[{"Identifier":"/intelcpu/0/temperature/0","Name":"CPU Core",'
            '"Value":68.5},{"Identifier":"/gpu-intel/0/temperature/0",'
            '"Name":"GPU Core","Value":54.0}]'
        )

    monkeypatch.setattr(
        "worker.main.subprocess.run", lambda *args, **kwargs: Completed()
    )

    assert _libre_hardware_temperatures() == (68.5, 54.0)


def test_libre_hardware_monitor_unavailable_is_not_fatal(monkeypatch) -> None:
    monkeypatch.setattr("worker.main.platform.system", lambda: "Windows")
    monkeypatch.setattr("worker.main.shutil.which", lambda name: None)

    assert _libre_hardware_temperatures() == (None, None)


def artifact_workspace(root: Path, job: JobRead, names: list[str]) -> WorkerWorkspace:
    directory = root / "artifacts" / str(job.id)
    directory.mkdir(parents=True)
    for name in names:
        (directory / name).write_bytes(b"payload")
    (directory / ".partial.tmp").write_bytes(b"ignore me")
    return WorkerWorkspace(
        root=root,
        allowed_dataset_hosts=frozenset(),
        max_dataset_bytes=1024,
    )


def test_worker_publishes_artifacts_before_completing(tmp_path: Path) -> None:
    job = running_job()
    client = FakeWorkerAPI(job)
    workspace = artifact_workspace(tmp_path, job, ["model.joblib", "metrics.json"])

    published = publish_artifacts(client, job, workspace)

    # Sorted, and the in-progress dotfile is skipped.
    assert published == 2
    assert client.uploaded == ["metrics.json", "model.joblib"]


def test_publishing_is_a_no_op_without_artifacts(tmp_path: Path) -> None:
    job = running_job()
    client = FakeWorkerAPI(job)
    workspace = WorkerWorkspace(
        root=tmp_path,
        allowed_dataset_hosts=frozenset(),
        max_dataset_bytes=1024,
    )

    assert publish_artifacts(client, job, workspace) == 0
    assert publish_artifacts(client, job, None) == 0
    assert client.uploaded == []


def test_artifact_upload_retries_then_fails_the_job(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("worker.main.time.sleep", lambda _seconds: None)
    job = running_job()
    workspace = artifact_workspace(tmp_path, job, ["model.joblib"])

    class FlakyThenFatal(FakeWorkerAPI):
        def __init__(self, job: JobRead | None, failures: int) -> None:
            super().__init__(job)
            self.failures = failures
            self.attempts = 0

        def upload_artifact(self, job: JobRead, path: Path) -> None:
            self.attempts += 1
            if self.attempts <= self.failures:
                raise RuntimeError("network hiccup")
            super().upload_artifact(job, path)

    recovers = FlakyThenFatal(job, failures=2)
    assert publish_artifacts(recovers, job, workspace) == 1
    assert recovers.attempts == 3

    # A publish that never succeeds must raise, so run_once fails the job rather
    # than reporting COMPLETED for results that went nowhere. This needs its own
    # workspace: a successful publish deletes the worker's copy, so reusing the
    # first one would leave nothing to upload and nothing to fail on.
    second = tmp_path / "second-run"
    other_workspace = artifact_workspace(second, job, ["model.joblib"])
    persistent = FlakyThenFatal(job, failures=99)
    with pytest.raises(RuntimeError, match="network hiccup"):
        publish_artifacts(persistent, job, other_workspace)
    assert persistent.attempts == 3
    # A failed publish must leave the worker's copy alone — it is the only
    # remaining copy of the results.
    assert (second / "artifacts" / str(job.id) / "model.joblib").exists()


def test_publishing_removes_the_worker_copy(tmp_path: Path) -> None:
    """Once the control plane holds the results the local copy is duplication.

    Keeping it means every laptop slowly accumulates everything it has ever
    produced, which is the failure this cleanup exists to prevent.
    """
    job = running_job()
    client = FakeWorkerAPI(job)
    workspace = artifact_workspace(tmp_path, job, ["model.joblib", "metrics.json"])
    run_inputs = tmp_path / "runs" / str(job.id) / "input"
    run_inputs.mkdir(parents=True)
    (run_inputs / "dataset.csv").write_bytes(b"a,b\n1,2\n")

    assert publish_artifacts(client, job, workspace) == 2

    assert not (tmp_path / "artifacts" / str(job.id)).exists()
    assert not (tmp_path / "runs" / str(job.id)).exists()
    # The content-addressed caches are shared between jobs and must survive.
    assert (tmp_path / "artifacts").exists()


def test_idle_backoff_grows_to_a_cap_and_resets_after_work() -> None:
    base = 5.0
    interval = base
    observed = []
    for _ in range(6):
        observed.append(interval)
        interval = next_idle_interval(interval, base)

    # Idle polling doubles until it reaches the cap, then holds there.
    assert observed == [5.0, 10.0, 20.0, 30.0, 30.0, 30.0]
    # A worker configured to poll slower than the cap is never sped up.
    assert next_idle_interval(60.0, 60.0) == 60.0


def test_idle_backoff_cap_stays_within_the_stale_window() -> None:
    from app.config import load_settings

    settings = load_settings()
    # A worker that is idling correctly must not be reported STALE. If the cap
    # ever exceeds the stale window, healthy workers disappear from best-fit
    # placement and the dashboard.
    assert settings.worker_stale_seconds >= MAX_IDLE_POLL_SECONDS * 2
