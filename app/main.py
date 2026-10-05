import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from secrets import token_urlsafe

from fastapi import FastAPI

from app.accounts import AccountStore
from app.api import create_router
from app.config import load_settings
from app.dashboard import create_dashboard_router
from app.database import Database
from app.http_errors import install_error_handlers
from app.repository import JobRepository
from app.service import JobService
from app.version import VERSION

ROUTINE_WORKER_PATHS = frozenset({"/workers/claim", "/workers/heartbeat"})
# Successful calls the platform makes to itself all day: application services
# resolving identities and checking the sorter's session, and the dashboard
# and Job Desk polling while open. Each wrote a journal line to the SD card.
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
        job_service = JobService(
            JobRepository(database),
            lease_seconds=settings.lease_seconds,
            worker_stale_seconds=settings.worker_stale_seconds,
            max_attempts=settings.max_attempts,
        )
        application.state.job_service = job_service
        application.state.account_store = AccountStore(database)
        application.state.settings = settings
        recovery_task = asyncio.create_task(
            recover_expired_jobs(job_service, settings.recovery_interval_seconds)
        )
        try:
            yield
        finally:
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
    application.include_router(create_router())
    application.include_router(create_dashboard_router())
    return application


async def recover_expired_jobs(job_service: JobService, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(job_service.recover_expired)
        except sqlite3.Error:
            logging.exception("expired-job recovery failed")


app = create_app()
