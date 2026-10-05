"""The one place a service error becomes an HTTP status.

Every route used to repeat the same ``except ... raise HTTPException`` for
these types. The table makes the mapping a fact about the error, and a route
that needs a different status or detail still catches the error itself.
"""

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.batch_script import BatchScriptError
from app.service import (
    IdempotencyConflictError,
    JobTransitionError,
    SchedulingCapacityError,
    WorkerNotFoundError,
)
from app.storage import StoragePolicyError

ERROR_STATUS: dict[type[Exception], int] = {
    WorkerNotFoundError: status.HTTP_404_NOT_FOUND,
    JobTransitionError: status.HTTP_409_CONFLICT,
    IdempotencyConflictError: status.HTTP_409_CONFLICT,
    SchedulingCapacityError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    StoragePolicyError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    BatchScriptError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


def _detail_response(
    status_code: int,
) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
    async def handle(_request: Request, error: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=status_code)

    return handle


def install_error_handlers(application: FastAPI) -> None:
    for error_type, status_code in ERROR_STATUS.items():
        application.add_exception_handler(error_type, _detail_response(status_code))
