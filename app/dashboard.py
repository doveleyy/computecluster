import hashlib
import logging
import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import time
from pathlib import Path, PurePosixPath
from secrets import compare_digest, token_urlsafe
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

import psutil
from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi import (
    Path as ApiPath,
)
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict

from app.accounts import (
    AccountStore,
    DashboardLogin,
    ExternalIdentityConflictError,
    ExternalIdentityRead,
    ExternalIdentityStatus,
    InvalidCurrentPasswordError,
    PasswordChange,
    PasswordReset,
    PortalSession,
    ServiceIdentityResolve,
    SessionIdentity,
    UserCreate,
    UserExistsError,
    UserNotFoundError,
    UserRead,
    UserUpdate,
    decode_session,
    encode_session,
)
from app.artifacts import (
    job_artifact_relative_path,
    legacy_artifact_relative_path,
)
from app.batch_script import (
    BatchScriptError,
    ParsedBatchScript,
    parse_batch_project,
    validate_project_archive,
)
from app.identity import ADMIN_USER_ID
from app.job_http import (
    create_batch_submission,
    finish_job,
    referenced_uploads,
    release_uploads,
)
from app.job_http import create_job as create_job_from_client
from app.power import (
    PowerAction,
    PowerControlUnavailableError,
    PowerRequestPendingError,
    queue_power_request,
)
from app.service import (
    IdempotencyConflictError,
    JobGroupNotFoundError,
    JobNotFoundError,
    JobService,
    JobTransitionError,
    SchedulingCapacityError,
    WorkerNotFoundError,
)
from app.storage import (
    STORAGE_ID,
    StoragePolicyError,
    browse_storage,
    copy_workspace_entry,
    create_workspace_directory,
    delete_workspace_entry,
    inspect_storage_project,
    is_logical_storage_path,
    member_storage_entries,
    member_storage_path,
    member_storage_path_allowed,
    move_workspace_entry,
    package_storage_project,
    resolve_logical_storage_path,
    resolve_storage_path,
    storage_file_reference,
    workspace_upload_target,
)
from app.version import VERSION
from contracts.models import (
    BatchSubmissionCreate,
    JobCreate,
    JobGroupCreate,
    JobGroupRead,
    JobRead,
    JobStatus,
    PythonBatchParameters,
    StorageInputReference,
    UploadedDatasetReference,
    UploadedInputReference,
    UploadedProjectReference,
    UploadedScriptReference,
    WorkerCapacityUpdate,
    WorkerRead,
    WorkerUpdate,
)

SESSION_COOKIE = "home_platform_dashboard"
DASHBOARD_HTML = Path(__file__).with_name("dashboard.html").read_text()
JOBS_HTML = Path(__file__).with_name("jobs.html").read_text()


class StoragePathRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str


class StorageProjectPreviewRequest(StoragePathRequest):
    entrypoint: str = "submit.hp"


class WorkspaceDirectoryCreate(StoragePathRequest):
    pass


class FileTransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    destination: str


class PiPowerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: PowerAction
    confirmation: str


