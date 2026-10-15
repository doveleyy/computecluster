"""Staged uploads: the four kinds a job can reference and how each is stored.

One table describes where a kind lives, what it accepts, and how it is served
back to a worker, so the Job Desk and token routes differ only in who may
call them.
"""

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse

from app.accounts import AccountStore, SessionIdentity
from app.batch_script import BatchScriptError, validate_project_archive
from app.config import Settings
from app.dashboard_auth import AccountStoreDependency, ApiToken, MemberSession
from contracts.models import (
    UploadedDatasetReference,
    UploadedInputReference,
    UploadedProjectReference,
    UploadedScriptReference,
)


@dataclass(frozen=True)
class UploadKind[
    Reference: (
        UploadedDatasetReference,
        UploadedScriptReference,
        UploadedProjectReference,
        UploadedInputReference,
    )
]:
    name: str
    subdirectory: str
    suffix: str
    label: str
    max_bytes: Callable[[Settings], int]
    reference: type[Reference]
    media_type: str
    missing: str
    validate_suffix: bool = True
    validate: Callable[[Path], None] | None = None
    download_filename: bool = True

    def directory(self, settings: Settings) -> Path:
        return settings.upload_directory / self.subdirectory

    def path(self, settings: Settings, upload_id: UUID) -> Path:
        return self.directory(settings) / f"{upload_id}{self.suffix}"


DATASET = UploadKind(
    "dataset",
    "",
    ".csv",
    "CSV file",
    lambda settings: settings.max_upload_bytes,
    UploadedDatasetReference,
    "text/csv",
    "Uploaded dataset not found",
)
SCRIPT = UploadKind(
    "script",
    "scripts",
    ".py",
    "Python script",
    lambda settings: settings.max_script_upload_bytes,
    UploadedScriptReference,
    "text/x-python",
    "Uploaded script not found",
)
PROJECT = UploadKind(
    "project",
    "projects",
    ".zip",
    "Project archive",
    lambda settings: settings.max_project_upload_bytes,
    UploadedProjectReference,
    "application/zip",
    "Uploaded project not found",
    validate=validate_project_archive,
)
INPUT = UploadKind(
    "input",
    "inputs",
    ".input",
    "Input file",
    lambda settings: settings.max_project_upload_bytes,
    UploadedInputReference,
    "application/octet-stream",
    "Uploaded input not found",
    validate_suffix=False,
    download_filename=False,
)


async def stage_upload[
    Reference: (
        UploadedDatasetReference,
        UploadedScriptReference,
        UploadedProjectReference,
        UploadedInputReference,
    )
](request: Request, file: UploadFile, kind: UploadKind[Reference]) -> Reference:
    settings: Settings = request.app.state.settings
    filename = file.filename or ""
    if kind.validate_suffix and Path(filename).suffix.lower() != kind.suffix:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Only {kind.suffix} files are accepted",
        )
    max_bytes = kind.max_bytes(settings)
    upload_id = uuid4()
    directory = kind.directory(settings)
    directory.mkdir(parents=True, exist_ok=True)
    target = kind.path(settings, upload_id)
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
                        detail=f"{kind.label} exceeds the {max_bytes // 1024} KB limit",
                    )
                digest.update(chunk)
                output.write(chunk)
        if size == 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{kind.label} cannot be empty",
            )
        os.replace(temporary, target)
    finally:
        await file.close()
        temporary.unlink(missing_ok=True)
    if kind.validate is not None:
        try:
            kind.validate(target)
        except BatchScriptError:
            target.unlink(missing_ok=True)
            raise
    return kind.reference(
        upload_id=upload_id, sha256=digest.hexdigest(), size_bytes=size
    )


async def member_upload[
    Reference: (
        UploadedDatasetReference,
        UploadedScriptReference,
        UploadedProjectReference,
        UploadedInputReference,
    )
](
    request: Request,
    file: UploadFile,
    kind: UploadKind[Reference],
    identity: SessionIdentity,
    account_store: AccountStore,
) -> Reference:
    reference = await stage_upload(request, file, kind)
    account_store.record_upload(reference.upload_id, identity.id, kind.name)
    return reference


def uploaded_file[
    Reference: (
        UploadedDatasetReference,
        UploadedScriptReference,
        UploadedProjectReference,
        UploadedInputReference,
    )
](request: Request, kind: UploadKind[Reference], upload_id: UUID) -> FileResponse:
    target = kind.path(request.app.state.settings, upload_id)
    if not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=kind.missing)
    return FileResponse(
        target,
        media_type=kind.media_type,
        filename=target.name if kind.download_filename else None,
    )


router = APIRouter()


@router.post(
    "/jobs-ui/api/uploads",
    response_model=UploadedDatasetReference,
    status_code=status.HTTP_201_CREATED,
)
async def jobs_portal_upload(
    request: Request,
    identity: MemberSession,
    account_store: AccountStoreDependency,
    file: Annotated[UploadFile, File()],
) -> UploadedDatasetReference:
    return await member_upload(request, file, DATASET, identity, account_store)


@router.post(
    "/jobs-ui/api/script-uploads",
    response_model=UploadedScriptReference,
    status_code=status.HTTP_201_CREATED,
)
async def jobs_portal_script_upload(
    request: Request,
    identity: MemberSession,
    account_store: AccountStoreDependency,
    file: Annotated[UploadFile, File()],
) -> UploadedScriptReference:
    return await member_upload(request, file, SCRIPT, identity, account_store)


@router.post(
    "/jobs-ui/api/project-uploads",
    response_model=UploadedProjectReference,
    status_code=status.HTTP_201_CREATED,
)
async def jobs_portal_project_upload(
    request: Request,
    identity: MemberSession,
    account_store: AccountStoreDependency,
    file: Annotated[UploadFile, File()],
) -> UploadedProjectReference:
    return await member_upload(request, file, PROJECT, identity, account_store)


@router.post(
    "/jobs-ui/api/input-uploads",
    response_model=UploadedInputReference,
    status_code=status.HTTP_201_CREATED,
)
async def jobs_portal_input_upload(
    request: Request,
    identity: MemberSession,
    account_store: AccountStoreDependency,
    file: Annotated[UploadFile, File()],
) -> UploadedInputReference:
    return await member_upload(request, file, INPUT, identity, account_store)


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
    return await stage_upload(request, file, DATASET)


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
    return await stage_upload(request, file, SCRIPT)


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
    return await stage_upload(request, file, PROJECT)


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
    return await stage_upload(request, file, INPUT)


@router.get("/datasets/uploads/{upload_id}", response_class=FileResponse)
def download_uploaded_dataset(
    upload_id: UUID,
    request: Request,
    _: ApiToken,
) -> FileResponse:
    return uploaded_file(request, DATASET, upload_id)


@router.get("/scripts/uploads/{upload_id}", response_class=FileResponse)
def download_uploaded_script(
    upload_id: UUID,
    request: Request,
    _: ApiToken,
) -> FileResponse:
    return uploaded_file(request, SCRIPT, upload_id)


@router.get("/projects/uploads/{upload_id}", response_class=FileResponse)
def download_uploaded_project(
    upload_id: UUID,
    request: Request,
    _: ApiToken,
) -> FileResponse:
    return uploaded_file(request, PROJECT, upload_id)


@router.get("/inputs/uploads/{upload_id}", response_class=FileResponse)
def download_uploaded_input(
    upload_id: UUID,
    request: Request,
    _: ApiToken,
) -> FileResponse:
    return uploaded_file(request, INPUT, upload_id)
