from __future__ import annotations

import hashlib
import re
import shlex
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from contracts.models import (
    BatchParameters,
    BatchSubmissionCreate,
    JobCreate,
    JobGroupCreate,
    JobGroupTaskCreate,
    SubmittableJobType,
)

MAX_PROJECT_FILES = 1000
MAX_EXPANDED_PROJECT_BYTES = 100 * 1024**2
SUPPORTED_RUNTIME = "scientific-python:1"


class BatchScriptError(ValueError):
    pass


@dataclass(frozen=True)
class BatchInputDeclaration:
    name: str
    default_path: str | None


@dataclass(frozen=True)
class ParsedBatchScript:
    name: str
    runtime: str
    cpu_limit: float
    memory_mb: int
    timeout_seconds: int
    array_start: int
    array_end: int
    environment: dict[str, str]
    inputs: tuple[BatchInputDeclaration, ...]
    worker_id: str | None


def validate_project_archive(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not entries:
                raise BatchScriptError("project archive cannot be empty")
            if len(entries) > MAX_PROJECT_FILES:
                raise BatchScriptError(
                    f"project archive contains more than {MAX_PROJECT_FILES} entries"
                )
            expanded = 0
            for entry in entries:
                _validate_archive_name(entry.filename)
                if entry.flag_bits & 0x1:
                    raise BatchScriptError("encrypted project entries are not allowed")
                # Unix file-type bits identify symlinks without extracting them.
                if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise BatchScriptError("project archive cannot contain symlinks")
                expanded += entry.file_size
                if expanded > MAX_EXPANDED_PROJECT_BYTES:
                    raise BatchScriptError(
                        "expanded project exceeds the 100 MiB safety limit"
                    )
    except zipfile.BadZipFile as error:
        raise BatchScriptError("project must be a valid ZIP archive") from error


def compile_batch_submission(
    submission: BatchSubmissionCreate,
    archive_path: Path,
) -> JobGroupCreate:
    _verify_reference(archive_path, submission)
    validate_project_archive(archive_path)
    entrypoint = _normalized_project_path(submission.entrypoint)
    parsed = parse_batch_project(archive_path, entrypoint)
    if parsed.runtime != SUPPORTED_RUNTIME:
        raise BatchScriptError(
            f"runtime {parsed.runtime!r} is unavailable; use {SUPPORTED_RUNTIME!r}"
        )
    target_worker = submission.target_worker_id or parsed.worker_id
    declared_inputs = {item.name for item in parsed.inputs}
    supplied_inputs = set(submission.inputs)
    if declared_inputs != supplied_inputs:
        missing = sorted(declared_inputs - supplied_inputs)
        unexpected = sorted(supplied_inputs - declared_inputs)
        details = []
        if missing:
            details.append(f"missing bindings: {', '.join(missing)}")
        if unexpected:
            details.append(f"undeclared bindings: {', '.join(unexpected)}")
        raise BatchScriptError(
            "input bindings do not match #HP declarations; " + "; ".join(details)
        )
    tasks = []
    for array_index in range(parsed.array_start, parsed.array_end + 1):
        parameters = BatchParameters(
            project=submission.project,
            entrypoint=entrypoint,
            runtime=parsed.runtime,
            timeout_seconds=parsed.timeout_seconds,
            cpu_limit=parsed.cpu_limit,
            memory_mb=parsed.memory_mb,
            array_index=array_index,
            environment=parsed.environment,
            inputs=submission.inputs,
        )
        tasks.append(
            JobGroupTaskCreate(
                task_id=str(array_index),
                job=JobCreate(
                    name=parsed.name,
                    type=SubmittableJobType.BATCH,
                    parameters=parameters,
                    target_worker_id=target_worker,
                ),
            )
        )
    return JobGroupCreate(name=parsed.name, tasks=tasks)


def parse_batch_script(script: str) -> ParsedBatchScript:
    lines = script.replace("\r\n", "\n").splitlines()
    if not lines or lines[0] != "#!/usr/bin/env bash":
        raise BatchScriptError("line 1 must be exactly #!/usr/bin/env bash")

    singletons: dict[str, str] = {}
    environment: dict[str, str] = {}
    declared_inputs: list[BatchInputDeclaration] = []
    executable_seen = False
    allowed_singletons = {
        "--version",
        "--name",
        "--runtime",
        "--cpus",
        "--memory-mb",
        "--time-limit",
        "--array",
        "--worker",
    }
    for line_number, line in enumerate(lines[1:], start=2):
        stripped = line.strip()
        if not stripped or (
            stripped.startswith("#") and not stripped.startswith("#HP")
        ):
            continue
        if not stripped.startswith("#HP"):
            executable_seen = True
            continue
        if executable_seen:
            raise BatchScriptError(
                f"#HP directive appears after the body on line {line_number}"
            )
        try:
            words = shlex.split(stripped, comments=False, posix=True)
        except ValueError as error:
            raise BatchScriptError(
                f"invalid quoting on line {line_number}: {error}"
            ) from error
        if len(words) != 3 or words[0] != "#HP":
            raise BatchScriptError(f"invalid #HP directive on line {line_number}")
        option, value = words[1], words[2]
        if option == "--env":
            if "=" not in value:
                raise BatchScriptError(
                    f"--env requires KEY=VALUE on line {line_number}"
                )
            key, item = value.split("=", 1)
            if key in environment:
                raise BatchScriptError(f"duplicate environment key {key!r}")
            environment[key] = item
        elif option == "--input":
            name, separator, raw_path = value.partition("=")
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,63}", name) is None:
                raise BatchScriptError(f"invalid input name {name!r}")
            if any(item.name == name for item in declared_inputs):
                raise BatchScriptError(f"duplicate input name {name!r}")
            if len(declared_inputs) >= 32:
                raise BatchScriptError("at most 32 named inputs are allowed")
            default_path = (
                _normalized_logical_storage_path(raw_path) if separator else None
            )
            declared_inputs.append(BatchInputDeclaration(name, default_path))
        elif option == "--after-success":
            raise BatchScriptError(
                f"{option} is planned but not implemented in this release"
            )
        elif option not in allowed_singletons:
            raise BatchScriptError(
                f"unknown directive {option!r} on line {line_number}"
            )
        elif option in singletons:
            raise BatchScriptError(f"duplicate directive {option!r}")
        else:
            singletons[option] = value

    required = {
        "--version",
        "--name",
        "--runtime",
        "--cpus",
        "--memory-mb",
        "--time-limit",
        "--array",
    }
    missing = sorted(required - singletons.keys())
    if missing:
        raise BatchScriptError(f"missing required directives: {', '.join(missing)}")
    if singletons["--version"] != "1":
        raise BatchScriptError("only #HP --version 1 is supported")

    start, end = _parse_array(singletons["--array"])
    try:
        cpu_limit = float(singletons["--cpus"])
        memory_mb = int(singletons["--memory-mb"])
        timeout_seconds = _parse_time_limit(singletons["--time-limit"])
        # Reuse the wire model for bounds and environment validation.
        BatchParameters.model_validate(
            {
                "project": {
                    "upload_id": "00000000-0000-0000-0000-000000000000",
                    "sha256": "0" * 64,
                    "size_bytes": 1,
                },
                "entrypoint": "submit.hp",
                "runtime": singletons["--runtime"],
                "timeout_seconds": timeout_seconds,
                "cpu_limit": cpu_limit,
                "memory_mb": memory_mb,
                "array_index": start,
                "environment": environment,
                "inputs": {},
            }
        )
    except ValueError as error:
        raise BatchScriptError(
            f"invalid batch resource or environment value: {error}"
        ) from error
    name = singletons["--name"].strip()
    if not name or len(name) > 100 or any(ord(character) < 32 for character in name):
        raise BatchScriptError("--name must contain 1-100 characters without controls")
    worker_id = singletons.get("--worker")
    if (
        worker_id is not None
        and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", worker_id) is None
    ):
        raise BatchScriptError("--worker contains an invalid worker ID")
    return ParsedBatchScript(
        name=name,
        runtime=singletons["--runtime"],
        cpu_limit=cpu_limit,
        memory_mb=memory_mb,
        timeout_seconds=timeout_seconds,
        array_start=start,
        array_end=end,
        environment=environment,
        inputs=tuple(declared_inputs),
        worker_id=worker_id,
    )


