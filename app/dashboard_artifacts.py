"""Published job results: where a run's files live and who may touch them."""

import hashlib
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Annotated, Any, cast
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

from app.accounts import SessionIdentity
from app.artifacts import (
    job_artifact_relative_path,
    legacy_artifact_relative_path,
)
from app.dashboard_auth import ApiToken, DashboardSession, owner_scope
from app.job_http import JobServiceDependency
from app.service import JobGroupNotFoundError, JobNotFoundError, JobService

router = APIRouter()


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
                    group_names[job.group_id] = job_service.get_group(job.group_id).name
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
    return sum(item.stat().st_size for item in directory.rglob("*") if item.is_file())


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
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) is None or ".." in name:
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


def artifact_manifest(directory: Path) -> list[dict[str, Any]]:
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


def artifact_file(directory: Path, name: str) -> FileResponse:
    target = directory / name
    if not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found"
        )
    return FileResponse(target, media_type="application/octet-stream", filename=name)


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

    settings = request.app.state.settings
    name = safe_artifact_name(file.filename or "")
    directory = artifact_directory_for(request, job_service, job_id)
    directory.mkdir(parents=True, exist_ok=True)

    used = sum(item.stat().st_size for item in directory.glob("*") if item.is_file())
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
    return artifact_manifest(artifact_directory_for(request, job_service, job_id))


@router.get("/jobs/{job_id}/artifacts/{filename}", response_class=FileResponse)
def download_artifact(
    job_id: UUID,
    filename: str,
    request: Request,
    _: ApiToken,
    job_service: JobServiceDependency,
) -> FileResponse:
    name = safe_artifact_name(filename)
    return artifact_file(artifact_directory_for(request, job_service, job_id), name)


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
    return artifact_manifest(artifact_directory_for(request, job_service, job_id))


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
    return artifact_file(artifact_directory_for(request, job_service, job_id), name)


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
