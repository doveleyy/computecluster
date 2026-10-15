from __future__ import annotations

import hashlib
import os
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from app.batch_script import (
    MAX_EXPANDED_PROJECT_BYTES,
    MAX_PROJECT_FILES,
    BatchScriptError,
    ParsedBatchScript,
    parse_batch_script,
    validate_project_archive,
)
from contracts.models import StorageInputReference, UploadedProjectReference

STORAGE_ID = "home-storage"


class StoragePolicyError(ValueError):
    pass


@dataclass(frozen=True)
class StorageEntry:
    name: str
    path: str
    kind: str
    size_bytes: int | None


def resolve_storage_path(root: Path, relative_path: str) -> Path:
    normalized = relative_path.strip().replace("\\", "/")
    relative = PurePosixPath(normalized or ".")
    if relative.is_absolute() or any(part in {"..", ""} for part in relative.parts):
        raise StoragePolicyError("storage path must stay inside HomeStorage")
    try:
        resolved_root = root.resolve(strict=True)
        candidate = resolved_root.joinpath(*relative.parts).resolve(strict=True)
    except OSError as error:
        raise StoragePolicyError("storage path does not exist") from error
    if not candidate.is_relative_to(resolved_root):
        raise StoragePolicyError("storage path escapes HomeStorage")
    current = resolved_root
    for part in relative.parts:
        if part == ".":
            continue
        current /= part
        if current.is_symlink():
            raise StoragePolicyError("storage paths cannot contain symbolic links")
    return candidate


def browse_storage(root: Path, relative_path: str = "") -> list[StorageEntry]:
    directory = resolve_storage_path(root, relative_path)
    if not directory.is_dir():
        raise StoragePolicyError("storage path is not a directory")
    prefix = PurePosixPath(relative_path.strip().replace("\\", "/"))
    if str(prefix) == ".":
        prefix = PurePosixPath()
    entries: list[StorageEntry] = []
    for item in sorted(
        directory.iterdir(), key=lambda path: (not path.is_dir(), path.name.lower())
    ):
        if item.is_symlink() or item.name.startswith("."):
            continue
        path = (prefix / item.name).as_posix()
        if item.is_dir():
            entries.append(StorageEntry(item.name, path, "directory", None))
        elif item.is_file():
            entries.append(StorageEntry(item.name, path, "file", item.stat().st_size))
    return entries


def is_logical_storage_path(path: str) -> bool:
    """Report whether a path uses the Home/Shared vocabulary.

    The provider tree has no top-level `users` alias and its shared directory is
    lowercase, so a leading `Home` or `Shared` segment identifies the logical
    form without ambiguity. That lets the token API keep accepting the older
    physical paths for the transitional Pi share, which has `projects/` and
    `inputs/` directories with no logical equivalent.
    """
    normalized = path.strip().replace("\\", "/").strip("/")
    if not normalized:
        return False
    return normalized.split("/", 1)[0].lower() in {"home", "shared"}


def _split_logical_path(logical_path: str) -> tuple[str, list[str]]:
    """Validate one logical path and split it into its root and remainder."""
    normalized = logical_path.strip().replace("\\", "/").strip("/")
    if not normalized:
        raise StoragePolicyError("choose Home or Shared")
    path = PurePosixPath(normalized)
    if any(part in {"..", ""} for part in path.parts):
        raise StoragePolicyError("storage path must stay inside Home or Shared")
    area, *remainder = path.parts
    root = area.lower()
    if root not in {"home", "shared"}:
        raise StoragePolicyError("member storage paths must start with Home or Shared")
    return root, remainder


def member_storage_path(user_id: UUID, logical_path: str) -> str:
    """Map member-facing Home/Shared paths to stable provider paths."""
    root, remainder = _split_logical_path(logical_path)
    if root == "home":
        return PurePosixPath("users", str(user_id), *remainder).as_posix()
    return PurePosixPath("shared", *remainder).as_posix()


def resolve_logical_storage_path(logical_path: str, *, user_id: UUID | None) -> str:
    """Map a logical path onto the provider tree for either kind of caller.

    A member session passes its own `user_id`, so `Home` means that account and
    nothing else. An administrator — the API token — passes `None`, because its
    authority spans every account and so `Home` alone would be ambiguous. It
    must name the target as `Home/<user-id>/...`, which keeps one vocabulary
    across both callers while making the wider reach visible in the path itself.
    """
    if user_id is not None:
        return member_storage_path(user_id, logical_path)
    root, remainder = _split_logical_path(logical_path)
    if root == "shared":
        return PurePosixPath("shared", *remainder).as_posix()
    if not remainder:
        raise StoragePolicyError(
            "administrators must name the account: Home/<user-id>/..."
        )
    try:
        owner = UUID(remainder[0])
    except ValueError as error:
        raise StoragePolicyError(
            "administrator Home paths must name a stable user ID"
        ) from error
    return PurePosixPath("users", str(owner), *remainder[1:]).as_posix()


