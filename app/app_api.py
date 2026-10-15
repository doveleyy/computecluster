import hashlib
import sqlite3
from collections.abc import Callable, Coroutine
from typing import Annotated, Any
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from starlette.background import BackgroundTask

from app.app_api_models import (
    ErrorResponse,
    FilesResponse,
    InputResponse,
    ResultsResponse,
    RunResponse,
    RunsResponse,
    UserResponse,
    WorkloadsResponse,
)
from app.app_assets import ComputeError
from app.app_compute import (
    AppCompute,
    AppInput,
    FileHandleRequest,
    RunRequest,
    run_view,
)
from app.app_deployments import environment_of, installed
from app.app_identity import AppContext, AppIdentity, Refusal
from app.app_results import RunResults
from app.app_workloads import MAX_INPUT_BYTES
from app.batch_script import BatchScriptError


def app_context(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    assertion: Annotated[str | None, Header(alias="X-Platform-User-Context")] = None,
) -> AppContext:
    identity: AppIdentity = request.app.state.app_identity
    result = identity.verify(authorization, assertion)
    if isinstance(result, Refusal):
        raise HTTPException(
            result.status, result.detail, headers={"Cache-Control": "no-store"}
        )
    return result


AppContextDependency = Annotated[AppContext, Depends(app_context)]


class AppRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                response = await handler(request)
            except ComputeError as error:
                response = JSONResponse(
                    {"detail": error.detail}, status_code=error.status
                )
            except HTTPException as error:
                response = JSONResponse(
                    {"detail": error.detail}, status_code=error.status_code
                )
            except RequestValidationError:
                response = JSONResponse({"detail": "Invalid request"}, status_code=422)
            except BatchScriptError:
                response = JSONResponse(
                    {"detail": "Installed workload is unavailable"}, status_code=409
                )
            except (OSError, sqlite3.Error):
                response = JSONResponse(
                    {"detail": "Application compute is unavailable"}, status_code=503
                )
            response.headers["Cache-Control"] = "no-store"
            return response

        return handle


def compute(request: Request) -> AppCompute:
    return AppCompute(request.app.state.job_service, request.app.state.settings)


ComputeDependency = Annotated[AppCompute, Depends(compute)]
PageLimit = Annotated[int, Query(ge=1, le=100)]
PageOffset = Annotated[int, Query(ge=0, le=100000)]
SubmissionKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
]


def errors(*statuses: int) -> dict[int | str, dict[str, Any]]:
    return {status: {"model": ErrorResponse} for status in statuses}


def active_release_sha256(domain: AppCompute, context: AppContext) -> str | None:
    with domain.database.connect() as connection:
        environment = environment_of(connection)
        current = (
            installed(connection, context.app.app_id, environment)
            if environment is not None
            else None
        )
    if current is None:
        return None
    return current.selection.manifest_sha256


