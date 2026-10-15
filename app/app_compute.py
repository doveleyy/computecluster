import hashlib
import json
import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.app_assets import ComputeError, confined_directory, confined_file, copy_digest
from app.app_identity import AppContext
from app.app_origin import AppOrigin, AppReplayConflict
from app.app_workloads import MAX_INPUT_BYTES, InstalledWorkload, WorkloadStore
from app.batch_script import compile_batch_submission
from app.config import Settings
from app.database import Database
from app.job_http import release_job_uploads
from app.service import JobService, JobTransitionError, SchedulingCapacityError
from app.storage import member_storage_path
from contracts.models import (
    BatchInputSource,
    BatchSubmissionCreate,
    FailureKind,
    JobGroupRead,
    JobStatus,
    UploadedInputReference,
)

MAX_OWNER_INPUT_BYTES = 200 * 1024**2
MAX_TOTAL_INPUT_BYTES = 1024**3
MAX_OWNER_UPLOADS = 100
MAX_OWNER_HANDLES = 100
MAX_OUTSTANDING_RUNS = 4
MAX_OWNER_OUTSTANDING_RUNS = 2


def reconcile_app_inputs(database: Database) -> None:
    root = database.path.parent / "app-compute" / "inputs"
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for reservation in connection.execute(
            "SELECT paths_json FROM app_staging_reservations"
        ):
            for name in json.loads(reservation["paths_json"]):
                Path(name).unlink(missing_ok=True)
        connection.execute("DELETE FROM app_staging_reservations")
        retained = {
            row["object_key"]
            for row in connection.execute(
                "SELECT object_key FROM app_inputs WHERE object_key IS NOT NULL "
                "AND deleted_at IS NULL"
            )
        }
        if root.exists():
            for path in root.iterdir():
                try:
                    object_key = str(UUID(path.name))
                except ValueError:
                    continue
                if object_key == path.name and object_key not in retained:
                    path.unlink()
        connection.execute("DELETE FROM app_input_reservations")


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    installation_id: UUID
    inputs: dict[
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")],
        UUID,
    ] = Field(max_length=32)


class FileHandleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str = Field(min_length=1, max_length=500)


