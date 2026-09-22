from __future__ import annotations

import hashlib
import logging
import os
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx

from contracts.models import (
    BatchInputSource,
    DatasetReference,
    DatasetSource,
    StorageInputReference,
    UploadedDatasetReference,
    UploadedInputReference,
    UploadedProjectReference,
)


class DatasetPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class WorkerWorkspace:
    root: Path
    allowed_dataset_hosts: frozenset[str]
    max_dataset_bytes: int
    max_cache_bytes: int = 20 * 1024**3
    control_plane_url: str | None = None
    api_token: str | None = None
    docker_executable: str | None = None
    container_image: str = "home-platform-ml:0.1"


CACHE_DIRECTORIES = ("cache", "inputs", "projects", "scripts")


def prune_cache(
    workspace: WorkerWorkspace,
    *,
    required_bytes: int = 0,
    protected: frozenset[Path] = frozenset(),
) -> tuple[int, int]:
    """Evict least-recently-used immutable inputs until a new item will fit.

    Workers execute one job at a time. A materialized input is protected while
    it is being prepared; inputs already linked into a run have a link count
    greater than one and are protected as well. Partial downloads are never
    treated as reusable cache entries.
    """
    if required_bytes > workspace.max_cache_bytes:
        raise DatasetPolicyError(
            f"input requires {required_bytes} cache bytes but worker limit is "
            f"{workspace.max_cache_bytes}"
        )
    entries: list[tuple[int, Path, int]] = []
    total = 0
    resolved_protected = {item.resolve() for item in protected}
    for directory_name in CACHE_DIRECTORIES:
        directory = workspace.root / directory_name
        if not directory.is_dir():
            continue
        for item in directory.iterdir():
            if not item.is_file() or item.name.endswith(".part"):
                continue
            stat = item.stat()
            total += stat.st_size
            if item.resolve() in resolved_protected or stat.st_nlink > 1:
                continue
            entries.append((stat.st_mtime_ns, item, stat.st_size))

    removed_files = 0
    removed_bytes = 0
    for _, item, size in sorted(entries):
        if total + required_bytes <= workspace.max_cache_bytes:
            break
        item.unlink(missing_ok=True)
        total -= size
        removed_files += 1
        removed_bytes += size
        logging.info("cache=evicted path=%s bytes=%s", item, size)
    if total + required_bytes > workspace.max_cache_bytes:
        raise DatasetPolicyError(
            "worker cache is full and its remaining entries are in use"
        )
    return removed_files, removed_bytes


def record_cache_use(path: Path) -> None:
    path.touch(exist_ok=True)