def create_app_router() -> APIRouter:
    router = APIRouter(prefix="/api/apps/v1", route_class=AppRoute)

    @router.get(
        "/user", response_model=UserResponse, responses=errors(401, 403, 422, 503)
    )
    def user(context: AppContextDependency, response: Response) -> dict[str, str]:
        response.headers["Cache-Control"] = "no-store"
        return {"id": str(context.user.id), "display_name": context.user.username}

    @router.get(
        "/workloads",
        response_model=WorkloadsResponse,
        responses=errors(401, 403, 422, 503),
    )
    def workloads(
        context: AppContextDependency, domain: ComputeDependency
    ) -> dict[str, object]:
        domain.grant(context, "jobs")
        return {
            "active_release_sha256": active_release_sha256(domain, context),
            "items": [
                item.public()
                for item in domain.workloads.list(context.app.app_id)
                if "jobs" in item.release.capabilities
            ],
        }

    @router.post(
        "/workloads/{installation_id}/uploads",
        status_code=201,
        response_model=InputResponse,
        responses=errors(401, 403, 404, 409, 413, 415, 422, 503),
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                },
            }
        },
    )
    async def upload(
        installation_id: UUID,
        context: AppContextDependency,
        domain: ComputeDependency,
        request: Request,
    ) -> dict[str, object]:
        if (
            request.headers.get("content-type", "").split(";")[0]
            != "application/octet-stream"
        ):
            raise ComputeError(415, "Expected application/octet-stream")
        reservation = domain.reserve_input(context, installation_id)
        directory = domain.workloads.root / "inputs"
        target = directory / str(reservation)
        retained = False
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            size = 0
            digest = hashlib.sha256()
            with target.open("xb") as output:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_INPUT_BYTES:
                        raise ComputeError(413, "Input exceeds the 20 MiB limit")
                    output.write(chunk)
                    digest.update(chunk)
            if size == 0:
                raise ComputeError(422, "Input cannot be empty")
            item = domain.save_input(
                context,
                installation_id,
                AppInput(
                    reservation, reservation, None, None, size, digest.hexdigest()
                ),
            )
            retained = True
            return item.public()
        finally:
            try:
                if not retained:
                    target.unlink(missing_ok=True)
            finally:
                domain.discard_reservation(reservation)

    @router.get(
        "/files",
        response_model=FilesResponse,
        responses=errors(401, 403, 404, 422, 503),
    )
    def files(
        context: AppContextDependency,
        domain: ComputeDependency,
        path: Annotated[str, Query(max_length=500)] = "Home",
        limit: PageLimit = 100,
        offset: PageOffset = 0,
    ) -> dict[str, object]:
        return domain.files(context, path, limit, offset)

    @router.delete(
        "/inputs/{input_id}",
        status_code=204,
        responses=errors(401, 403, 404, 422, 503),
    )
    def delete_input(
        input_id: UUID, context: AppContextDependency, domain: ComputeDependency
    ) -> Response:
        domain.delete_input(context, input_id)
        return Response(status_code=204)

    @router.post(
        "/workloads/{installation_id}/file-handles",
        status_code=201,
        response_model=InputResponse,
        responses=errors(401, 403, 404, 409, 413, 422, 503),
    )
    def file_handle(
        installation_id: UUID,
        body: FileHandleRequest,
        context: AppContextDependency,
        domain: ComputeDependency,
    ) -> dict[str, object]:
        return domain.file_handle(context, installation_id, body.path).public()

    @router.post(
        "/runs",
        status_code=201,
        response_model=RunResponse,
        responses={200: {"model": RunResponse}, **errors(401, 403, 404, 409, 422, 503)},
    )
    def submit(
        body: RunRequest,
        key: SubmissionKey,
        context: AppContextDependency,
        domain: ComputeDependency,
        response: Response,
    ) -> dict[str, object]:
        result, created = domain.submit(context, body, key)
        response.status_code = 201 if created else 200
        return result

    @router.get(
        "/runs", response_model=RunsResponse, responses=errors(401, 403, 422, 503)
    )
    def runs(
        context: AppContextDependency,
        domain: ComputeDependency,
        limit: PageLimit = 100,
        offset: PageOffset = 0,
    ) -> dict[str, object]:
        return domain.runs(context, limit, offset)

    @router.get(
        "/runs/{run_id}",
        response_model=RunResponse,
        responses=errors(401, 403, 404, 422, 503),
    )
    def run(
        run_id: UUID, context: AppContextDependency, domain: ComputeDependency
    ) -> dict[str, object]:
        group, installed = domain.run(context, run_id)
        return run_view(group, installed.id)

    @router.post(
        "/runs/{run_id}/cancel",
        response_model=RunResponse,
        responses=errors(401, 403, 404, 422, 503),
    )
    def cancel(
        run_id: UUID, context: AppContextDependency, domain: ComputeDependency
    ) -> dict[str, object]:
        return domain.cancel(context, run_id)

    @router.get(
        "/runs/{run_id}/results",
        response_model=ResultsResponse,
        responses=errors(401, 403, 404, 409, 422, 503),
    )
    def results(
        run_id: UUID, context: AppContextDependency, domain: ComputeDependency
    ) -> dict[str, object]:
        return RunResults(domain).manifest(context, run_id)

    @router.get(
        "/runs/{run_id}/results/{index}/{name}",
        response_class=StreamingResponse,
        responses={
            200: {
                "content": {
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                }
            },
            **errors(401, 403, 404, 409, 422, 503),
        },
    )
    def download(
        run_id: UUID,
        index: Annotated[int, Path(ge=0)],
        name: str,
        context: AppContextDependency,
        domain: ComputeDependency,
        sha256: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    ) -> StreamingResponse:
        stream = RunResults(domain).download(context, run_id, index, name, sha256)
        return StreamingResponse(
            stream.body,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
            background=BackgroundTask(stream.close),
        )

    return router
