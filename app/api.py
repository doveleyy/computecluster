import sqlite3
from secrets import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Request,
    Security,
    status,
)
from fastapi.security import APIKeyHeader

from app.job_http import (
    IdempotencyKey,
    JobServiceDependency,
    finish_job,
    release_uploads,
)
from app.job_http import create_job as create_job_from_client
from app.service import JobGroupNotFoundError, WorkerNotFoundError
from app.version import VERSION
from contracts.models import (
    JobCompletion,
    JobCreate,
    JobFailure,
    JobGroupCreate,
    JobGroupRead,
    JobRead,
    JobStatus,
    WorkerCapacityUpdate,
    WorkerClaim,
    WorkerHeartbeat,
    WorkerHeartbeatResponse,
    WorkerRead,
    WorkerUpdate,
)


def create_router() -> APIRouter:
    router = APIRouter()
    api_token_header = APIKeyHeader(name="X-API-Token", auto_error=False)

    def require_api_token(
        request: Request,
        supplied_token: Annotated[str | None, Security(api_token_header)],
    ) -> None:
        expected_token = request.app.state.settings.api_token
        if expected_token is None:
            return
        if supplied_token is None or not compare_digest(supplied_token, expected_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid API token",
            )

    Authorized = Annotated[None, Depends(require_api_token)]

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "healthy"}

    @router.get("/ready")
    def ready(job_service: JobServiceDependency) -> dict[str, str]:
        try:
            job_service.ping()
        except sqlite3.Error as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database is unavailable",
            ) from error
        return {"status": "ready"}

    @router.get("/version")
    def version() -> dict[str, str]:
        return {"name": "personal-home-platform", "version": VERSION}

    @router.post(
        "/jobs",
        response_model=JobRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_job(
        job_create: JobCreate,
        job_service: JobServiceDependency,
        _: Authorized,
        idempotency_key: IdempotencyKey = None,
    ) -> JobRead:
        return create_job_from_client(job_service, job_create, idempotency_key)

    @router.post(
        "/job-groups",
        response_model=JobGroupRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_job_group(
        group_create: JobGroupCreate,
        job_service: JobServiceDependency,
        _: Authorized,
        idempotency_key: IdempotencyKey = None,
    ) -> JobGroupRead:
        return job_service.create_group(group_create, idempotency_key)

    @router.get("/job-groups", response_model=list[JobGroupRead])
    def list_job_groups(
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> list[JobGroupRead]:
        return job_service.list_groups()

    @router.get("/job-groups/{group_id}", response_model=JobGroupRead)
    def get_job_group(
        group_id: UUID,
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> JobGroupRead:
        try:
            return job_service.get_group(group_id)
        except JobGroupNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job group with ID {group_id} not found",
            ) from None

    @router.get("/jobs", response_model=list[JobRead])
    def list_jobs(
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> list[JobRead]:
        return job_service.list()

    @router.get("/jobs/{job_id}", response_model=JobRead)
    def get_job(
        job_id: UUID,
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> JobRead:
        job = job_service.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job with ID {job_id} not found",
            )
        return job

    @router.post("/jobs/{job_id}/cancel", response_model=JobRead)
    def cancel_job(
        job_id: UUID,
        request: Request,
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> JobRead:
        job = finish_job(lambda: job_service.cancel(job_id), job_id)
        if job.status is JobStatus.FAILED:
            release_uploads(request, job_service, job)
        return job

    @router.post("/workers/claim", response_model=JobRead | None)
    def claim_job(
        claim: WorkerClaim,
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> JobRead | None:
        return job_service.claim(claim)

    @router.post("/workers/heartbeat", response_model=WorkerHeartbeatResponse)
    def worker_heartbeat(
        heartbeat: WorkerHeartbeat,
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> WorkerHeartbeatResponse:
        return job_service.heartbeat(heartbeat)

    @router.get("/workers", response_model=list[WorkerRead])
    def list_workers(
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> list[WorkerRead]:
        return job_service.list_workers()

    @router.patch("/workers/{worker_id}", response_model=WorkerRead)
    def update_worker(
        worker_id: Annotated[
            str,
            Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"),
        ],
        update: WorkerUpdate,
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> WorkerRead:
        try:
            return job_service.set_worker_enabled(worker_id, update.enabled)
        except WorkerNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Worker with ID {worker_id} not found",
            ) from None

    @router.put("/workers/{worker_id}/capacity", response_model=WorkerRead)
    def update_worker_capacity(
        worker_id: Annotated[
            str,
            Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"),
        ],
        capacity: WorkerCapacityUpdate,
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> WorkerRead:
        try:
            return job_service.set_worker_capacity(worker_id, capacity)
        except WorkerNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Worker with ID {worker_id} not found",
            ) from None

    @router.post("/jobs/{job_id}/complete", response_model=JobRead)
    def complete_job(
        job_id: UUID,
        completion: JobCompletion,
        request: Request,
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> JobRead:
        job = finish_job(lambda: job_service.complete(job_id, completion), job_id)
        release_uploads(request, job_service, job)
        return job

    @router.post("/jobs/{job_id}/fail", response_model=JobRead)
    def fail_job(
        job_id: UUID,
        failure: JobFailure,
        request: Request,
        job_service: JobServiceDependency,
        _: Authorized,
    ) -> JobRead:
        job = finish_job(lambda: job_service.fail(job_id, failure), job_id)
        release_uploads(request, job_service, job)
        return job

    return router