def materialize_dataset(
    reference: DatasetSource,
    workspace: WorkerWorkspace,
    cancellation_event: threading.Event | None = None,
) -> Path:
    if cancellation_event is not None and cancellation_event.is_set():
        raise RuntimeError("job cancelled while preparing its dataset")
    if isinstance(reference, StorageInputReference):
        return materialize_batch_input(reference, workspace, cancellation_event)
    if isinstance(reference, UploadedDatasetReference):
        if workspace.control_plane_url is None or workspace.api_token is None:
            raise DatasetPolicyError("uploaded dataset access is not configured")
        source_url = (
            f"{workspace.control_plane_url.rstrip('/')}/datasets/uploads/"
            f"{reference.upload_id}"
        )
        headers = {
            "Accept-Encoding": "identity",
            "X-API-Token": workspace.api_token,
        }
        source_label = f"upload:{reference.upload_id}"
    else:
        hostname = (reference.url.host or "").lower()
        if hostname not in workspace.allowed_dataset_hosts:
            raise DatasetPolicyError(
                f"dataset host {hostname!r} is not in this worker's allowlist"
            )
        source_url = str(reference.url)
        headers = {"Accept-Encoding": "identity"}
        source_label = hostname
    if reference.size_bytes > workspace.max_dataset_bytes:
        raise DatasetPolicyError(
            f"declared dataset size {reference.size_bytes} exceeds worker limit "
            f"{workspace.max_dataset_bytes}"
        )

    cache_directory = workspace.root / "cache"
    cache_directory.mkdir(parents=True, exist_ok=True)
    target = cache_directory / reference.sha256
    if target.is_file() and _matches_reference(target, reference):
        record_cache_use(target)
        logging.info("dataset=%s cache=hit", reference.sha256)
        return target
    if target.exists():
        target.unlink()

    prune_cache(workspace, required_bytes=reference.size_bytes)
    temporary = cache_directory / f".{reference.sha256}.{uuid4().hex}.part"
    digest = hashlib.sha256()
    received = 0
    logging.info(
        "dataset=%s cache=miss bytes=%s source_host=%s",
        reference.sha256,
        reference.size_bytes,
        source_label,
    )
    try:
        with httpx.stream(
            "GET",
            source_url,
            headers=headers,
            follow_redirects=False,
            timeout=httpx.Timeout(30, read=120),
        ) as response:
            if response.is_redirect:
                raise DatasetPolicyError("dataset redirects are not allowed")
            response.raise_for_status()
            declared_length = response.headers.get("Content-Length")
            if (
                declared_length is not None
                and int(declared_length) != reference.size_bytes
            ):
                raise DatasetPolicyError(
                    "dataset Content-Length does not match declared size"
                )
            with temporary.open("xb") as output:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    if cancellation_event is not None and cancellation_event.is_set():
                        raise RuntimeError(
                            "job cancelled while downloading its dataset"
                        )
                    received += len(chunk)
                    if received > reference.size_bytes:
                        raise DatasetPolicyError("dataset exceeds declared size")
                    digest.update(chunk)
                    output.write(chunk)
        if received != reference.size_bytes:
            raise DatasetPolicyError("dataset size does not match declared size")
        if digest.hexdigest() != reference.sha256:
            raise DatasetPolicyError("dataset SHA-256 does not match declaration")
        os.replace(temporary, target)
        logging.info("dataset=%s cache=stored bytes=%s", reference.sha256, received)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def materialize_project(
    reference: UploadedProjectReference,
    workspace: WorkerWorkspace,
    destination: Path,
    cancellation_event: threading.Event | None = None,
) -> None:
    if workspace.control_plane_url is None or workspace.api_token is None:
        raise DatasetPolicyError("uploaded project access is not configured")
    cache_directory = workspace.root / "projects"
    cache_directory.mkdir(parents=True, exist_ok=True)
    archive_path = cache_directory / f"{reference.sha256}.zip"
    if not archive_path.is_file() or not _matches_reference(archive_path, reference):
        archive_path.unlink(missing_ok=True)
        prune_cache(workspace, required_bytes=reference.size_bytes)
        _download_project(reference, workspace, archive_path, cancellation_event)
    else:
        record_cache_use(archive_path)

    destination.mkdir(parents=True, exist_ok=True)
    expanded = 0
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if len(entries) > 1000:
                raise DatasetPolicyError("project archive contains too many entries")
            for entry in entries:
                if cancellation_event is not None and cancellation_event.is_set():
                    raise RuntimeError("job cancelled while preparing its project")
                if "\\" in entry.filename:
                    raise DatasetPolicyError("project paths must use forward slashes")
                relative = Path(entry.filename)
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or (entry.external_attr >> 16) & 0o170000 == 0o120000
                ):
                    raise DatasetPolicyError("project archive contains an unsafe path")
                expanded += entry.file_size
                if expanded > 100 * 1024**2:
                    raise DatasetPolicyError("expanded project exceeds 100 MiB")
                target = destination / relative
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
    except zipfile.BadZipFile as error:
        raise DatasetPolicyError(
            "project cache contains an invalid ZIP archive"
        ) from error