def member_storage_entries(
    root: Path, user_id: UUID, logical_path: str = ""
) -> list[StorageEntry]:
    normalized = logical_path.strip().replace("\\", "/").strip("/")
    if not normalized:
        return [
            StorageEntry("Home", "Home", "directory", None),
            StorageEntry("Shared", "Shared", "directory", None),
        ]
    physical_path = member_storage_path(user_id, normalized)
    entries = browse_storage(root, physical_path)
    physical_prefix = PurePosixPath(physical_path)
    logical_prefix = PurePosixPath(normalized)
    return [
        StorageEntry(
            entry.name,
            (
                logical_prefix / PurePosixPath(entry.path).relative_to(physical_prefix)
            ).as_posix(),
            entry.kind,
            entry.size_bytes,
        )
        for entry in entries
    ]


def member_storage_path_allowed(user_id: UUID, physical_path: str) -> bool:
    normalized = PurePosixPath(physical_path.strip().replace("\\", "/"))
    parts = normalized.parts
    return (len(parts) >= 2 and parts[:2] == ("users", str(user_id))) or (
        len(parts) >= 1 and parts[0] == "shared"
    )


def member_workspace_path(user_id: UUID, logical_path: str) -> str:
    """Map only the mutable virtual Home/Workspace subtree."""
    normalized = logical_path.strip().replace("\\", "/").strip("/")
    path = PurePosixPath(normalized)
    if (
        len(path.parts) < 2
        or tuple(part.lower() for part in path.parts[:2]) != ("home", "workspace")
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise StoragePolicyError("workspace paths must stay inside Home/Workspace")
    return PurePosixPath("users", str(user_id), "Workspace", *path.parts[2:]).as_posix()


def create_workspace_directory(root: Path, user_id: UUID, logical_path: str) -> None:
    physical_path = PurePosixPath(member_workspace_path(user_id, logical_path))
    if len(physical_path.parts) <= 3:
        raise StoragePolicyError("choose a new directory inside Home/Workspace")
    parent = resolve_storage_path(root, physical_path.parent.as_posix())
    if not parent.is_dir():
        raise StoragePolicyError("workspace parent must be a directory")
    name = physical_path.name
    if (
        name.startswith(".")
        or len(name) > 100
        or any(ord(character) < 32 for character in name)
    ):
        raise StoragePolicyError("workspace directory name is not allowed")
    target = parent / name
    try:
        target.mkdir()
    except FileExistsError as error:
        raise StoragePolicyError("workspace path already exists") from error
    except OSError as error:
        raise StoragePolicyError("workspace directory could not be created") from error


def workspace_upload_target(
    root: Path,
    user_id: UUID,
    logical_directory: str,
    filename: str,
) -> Path:
    physical_directory = member_workspace_path(user_id, logical_directory)
    directory = resolve_storage_path(root, physical_directory)
    if not directory.is_dir():
        raise StoragePolicyError("workspace upload destination must be a directory")
    if (
        not filename
        or filename in {".", ".."}
        or filename.startswith(".")
        or len(filename) > 200
        or "/" in filename
        or "\\" in filename
        or any(ord(character) < 32 for character in filename)
    ):
        raise StoragePolicyError("workspace filename is not allowed")
    target = directory / filename
    if target.exists():
        raise StoragePolicyError("workspace file already exists")
    return target


def _workspace_entry(root: Path, user_id: UUID, logical_path: str) -> Path:
    physical_path = member_workspace_path(user_id, logical_path)
    workspace_root = resolve_storage_path(
        root, PurePosixPath("users", str(user_id), "Workspace").as_posix()
    )
    target = resolve_storage_path(root, physical_path)
    if target == workspace_root:
        raise StoragePolicyError("the Workspace root cannot be changed")
    return target


def _workspace_destination(root: Path, user_id: UUID, logical_path: str) -> Path:
    physical_path = PurePosixPath(member_workspace_path(user_id, logical_path))
    if len(physical_path.parts) <= 3:
        raise StoragePolicyError("choose a destination inside Home/Workspace")
    parent = resolve_storage_path(root, physical_path.parent.as_posix())
    if not parent.is_dir():
        raise StoragePolicyError("workspace destination must be inside a directory")
    name = physical_path.name
    if (
        name.startswith(".")
        or len(name) > 200
        or any(ord(character) < 32 for character in name)
    ):
        raise StoragePolicyError("workspace destination name is not allowed")
    target = parent / name
    if target.exists():
        raise StoragePolicyError("workspace destination already exists")
    return target


def workspace_entry_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_symlink():
            raise StoragePolicyError("workspace trees cannot contain symbolic links")
        if item.is_file():
            total += item.stat().st_size
    return total


def move_workspace_entry(
    root: Path, user_id: UUID, source_path: str, destination_path: str
) -> None:
    source = _workspace_entry(root, user_id, source_path)
    destination = _workspace_destination(root, user_id, destination_path)
    if source.is_dir() and destination.parent.is_relative_to(source):
        raise StoragePolicyError("a directory cannot be moved inside itself")
    try:
        source.rename(destination)
    except OSError as error:
        raise StoragePolicyError("workspace entry could not be moved") from error


def copy_workspace_entry(
    root: Path,
    user_id: UUID,
    source_path: str,
    destination_path: str,
    *,
    max_bytes: int,
) -> int:
    source = _workspace_entry(root, user_id, source_path)
    destination = _workspace_destination(root, user_id, destination_path)
    if source.is_dir() and destination.parent.is_relative_to(source):
        raise StoragePolicyError("a directory cannot be copied inside itself")
    size = workspace_entry_size(source)
    if size > max_bytes:
        raise StoragePolicyError("workspace copy exceeds the configured limit")
    try:
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
    except OSError as error:
        if destination.is_dir():
            shutil.rmtree(destination, ignore_errors=True)
        else:
            destination.unlink(missing_ok=True)
        raise StoragePolicyError("workspace entry could not be copied") from error
    return size


def delete_workspace_entry(root: Path, user_id: UUID, logical_path: str) -> int:
    target = _workspace_entry(root, user_id, logical_path)
    size = workspace_entry_size(target)
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as error:
        raise StoragePolicyError("workspace entry could not be deleted") from error
    return size


def storage_file_reference(root: Path, relative_path: str) -> StorageInputReference:
    path = resolve_storage_path(root, relative_path)
    if not path.is_file():
        raise StoragePolicyError("storage input must be a regular file")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    if size == 0:
        raise StoragePolicyError("storage input cannot be empty")
    return StorageInputReference(
        storage_id=STORAGE_ID,
        path=PurePosixPath(relative_path).as_posix(),
        sha256=digest.hexdigest(),
        size_bytes=size,
    )


def package_storage_project(
    root: Path,
    relative_path: str,
    upload_directory: Path,
    max_archive_bytes: int,
) -> UploadedProjectReference:
    source = resolve_storage_path(root, relative_path)
    if not source.is_dir():
        raise StoragePolicyError("project path must be a directory")
    files = sorted(path for path in source.rglob("*") if path.is_file())
    if not files:
        raise StoragePolicyError("project directory cannot be empty")
    if len(files) > MAX_PROJECT_FILES:
        raise StoragePolicyError(
            f"project contains more than {MAX_PROJECT_FILES} files"
        )
    if any(path.is_symlink() for path in source.rglob("*")):
        raise StoragePolicyError("project directory cannot contain symbolic links")
    expanded = sum(path.stat().st_size for path in files)
    if expanded > MAX_EXPANDED_PROJECT_BYTES:
        raise StoragePolicyError("expanded project exceeds the 100 MiB safety limit")

    upload_id: UUID = uuid4()
    upload_directory.mkdir(parents=True, exist_ok=True)
    target = upload_directory / f"{upload_id}.zip"
    temporary = upload_directory / f".{upload_id}.part"
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for path in files:
                archive.write(path, path.relative_to(source).as_posix())
        size = temporary.stat().st_size
        if size > max_archive_bytes:
            raise StoragePolicyError(
                f"compressed project exceeds the {max_archive_bytes // 1024} KB limit"
            )
        validate_project_archive(temporary)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        os.replace(temporary, target)
    except (BatchScriptError, OSError, zipfile.BadZipFile) as error:
        if isinstance(error, StoragePolicyError):
            raise
        raise StoragePolicyError(str(error)) from error
    finally:
        temporary.unlink(missing_ok=True)
    return UploadedProjectReference(
        upload_id=upload_id,
        sha256=digest,
        size_bytes=size,
    )


def inspect_storage_project(
    root: Path,
    relative_path: str,
    entrypoint: str,
) -> tuple[ParsedBatchScript, list[str]]:
    """Inspect a storage project for UI review without executing its contents."""
    source = resolve_storage_path(root, relative_path)
    if not source.is_dir():
        raise StoragePolicyError("project path must be a directory")
    try:
        entrypoint_path = resolve_storage_path(source, entrypoint)
    except StoragePolicyError as error:
        raise StoragePolicyError(f"invalid project entrypoint: {error}") from error
    if not entrypoint_path.is_file():
        raise StoragePolicyError("project entrypoint must be a regular file")
    try:
        parsed = parse_batch_script(entrypoint_path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as error:
        raise StoragePolicyError("batch entrypoint must be UTF-8 text") from error
    except BatchScriptError as error:
        raise StoragePolicyError(str(error)) from error
    except OSError as error:
        raise StoragePolicyError("batch entrypoint could not be read") from error

    files: list[str] = []
    expanded = 0
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise StoragePolicyError("project directory cannot contain symbolic links")
        if path.is_file():
            files.append(path.relative_to(source).as_posix())
            expanded += path.stat().st_size
            if len(files) > MAX_PROJECT_FILES:
                raise StoragePolicyError(
                    f"project contains more than {MAX_PROJECT_FILES} files"
                )
            if expanded > MAX_EXPANDED_PROJECT_BYTES:
                raise StoragePolicyError(
                    "expanded project exceeds the 100 MiB safety limit"
                )
    if not files:
        raise StoragePolicyError("project directory cannot be empty")
    return parsed, files
