import asyncio
import logging
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from functools import partial
from pathlib import Path
from secrets import token_urlsafe

from fastapi import FastAPI

from app.accounts import AccountStore
from app.api import create_router
from app.app_api import create_app_router
from app.app_compute import reconcile_app_inputs
from app.app_gateway import AppGateway, create_upstream_client
from app.app_identity import AppIdentity
from app.app_registry import AppRegistry
from app.config import load_settings
from app.dashboard import create_dashboard_router
from app.database import Database
from app.http_errors import install_error_handlers
from app.repository import JobRepository
from app.request_guards import RequestGuard
from app.service import JobService
from app.version import VERSION

ROUTINE_WORKER_PATHS = frozenset({"/workers/claim", "/workers/heartbeat"})
# Successful calls the platform makes to itself all day: managed apps resolving
# gateway context through the app API, legacy identity resolution, and the
# dashboard and Job Desk polling while open. Each wrote a journal line to the
# SD card.
ROUTINE_POST_PATHS = ROUTINE_WORKER_PATHS | {"/internal/service-identities/resolve"}
ROUTINE_GET_PATHS = frozenset(
    {
        "/jobs-ui/api/session",
        "/jobs-ui/api/jobs",
        "/jobs-ui/api/job-groups",
        "/jobs-ui/api/workload-owners",
        "/jobs-ui/api/workers",
        "/dashboard/api/workers",
        "/dashboard/api/system",
        "/dashboard/api/services",
        "/api/apps/v1/user",
    }
)


class RoutineWorkerAccessFilter(logging.Filter):
    """Drop only successful high-frequency routine access lines.

    Application warnings, failed requests (including an unlinked identity's
    404), and every other route remain visible. Uvicorn supplies access fields
    as positional logging arguments.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        arguments = record.args
        if not isinstance(arguments, tuple) or len(arguments) < 5:
            return True
        method = str(arguments[1])
        path = str(arguments[2]).split("?", 1)[0]
        try:
            status_code = int(str(arguments[4]))
        except ValueError:
            return True
        routine = (method == "POST" and path in ROUTINE_POST_PATHS) or (
            method == "GET" and path in ROUTINE_GET_PATHS
        )
        return not (routine and 200 <= status_code < 300)


def configure_access_logging() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, RoutineWorkerAccessFilter) for item in logger.filters):
        logger.addFilter(RoutineWorkerAccessFilter())


def create_app(database_path: Path | None = None) -> FastAPI:
    settings = load_settings()
    database = Database(database_path or settings.database_path)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        configure_access_logging()
        database.initialize()
        reconcile_app_inputs(database)
        job_service = JobService(
            JobRepository(database),
            lease_seconds=settings.lease_seconds,
            worker_stale_seconds=settings.worker_stale_seconds,
            max_attempts=settings.max_attempts,
        )
        application.state.job_service = job_service
        application.state.account_store = AccountStore(database)
        application.state.app_registry = AppRegistry(database)
        if settings.app_platform is not None:
            application.state.app_registry.publish_instance(
                settings.app_platform.instance
            )
        application.state.app_identity = AppIdentity(
            application.state.account_store,
            application.state.app_registry,
            settings.app_platform,
        )
        application.state.gateway_client = create_upstream_client()
        application.state.settings = settings
        recovery_task = asyncio.create_task(
            recover_expired_jobs(job_service, settings.recovery_interval_seconds)
        )
        from app.app_discovery import discovery_loop

        discovery_task = (
            asyncio.create_task(discovery_loop(database, settings.release_read_token))
            if settings.release_discovery_enabled
            else None
        )
        deployment_task = asyncio.create_task(reconcile_deployments(database))
        try:
            yield
        finally:
            deployment_task.cancel()
            with suppress(asyncio.CancelledError):
                await deployment_task
            if discovery_task is not None:
                discovery_task.cancel()
                with suppress(asyncio.CancelledError):
                    await discovery_task
            await application.state.gateway_client.aclose()
            recovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await recovery_task

    application = FastAPI(
        title="Personal Home Platform",
        version=VERSION,
        lifespan=lifespan,
    )
    # Signs dashboard sessions when no API token is configured; per app, so
    # one process can host independent test apps.
    application.state.anonymous_session = token_urlsafe(32)
    install_error_handlers(application)
    application.add_middleware(AppGateway)
    application.add_middleware(RequestGuard)
    application.include_router(create_router())
    application.include_router(create_dashboard_router())
    application.include_router(create_app_router())
    return application


async def recover_expired_jobs(job_service: JobService, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(job_service.recover_expired)
        except sqlite3.Error:
            logging.exception("expired-job recovery failed")


app = create_app()


async def reconcile_deployments(database: Database) -> None:
    from app.app_contracts import ContractError
    from app.app_deployments import AppsService
    from ops.installer_policy import app_fingerprint, load_policy

    path = os.environ.get("HOME_PLATFORM_INSTALLER_POLICY")
    if path is None:
        return
    while True:
        try:
            policy = load_policy(Path(path))
            await asyncio.to_thread(
                AppsService(
                    database,
                    Path(policy.spool),
                    partial(app_fingerprint, policy),
                ).reconcile
            )
        except Exception as error:
            logging.getLogger(__name__).warning(
                "Application receipts are unavailable",
                exc_info=not isinstance(error, (ContractError, OSError, sqlite3.Error)),
            )
        await asyncio.sleep(5)