@dataclass(frozen=True)
class AppInput:
    id: UUID
    object_key: UUID | None
    logical_path: str | None
    fingerprint: str | None
    size_bytes: int
    sha256: str

    def public(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


def run_view(group: JobGroupRead, installation_id: UUID) -> dict[str, object]:
    tasks = [
        {
            "index": task.task_index,
            "status": "CANCELLED"
            if task.status == JobStatus.FAILED
            and task.failure_kind == FailureKind.CANCELLED_BY_USER
            else task.status.value,
        }
        for task in group.tasks
    ]
    statuses = {task["status"] for task in tasks}
    if statuses == {"QUEUED"}:
        status = "QUEUED"
    elif statuses == {"COMPLETED"}:
        status = "COMPLETED"
    elif statuses & {"RUNNING", "QUEUED"}:
        status = "RUNNING"
    elif "FAILED" in statuses:
        status = "FAILED"
    else:
        status = "CANCELLED"
    return {
        "id": str(group.id),
        "installation_id": str(installation_id),
        "status": status,
        "tasks": tasks,
    }


class AppCompute:
    def __init__(self, jobs: JobService, settings: Settings) -> None:
        self.jobs = jobs
        self.settings = settings
        self.database = jobs.repository.database
        self.workloads = WorkloadStore(self.database)

    @staticmethod
    def grant(
        context: AppContext, capability: str, installed: InstalledWorkload | None = None
    ) -> None:
        if capability not in context.app.registration.capabilities or (
            installed is not None and capability not in installed.release.capabilities
        ):
            raise ComputeError(403, "Application capability is not granted")

    def installed(
        self, context: AppContext, installation_id: UUID
    ) -> InstalledWorkload:
        self.grant(context, "jobs")
        installed = self.workloads.get(context.app.app_id, installation_id)
        self.grant(context, "jobs", installed)
        return installed

    def storage_access(
        self, context: AppContext, installed: InstalledWorkload | None = None
    ) -> None:
        self.grant(context, "files", installed)
        if not (
            self.settings.member_storage_enabled
            or context.user.id in self.settings.member_storage_user_ids
        ):
            raise ComputeError(403, "Member storage is not enabled")

    def logical_path(self, context: AppContext, logical: str) -> str:
        parts = logical.split("/")
        if (
            parts[0] not in {"Home", "Shared"}
            or any(part in {"", ".", ".."} for part in parts)
            or "\\" in logical
            or "\x00" in logical
        ):
            raise ComputeError(422, "Path must be inside Home or Shared")
        return member_storage_path(context.user.id, logical)

    def files(
        self, context: AppContext, path: str, limit: int, offset: int
    ) -> dict[str, object]:
        self.storage_access(context)
        physical = self.logical_path(context, path)
        entries: list[dict[str, object]] = []
        try:
            with confined_directory(
                self.settings.storage_directory, physical
            ) as directory:
                for name in sorted(os.listdir(directory)):
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode):
                        entries.append(
                            {
                                "name": name,
                                "path": path + "/" + name,
                                "kind": "file"
                                if stat.S_ISREG(info.st_mode)
                                else "directory",
                                "size_bytes": info.st_size
                                if stat.S_ISREG(info.st_mode)
                                else None,
                            }
                        )
        except OSError as error:
            raise ComputeError(404, "Directory not found") from error
        return {"items": entries[offset : offset + limit]}

    def reserve_input(self, context: AppContext, installation_id: UUID) -> UUID:
        self.installed(context, installation_id)
        reservation = uuid4()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner = (context.app.app_id, str(context.user.id))
            retained = (
                "(SELECT app_id, owner_user_id, size_bytes FROM app_inputs "
                "WHERE object_key IS NOT NULL AND deleted_at IS NULL UNION ALL "
                "SELECT app_id, owner_user_id, size_bytes FROM app_input_reservations)"
            )
            count, used = connection.execute(
                f"SELECT count(*), coalesce(sum(size_bytes), 0) FROM {retained} "
                "WHERE app_id = ? AND owner_user_id = ?",
                owner,
            ).fetchone()
            total = connection.execute(
                f"SELECT coalesce(sum(size_bytes), 0) FROM {retained}"
            ).fetchone()[0]
            if (
                count >= MAX_OWNER_UPLOADS
                or used + MAX_INPUT_BYTES > MAX_OWNER_INPUT_BYTES
                or total + MAX_INPUT_BYTES > MAX_TOTAL_INPUT_BYTES
            ):
                raise ComputeError(413, "Retained input quota exhausted")
            connection.execute(
                "INSERT INTO app_input_reservations VALUES (?, ?, ?, ?)",
                (str(reservation), *owner, MAX_INPUT_BYTES),
            )
        return reservation

    def discard_reservation(self, reservation: UUID) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM app_input_reservations WHERE id = ?", (str(reservation),)
            )

    def save_input(
        self, context: AppContext, installation_id: UUID, item: AppInput
    ) -> AppInput:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if item.object_key is None:
                handles = connection.execute(
                    "SELECT count(*) FROM app_inputs WHERE app_id = ? "
                    "AND owner_user_id = ? AND logical_path IS NOT NULL "
                    "AND deleted_at IS NULL",
                    (context.app.app_id, str(context.user.id)),
                ).fetchone()[0]
                if handles >= MAX_OWNER_HANDLES:
                    raise ComputeError(413, "Retained input quota exhausted")
            elif (
                connection.execute(
                    "DELETE FROM app_input_reservations WHERE id = ? "
                    "AND app_id = ? AND owner_user_id = ?",
                    (str(item.id), context.app.app_id, str(context.user.id)),
                ).rowcount
                != 1
            ):
                raise RuntimeError("Input has no quota reservation")
            connection.execute(
                "INSERT INTO app_inputs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    str(item.id),
                    context.app.app_id,
                    str(context.user.id),
                    str(installation_id),
                    str(item.object_key) if item.object_key else None,
                    item.logical_path,
                    item.fingerprint,
                    item.size_bytes,
                    item.sha256,
                ),
            )
        return item

    def file_handle(
        self, context: AppContext, installation_id: UUID, logical: str
    ) -> AppInput:
        installed = self.installed(context, installation_id)
        self.storage_access(context, installed)
        physical = self.logical_path(context, logical)
        try:
            with confined_file(self.settings.storage_directory, physical) as source:
                size, digest, stamp = copy_digest(source, None, MAX_INPUT_BYTES)
        except OSError as error:
            raise ComputeError(404, "File not found") from error
        if size == 0:
            raise ComputeError(422, "Input cannot be empty")
        return self.save_input(
            context,
            installation_id,
            AppInput(uuid4(), None, logical, stamp, size, digest),
        )

    def input(
        self, context: AppContext, installation_id: UUID, input_id: UUID
    ) -> AppInput:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM app_inputs WHERE id = ? AND app_id = ? "
                "AND owner_user_id = ? AND installation_id = ? "
                "AND deleted_at IS NULL",
                (
                    str(input_id),
                    context.app.app_id,
                    str(context.user.id),
                    str(installation_id),
                ),
            ).fetchone()
        if row is None:
            raise ComputeError(404, "Input not found")
        return AppInput(
            input_id,
            UUID(row["object_key"]) if row["object_key"] else None,
            row["logical_path"],
            row["fingerprint"],
            row["size_bytes"],
            row["sha256"],
        )

    def delete_input(self, context: AppContext, input_id: UUID) -> None:
        self.grant(context, "jobs")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT object_key FROM app_inputs WHERE id = ? AND app_id = ? "
                "AND owner_user_id = ?",
                (str(input_id), context.app.app_id, str(context.user.id)),
            ).fetchone()
            if row is None:
                raise ComputeError(404, "Input not found")
            connection.execute(
                "UPDATE app_inputs SET deleted_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND deleted_at IS NULL",
                (str(input_id),),
            )
        # Submission copies inputs into run staging, so no run reads these
        # bytes after it exists. Startup reconciliation removes a file left
        # behind by a crash after the tombstone commits.
        if row["object_key"] is not None:
            (self.workloads.root / "inputs" / row["object_key"]).unlink(missing_ok=True)

    def run(
        self, context: AppContext, run_id: UUID
    ) -> tuple[JobGroupRead, InstalledWorkload]:
        self.grant(context, "jobs")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT installation_id FROM app_runs WHERE group_id = ? "
                "AND app_id = ? AND owner_user_id = ?",
                (str(run_id), context.app.app_id, str(context.user.id)),
            ).fetchone()
        if row is None:
            raise ComputeError(404, "Run not found")
        installed = self.installed(context, UUID(row["installation_id"]))
        return self.jobs.get_group(run_id, str(context.user.id)), installed

    def runs(self, context: AppContext, limit: int, offset: int) -> dict[str, object]:
        self.grant(context, "jobs")
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT group_id FROM app_runs WHERE app_id = ? "
                "AND owner_user_id = ? ORDER BY created_at DESC, group_id DESC "
                "LIMIT ? OFFSET ?",
                (context.app.app_id, str(context.user.id), limit, offset),
            ).fetchall()
        return {
            "items": [
                run_view(group, installed.id)
                for group, installed in (
                    self.run(context, UUID(row["group_id"])) for row in rows
                )
            ]
        }

    def replay(
        self, context: AppContext, key: str, payload_hash: str
    ) -> dict[str, object] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT group_id, payload_sha256 FROM app_runs WHERE app_id = ? "
                "AND owner_user_id = ? AND submission_key = ?",
                (context.app.app_id, str(context.user.id), key),
            ).fetchone()
        if row is None:
            return None
        if row["payload_sha256"] != payload_hash:
            raise ComputeError(
                409, "Submission key already used for a different request"
            )
        group, installed = self.run(context, UUID(row["group_id"]))
        return run_view(group, installed.id)

    def submit(
        self, context: AppContext, body: RunRequest, key: str
    ) -> tuple[dict[str, object], bool]:
        self.grant(context, "jobs")
        payload_hash = hashlib.sha256(
            json.dumps(
                body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        previous = self.replay(context, key, payload_hash)
        if previous is not None:
            return previous, False
        installed = self.installed(context, body.installation_id)
        if set(body.inputs) != set(installed.inputs):
            raise ComputeError(422, "Input names must match the installed workload")
        selected = {
            name: self.input(context, installed.id, input_id)
            for name, input_id in body.inputs.items()
        }
        if sum(item.size_bytes for item in selected.values()) > MAX_OWNER_INPUT_BYTES:
            raise ComputeError(409, "Run input staging quota exceeded")
        reservation = uuid4()
        project_id = uuid4()
        project_dir = self.settings.upload_directory / "projects"
        project_path = project_dir / f"{project_id}.zip"
        input_dir = self.settings.upload_directory / "inputs"
        upload_ids = {name: uuid4() for name in selected}
        paths = [project_path] + [
            input_dir / f"{upload_id}.input" for upload_id in upload_ids.values()
        ]
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            concurrent_replay = connection.execute(
                "SELECT 1 FROM app_runs WHERE app_id = ? AND owner_user_id = ? "
                "AND submission_key = ?",
                (context.app.app_id, str(context.user.id), key),
            ).fetchone()
            outstanding, owned = connection.execute(
                "SELECT count(*), coalesce(sum(app_id = ? AND owner_user_id = ?), 0) "
                "FROM (SELECT app_id, owner_user_id FROM app_staging_reservations "
                "UNION ALL SELECT app_id, owner_user_id FROM app_runs r WHERE EXISTS "
                "(SELECT 1 FROM jobs j WHERE j.group_id = r.group_id "
                "AND j.status IN ('QUEUED', 'RUNNING')))",
                (context.app.app_id, str(context.user.id)),
            ).fetchone()
            if concurrent_replay is None and (
                outstanding >= MAX_OUTSTANDING_RUNS
                or owned >= MAX_OWNER_OUTSTANDING_RUNS
            ):
                raise ComputeError(409, "App run staging capacity is unavailable")
            if concurrent_replay is None:
                connection.execute(
                    "INSERT INTO app_staging_reservations VALUES (?, ?, ?, ?)",
                    (
                        str(reservation),
                        context.app.app_id,
                        str(context.user.id),
                        json.dumps([str(path.absolute()) for path in paths]),
                    ),
                )
        if concurrent_replay is not None:
            previous = self.replay(context, key, payload_hash)
            assert previous is not None
            return previous, False
        retained = False
        try:
            project_dir.mkdir(parents=True, exist_ok=True)
            project = self.workloads.copy_project(installed, project_path, project_id)
            references: dict[str, BatchInputSource] = {}
            provenance: dict[str, object] = {}
            for name, item in selected.items():
                if item.logical_path is not None:
                    self.storage_access(context, installed)
                    root = self.settings.storage_directory
                    relative = self.logical_path(context, item.logical_path)
                else:
                    root = self.workloads.root / "inputs"
                    relative = str(item.object_key)
                upload_id = upload_ids[name]
                input_dir.mkdir(parents=True, exist_ok=True)
                target = input_dir / f"{upload_id}.input"
                with (
                    confined_file(root, relative) as source,
                    target.open("xb") as output,
                ):
                    size, digest, stamp = copy_digest(source, output, MAX_INPUT_BYTES)
                if (
                    size != item.size_bytes
                    or digest != item.sha256
                    or (item.fingerprint is not None and stamp != item.fingerprint)
                ):
                    raise ComputeError(409, "Input changed after selection")
                references[name] = UploadedInputReference(
                    upload_id=upload_id, sha256=digest, size_bytes=size
                )
                provenance[name] = {
                    **item.public(),
                    "logical_path": item.logical_path,
                    "fingerprint": item.fingerprint,
                }
            create = compile_batch_submission(
                BatchSubmissionCreate(
                    project=project,
                    entrypoint=installed.workload.entrypoint,
                    inputs=references,
                ),
                project_path,
            )
            origin = AppOrigin(
                context.app.app_id,
                context.user.id,
                installed.id,
                key,
                payload_hash,
                json.dumps(provenance, sort_keys=True),
                reservation,
            )
            group = self.jobs.create_group(
                create, owner_user_id=str(context.user.id), app_origin=origin
            )
            retained = any(
                getattr(task.parameters, "project", None) == project
                for task in group.tasks
            )
            return run_view(group, installed.id), retained
        except AppReplayConflict as error:
            raise ComputeError(
                409, "Submission key already used for a different request"
            ) from error
        except SchedulingCapacityError as error:
            raise ComputeError(409, "Workload capacity is unavailable") from error
        except OSError as error:
            raise ComputeError(409, "Input or workload is unavailable") from error
        finally:
            if not retained:
                for path in paths:
                    path.unlink(missing_ok=True)
                with self.database.connect() as connection:
                    connection.execute(
                        "DELETE FROM app_staging_reservations WHERE id = ?",
                        (str(reservation),),
                    )

    def cancel(self, context: AppContext, run_id: UUID) -> dict[str, object]:
        group, installed = self.run(context, run_id)
        for task in group.tasks:
            if task.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
                with suppress(JobTransitionError):
                    self.jobs.cancel(task.id, str(context.user.id))
        current = self.jobs.get_group(run_id, str(context.user.id))
        for task in current.tasks:
            if task.status in {JobStatus.COMPLETED, JobStatus.FAILED}:
                release_job_uploads(self.settings, self.jobs, task)
        return run_view(current, installed.id)