def create_dashboard_router() -> APIRouter:
    router = APIRouter()
    anonymous_session = token_urlsafe(32)

    def session_secret(request: Request) -> str:
        return request.app.state.settings.api_token or anonymous_session

    def get_account_store(request: Request) -> AccountStore:
        return cast(AccountStore, request.app.state.account_store)

    def require_dashboard_session(
        request: Request,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        supplied: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> SessionIdentity:
        claims = (
            decode_session(supplied, session_secret(request), int(time.time()))
            if supplied is not None
            else None
        )
        identity = (
            account_store.get_identity(claims[0], claims[1])
            if claims is not None
            else None
        )
        if identity is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Dashboard login required",
            )
        return identity

    def require_admin_session(
        identity: Annotated[SessionIdentity, Depends(require_dashboard_session)],
    ) -> SessionIdentity:
        if not identity.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Administrator access required",
            )
        return identity

    def require_member_session(
        identity: Annotated[SessionIdentity, Depends(require_dashboard_session)],
    ) -> SessionIdentity:
        if identity.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Member access required; use the operator dashboard",
            )
        return identity

    def require_api_token(
        request: Request,
        supplied: Annotated[str | None, Header(alias="X-API-Token")] = None,
    ) -> None:
        expected = request.app.state.settings.api_token
        if expected is not None and (
            supplied is None or not compare_digest(supplied, expected)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid API token",
            )

    DashboardSession = Annotated[SessionIdentity, Depends(require_dashboard_session)]
    AdminSession = Annotated[SessionIdentity, Depends(require_admin_session)]
    MemberSession = Annotated[SessionIdentity, Depends(require_member_session)]
    ApiToken = Annotated[None, Depends(require_api_token)]

    def require_service_identity_token(
        request: Request,
        supplied: Annotated[
            str | None, Header(alias="X-Service-Identity-Token")
        ] = None,
    ) -> None:
        expected = request.app.state.settings.service_identity_token
        if expected is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Service identity resolution is not configured",
            )
        if supplied is None or not compare_digest(supplied, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid service identity token",
            )

    ServiceIdentityToken = Annotated[None, Depends(require_service_identity_token)]

    def get_job_service(request: Request) -> JobService:
        return cast(JobService, request.app.state.job_service)

    JobServiceDependency = Annotated[JobService, Depends(get_job_service)]

    def owner_scope(identity: SessionIdentity) -> str | None:
        return None if identity.is_admin else str(identity.id)

    def validate_upload_ownership(
        account_store: AccountStore,
        identity: SessionIdentity,
        jobs: list[JobCreate],
    ) -> None:
        if identity.is_admin:
            return
        for job in jobs:
            for source in getattr(job.parameters, "inputs", {}).values():
                if isinstance(source, StorageInputReference):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Personal NAS storage is not provisioned yet",
                    )
            for upload_id in referenced_uploads(job):
                if not account_store.upload_belongs_to(upload_id, identity.id):
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Uploaded object not found",
                    )

    def validate_upload_ids(
        account_store: AccountStore,
        identity: SessionIdentity,
        upload_ids: set[UUID],
    ) -> None:
        if identity.is_admin:
            return
        for upload_id in upload_ids:
            if not account_store.upload_belongs_to(upload_id, identity.id):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Uploaded object not found",
                )

    async def save_upload(
        file: UploadFile,
        *,
        directory: Path,
        suffix: str,
        max_bytes: int,
        label: str,
        validate_suffix: bool = True,
    ) -> tuple[UUID, str, int]:
        filename = file.filename or ""
        if validate_suffix and Path(filename).suffix.lower() != suffix:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"Only {suffix} files are accepted",
            )
        upload_id = uuid4()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{upload_id}{suffix}"
        temporary = directory / f".{upload_id}.part"
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("xb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                            detail=f"{label} exceeds the {max_bytes // 1024} KB limit",
                        )
                    digest.update(chunk)
                    output.write(chunk)
            if size == 0:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"{label} cannot be empty",
                )
            os.replace(temporary, target)
        finally:
            await file.close()
            temporary.unlink(missing_ok=True)
        return upload_id, digest.hexdigest(), size

    async def save_project_upload(
        request: Request, file: UploadFile
    ) -> UploadedProjectReference:
        directory = request.app.state.settings.upload_directory / "projects"
        upload_id, digest, size = await save_upload(
            file,
            directory=directory,
            suffix=".zip",
            max_bytes=request.app.state.settings.max_project_upload_bytes,
            label="Project archive",
        )
        target = directory / f"{upload_id}.zip"
        try:
            validate_project_archive(target)
        except BatchScriptError as error:
            target.unlink(missing_ok=True)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
            ) from error
        return UploadedProjectReference(
            upload_id=upload_id, sha256=digest, size_bytes=size
        )

    @router.get("/dashboard", response_class=HTMLResponse)
    def dashboard() -> str:
        return DASHBOARD_HTML

    @router.get("/dashboard/operations", response_class=HTMLResponse)
    def dashboard_operations() -> str:
        return DASHBOARD_HTML

    @router.get("/dashboard/jobs", response_class=HTMLResponse)
    def dashboard_jobs_page() -> str:
        return JOBS_HTML

    @router.get("/dashboard/files", response_class=HTMLResponse)
    def dashboard_files_page() -> str:
        return JOBS_HTML

    @router.post("/dashboard/login", status_code=status.HTTP_204_NO_CONTENT)
    def login(
        credentials: DashboardLogin,
        request: Request,
        response: Response,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
    ) -> None:
        expected = request.app.state.settings.api_token
        identity: SessionIdentity | None = None
        if credentials.token is not None:
            valid = expected is None or compare_digest(credentials.token, expected)
            if valid:
                identity = account_store.get_identity(UUID(ADMIN_USER_ID))
        elif credentials.username is not None and credentials.password is not None:
            identity = account_store.authenticate(
                credentials.username.strip().lower(), credentials.password
            )
        if identity is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
            )
        expires_at = int(time.time()) + 30 * 24 * 60 * 60
        response.set_cookie(
            SESSION_COOKIE,
            encode_session(
                identity,
                session_secret(request),
                expires_at,
                account_store.session_version(identity.id),
            ),
            httponly=True,
            samesite="strict",
            max_age=30 * 24 * 60 * 60,
            path="/",
        )

    @router.post("/dashboard/logout", status_code=status.HTTP_204_NO_CONTENT)
    def logout(response: Response) -> None:
        response.delete_cookie(SESSION_COOKIE, path="/")

    @router.get("/jobs-ui/api/session", response_model=PortalSession)
    def jobs_portal_session(
        request: Request, identity: DashboardSession
    ) -> PortalSession:
        return PortalSession(
            **identity.model_dump(),
            storage_enabled=(
                identity.is_admin
                or request.app.state.settings.member_storage_enabled
                or identity.id in request.app.state.settings.member_storage_user_ids
            ),
        )

    def tailscale_request_identity(
        request: Request,
        login: str | None,
        display_name: str | None,
    ) -> tuple[str, str] | None:
        if request.url.scheme != "https" or login is None:
            return None
        normalized_login = login.strip().lower()
        if not normalized_login:
            return None
        return normalized_login, (display_name or login).strip()

    @router.get(
        "/jobs-ui/api/account/identities/tailscale",
        response_model=ExternalIdentityStatus,
    )
    def tailscale_identity_status(
        request: Request,
        identity: MemberSession,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        tailscale_login: Annotated[
            str | None, Header(alias="Tailscale-User-Login")
        ] = None,
        tailscale_name: Annotated[
            str | None, Header(alias="Tailscale-User-Name")
        ] = None,
    ) -> ExternalIdentityStatus:
        request_identity = tailscale_request_identity(
            request, tailscale_login, tailscale_name
        )
        return ExternalIdentityStatus(
            linked=account_store.external_identity(identity.id, "tailscale"),
            request_subject=(request_identity[0] if request_identity else None),
            request_display_name=(request_identity[1] if request_identity else None),
        )

    @router.post(
        "/jobs-ui/api/account/identities/tailscale",
        response_model=ExternalIdentityRead,
    )
    def link_tailscale_identity(
        request: Request,
        identity: MemberSession,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        tailscale_login: Annotated[
            str | None, Header(alias="Tailscale-User-Login")
        ] = None,
        tailscale_name: Annotated[
            str | None, Header(alias="Tailscale-User-Name")
        ] = None,
    ) -> ExternalIdentityRead:
        request_identity = tailscale_request_identity(
            request, tailscale_login, tailscale_name
        )
        if request_identity is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Open Job Desk through its private HTTPS Tailscale URL",
            )
        try:
            return account_store.link_external_identity(
                identity.id,
                "tailscale",
                request_identity[0],
                request_identity[1],
            )
        except ExternalIdentityConflictError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This account or Tailscale identity is already linked",
            ) from error

    @router.delete(
        "/jobs-ui/api/account/identities/tailscale",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def unlink_tailscale_identity(
        identity: MemberSession,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
    ) -> Response:
        account_store.unlink_external_identity(identity.id, "tailscale")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        "/internal/service-identities/resolve",
        response_model=SessionIdentity,
    )
    def resolve_service_identity(
        resolution: ServiceIdentityResolve,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        _: ServiceIdentityToken,
    ) -> SessionIdentity:
        identity = account_store.resolve_external_identity(
            resolution.provider, resolution.subject
        )
        if identity is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Linked identity not found",
            )
        return identity

    @router.post(
        "/jobs-ui/api/account/password",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def jobs_portal_change_password(
        password: PasswordChange,
        response: Response,
        identity: MemberSession,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
    ) -> None:
        try:
            account_store.change_password(
                identity.id, password.current_password, password.new_password
            )
        except InvalidCurrentPasswordError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Current password is incorrect",
            ) from error
        response.delete_cookie(SESSION_COOKIE, path="/")

    @router.get("/jobs-ui/api/workload-owners")
    def jobs_portal_workload_owners(
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        _: AdminSession,
    ) -> dict[str, dict[str, str]]:
        return account_store.workload_owners()

    @router.get("/dashboard/api/users", response_model=list[UserRead])
    def dashboard_users(
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        _: AdminSession,
    ) -> list[UserRead]:
        return account_store.list()

    @router.post(
        "/dashboard/api/users",
        response_model=UserRead,
        status_code=status.HTTP_201_CREATED,
    )
    def dashboard_create_user(
        user_create: UserCreate,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        _: AdminSession,
    ) -> UserRead:
        try:
            return account_store.create(user_create)
        except UserExistsError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username already exists",
            ) from error

    @router.patch("/dashboard/api/users/{user_id}", response_model=UserRead)
    def dashboard_update_user(
        user_id: UUID,
        update: UserUpdate,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        _: AdminSession,
    ) -> UserRead:
        try:
            return account_store.set_disabled(user_id, update.disabled)
        except UserNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Member account not found",
            ) from error

    @router.put(
        "/dashboard/api/users/{user_id}/password",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def dashboard_reset_user_password(
        user_id: UUID,
        password: PasswordReset,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        _: AdminSession,
    ) -> None:
        try:
            account_store.reset_password(user_id, password.new_password)
        except UserNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Member account not found",
            ) from error

    @router.get("/jobs-ui", response_class=HTMLResponse)
    def jobs_page() -> str:
        return JOBS_HTML

    @router.get("/jobs-ui/new", response_class=HTMLResponse)
    def jobs_submit_page() -> str:
        return JOBS_HTML

    @router.get("/jobs-ui/files", response_class=HTMLResponse)
    def jobs_files_page() -> str:
        return JOBS_HTML

    @router.get("/dashboard/api/system")
    def system_metrics(_: AdminSession) -> dict[str, Any]:
        return collect_system_metrics()

    @router.get("/dashboard/api/services")
    def dashboard_services(
        job_service: JobServiceDependency,
        _: AdminSession,
    ) -> dict[str, Any]:
        return collect_service_health(job_service)

    @router.get("/dashboard/api/workers", response_model=list[WorkerRead])
    def dashboard_workers(
        job_service: JobServiceDependency,
        _: AdminSession,
    ) -> list[WorkerRead]:
        return job_service.list_workers()

    @router.patch("/dashboard/api/workers/{worker_id}", response_model=WorkerRead)
    def dashboard_update_worker(
        worker_id: Annotated[
            str,
            ApiPath(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"),
        ],
        update: WorkerUpdate,
        job_service: JobServiceDependency,
        _: AdminSession,
    ) -> WorkerRead:
        try:
            return job_service.set_worker_enabled(worker_id, update.enabled)
        except WorkerNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Worker with ID {worker_id} not found",
            ) from None

    @router.put(
        "/dashboard/api/workers/{worker_id}/capacity", response_model=WorkerRead
    )
    def dashboard_update_worker_capacity(
        worker_id: Annotated[
            str,
            ApiPath(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"),
        ],
        capacity: WorkerCapacityUpdate,
        job_service: JobServiceDependency,
        _: AdminSession,
    ) -> WorkerRead:
        try:
            return job_service.set_worker_capacity(worker_id, capacity)
        except WorkerNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Worker with ID {worker_id} not found",
            ) from None

    @router.get("/dashboard/api/jobs", response_model=list[JobRead])
    def dashboard_jobs(
        job_service: JobServiceDependency,
        _: AdminSession,
    ) -> list[JobRead]:
        return job_service.list()

    @router.post("/dashboard/api/system/power", status_code=status.HTTP_202_ACCEPTED)
    def dashboard_power(
        power_request: PiPowerRequest,
        request: Request,
        job_service: JobServiceDependency,
        _: AdminSession,
    ) -> dict[str, str]:
        if request.app.state.settings.api_token is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Pi power control requires configured authentication",
            )
        expected = power_request.action.value.upper()
        if power_request.confirmation != expected:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Type {expected} exactly to confirm",
            )

        workers = job_service.list_workers()
        enabled_workers = [worker.id for worker in workers if worker.enabled]
        if enabled_workers:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Disable scheduling on every worker before controlling Pi power: "
                    + ", ".join(enabled_workers)
                ),
            )
        running_jobs = [
            str(job.id) for job in job_service.list() if job.status is JobStatus.RUNNING
        ]
        if running_jobs:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Wait for or cancel running jobs before controlling Pi power: "
                    + ", ".join(running_jobs)
                ),
            )
        try:
            queue_power_request(
                request.app.state.settings.power_request_directory,
                power_request.action,
            )
        except PowerControlUnavailableError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
            ) from error
        except PowerRequestPendingError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error
        return {
            "action": power_request.action.value,
            "state": "ACCEPTED",
            "message": "The NAS will be stopped and the SSD safely unmounted first",
        }

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
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        idempotency_key: Annotated[
            str | None,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=128,
                pattern=r"^[A-Za-z0-9._:-]+$",
            ),
        ] = None,
    ) -> JobRead:
        validate_upload_ownership(account_store, identity, [job_create])
        if isinstance(job_create.parameters, PythonBatchParameters) and isinstance(
            job_create.parameters.dataset, StorageInputReference
        ):
            require_member_storage(request, identity)
            reference = job_create.parameters.dataset
            if not identity.is_admin and not member_storage_path_allowed(
                identity.id, reference.path
            ):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Storage input not found",
                )
            try:
                actual = storage_file_reference(
                    request.app.state.settings.storage_directory, reference.path
                )
            except StoragePolicyError as error:
                raise storage_error(error) from error
            if actual != reference:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Storage input changed after selection",
                )
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
        job_service: JobServiceDependency,
        identity: MemberSession,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        idempotency_key: Annotated[
            str | None,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=128,
                pattern=r"^[A-Za-z0-9._:-]+$",
            ),
        ] = None,
    ) -> JobGroupRead:
        try:
            validate_upload_ownership(
                account_store, identity, [task.job for task in group_create.tasks]
            )
            return job_service.create_group(
                group_create, idempotency_key, str(identity.id)
            )
        except WorkerNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
            ) from error
        except SchedulingCapacityError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
            ) from error
        except IdempotencyConflictError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error

    @router.post("/jobs-ui/api/jobs/{job_id}/cancel", response_model=JobRead)
    def jobs_portal_cancel(
        job_id: UUID,
        request: Request,
        job_service: JobServiceDependency,
        identity: DashboardSession,
    ) -> JobRead:
        job = finish_job(
            lambda: job_service.cancel(job_id, owner_scope(identity)), job_id
        )
        if job.status is JobStatus.FAILED:
            release_uploads(request, job_service, job)
        return job

    @router.post(
        "/jobs-ui/api/uploads",
        response_model=UploadedDatasetReference,
        status_code=status.HTTP_201_CREATED,
    )
    async def jobs_portal_upload(
        request: Request,
        identity: MemberSession,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        file: Annotated[UploadFile, File()],
    ) -> UploadedDatasetReference:
        settings = request.app.state.settings
        upload_id, digest, size = await save_upload(
            file,
            directory=settings.upload_directory,
            suffix=".csv",
            max_bytes=settings.max_upload_bytes,
            label="CSV file",
        )
        account_store.record_upload(upload_id, identity.id, "dataset")
        return UploadedDatasetReference(
            upload_id=upload_id,
            sha256=digest,
            size_bytes=size,
        )

    @router.post(
        "/jobs-ui/api/script-uploads",
        response_model=UploadedScriptReference,
        status_code=status.HTTP_201_CREATED,
    )
    async def jobs_portal_script_upload(
        request: Request,
        identity: MemberSession,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        file: Annotated[UploadFile, File()],
    ) -> UploadedScriptReference:
        settings = request.app.state.settings
        upload_id, digest, size = await save_upload(
            file,
            directory=settings.upload_directory / "scripts",
            suffix=".py",
            max_bytes=settings.max_script_upload_bytes,
            label="Python script",
        )
        account_store.record_upload(upload_id, identity.id, "script")
        return UploadedScriptReference(
            upload_id=upload_id,
            sha256=digest,
            size_bytes=size,
        )

    @router.post(
        "/jobs-ui/api/project-uploads",
        response_model=UploadedProjectReference,
        status_code=status.HTTP_201_CREATED,
    )
    async def jobs_portal_project_upload(
        request: Request,
        identity: MemberSession,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        file: Annotated[UploadFile, File()],
    ) -> UploadedProjectReference:
        project = await save_project_upload(request, file)
        account_store.record_upload(project.upload_id, identity.id, "project")
        return project

    @router.post(
        "/jobs-ui/api/input-uploads",
        response_model=UploadedInputReference,
        status_code=status.HTTP_201_CREATED,
    )
    async def jobs_portal_input_upload(
        request: Request,
        identity: MemberSession,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        file: Annotated[UploadFile, File()],
    ) -> UploadedInputReference:
        upload_id, digest, size = await save_upload(
            file,
            directory=request.app.state.settings.upload_directory / "inputs",
            suffix=".input",
            max_bytes=request.app.state.settings.max_project_upload_bytes,
            label="Input file",
            validate_suffix=False,
        )
        account_store.record_upload(upload_id, identity.id, "input")
        return UploadedInputReference(
            upload_id=upload_id, sha256=digest, size_bytes=size
        )

    def storage_error(error: StoragePolicyError) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        )

    def administrator_storage_path(path: str) -> str:
        """Accept the logical vocabulary on the token API, physical as legacy.

        Job Desk, `#HP` defaults and the CLI all speak `Home/...` and
        `Shared/...`; this is where an administrator's version of that is
        resolved. Physical paths still pass through untouched because the
        transitional Pi share has `projects/` and `inputs/` directories that
        have no logical equivalent yet.
        """
        if not is_logical_storage_path(path):
            return path
        return resolve_logical_storage_path(path, user_id=None)

    def member_storage_available(request: Request, identity: SessionIdentity) -> bool:
        settings = request.app.state.settings
        return bool(
            identity.is_admin
            or settings.member_storage_enabled
            or identity.id in settings.member_storage_user_ids
        )

    def require_member_storage(request: Request, identity: SessionIdentity) -> None:
        if not member_storage_available(request, identity):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Personal NAS storage is not provisioned yet",
            )

    def member_workspace_available(request: Request, identity: SessionIdentity) -> bool:
        settings = request.app.state.settings
        return bool(
            not identity.is_admin
            and settings.workspace_directory is not None
            and (
                settings.member_workspace_enabled
                or identity.id in settings.member_workspace_user_ids
            )
        )

    def require_member_workspace(request: Request, identity: SessionIdentity) -> Path:
        if not member_workspace_available(request, identity):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Job Desk workspace editing is not provisioned yet",
            )
        return cast(Path, request.app.state.settings.workspace_directory)

    def require_administrator_workspace(request: Request) -> Path:
        root = request.app.state.settings.workspace_directory
        if root is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Administrator Workspace management is not provisioned",
            )
        return cast(Path, root)

    def administrator_workspace_path(path: str) -> tuple[UUID, str]:
        """Map an operator Files path to one member's constrained Workspace.

        The writer mount can alter only ``users/*/Workspace``. Keeping the
        visible ``Storage/users/<uuid>/Workspace`` prefix in the request makes
        the administrator's wider scope explicit without creating a second
        path vocabulary for the same NAS tree.
        """
        normalized = path.strip().replace("\\", "/").strip("/")
        parts = PurePosixPath(normalized).parts
        if (
            len(parts) < 4
            or tuple(part.lower() for part in parts[:2]) != ("storage", "users")
            or parts[3].lower() != "workspace"
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise StoragePolicyError(
                "administrator changes must stay inside Storage/users/<user-id>/Workspace"
            )
        try:
            owner_id = UUID(parts[2])
        except ValueError as error:
            raise StoragePolicyError(
                "administrator Workspace paths must name a stable user ID"
            ) from error
        logical = PurePosixPath("Home", "Workspace", *parts[4:]).as_posix()
        return owner_id, logical

    def administrator_workspace_available(path: str) -> bool:
        try:
            administrator_workspace_path(path)
        except StoragePolicyError:
            return False
        return True

    def scoped_storage_path(identity: SessionIdentity, logical_path: str) -> str:
        return (
            logical_path
            if identity.is_admin
            else member_storage_path(identity.id, logical_path)
        )

    def artifact_filesystem_root(request: Request) -> Path:
        """Return the artifact provider only when its configured disk is safe."""
        settings = request.app.state.settings
        root: Path = settings.artifact_directory
        if settings.artifact_requires_mount:
            probe = root if root.exists() else root.parent
            try:
                on_root_filesystem = probe.stat().st_dev == Path("/").stat().st_dev
            except OSError:
                on_root_filesystem = True
            if on_root_filesystem:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Artifact storage is not mounted",
                )
        return root

    def member_artifact_runs(
        request: Request, job_service: JobService, identity: SessionIdentity
    ) -> dict[str, Path]:
        """Map every artifact directory this member owns to its physical path.

        Ownership is derived from the job records, never from the directory
        layout. The live store is still flat — every run sits directly under the
        artifact root whoever owns it — so a path on its own cannot say who may
        read it. Reading the set from owned jobs is correct under that flat
        layout and under the prepared `<owner-id>/` one, and it cannot silently
        widen if `HOME_PLATFORM_ARTIFACT_OWNER_SCOPED` is flipped.
        """
        settings = request.app.state.settings
        root = artifact_filesystem_root(request)
        base = root / str(identity.id) if settings.artifact_owner_scoped else root
        if not base.is_dir():
            return {}
        group_names: dict[UUID, str | None] = {}
        runs: dict[str, Path] = {}
        for job in job_service.list(str(identity.id)):
            group_name: str | None = None
            if job.group_id is not None and job.task_id is not None:
                if job.group_id not in group_names:
                    try:
                        group_names[job.group_id] = job_service.get_group(
                            job.group_id
                        ).name
                    except JobGroupNotFoundError:
                        group_names[job.group_id] = None
                group_name = group_names[job.group_id]
            current = base / job_artifact_relative_path(job, group_name=group_name)
            legacy = base / legacy_artifact_relative_path(job.id)
            directory = current if current.exists() else legacy
            if not directory.is_dir():
                continue
            # A run is the top-level entry under the base. An array child lives
            # one level deeper, inside its group's directory, so both map to the
            # same run.
            run = directory.relative_to(base).parts[0]
            runs[run] = base / run
        return runs

    def logical_file_location(
        request: Request,
        identity: SessionIdentity,
        logical_path: str,
        job_service: JobService,
    ) -> tuple[Path, str, str]:
        """Resolve one Files-page path to (provider root, relative path, area)."""
        normalized = logical_path.strip().replace("\\", "/").strip("/")
        if not normalized:
            raise StoragePolicyError("choose a file area")
        logical = PurePosixPath(normalized)
        if any(part in {"", ".", ".."} for part in logical.parts):
            raise StoragePolicyError("file path must stay inside its area")
        area, *remainder = logical.parts
        area_key = area.lower()
        relative = PurePosixPath(*remainder).as_posix() if remainder else ""

        if identity.is_admin:
            if area_key == "storage":
                return request.app.state.settings.storage_directory, relative, "Storage"
            if area_key == "artifacts":
                return artifact_filesystem_root(request), relative, "Artifacts"
            raise StoragePolicyError("choose Storage or Artifacts")

        if area_key in {"home", "shared"}:
            require_member_storage(request, identity)
            physical = member_storage_path(identity.id, normalized)
            return request.app.state.settings.storage_directory, physical, area.title()
        if area_key == "artifacts":
            runs = member_artifact_runs(request, job_service, identity)
            if not remainder:
                # The caller lists owned runs directly; there is no single
                # directory that holds exactly this member's results.
                raise StoragePolicyError("choose one of your artifacts")
            run = runs.get(remainder[0])
            if run is None:
                raise StoragePolicyError("artifact not found")
            return run.parent, relative, "Artifacts"
        raise StoragePolicyError("choose Home, Shared or Artifacts")

    def virtual_file_entries(
        provider_root: Path,
        relative_path: str,
        logical_path: str,
    ) -> list[dict[str, Any]]:
        if not provider_root.exists() and not relative_path:
            return []
        entries = browse_storage(provider_root, relative_path)
        return [
            {
                "name": entry.name,
                "path": (PurePosixPath(logical_path) / entry.name).as_posix(),
                "kind": entry.kind,
                "size_bytes": entry.size_bytes,
            }
            for entry in entries
        ]

    def remove_permanently(target: Path) -> int:
        size = directory_size(target) if target.is_dir() else target.stat().st_size
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return size

    def default_storage_path(identity: SessionIdentity, logical_path: str) -> str:
        """Resolve a `#HP --input NAME=PATH` default for the submitting session.

        An administrator gets no implicit `Home`: a project header is written
        once and reused, so it cannot name which account's private tree a run
        should read. `Shared` is unambiguous and resolves for either caller.
        """
        if not identity.is_admin:
            return member_storage_path(identity.id, logical_path)
        if not is_logical_storage_path(logical_path):
            raise StoragePolicyError(
                "default #HP input paths must start with Home or Shared"
            )
        try:
            return resolve_logical_storage_path(logical_path, user_id=None)
        except StoragePolicyError as error:
            raise StoragePolicyError(
                "Home defaults require a member session; administrators must "
                f"bind that input explicitly ({error})"
            ) from error

    def project_archive(request: Request, project: UploadedProjectReference) -> Path:
        upload_directory = cast(Path, request.app.state.settings.upload_directory)
        return upload_directory / "projects" / f"{project.upload_id}.zip"

    def parsed_project(
        request: Request,
        project: UploadedProjectReference,
        entrypoint: str,
    ) -> ParsedBatchScript:
        try:
            return parse_batch_project(project_archive(request, project), entrypoint)
        except BatchScriptError as error:
            raise storage_error(StoragePolicyError(str(error))) from error

    def resolve_default_storage_inputs(
        submission: BatchSubmissionCreate,
        request: Request,
        identity: SessionIdentity,
    ) -> BatchSubmissionCreate:
        parsed = parsed_project(request, submission.project, submission.entrypoint)
        inputs = dict(submission.inputs)
        for declaration in parsed.inputs:
            if declaration.name in inputs or declaration.default_path is None:
                continue
            require_member_storage(request, identity)
            try:
                inputs[declaration.name] = storage_file_reference(
                    request.app.state.settings.storage_directory,
                    default_storage_path(identity, declaration.default_path),
                )
            except StoragePolicyError as error:
                raise storage_error(error) from error
        return submission.model_copy(update={"inputs": inputs})

    @router.get("/jobs-ui/api/storage")
    def jobs_portal_storage(
        request: Request,
        identity: MemberSession,
        path: str = "",
    ) -> dict[str, Any]:
        require_member_storage(request, identity)
        try:
            entries = (
                browse_storage(request.app.state.settings.storage_directory, path)
                if identity.is_admin
                else member_storage_entries(
                    request.app.state.settings.storage_directory, identity.id, path
                )
            )
        except StoragePolicyError as error:
            raise storage_error(error) from error
        return {
            "storage_id": STORAGE_ID,
            "path": path,
            "workspace_writable": (
                member_workspace_available(request, identity)
                and (
                    path.lower() == "home/workspace"
                    or path.lower().startswith("home/workspace/")
                )
            ),
            "entries": [entry.__dict__ for entry in entries],
        }

    @router.get(
        "/jobs-ui/api/storage/download",
        response_class=FileResponse,
    )
    def jobs_portal_storage_download(
        path: str,
        request: Request,
        identity: MemberSession,
    ) -> FileResponse:
        """Stream an authorized Home/Shared file through the Job Desk session.

        Members submit only logical paths. Their immutable session identity is
        mapped to the physical UUID-keyed tree here, so neither the browser nor
        a caller-controlled path can select another member's directory.
        """
        require_member_storage(request, identity)
        try:
            target = resolve_storage_path(
                request.app.state.settings.storage_directory,
                scoped_storage_path(identity, path),
            )
        except StoragePolicyError as error:
            raise storage_error(error) from error
        if not target.is_file():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="choose a regular file to download",
            )
        return FileResponse(
            target,
            media_type="application/octet-stream",
            filename=target.name,
        )

    @router.get("/jobs-ui/api/files")
    def jobs_portal_files(
        request: Request,
        identity: DashboardSession,
        job_service: JobServiceDependency,
        path: str = "",
    ) -> dict[str, Any]:
        normalized = path.strip().replace("\\", "/").strip("/")
        if not normalized:
            areas = (
                ["Storage", "Artifacts"]
                if identity.is_admin
                else (
                    ["Home", "Shared", "Artifacts"]
                    if member_storage_available(request, identity)
                    else ["Artifacts"]
                )
            )
            return {
                "path": "",
                "can_manage": False,
                "can_clear_artifacts": False,
                "can_delete_artifacts": False,
                "entries": [
                    {
                        "name": area,
                        "path": area,
                        "kind": "directory",
                        "size_bytes": None,
                    }
                    for area in areas
                ],
            }
        lower = normalized.lower()
        if lower == "artifacts" and not identity.is_admin:
            # Built from owned jobs rather than a directory walk: in the live
            # flat layout the artifact root holds every member's runs together.
            runs = member_artifact_runs(request, job_service, identity)
            return {
                "path": normalized,
                "can_manage": False,
                "can_clear_artifacts": bool(runs),
                "can_delete_artifacts": True,
                "entries": [
                    {
                        "name": name,
                        "path": f"Artifacts/{name}",
                        "kind": "directory",
                        "size_bytes": None,
                    }
                    for name in sorted(runs)
                ],
            }
        try:
            provider_root, relative, area = logical_file_location(
                request, identity, normalized, job_service
            )
            entries = virtual_file_entries(provider_root, relative, normalized)
        except StoragePolicyError as error:
            raise storage_error(error) from error
        return {
            "path": normalized,
            "can_manage": bool(
                (
                    identity.is_admin
                    and request.app.state.settings.workspace_directory is not None
                    and administrator_workspace_available(normalized)
                )
                or (
                    not identity.is_admin
                    and (
                        lower == "home/workspace" or lower.startswith("home/workspace/")
                    )
                )
            ),
            "can_clear_artifacts": bool(
                not identity.is_admin and area == "Artifacts" and not relative
            ),
            "can_delete_artifacts": bool(
                area == "Artifacts" and (not identity.is_admin or bool(relative))
            ),
            "entries": entries,
        }

    @router.get(
        "/jobs-ui/api/files/download",
        response_class=FileResponse,
    )
    def jobs_portal_file_download(
        path: str,
        request: Request,
        identity: DashboardSession,
        job_service: JobServiceDependency,
    ) -> FileResponse:
        try:
            provider_root, relative, _ = logical_file_location(
                request, identity, path, job_service
            )
            target = resolve_storage_path(provider_root, relative)
        except StoragePolicyError as error:
            raise storage_error(error) from error
        if not target.is_file():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="choose a regular file to download",
            )
        return FileResponse(
            target,
            media_type="application/octet-stream",
            filename=target.name,
        )

    @router.post("/jobs-ui/api/files/move")
    def jobs_portal_file_move(
        transfer: FileTransferRequest,
        request: Request,
        identity: DashboardSession,
    ) -> dict[str, Any]:
        if identity.is_admin:
            workspace_root = require_administrator_workspace(request)
            try:
                source_owner, source = administrator_workspace_path(transfer.source)
                destination_owner, destination = administrator_workspace_path(
                    transfer.destination
                )
            except StoragePolicyError as error:
                raise storage_error(error) from error
            if source_owner != destination_owner:
                raise storage_error(
                    StoragePolicyError("Workspace moves cannot cross member accounts")
                )
            owner_id = source_owner
        else:
            workspace_root = require_member_workspace(request, identity)
            owner_id = identity.id
            source = transfer.source
            destination = transfer.destination
        try:
            move_workspace_entry(
                workspace_root,
                owner_id,
                source,
                destination,
            )
        except StoragePolicyError as error:
            raise storage_error(error) from error
        logging.info(
            "workspace entry moved owner=%s source=%s destination=%s",
            owner_id,
            transfer.source,
            transfer.destination,
        )
        return {"source": transfer.source, "path": transfer.destination}

    @router.post("/jobs-ui/api/files/copy", status_code=status.HTTP_201_CREATED)
    def jobs_portal_file_copy(
        transfer: FileTransferRequest,
        request: Request,
        identity: DashboardSession,
    ) -> dict[str, Any]:
        if identity.is_admin:
            workspace_root = require_administrator_workspace(request)
            try:
                source_owner, source = administrator_workspace_path(transfer.source)
                destination_owner, destination = administrator_workspace_path(
                    transfer.destination
                )
            except StoragePolicyError as error:
                raise storage_error(error) from error
            if source_owner != destination_owner:
                raise storage_error(
                    StoragePolicyError("Workspace copies cannot cross member accounts")
                )
            owner_id = source_owner
        else:
            workspace_root = require_member_workspace(request, identity)
            owner_id = identity.id
            source = transfer.source
            destination = transfer.destination
        try:
            copied = copy_workspace_entry(
                workspace_root,
                owner_id,
                source,
                destination,
                max_bytes=request.app.state.settings.max_workspace_upload_bytes,
            )
        except StoragePolicyError as error:
            raise storage_error(error) from error
        logging.info(
            "workspace entry copied owner=%s source=%s destination=%s bytes=%d",
            owner_id,
            transfer.source,
            transfer.destination,
            copied,
        )
        return {
            "source": transfer.source,
            "path": transfer.destination,
            "copied_bytes": copied,
        }

    @router.delete("/jobs-ui/api/files")
    def jobs_portal_file_delete(
        selection: StoragePathRequest,
        request: Request,
        identity: DashboardSession,
        job_service: JobServiceDependency,
    ) -> dict[str, Any]:
        normalized = selection.path.strip().replace("\\", "/").strip("/")
        lower = normalized.lower()
        try:
            if identity.is_admin and administrator_workspace_available(normalized):
                workspace_root = require_administrator_workspace(request)
                owner_id, logical_path = administrator_workspace_path(normalized)
                freed = delete_workspace_entry(workspace_root, owner_id, logical_path)
            elif identity.is_admin and lower.startswith("artifacts/"):
                if any(job.status is JobStatus.RUNNING for job in job_service.list()):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Wait for running jobs before deleting artifacts",
                    )
                provider_root, relative, _ = logical_file_location(
                    request, identity, normalized, job_service
                )
                target = resolve_storage_path(provider_root, relative)
                freed = remove_permanently(target)
            elif identity.is_admin:
                raise StoragePolicyError(
                    "administrators may delete only member Workspace entries or artifact entries"
                )
            elif lower.startswith("home/workspace/"):
                workspace_root = require_member_workspace(request, identity)
                freed = delete_workspace_entry(workspace_root, identity.id, normalized)
            elif lower == "artifacts" or lower.startswith("artifacts/"):
                if any(
                    job.status is JobStatus.RUNNING
                    for job in job_service.list(owner_scope(identity))
                ):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Wait for running jobs before deleting artifacts",
                    )
                runs = member_artifact_runs(request, job_service, identity)
                if lower == "artifacts":
                    # Only this member's own runs, even though the flat store
                    # holds everyone's side by side.
                    freed = 0
                    for run in runs.values():
                        if run.is_symlink():
                            raise StoragePolicyError(
                                "artifact trees cannot contain symbolic links"
                            )
                        if run.is_dir():
                            freed += remove_permanently(run)
                else:
                    parts = PurePosixPath(normalized).parts[1:]
                    selected = runs.get(parts[0]) if parts else None
                    if selected is None:
                        raise StoragePolicyError("artifact not found")
                    member_relative = PurePosixPath(*parts)
                    target = resolve_storage_path(
                        selected.parent, member_relative.as_posix()
                    )
                    freed = remove_permanently(target)
            else:
                raise StoragePolicyError(
                    "only Home/Workspace and your Artifacts can be deleted"
                )
        except StoragePolicyError as error:
            raise storage_error(error) from error
        except OSError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="file could not be deleted",
            ) from error
        logging.info(
            "file entry permanently deleted owner=%s path=%s bytes=%d",
            identity.id,
            normalized,
            freed,
        )
        return {"deleted": normalized, "freed_bytes": freed}

    @router.post(
        "/jobs-ui/api/workspace/directories",
        status_code=status.HTTP_201_CREATED,
    )
    def jobs_portal_workspace_directory(
        creation: WorkspaceDirectoryCreate,
        request: Request,
        identity: DashboardSession,
    ) -> dict[str, str]:
        if identity.is_admin:
            workspace_root = require_administrator_workspace(request)
            try:
                owner_id, logical_path = administrator_workspace_path(creation.path)
            except StoragePolicyError as error:
                raise storage_error(error) from error
        else:
            workspace_root = require_member_workspace(request, identity)
            owner_id = identity.id
            logical_path = creation.path
        try:
            create_workspace_directory(workspace_root, owner_id, logical_path)
        except StoragePolicyError as error:
            raise storage_error(error) from error
        return {"path": creation.path}

    @router.post(
        "/jobs-ui/api/workspace/uploads",
        status_code=status.HTTP_201_CREATED,
    )
    async def jobs_portal_workspace_upload(
        request: Request,
        identity: DashboardSession,
        directory: Annotated[str, Form()],
        file: Annotated[UploadFile, File()],
    ) -> dict[str, Any]:
        if identity.is_admin:
            workspace_root = require_administrator_workspace(request)
            try:
                owner_id, logical_directory = administrator_workspace_path(directory)
            except StoragePolicyError as error:
                await file.close()
                raise storage_error(error) from error
        else:
            workspace_root = require_member_workspace(request, identity)
            owner_id = identity.id
            logical_directory = directory
        filename = file.filename or ""
        try:
            target = workspace_upload_target(
                workspace_root, owner_id, logical_directory, filename
            )
        except StoragePolicyError as error:
            await file.close()
            raise storage_error(error) from error

        temporary = target.parent / f".{uuid4()}.part"
        size = 0
        reserved = False
        try:
            target.touch(exist_ok=False)
            reserved = True
            with temporary.open("xb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > request.app.state.settings.max_workspace_upload_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                            detail="Workspace upload exceeds the configured limit",
                        )
                    output.write(chunk)
            if size == 0:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Workspace file cannot be empty",
                )
            os.replace(temporary, target)
            reserved = False
        except FileExistsError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Workspace file already exists",
            ) from error
        except OSError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Workspace upload could not be stored",
            ) from error
        finally:
            await file.close()
            temporary.unlink(missing_ok=True)
            if reserved:
                target.unlink(missing_ok=True)
        return {
            "name": filename,
            "path": f"{directory.rstrip('/')}/{filename}",
            "size_bytes": size,
        }

    @router.post(
        "/jobs-ui/api/storage/references",
        response_model=StorageInputReference,
    )
    def jobs_portal_storage_reference(
        selection: StoragePathRequest,
        request: Request,
        identity: MemberSession,
    ) -> StorageInputReference:
        require_member_storage(request, identity)
        try:
            return storage_file_reference(
                request.app.state.settings.storage_directory,
                scoped_storage_path(identity, selection.path),
            )
        except StoragePolicyError as error:
            raise storage_error(error) from error

    @router.post(
        "/jobs-ui/api/storage/project-uploads",
        response_model=UploadedProjectReference,
        status_code=status.HTTP_201_CREATED,
    )
    def jobs_portal_storage_project(
        selection: StoragePathRequest,
        request: Request,
        identity: MemberSession,
        account_store: Annotated[AccountStore, Depends(get_account_store)],
    ) -> UploadedProjectReference:
        settings = request.app.state.settings
        require_member_storage(request, identity)
        try:
            project = package_storage_project(
                settings.storage_directory,
                scoped_storage_path(identity, selection.path),
                settings.upload_directory / "projects",
                settings.max_project_upload_bytes,
            )
        except StoragePolicyError as error:
            raise storage_error(error) from error
        account_store.record_upload(project.upload_id, identity.id, "project")
        return project

    @router.post("/jobs-ui/api/storage/project-preview")
    def jobs_portal_storage_project_preview(
        selection: StorageProjectPreviewRequest,
        request: Request,
        identity: MemberSession,
    ) -> dict[str, Any]:
        require_member_storage(request, identity)
        try:
            parsed, files = inspect_storage_project(
                request.app.state.settings.storage_directory,
                scoped_storage_path(identity, selection.path),
                selection.entrypoint,
            )
        except StoragePolicyError as error:
            raise storage_error(error) from error
        return {
            "project_path": selection.path,
            "entrypoint": selection.entrypoint,
            "name": parsed.name,
            "runtime": parsed.runtime,
            "cpu_limit": parsed.cpu_limit,
            "memory_mb": parsed.memory_mb,
            "timeout_seconds": parsed.timeout_seconds,
            "array_start": parsed.array_start,
            "array_end": parsed.array_end,
            "worker_id": parsed.worker_id,
            "files": files,
            "inputs": [
                {"name": item.name, "default_path": item.default_path}
                for item in parsed.inputs
            ],
        }

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
        account_store: Annotated[AccountStore, Depends(get_account_store)],
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=128),
        ] = None,
    ) -> JobGroupRead:
        validate_upload_ids(account_store, identity, {submission.project.upload_id})
        submission = resolve_default_storage_inputs(submission, request, identity)
        upload_ids = {submission.project.upload_id}
        for source in submission.inputs.values():
            if not identity.is_admin and isinstance(source, StorageInputReference):
                require_member_storage(request, identity)
                if not member_storage_path_allowed(identity.id, source.path):
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Storage input not found",
                    )
                try:
                    actual = storage_file_reference(
                        request.app.state.settings.storage_directory, source.path
                    )
                except StoragePolicyError as error:
                    raise storage_error(error) from error
                if actual != source:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Storage input changed after selection",
                    )
            if isinstance(source, UploadedInputReference):
                upload_ids.add(source.upload_id)
        validate_upload_ids(account_store, identity, upload_ids)
        return create_batch_submission(
            request, job_service, submission, idempotency_key, str(identity.id)
        )

    @router.post(
        "/uploads/datasets",
        response_model=UploadedDatasetReference,
        status_code=status.HTTP_201_CREATED,
    )
    async def api_dataset_upload(
        request: Request,
        _: ApiToken,
        file: Annotated[UploadFile, File()],
    ) -> UploadedDatasetReference:
        settings = request.app.state.settings
        upload_id, digest, size = await save_upload(
            file,
            directory=settings.upload_directory,
            suffix=".csv",
            max_bytes=settings.max_upload_bytes,
            label="CSV file",
        )
        return UploadedDatasetReference(
            upload_id=upload_id, sha256=digest, size_bytes=size
        )

    @router.post(
        "/uploads/scripts",
        response_model=UploadedScriptReference,
        status_code=status.HTTP_201_CREATED,
    )
    async def api_script_upload(
        request: Request,
        _: ApiToken,
        file: Annotated[UploadFile, File()],
    ) -> UploadedScriptReference:
        settings = request.app.state.settings
        upload_id, digest, size = await save_upload(
            file,
            directory=settings.upload_directory / "scripts",
            suffix=".py",
            max_bytes=settings.max_script_upload_bytes,
            label="Python script",
        )
        return UploadedScriptReference(
            upload_id=upload_id, sha256=digest, size_bytes=size
        )

    @router.post(
        "/uploads/projects",
        response_model=UploadedProjectReference,
        status_code=status.HTTP_201_CREATED,
    )
    async def api_project_upload(
        request: Request,
        _: ApiToken,
        file: Annotated[UploadFile, File()],
    ) -> UploadedProjectReference:
        return await save_project_upload(request, file)

    @router.post(
        "/uploads/inputs",
        response_model=UploadedInputReference,
        status_code=status.HTTP_201_CREATED,
    )
    async def api_input_upload(
        request: Request,
        _: ApiToken,
        file: Annotated[UploadFile, File()],
    ) -> UploadedInputReference:
        upload_id, digest, size = await save_upload(
            file,
            directory=request.app.state.settings.upload_directory / "inputs",
            suffix=".input",
            max_bytes=request.app.state.settings.max_project_upload_bytes,
            label="Input file",
            validate_suffix=False,
        )
        return UploadedInputReference(
            upload_id=upload_id, sha256=digest, size_bytes=size
        )

    @router.get("/storage")
    def api_storage(
        request: Request,
        _: ApiToken,
        path: str = "",
    ) -> dict[str, Any]:
        try:
            entries = browse_storage(
                request.app.state.settings.storage_directory,
                administrator_storage_path(path),
            )
        except StoragePolicyError as error:
            raise storage_error(error) from error
        return {
            "storage_id": STORAGE_ID,
            "path": path,
            "entries": [entry.__dict__ for entry in entries],
        }

    @router.post("/storage/references", response_model=StorageInputReference)
    def api_storage_reference(
        selection: StoragePathRequest,
        request: Request,
        _: ApiToken,
    ) -> StorageInputReference:
        try:
            return storage_file_reference(
                request.app.state.settings.storage_directory,
                administrator_storage_path(selection.path),
            )
        except StoragePolicyError as error:
            raise storage_error(error) from error

    @router.post(
        "/storage/project-uploads",
        response_model=UploadedProjectReference,
        status_code=status.HTTP_201_CREATED,
    )
    def api_storage_project(
        selection: StoragePathRequest,
        request: Request,
        _: ApiToken,
    ) -> UploadedProjectReference:
        settings = request.app.state.settings
        try:
            return package_storage_project(
                settings.storage_directory,
                administrator_storage_path(selection.path),
                settings.upload_directory / "projects",
                settings.max_project_upload_bytes,
            )
        except StoragePolicyError as error:
            raise storage_error(error) from error

    @router.get("/storage/files/{file_path:path}", response_class=FileResponse)
    def api_storage_file(
        file_path: str,
        request: Request,
        _: ApiToken,
    ) -> FileResponse:
        try:
            target = resolve_storage_path(
                request.app.state.settings.storage_directory,
                administrator_storage_path(file_path),
            )
        except StoragePolicyError as error:
            raise storage_error(error) from error
        if not target.is_file():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="storage path is not a regular file",
            )
        return FileResponse(target, media_type="application/octet-stream")

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
        return create_batch_submission(
            request, job_service, submission, idempotency_key
        )

    @router.get("/datasets/uploads/{upload_id}", response_class=FileResponse)
    def download_uploaded_dataset(
        upload_id: UUID,
        request: Request,
        _: ApiToken,
    ) -> FileResponse:
        target = request.app.state.settings.upload_directory / f"{upload_id}.csv"
        if not target.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Uploaded dataset not found",
            )
        return FileResponse(
            target,
            media_type="text/csv",
            filename=f"{upload_id}.csv",
        )

    @router.get("/scripts/uploads/{upload_id}", response_class=FileResponse)
    def download_uploaded_script(
        upload_id: UUID,
        request: Request,
        _: ApiToken,
    ) -> FileResponse:
        target = (
            request.app.state.settings.upload_directory / "scripts" / f"{upload_id}.py"
        )
        if not target.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Uploaded script not found",
            )
        return FileResponse(
            target,
            media_type="text/x-python",
            filename=f"{upload_id}.py",
        )

    @router.get("/projects/uploads/{upload_id}", response_class=FileResponse)
    def download_uploaded_project(
        upload_id: UUID,
        request: Request,
        _: ApiToken,
    ) -> FileResponse:
        target = (
            request.app.state.settings.upload_directory
            / "projects"
            / f"{upload_id}.zip"
        )
        if not target.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Uploaded project not found",
            )
        return FileResponse(
            target,
            media_type="application/zip",
            filename=f"{upload_id}.zip",
        )

    @router.get("/inputs/uploads/{upload_id}", response_class=FileResponse)
    def download_uploaded_input(
        upload_id: UUID,
        request: Request,
        _: ApiToken,
    ) -> FileResponse:
        target = (
            request.app.state.settings.upload_directory
            / "inputs"
            / f"{upload_id}.input"
        )
        if not target.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Uploaded input not found",
            )
        return FileResponse(target, media_type="application/octet-stream")

    def artifact_directory_for(
        request: Request, job_service: JobService, job_id: UUID
    ) -> Path:
        """Resolve a job's artifact directory, refusing to write to the wrong disk.

        On the Pi this lives on mounted storage. If that provider is absent the
        mount point is an ordinary directory on the small system card, so a
        write would silently fill the boot disk instead of failing. Comparing
        device IDs against `/` catches that regardless of how the path is
        arranged, which a path-shape check would not.

        Results published before 0.33.0 live under the job's bare UUID. Those
        directories are still served where they exist, so changing the layout
        does not strand completed work. New jobs get the readable name-derived
        directory, and because the first upload creates it, every later upload
        for that job finds it and stays alongside the first.
        """
        settings = request.app.state.settings
        root = artifact_filesystem_root(request)
        if not settings.artifact_owner_scoped:
            base = root
        else:
            base = root / job_service.owner_user_id(job_id)
            if not base.is_dir():
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Artifact storage is not provisioned for this job owner",
                )
        legacy = base / legacy_artifact_relative_path(job_id)
        job = job_service.get(job_id)
        if job is None:
            # A worker cannot publish to a job that does not exist, and a reader
            # asking about one should not be told a directory name for it.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Job not found"
            )
        group_name = (
            job_service.get_group(job.group_id).name
            if job.group_id is not None and job.task_id is not None
            else None
        )
        current = base / job_artifact_relative_path(job, group_name=group_name)
        if not current.exists() and legacy.is_dir():
            return legacy
        return current

    def directory_size(directory: Path) -> int:
        return sum(
            item.stat().st_size for item in directory.rglob("*") if item.is_file()
        )

    def evict_artifacts_over_cap(request: Request) -> list[str]:
        """Delete whole run directories until the store is back under its cap.

        Nothing expires because it is old. This is only a backstop against a
        runaway filling the disk, so it evicts the least recently touched runs
        first and logs every removal loudly — losing results silently would be
        worse than running out of space.

        The unit is one submission, not one job. An array's children live inside
        their group's directory, so a four-child array is evicted whole rather
        than leaving three orphaned indices behind.
        """
        settings = request.app.state.settings
        root: Path = settings.artifact_directory
        if not root.is_dir():
            return []
        runs = (
            [item for item in root.iterdir() if item.is_dir()]
            if not settings.artifact_owner_scoped
            else [
                run
                for owner in root.iterdir()
                if owner.is_dir()
                for run in owner.iterdir()
                if run.is_dir()
            ]
        )
        runs.sort(key=lambda item: item.stat().st_mtime)
        total = sum(directory_size(run) for run in runs)
        evicted: list[str] = []
        for run in runs:
            if total <= settings.max_artifact_store_bytes:
                break
            size = directory_size(run)
            shutil.rmtree(run, ignore_errors=True)
            total -= size
            evicted.append(run.name)
            logging.warning(
                "artifact store over %s bytes; evicted run=%s freeing %s bytes",
                settings.max_artifact_store_bytes,
                run.name,
                size,
            )
        return evicted

    def safe_artifact_name(filename: str) -> str:
        """Reject anything that is not a plain, self-contained file name.

        Artifact names come from a worker executing user-supplied code, so they
        are untrusted input used to build a path. Allow-list rather than strip:
        no separators, no traversal, no leading dot, no control characters.
        """
        name = (filename or "").strip()
        if (
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) is None
            or ".." in name
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Artifact file name is not acceptable",
            )
        return name

    def artifact_manifest_entry(path: Path) -> dict[str, Any]:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return {
            "filename": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": digest.hexdigest(),
        }

    @router.post(
        "/jobs/{job_id}/artifacts",
        status_code=status.HTTP_201_CREATED,
    )
    async def upload_artifact(
        job_id: UUID,
        request: Request,
        job_service: JobServiceDependency,
        _: ApiToken,
        worker_id: Annotated[str, Form()],
        lease_token: Annotated[UUID, Form()],
        file: Annotated[UploadFile, File()],
    ) -> dict[str, Any]:
        # Publishing results mutates a job's output, so it needs the same
        # authority as completing it: the caller must hold the current lease.
        try:
            job_service.authorize_lease(job_id, worker_id, lease_token)
        except JobNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
            ) from error
        except JobTransitionError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error

        settings = request.app.state.settings
        name = safe_artifact_name(file.filename or "")
        directory = artifact_directory_for(request, job_service, job_id)
        directory.mkdir(parents=True, exist_ok=True)

        used = sum(
            item.stat().st_size for item in directory.glob("*") if item.is_file()
        )
        target = directory / name
        temporary = directory / f".{uuid4().hex}.part"
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("xb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_artifact_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                            detail=(
                                "Artifact exceeds the "
                                f"{settings.max_artifact_bytes} byte limit"
                            ),
                        )
                    if used + size > settings.max_job_artifact_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                            detail=(
                                "Job artifacts exceed the "
                                f"{settings.max_job_artifact_bytes} byte limit"
                            ),
                        )
                    digest.update(chunk)
                    output.write(chunk)
            os.replace(temporary, target)
        finally:
            await file.close()
            temporary.unlink(missing_ok=True)

        evicted = evict_artifacts_over_cap(request)

        return {
            "job_id": str(job_id),
            "filename": name,
            "size_bytes": size,
            "sha256": digest.hexdigest(),
            "evicted_runs": evicted,
        }

    @router.get("/jobs/{job_id}/artifacts")
    def list_artifacts(
        job_id: UUID,
        request: Request,
        _: ApiToken,
        job_service: JobServiceDependency,
    ) -> list[dict[str, Any]]:
        directory = artifact_directory_for(request, job_service, job_id)
        if not directory.is_dir():
            return []
        return sorted(
            (
                artifact_manifest_entry(item)
                for item in directory.iterdir()
                if item.is_file() and not item.name.startswith(".")
            ),
            key=lambda item: cast(str, item["filename"]),
        )

    @router.get("/jobs/{job_id}/artifacts/{filename}", response_class=FileResponse)
    def download_artifact(
        job_id: UUID,
        filename: str,
        request: Request,
        _: ApiToken,
        job_service: JobServiceDependency,
    ) -> FileResponse:
        name = safe_artifact_name(filename)
        target = artifact_directory_for(request, job_service, job_id) / name
        if not target.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found"
            )
        return FileResponse(
            target, media_type="application/octet-stream", filename=name
        )

    @router.get("/jobs-ui/api/jobs/{job_id}/artifacts")
    def jobs_portal_list_artifacts(
        job_id: UUID,
        request: Request,
        identity: DashboardSession,
        job_service: JobServiceDependency,
    ) -> list[dict[str, Any]]:
        if job_service.get(job_id, owner_scope(identity)) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Job not found"
            )
        directory = artifact_directory_for(request, job_service, job_id)
        if not directory.is_dir():
            return []
        return sorted(
            (
                artifact_manifest_entry(item)
                for item in directory.iterdir()
                if item.is_file() and not item.name.startswith(".")
            ),
            key=lambda item: cast(str, item["filename"]),
        )

    @router.get(
        "/jobs-ui/api/jobs/{job_id}/artifacts/{filename}",
        response_class=FileResponse,
    )
    def jobs_portal_download_artifact(
        job_id: UUID,
        filename: str,
        request: Request,
        identity: DashboardSession,
        job_service: JobServiceDependency,
    ) -> FileResponse:
        if job_service.get(job_id, owner_scope(identity)) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Job not found"
            )
        name = safe_artifact_name(filename)
        target = artifact_directory_for(request, job_service, job_id) / name
        if not target.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found"
            )
        return FileResponse(
            target, media_type="application/octet-stream", filename=name
        )

    @router.delete("/jobs/{job_id}/artifacts")
    def delete_artifacts(
        job_id: UUID,
        request: Request,
        _: ApiToken,
        job_service: JobServiceDependency,
    ) -> dict[str, Any]:
        """Delete every published file for a job.

        Deletion is deliberate and operator-driven: results are kept until you
        say otherwise. The job record itself is untouched, so its history,
        stdout and file names survive — only the bytes go.
        """
        directory = artifact_directory_for(request, job_service, job_id)
        if not directory.is_dir():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No artifacts for this job",
            )
        removed = sorted(item.name for item in directory.iterdir() if item.is_file())
        freed = directory_size(directory)
        shutil.rmtree(directory, ignore_errors=True)
        logging.info(
            "artifacts deleted job=%s files=%d bytes=%d", job_id, len(removed), freed
        )
        return {"job_id": str(job_id), "deleted": removed, "freed_bytes": freed}

    @router.delete("/jobs/{job_id}/artifacts/{filename}")
    def delete_artifact(
        job_id: UUID,
        filename: str,
        request: Request,
        _: ApiToken,
        job_service: JobServiceDependency,
    ) -> dict[str, Any]:
        name = safe_artifact_name(filename)
        target = artifact_directory_for(request, job_service, job_id) / name
        if not target.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found"
            )
        freed = target.stat().st_size
        target.unlink()
        return {"job_id": str(job_id), "deleted": [name], "freed_bytes": freed}

    return router


