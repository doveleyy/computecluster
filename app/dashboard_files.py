"""The Files page: one logical tree over storage, Workspace and results.

A member sees ``Home``, ``Shared`` and ``Artifacts``; an administrator sees
``Storage`` and ``Artifacts``. Writes are confined to a member's Workspace,
which an administrator reaches through ``Storage/users/<id>/Workspace``.
"""

import logging
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from app.accounts import SessionIdentity
from app.dashboard_artifacts import (
    artifact_filesystem_root,
    directory_size,
    member_artifact_runs,
)
from app.dashboard_auth import (
    DashboardSession,
    member_storage_available,
    owner_scope,
    require_administrator_workspace,
    require_member_storage,
    require_member_workspace,
)
from app.dashboard_storage import StoragePathRequest, regular_file
from app.job_http import JobServiceDependency
from app.service import JobService
from app.storage import (
    StoragePolicyError,
    browse_storage,
    copy_workspace_entry,
    create_workspace_directory,
    delete_workspace_entry,
    member_storage_path,
    move_workspace_entry,
    resolve_storage_path,
    workspace_upload_target,
)
from contracts.models import JobStatus

router = APIRouter()


class WorkspaceDirectoryCreate(StoragePathRequest):
    pass


class FileTransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    destination: str


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


def workspace_target(
    request: Request, identity: SessionIdentity, path: str
) -> tuple[Path, UUID, str]:
    """Resolve a Files path to (workspace root, owning member, logical path)."""
    if identity.is_admin:
        root = require_administrator_workspace(request)
        owner_id, logical = administrator_workspace_path(path)
        return root, owner_id, logical
    return require_member_workspace(request, identity), identity.id, path


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
    provider_root, relative, area = logical_file_location(
        request, identity, normalized, job_service
    )
    entries = virtual_file_entries(provider_root, relative, normalized)
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
                and (lower == "home/workspace" or lower.startswith("home/workspace/"))
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
    provider_root, relative, _ = logical_file_location(
        request, identity, path, job_service
    )
    target = resolve_storage_path(provider_root, relative)
    return regular_file(target, "choose a regular file to download")


@router.post("/jobs-ui/api/files/move")
def jobs_portal_file_move(
    transfer: FileTransferRequest,
    request: Request,
    identity: DashboardSession,
) -> dict[str, Any]:
    workspace_root, owner_id, source = workspace_target(
        request, identity, transfer.source
    )
    _, destination_owner, destination = workspace_target(
        request, identity, transfer.destination
    )
    if owner_id != destination_owner:
        raise StoragePolicyError("Workspace moves cannot cross member accounts")
    move_workspace_entry(workspace_root, owner_id, source, destination)
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
    workspace_root, owner_id, source = workspace_target(
        request, identity, transfer.source
    )
    _, destination_owner, destination = workspace_target(
        request, identity, transfer.destination
    )
    if owner_id != destination_owner:
        raise StoragePolicyError("Workspace copies cannot cross member accounts")
    copied = copy_workspace_entry(
        workspace_root,
        owner_id,
        source,
        destination,
        max_bytes=request.app.state.settings.max_workspace_upload_bytes,
    )
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
            workspace_root, owner_id, logical_path = workspace_target(
                request, identity, normalized
            )
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
                "administrators may delete only member Workspace entries "
                "or artifact entries"
            )
        elif lower.startswith("home/workspace/"):
            workspace_root, owner_id, logical_path = workspace_target(
                request, identity, normalized
            )
            freed = delete_workspace_entry(workspace_root, owner_id, logical_path)
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
    workspace_root, owner_id, logical_path = workspace_target(
        request, identity, creation.path
    )
    create_workspace_directory(workspace_root, owner_id, logical_path)
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
    workspace_root, owner_id, logical_directory = workspace_target(
        request, identity, directory
    )
    filename = file.filename or ""
    target = workspace_upload_target(
        workspace_root, owner_id, logical_directory, filename
    )

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