def materialize_batch_input(
    reference: BatchInputSource,
    workspace: WorkerWorkspace,
    cancellation_event: threading.Event | None = None,
) -> Path:
    if isinstance(reference, DatasetReference):
        return materialize_dataset(reference, workspace, cancellation_event)
    if workspace.control_plane_url is None or workspace.api_token is None:
        raise DatasetPolicyError("control-plane input access is not configured")
    if reference.size_bytes > workspace.max_dataset_bytes:
        raise DatasetPolicyError(
            f"declared input size {reference.size_bytes} exceeds worker limit "
            f"{workspace.max_dataset_bytes}"
        )
    cache_directory = workspace.root / "inputs"
    cache_directory.mkdir(parents=True, exist_ok=True)
    target = cache_directory / reference.sha256
    if target.is_file() and _matches_reference(target, reference):
        record_cache_use(target)
        return target
    target.unlink(missing_ok=True)
    prune_cache(workspace, required_bytes=reference.size_bytes)
    temporary = cache_directory / f".{reference.sha256}.{uuid4().hex}.part"
    digest = hashlib.sha256()
    received = 0
    if isinstance(reference, StorageInputReference):
        if reference.storage_id != "home-storage":
            raise DatasetPolicyError(
                f"storage provider {reference.storage_id!r} is not configured"
            )
        source_url = (
            f"{workspace.control_plane_url.rstrip('/')}/storage/files/"
            f"{quote(reference.path, safe='/')}"
        )
    else:
        source_url = (
            f"{workspace.control_plane_url.rstrip('/')}/inputs/uploads/"
            f"{reference.upload_id}"
        )
    try:
        with httpx.stream(
            "GET",
            source_url,
            headers={
                "Accept-Encoding": "identity",
                "X-API-Token": workspace.api_token,
            },
            follow_redirects=False,
            timeout=httpx.Timeout(30, read=120),
        ) as response:
            if response.is_redirect:
                raise DatasetPolicyError("input redirects are not allowed")
            response.raise_for_status()
            declared_length = response.headers.get("Content-Length")
            if (
                declared_length is not None
                and int(declared_length) != reference.size_bytes
            ):
                raise DatasetPolicyError(
                    "input Content-Length does not match declared size"
                )
            with temporary.open("xb") as output:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    if cancellation_event is not None and cancellation_event.is_set():
                        raise RuntimeError("job cancelled while downloading its input")
                    received += len(chunk)
                    if received > reference.size_bytes:
                        raise DatasetPolicyError("input exceeds declared size")
                    digest.update(chunk)
                    output.write(chunk)
        if received != reference.size_bytes or digest.hexdigest() != reference.sha256:
            raise DatasetPolicyError(
                "input does not match its declared size and digest"
            )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _download_project(
    reference: UploadedProjectReference,
    workspace: WorkerWorkspace,
    target: Path,
    cancellation_event: threading.Event | None,
) -> None:
    if workspace.control_plane_url is None or workspace.api_token is None:
        raise DatasetPolicyError("uploaded project access is not configured")
    temporary = target.with_name(f".{reference.sha256}.{uuid4().hex}.part")
    digest = hashlib.sha256()
    received = 0
    try:
        with httpx.stream(
            "GET",
            f"{workspace.control_plane_url.rstrip('/')}/projects/uploads/{reference.upload_id}",
            headers={"Accept-Encoding": "identity", "X-API-Token": workspace.api_token},
            follow_redirects=False,
            timeout=httpx.Timeout(30, read=120),
        ) as response:
            response.raise_for_status()
            with temporary.open("xb") as output:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    if cancellation_event is not None and cancellation_event.is_set():
                        raise RuntimeError(
                            "job cancelled while downloading its project"
                        )
                    received += len(chunk)
                    if received > reference.size_bytes:
                        raise DatasetPolicyError("project exceeds declared size")
                    digest.update(chunk)
                    output.write(chunk)
        if received != reference.size_bytes or digest.hexdigest() != reference.sha256:
            raise DatasetPolicyError(
                "project does not match its declared size and digest"
            )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _matches_reference(
    path: Path,
    reference: (
        DatasetSource
        | UploadedProjectReference
        | UploadedInputReference
        | StorageInputReference
    ),
) -> bool:
    if path.stat().st_size != reference.size_bytes:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as dataset:
        for chunk in iter(lambda: dataset.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == reference.sha256