def collect_system_metrics() -> dict[str, Any]:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    load = os.getloadavg()
    return {
        "hostname": socket.gethostname(),
        "platform": platform.system(),
        "cpu": {
            "percent": psutil.cpu_percent(interval=None),
            "logical_cores": psutil.cpu_count(logical=True),
            "load_1": load[0],
            "load_5": load[1],
            "load_15": load[2],
            "temperature_c": cpu_temperature(),
        },
        "memory": {
            "total": memory.total,
            "used": memory.used,
            "available": memory.available,
            "percent": memory.percent,
        },
        "storage": {
            "mount": "/",
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
            "percent": disk.percent,
        },
        "uptime_seconds": uptime_seconds(),
    }


def collect_service_health(job_service: JobService) -> dict[str, Any]:
    try:
        job_service.ping()
        database_state = "ONLINE"
        database_detail = "SQLite ready"
    except sqlite3.Error:
        database_state = "OFFLINE"
        database_detail = "SQLite unavailable"

    synology_host = os.environ.get("HOME_PLATFORM_SYNOLOGY_HOST", "").strip()
    if synology_host:
        synology_smb_reachable = tcp_reachable(synology_host, 445)
        synology_dsm_reachable = tcp_reachable(synology_host, 5001)
        synology_state = "ONLINE" if synology_smb_reachable else "OFFLINE"
    else:
        synology_smb_reachable = False
        synology_dsm_reachable = False
        synology_state = "UNKNOWN"

    return {
        "control_plane": {
            "state": "ONLINE",
            "version": VERSION,
            "detail": "FastAPI coordination service",
        },
        "database": {
            "state": database_state,
            "engine": "SQLite",
            "detail": database_detail,
        },
        "synology_nas": {
            "state": synology_state,
            "configured": bool(synology_host),
            "host": synology_host or None,
            "smb": "reachable" if synology_smb_reachable else "unreachable",
            "management": ("reachable" if synology_dsm_reachable else "unreachable"),
            "role": "primary storage",
        },
    }


def command_output(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = completed.stdout.strip()
    return output or None


def tcp_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def uptime_seconds() -> int:
    try:
        return max(0, int(time.time() - psutil.boot_time()))
    except OSError:
        return 0


def cpu_temperature() -> float | None:
    sensor_reader = getattr(psutil, "sensors_temperatures", None)
    if sensor_reader is None:
        return None
    try:
        temperatures = sensor_reader()
    except OSError:
        return None
    for readings in temperatures.values():
        if readings:
            return round(float(readings[0].current), 1)
    return None
