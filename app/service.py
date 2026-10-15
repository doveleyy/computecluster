from builtins import list as list_type
from datetime import UTC, datetime, timedelta
from typing import NoReturn
from uuid import UUID, uuid4

from app.app_origin import AppOrigin
from app.identity import ADMIN_USER_ID
from app.repository import JobRepository
from contracts.models import (
    BatchParameters,
    BatchResult,
    FailureKind,
    JobCompletion,
    JobCreate,
    JobFailure,
    JobGroupCreate,
    JobGroupRead,
    JobRead,
    JobStatus,
    JobType,
    PythonBatchParameters,
    PythonBatchResult,
    SleepResult,
    WorkerCapacityUpdate,
    WorkerClaim,
    WorkerHeartbeat,
    WorkerHeartbeatResponse,
    WorkerRead,
)


class JobTransitionError(Exception):
    pass


class JobNotFoundError(Exception):
    pass


class JobGroupNotFoundError(Exception):
    pass


class WorkerNotFoundError(Exception):
    pass


class IdempotencyConflictError(Exception):
    pass


class SchedulingCapacityError(Exception):
    pass


class JobService:
    def __init__(
        self,
        repository: JobRepository,
        *,
        lease_seconds: int = 15,
        worker_stale_seconds: int = 20,
        max_attempts: int = 3,
    ) -> None:
        self.repository = repository
        self.lease_seconds = lease_seconds
        self.worker_stale_seconds = worker_stale_seconds
        self.max_attempts = max_attempts

    def create(
        self,
        job_create: JobCreate,
        idempotency_key: str | None = None,
        owner_user_id: str = ADMIN_USER_ID,
    ) -> JobRead:
        if (
            job_create.target_worker_id is not None
            and not self.repository.worker_exists(job_create.target_worker_id)
        ):
            raise WorkerNotFoundError(job_create.target_worker_id)
        self._validate_batch_capacity(job_create)
        now = datetime.now(UTC)
        job = JobRead(
            id=uuid4(),
            name=job_create.name,
            type=JobType(job_create.type.value),
            parameters=job_create.parameters,
            target_worker_id=job_create.target_worker_id,
            status=JobStatus.QUEUED,
            created_at=now,
            updated_at=now,
            max_attempts=self.max_attempts,
        )
        stored = self.repository.add(job, idempotency_key, owner_user_id)
        if (
            stored.type.value != job_create.type.value
            or stored.parameters != job_create.parameters
            or stored.name != job_create.name
            or stored.target_worker_id != job_create.target_worker_id
        ):
            raise IdempotencyConflictError(
                "Idempotency key was already used for a different request"
            )
        return stored

    def get(self, job_id: UUID, owner_user_id: str | None = None) -> JobRead | None:
        return self.repository.get(job_id, owner_user_id)

    def owner_user_id(self, job_id: UUID) -> str:
        owner_user_id = self.repository.owner_user_id(job_id)
        if owner_user_id is None:
            raise JobNotFoundError(job_id)
        return owner_user_id

    def create_group(
        self,
        group_create: JobGroupCreate,
        idempotency_key: str | None = None,
        owner_user_id: str = ADMIN_USER_ID,
        app_origin: AppOrigin | None = None,
    ) -> JobGroupRead:
        for task in group_create.tasks:
            if (
                task.job.target_worker_id is not None
                and not self.repository.worker_exists(task.job.target_worker_id)
            ):
                raise WorkerNotFoundError(task.job.target_worker_id)
            self._validate_batch_capacity(task.job)

        now = datetime.now(UTC)
        group_id = uuid4()
        tasks = [
            JobRead(
                id=uuid4(),
                name=task.job.name or task.task_id,
                type=JobType(task.job.type.value),
                parameters=task.job.parameters,
                target_worker_id=task.job.target_worker_id,
                group_id=group_id,
                task_id=task.task_id,
                task_index=index,
                status=JobStatus.QUEUED,
                created_at=now,
                updated_at=now,
                max_attempts=self.max_attempts,
            )
            for index, task in enumerate(group_create.tasks)
        ]
        group = JobGroupRead(
            id=group_id,
            name=group_create.name,
            status=JobStatus.QUEUED,
            created_at=now,
            updated_at=now,
            tasks=tasks,
        )
        request_json = group_create.model_dump_json()
        stored, stored_request = self.repository.add_group(
            group, request_json, idempotency_key, owner_user_id, app_origin
        )
        if stored_request != request_json:
            raise IdempotencyConflictError(
                "Idempotency key was already used for a different group request"
            )
        return stored

    def get_group(
        self, group_id: UUID, owner_user_id: str | None = None
    ) -> JobGroupRead:
        group = self.repository.get_group(group_id, owner_user_id)
        if group is None:
            raise JobGroupNotFoundError(group_id)
        return group

    def list_groups(self, owner_user_id: str | None = None) -> list[JobGroupRead]:
        return self.repository.list_groups(owner_user_id)

    def list(self, owner_user_id: str | None = None) -> list[JobRead]:
        return self.repository.list(owner_user_id)

    def ping(self) -> None:
        self.repository.ping()

    def claim(self, claim: WorkerClaim) -> JobRead | None:
        now = datetime.now(UTC)
        return self.repository.claim(
            worker_id=claim.worker_id,
            supported_types=claim.supported_types,
            claimed_at=now,
            lease_token=uuid4(),
            lease_expires_at=now + timedelta(seconds=self.lease_seconds),
            metrics=claim.metrics,
            stale_before=now - timedelta(seconds=self.worker_stale_seconds),
        )

    def heartbeat(self, heartbeat: WorkerHeartbeat) -> WorkerHeartbeatResponse:
        now = datetime.now(UTC)
        cancellation_requested = self.repository.heartbeat(
            worker_id=heartbeat.worker_id,
            supported_types=heartbeat.supported_types,
            seen_at=now,
            lease_expires_at=now + timedelta(seconds=self.lease_seconds),
            current_job_id=heartbeat.current_job_id,
            lease_token=heartbeat.lease_token,
            metrics=heartbeat.metrics,
        )
        if cancellation_requested is None:
            raise JobTransitionError("The job lease is no longer valid")
        return WorkerHeartbeatResponse(
            cancellation_requested=cancellation_requested,
        )

    def cancel(self, job_id: UUID, owner_user_id: str | None = None) -> JobRead:
        existing = self.repository.get(job_id, owner_user_id)
        if existing is None:
            raise JobNotFoundError(job_id)
        if existing.status in {JobStatus.COMPLETED, JobStatus.FAILED}:
            raise JobTransitionError(
                f"Job {job_id} is already terminal with status {existing.status}"
            )
        job = self.repository.cancel(job_id, datetime.now(UTC))
        if job is None:
            raise JobNotFoundError(job_id)
        return job

    def list_workers(self) -> list_type[WorkerRead]:
        stale_before = datetime.now(UTC) - timedelta(seconds=self.worker_stale_seconds)
        return self.repository.list_workers(stale_before)

    def set_worker_enabled(self, worker_id: str, enabled: bool) -> WorkerRead:
        stale_before = datetime.now(UTC) - timedelta(seconds=self.worker_stale_seconds)
        worker = self.repository.set_worker_enabled(worker_id, enabled, stale_before)
        if worker is None:
            raise WorkerNotFoundError(worker_id)
        return worker

    def set_worker_capacity(
        self, worker_id: str, capacity: WorkerCapacityUpdate
    ) -> WorkerRead:
        stale_before = datetime.now(UTC) - timedelta(seconds=self.worker_stale_seconds)
        worker = self.repository.set_worker_capacity(
            worker_id,
            capacity.max_job_cpu,
            capacity.max_job_memory_mb,
            stale_before,
        )
        if worker is None:
            raise WorkerNotFoundError(worker_id)
        return worker

    def _validate_batch_capacity(self, job_create: JobCreate) -> None:
        if not isinstance(
            job_create.parameters, (PythonBatchParameters, BatchParameters)
        ):
            return
        workers = self.repository.list_workers(datetime.min.replace(tzinfo=UTC))
        if job_create.target_worker_id is not None:
            workers = [
                worker for worker in workers if worker.id == job_create.target_worker_id
            ]
        adequate = [
            worker
            for worker in workers
            if worker.max_job_cpu is not None
            and worker.max_job_memory_mb is not None
            and worker.max_job_cpu >= job_create.parameters.cpu_limit
            and worker.max_job_memory_mb >= job_create.parameters.memory_mb
        ]
        if adequate:
            return
        requested = (
            f"{job_create.parameters.cpu_limit:g} CPU and "
            f"{job_create.parameters.memory_mb} MiB RAM"
        )
        if job_create.target_worker_id is not None:
            raise SchedulingCapacityError(
                f"Worker {job_create.target_worker_id!r} is not configured to "
                f"accept a job requesting {requested}"
            )
        raise SchedulingCapacityError(
            f"No registered worker is configured to accept a job requesting {requested}"
        )

    def recover_expired(self) -> int:
        return self.repository.recover_expired(datetime.now(UTC))

    def complete(self, job_id: UUID, completion: JobCompletion) -> JobRead:
        existing = self.repository.get(job_id)
        if existing is None:
            raise JobNotFoundError(job_id)
        result_matches = (
            (
                existing.type is JobType.SLEEP
                and isinstance(completion.result, SleepResult)
            )
            or (
                existing.type is JobType.PYTHON_BATCH
                and isinstance(completion.result, PythonBatchResult)
            )
            or (
                existing.type is JobType.BATCH
                and isinstance(completion.result, BatchResult)
            )
        )
        if not result_matches:
            raise JobTransitionError(
                f"Job {job_id} result does not match type {existing.type}"
            )
        job = self.repository.complete(
            job_id=job_id,
            worker_id=completion.worker_id,
            lease_token=completion.lease_token,
            result=completion.result.model_dump(mode="json"),
            finished_at=datetime.now(UTC),
        )
        if job is None:
            self._raise_transition_error(job_id, completion.worker_id)
        return job

    def fail(self, job_id: UUID, failure: JobFailure) -> JobRead:
        existing = self.repository.get(job_id)
        if existing is None:
            raise JobNotFoundError(job_id)
        if existing.cancellation_requested:
            failure = failure.model_copy(
                update={
                    "failure_kind": FailureKind.CANCELLED_BY_USER,
                    "error": "cancelled by user",
                }
            )
        job = self.repository.fail(
            job_id=job_id,
            worker_id=failure.worker_id,
            lease_token=failure.lease_token,
            error=failure.error,
            failure_kind=failure.failure_kind,
            finished_at=datetime.now(UTC),
        )
        if job is None:
            self._raise_transition_error(job_id, failure.worker_id)
        return job

    def authorize_lease(
        self, job_id: UUID, worker_id: str, lease_token: UUID
    ) -> JobRead:
        """Return the job only if this worker currently holds its lease.

        Used by artifact upload, which happens while the job is still RUNNING.
        Publishing results is a mutation of the job's output, so it needs the
        same authority as completing it: a stale token must not be able to
        overwrite the artifacts of whichever worker took the job over.
        """
        existing = self.repository.get(job_id)
        if existing is None:
            raise JobNotFoundError(job_id)
        if (
            existing.status is not JobStatus.RUNNING
            or existing.worker_id != worker_id
            or existing.lease_token != lease_token
            or existing.lease_expires_at is None
            or existing.lease_expires_at <= datetime.now(UTC)
            or existing.cancellation_requested
        ):
            raise JobTransitionError(
                f"Job {job_id} is {existing.status}; worker {worker_id!r} does not "
                "hold its current lease"
            )
        return existing

    def _raise_transition_error(self, job_id: UUID, worker_id: str) -> NoReturn:
        existing = self.repository.get(job_id)
        if existing is None:
            raise JobNotFoundError(job_id)
        raise JobTransitionError(
            f"Job {job_id} is {existing.status}; worker {worker_id!r} does not "
            "hold its current lease"
        )
