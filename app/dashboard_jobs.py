"""The member Job Desk: submitting, listing and cancelling one's own jobs."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.accounts import AccountStore, SessionIdentity
from app.batch_script import ParsedBatchScript, parse_batch_project
from app.dashboard_auth import (
    AccountStoreDependency,
    ApiToken,
    DashboardSession,
    MemberSession,
    owner_scope,
    require_member_storage,
)
from app.dashboard_uploads import PROJECT
from app.job_http import (
    IdempotencyKey,
    JobServiceDependency,
    create_batch_submission,
    finish_job,
    release_uploads,
)
from app.job_http import create_job as create_job_from_client
from app.service import JobGroupNotFoundError
from app.storage import (
    member_storage_path,
    member_storage_path_allowed,
    storage_file_reference,
)
from contracts.models import (
    BatchSubmissionCreate,
    DatasetReference,
    JobCreate,
    JobGroupCreate,
    JobGroupRead,
    JobRead,
    JobStatus,
    StorageInputReference,
    UploadedDatasetReference,
    UploadedInputReference,
    UploadedProjectReference,
    UploadedScriptReference,
    WorkerRead,
)

UPLOADED_REFERENCE_TYPES = (
    UploadedDatasetReference,
    UploadedScriptReference,
    UploadedProjectReference,
    UploadedInputReference,
)
SubmissionSource = (
    DatasetReference
    | UploadedDatasetReference
    | UploadedScriptReference
    | UploadedProjectReference
    | UploadedInputReference
    | StorageInputReference
)

router = APIRouter()


def submission_sources(
    payload: JobCreate | BatchSubmissionCreate,
) -> list[SubmissionSource]:
    fields = payload.parameters if isinstance(payload, JobCreate) else payload
    named = (getattr(fields, name, None) for name in ("dataset", "script", "project"))
    sources: list[SubmissionSource] = [source for source in named if source is not None]
    sources.extend(getattr(fields, "inputs", {}).values())
    return sources


def authorize_member_sources(
    request: Request,
    account_store: AccountStore,
    identity: SessionIdentity,
    payloads: list[JobCreate] | list[BatchSubmissionCreate],
) -> None:
    """Refuse any upload or storage reference a member may not read.

    Every member submission path funnels through here, so no route can
    accept a reference type the others check.
    """
    sources = [source for payload in payloads for source in submission_sources(payload)]
    for source in sources:
        if isinstance(source, UPLOADED_REFERENCE_TYPES) and (
            not account_store.upload_belongs_to(source.upload_id, identity.id)
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Uploaded object not found",
            )
    for source in sources:
        if not isinstance(source, StorageInputReference):
            continue
        require_member_storage(request, identity)
        if not member_storage_path_allowed(identity.id, source.path):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Storage input not found",
            )
        actual = storage_file_reference(
            request.app.state.settings.storage_directory, source.path
        )
        if actual != source:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Storage input changed after selection",
            )


def parsed_project(
    request: Request,
    project: UploadedProjectReference,
    entrypoint: str,
) -> ParsedBatchScript:
    archive = PROJECT.path(request.app.state.settings, project.upload_id)
    return parse_batch_project(archive, entrypoint)


def resolve_default_storage_inputs(
    submission: BatchSubmissionCreate,
    request: Request,
    identity: SessionIdentity,
) -> BatchSubmissionCreate:
    """Bind each `#HP --input NAME=PATH` default to the member's own storage."""
    parsed = parsed_project(request, submission.project, submission.entrypoint)
    inputs = dict(submission.inputs)
    for declaration in parsed.inputs:
        if declaration.name in inputs or declaration.default_path is None:
            continue
        require_member_storage(request, identity)
        inputs[declaration.name] = storage_file_reference(
            request.app.state.settings.storage_directory,
            member_storage_path(identity.id, declaration.default_path),
        )
    return submission.model_copy(update={"inputs": inputs})


