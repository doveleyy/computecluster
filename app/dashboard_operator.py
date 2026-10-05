"""Administrator-only API behind the operator dashboard."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict

from app.accounts import (
    PasswordReset,
    UserCreate,
    UserExistsError,
    UserNotFoundError,
    UserRead,
    UserUpdate,
)
from app.dashboard_auth import AccountStoreDependency, AdminSession
from app.job_http import JobServiceDependency
from app.power import (
    PowerAction,
    PowerControlUnavailableError,
    PowerRequestPendingError,
    queue_power_request,
)
from app.service import WorkerNotFoundError
from app.system_health import collect_service_health, collect_system_metrics
from contracts.models import (
    JobRead,
    JobStatus,
    WorkerCapacityUpdate,
    WorkerRead,
    WorkerUpdate,
)

router = APIRouter()


class PiPowerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: PowerAction
    confirmation: str


@router.get("/jobs-ui/api/workload-owners")
def jobs_portal_workload_owners(
    account_store: AccountStoreDependency,
    _: AdminSession,
) -> dict[str, dict[str, str]]:
    return account_store.workload_owners()


@router.get("/dashboard/api/users", response_model=list[UserRead])
def dashboard_users(
    account_store: AccountStoreDependency,
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
    account_store: AccountStoreDependency,
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
    account_store: AccountStoreDependency,
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
    account_store: AccountStoreDependency,
    _: AdminSession,
) -> None:
    try:
        account_store.reset_password(user_id, password.new_password)
    except UserNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member account not found",
        ) from error


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


@router.put("/dashboard/api/workers/{worker_id}/capacity", response_model=WorkerRead)
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
