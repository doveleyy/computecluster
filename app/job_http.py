"""Shared HTTP-layer helpers for job submission, transitions and staged inputs."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, cast
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status

from app.batch_script import compile_batch_submission
from app.service import JobNotFoundError, JobService
from contracts.models import (
    BatchSubmissionCreate,
    JobCreate,
    JobGroupRead,
    JobRead,
    JobStatus,
    UploadedDatasetReference,
    UploadedInputReference,
    UploadedProjectReference,
    UploadedScriptReference,
)


def get_job_service(request: Request) -> JobService:
    return cast(JobService, request.app.state.job_service)


JobServiceDependency = Annotated[JobService, Depends(get_job_service)]
IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
]


def create_batch_submission(
    request: Request,
    job_service: JobService,
    submission: BatchSubmissionCreate,
    idempotency_key: str | None,
    owner_user_id: str | None = None,
) -> JobGroupRead:
    archive = (
        request.app.state.settings.upload_directory
        / "projects"
        / f"{submission.project.upload_id}.zip"
    )
    group_create = compile_batch_submission(submission, archive)
    if owner_user_id is None:
        return job_service.create_group(group_create, idempotency_key)
    return job_service.create_group(group_create, idempotency_key, owner_user_id)


def create_job(
    job_service: JobService,
    job_create: JobCreate,
    idempotency_key: str | None,
    owner_user_id: str | None = None,
) -> JobRead:
    """Run the one canonical submission path for every human client adapter."""
    if owner_user_id is None:
        return job_service.create(job_create, idempotency_key)
    return job_service.create(job_create, idempotency_key, owner_user_id)


def referenced_uploads(job: JobRead | JobCreate) -> set[UUID]:
    """Return staged upload IDs referenced by a job, if any."""
    found: set[UUID] = set()
    dataset = getattr(job.parameters, "dataset", None)
    if isinstance(dataset, UploadedDatasetReference):
        found.add(dataset.upload_id)
    script = getattr(job.parameters, "script", None)
    if isinstance(script, UploadedScriptReference):
        found.add(script.upload_id)
    project = getattr(job.parameters, "project", None)
    if isinstance(project, UploadedProjectReference):
        found.add(project.upload_id)
    for source in getattr(job.parameters, "inputs", {}).values():
        if isinstance(source, UploadedInputReference):
            found.add(source.upload_id)
    return found


def release_uploads(request: Request, job_service: JobService, job: JobRead) -> None:
    """Delete terminal-job uploads unless another unfinished job still needs them."""
    wanted = referenced_uploads(job)
    if not wanted:
        return
    still_needed: set[UUID] = set()
    for other in job_service.list():
        if other.id != job.id and other.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
            still_needed |= referenced_uploads(other)

    directory: Path = request.app.state.settings.upload_directory
    for upload_id in wanted - still_needed:
        (directory / f"{upload_id}.csv").unlink(missing_ok=True)
        (directory / "scripts" / f"{upload_id}.py").unlink(missing_ok=True)
        (directory / "projects" / f"{upload_id}.zip").unlink(missing_ok=True)
        (directory / "inputs" / f"{upload_id}.input").unlink(missing_ok=True)
    account_store = getattr(request.app.state, "account_store", None)
    if account_store is not None:
        account_store.forget_uploads(wanted - still_needed)


def finish_job(action: Callable[[], JobRead], job_id: UUID) -> JobRead:
    """Map a missing job to the public 404; transition conflicts map app-wide."""
    try:
        return action()
    except JobNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job with ID {job_id} not found",
        ) from None