@router.get("/jobs-ui/api/jobs", response_model=list[JobRead])
def jobs_portal_list(
    job_service: JobServiceDependency,
    identity: DashboardSession,
) -> list[JobRead]:
    return job_service.list(owner_scope(identity))


@router.get("/jobs-ui/api/workers", response_model=list[WorkerRead])
def jobs_portal_workers(
    job_service: JobServiceDependency,
    identity: DashboardSession,
) -> list[WorkerRead]:
    workers = job_service.list_workers()
    if identity.is_admin:
        return workers
    return [
        worker.model_copy(update={"metrics": None, "current_job_id": None})
        for worker in workers
    ]


@router.post(
    "/jobs-ui/api/jobs",
    response_model=JobRead,
    status_code=status.HTTP_201_CREATED,
)
def jobs_portal_create(
    job_create: JobCreate,
    request: Request,
    job_service: JobServiceDependency,
    identity: MemberSession,
    account_store: AccountStoreDependency,
    idempotency_key: IdempotencyKey = None,
) -> JobRead:
    authorize_member_sources(request, account_store, identity, [job_create])
    return create_job_from_client(
        job_service, job_create, idempotency_key, str(identity.id)
    )


@router.get("/jobs-ui/api/job-groups", response_model=list[JobGroupRead])
def jobs_portal_groups(
    job_service: JobServiceDependency,
    identity: DashboardSession,
) -> list[JobGroupRead]:
    return job_service.list_groups(owner_scope(identity))


@router.get("/jobs-ui/api/job-groups/{group_id}", response_model=JobGroupRead)
def jobs_portal_group(
    group_id: UUID,
    job_service: JobServiceDependency,
    identity: DashboardSession,
) -> JobGroupRead:
    try:
        return job_service.get_group(group_id, owner_scope(identity))
    except JobGroupNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job group with ID {group_id} not found",
        ) from None


@router.post(
    "/jobs-ui/api/job-groups",
    response_model=JobGroupRead,
    status_code=status.HTTP_201_CREATED,
)
def jobs_portal_create_group(
    group_create: JobGroupCreate,
    request: Request,
    job_service: JobServiceDependency,
    identity: MemberSession,
    account_store: AccountStoreDependency,
    idempotency_key: IdempotencyKey = None,
) -> JobGroupRead:
    authorize_member_sources(
        request, account_store, identity, [task.job for task in group_create.tasks]
    )
    return job_service.create_group(group_create, idempotency_key, str(identity.id))


@router.post("/jobs-ui/api/jobs/{job_id}/cancel", response_model=JobRead)
def jobs_portal_cancel(
    job_id: UUID,
    request: Request,
    job_service: JobServiceDependency,
    identity: DashboardSession,
) -> JobRead:
    job = finish_job(lambda: job_service.cancel(job_id, owner_scope(identity)), job_id)
    if job.status is JobStatus.FAILED:
        release_uploads(request, job_service, job)
    return job


@router.post(
    "/jobs-ui/api/batch-submissions",
    response_model=JobGroupRead,
    status_code=status.HTTP_201_CREATED,
)
def jobs_portal_batch_submission(
    submission: BatchSubmissionCreate,
    request: Request,
    job_service: JobServiceDependency,
    identity: MemberSession,
    account_store: AccountStoreDependency,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=128),
    ] = None,
) -> JobGroupRead:
    authorize_member_sources(request, account_store, identity, [submission])
    submission = resolve_default_storage_inputs(submission, request, identity)
    return create_batch_submission(
        request, job_service, submission, idempotency_key, str(identity.id)
    )


@router.post(
    "/batch-submissions",
    response_model=JobGroupRead,
    status_code=status.HTTP_201_CREATED,
)
def api_batch_submission(
    submission: BatchSubmissionCreate,
    request: Request,
    job_service: JobServiceDependency,
    _: ApiToken,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=128),
    ] = None,
) -> JobGroupRead:
    return create_batch_submission(request, job_service, submission, idempotency_key)