def parse_batch_project(archive_path: Path, entrypoint: str) -> ParsedBatchScript:
    """Read and validate one batch entrypoint without executing project code."""
    normalized_entrypoint = _normalized_project_path(entrypoint)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            try:
                script_bytes = archive.read(normalized_entrypoint)
            except KeyError as error:
                raise BatchScriptError(
                    f"entrypoint {normalized_entrypoint!r} is not present "
                    "in the project"
                ) from error
    except zipfile.BadZipFile as error:
        raise BatchScriptError("project must be a valid ZIP archive") from error
    try:
        script = script_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BatchScriptError("batch entrypoint must be UTF-8 text") from error
    return parse_batch_script(script)


def _parse_array(value: str) -> tuple[int, int]:
    pieces = value.split("-", 1)
    if len(pieces) != 2:
        raise BatchScriptError("--array must use the inclusive START-END form")
    try:
        start, end = (int(piece) for piece in pieces)
    except ValueError as error:
        raise BatchScriptError("--array bounds must be integers") from error
    if start < 1 or end < start:
        raise BatchScriptError("--array requires 1 <= START <= END")
    if end - start + 1 > 1000:
        raise BatchScriptError("--array may create at most 1000 children")
    return start, end


def _parse_time_limit(value: str) -> int:
    pieces = value.split(":")
    if len(pieces) != 3 or any(len(piece) != 2 for piece in pieces[1:]):
        raise BatchScriptError("--time-limit must use HH:MM:SS")
    try:
        hours, minutes, seconds = (int(piece) for piece in pieces)
    except ValueError as error:
        raise BatchScriptError("--time-limit must contain integers") from error
    if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        raise BatchScriptError("--time-limit contains an invalid time")
    total = hours * 3600 + minutes * 60 + seconds
    if not 1 <= total <= 7 * 24 * 3600:
        raise BatchScriptError("--time-limit must be between one second and seven days")
    return total


def _normalized_project_path(value: str) -> str:
    if "\\" in value:
        raise BatchScriptError("project paths must use POSIX forward slashes")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BatchScriptError("entrypoint must be a normalized project-relative path")
    return path.as_posix()


def _normalized_logical_storage_path(value: str) -> str:
    if not value or len(value) > 500 or "\\" in value:
        raise BatchScriptError(
            "default #HP input path must be a normalized Home/... or Shared/... path"
        )
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or len(path.parts) < 2
        or path.parts[0] not in {"Home", "Shared"}
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise BatchScriptError(
            "default #HP input path must be a normalized Home/... or Shared/... path"
        )
    return path.as_posix()


def _validate_archive_name(value: str) -> None:
    normalized = value.rstrip("/")
    if not normalized:
        return
    _normalized_project_path(normalized)


def _verify_reference(path: Path, submission: BatchSubmissionCreate) -> None:
    if not path.is_file():
        raise BatchScriptError("uploaded project not found")
    if path.stat().st_size != submission.project.size_bytes:
        raise BatchScriptError("project size does not match its upload reference")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != submission.project.sha256:
        raise BatchScriptError("project digest does not match its upload reference")
