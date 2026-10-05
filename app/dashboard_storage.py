"""The NAS storage share as seen through the Job Desk and the token API.

Members speak logical ``Home/...`` and ``Shared/...`` paths that resolve to
their own UUID-keyed tree. The token API is the administrator's view and
accepts the same vocabulary plus the physical paths the transitional share
still has.
"""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from app.dashboard_auth import (
    AccountStoreDependency,
    ApiToken,
    MemberSession,
    member_workspace_available,
    require_member_storage,
)
from app.storage import (
    STORAGE_ID,
    browse_storage,
    inspect_storage_project,
    is_logical_storage_path,
    member_storage_entries,
    member_storage_path,
    package_storage_project,
    resolve_logical_storage_path,
    resolve_storage_path,
    storage_file_reference,
)
from contracts.models import StorageInputReference, UploadedProjectReference

router = APIRouter()


class StoragePathRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str


class StorageProjectPreviewRequest(StoragePathRequest):
    entrypoint: str = "submit.hp"


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


def regular_file(target: Path, detail: str) -> FileResponse:
    if not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail
        )
    return FileResponse(
        target, media_type="application/octet-stream", filename=target.name
    )


@router.get("/jobs-ui/api/storage")
def jobs_portal_storage(
    request: Request,
    identity: MemberSession,
    path: str = "",
) -> dict[str, Any]:
    require_member_storage(request, identity)
    entries = member_storage_entries(
        request.app.state.settings.storage_directory, identity.id, path
    )
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
    target = resolve_storage_path(
        request.app.state.settings.storage_directory,
        member_storage_path(identity.id, path),
    )
    return regular_file(target, "choose a regular file to download")


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
    return storage_file_reference(
        request.app.state.settings.storage_directory,
        member_storage_path(identity.id, selection.path),
    )


@router.post(
    "/jobs-ui/api/storage/project-uploads",
    response_model=UploadedProjectReference,
    status_code=status.HTTP_201_CREATED,
)
def jobs_portal_storage_project(
    selection: StoragePathRequest,
    request: Request,
    identity: MemberSession,
    account_store: AccountStoreDependency,
) -> UploadedProjectReference:
    settings = request.app.state.settings
    require_member_storage(request, identity)
    project = package_storage_project(
        settings.storage_directory,
        member_storage_path(identity.id, selection.path),
        settings.upload_directory / "projects",
        settings.max_project_upload_bytes,
    )
    account_store.record_upload(project.upload_id, identity.id, "project")
    return project


@router.post("/jobs-ui/api/storage/project-preview")
def jobs_portal_storage_project_preview(
    selection: StorageProjectPreviewRequest,
    request: Request,
    identity: MemberSession,
) -> dict[str, Any]:
    require_member_storage(request, identity)
    parsed, files = inspect_storage_project(
        request.app.state.settings.storage_directory,
        member_storage_path(identity.id, selection.path),
        selection.entrypoint,
    )
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


@router.get("/storage")
def api_storage(
    request: Request,
    _: ApiToken,
    path: str = "",
) -> dict[str, Any]:
    entries = browse_storage(
        request.app.state.settings.storage_directory,
        administrator_storage_path(path),
    )
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
    return storage_file_reference(
        request.app.state.settings.storage_directory,
        administrator_storage_path(selection.path),
    )


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
    return package_storage_project(
        settings.storage_directory,
        administrator_storage_path(selection.path),
        settings.upload_directory / "projects",
        settings.max_project_upload_bytes,
    )


@router.get("/storage/files/{file_path:path}", response_class=FileResponse)
def api_storage_file(
    file_path: str,
    request: Request,
    _: ApiToken,
) -> FileResponse:
    target = resolve_storage_path(
        request.app.state.settings.storage_directory,
        administrator_storage_path(file_path),
    )
    if not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="storage path is not a regular file",
        )
    return FileResponse(target, media_type="application/octet-stream")
