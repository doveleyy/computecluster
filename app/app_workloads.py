import json
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from app.app_assets import ComputeError, confined_file, copy_digest
from app.app_contracts import ContractError, ReleaseManifest, Workload, parse_document
from app.batch_script import (
    BatchScriptError,
    compile_batch_submission,
    parse_batch_project,
    validate_project_archive,
)
from app.database import Database
from contracts.models import (
    BatchSubmissionCreate,
    UploadedInputReference,
    UploadedProjectReference,
)

MAX_INPUT_BYTES = 20 * 1024**2


@dataclass(frozen=True)
class InstalledWorkload:
    id: UUID
    app_id: str
    release_sha256: str
    workload: Workload
    object_key: UUID
    inputs: tuple[str, ...]
    release: ReleaseManifest

    def public(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "release_sha256": self.release_sha256,
            "workload_id": self.workload.id,
            "inputs": list(self.inputs),
        }


class WorkloadStore:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.root = database.path.parent / "app-compute"

    def release(self, app_id: str, digest: str) -> ReleaseManifest:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT manifest_bytes FROM app_releases WHERE app_id = ? "
                "AND manifest_sha256 = ?",
                (app_id, digest),
            ).fetchone()
        if row is None:
            raise ContractError("Release is not recorded")
        return parse_document(ReleaseManifest, bytes(row["manifest_bytes"]))

    def get(self, app_id: str, installation_id: UUID) -> InstalledWorkload:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM app_workloads WHERE id = ? AND app_id = ?",
                (str(installation_id), app_id),
            ).fetchone()
        if row is None:
            raise ComputeError(404, "Workload not found")
        release = self.release(app_id, row["release_sha256"])
        workload = next(
            item for item in release.workloads if item.id == row["workload_id"]
        )
        return InstalledWorkload(
            installation_id,
            app_id,
            row["release_sha256"],
            workload,
            UUID(row["object_key"]),
            tuple(json.loads(row["inputs_json"])),
            release,
        )

    def list(self, app_id: str) -> list[InstalledWorkload]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM app_workloads WHERE app_id = ? ORDER BY id", (app_id,)
            ).fetchall()
        return [self.get(app_id, UUID(row["id"])) for row in rows]

    def install(
        self, app_id: str, release_sha256: str, workload_id: str, archive: Path
    ) -> InstalledWorkload:
        release = self.release(app_id, release_sha256)
        workload = next(
            (item for item in release.workloads if item.id == workload_id), None
        )
        if workload is None:
            raise ContractError("Workload is not declared by this release")
        directory = self.root / "workloads"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        object_key = uuid4()
        target = directory / str(object_key)
        retained = False
        try:
            with archive.open("rb") as source, target.open("xb") as output:
                size, digest, _ = copy_digest(source, output, MAX_INPUT_BYTES)
            if digest != workload.sha256:
                raise ContractError(
                    "Workload archive digest does not match the release"
                )
            validate_project_archive(target)
            with zipfile.ZipFile(target) as package:
                names: set[str] = set()
                for item in package.infolist():
                    name = str(PurePosixPath(item.filename.rstrip("/")))
                    kind = stat.S_IFMT(item.external_attr >> 16)
                    if (
                        name.casefold() in names
                        or kind not in {0, stat.S_IFREG, stat.S_IFDIR}
                        or item.orig_filename != item.filename
                        or item.filename.rstrip("/") != name
                    ):
                        raise ContractError(
                            "Workload ZIP has duplicate or unsafe entries"
                        )
                    names.add(name.casefold())
                if package.testzip() is not None:
                    raise ContractError("Workload ZIP content is corrupt")
            parsed = parse_batch_project(target, workload.entrypoint)
            if parsed.worker_id is not None or parsed.runtime != workload.runtime:
                raise ContractError(
                    "Workload runtime or worker targeting is not permitted"
                )
            inputs = tuple(item.name for item in parsed.inputs)
            compile_batch_submission(
                BatchSubmissionCreate(
                    project=UploadedProjectReference(
                        upload_id=object_key, size_bytes=size, sha256=digest
                    ),
                    entrypoint=workload.entrypoint,
                    inputs={
                        name: UploadedInputReference(
                            upload_id=uuid4(), sha256="0" * 64, size_bytes=1
                        )
                        for name in inputs
                    },
                ),
                target,
            )
            installation_id = uuid4()
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                previous = connection.execute(
                    "SELECT id FROM app_workloads WHERE app_id = ? "
                    "AND release_sha256 = ? AND workload_id = ?",
                    (app_id, release_sha256, workload_id),
                ).fetchone()
                if previous is not None:
                    installation_id = UUID(previous["id"])
                else:
                    connection.execute(
                        "INSERT INTO app_workloads (id, app_id, release_sha256, "
                        "workload_id, object_key, inputs_json) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            str(installation_id),
                            app_id,
                            release_sha256,
                            workload_id,
                            str(object_key),
                            json.dumps(inputs),
                        ),
                    )
                    retained = True
            return self.get(app_id, installation_id)
        except (BatchScriptError, ComputeError) as error:
            raise ContractError(str(error)) from error
        except (zipfile.BadZipFile, NotImplementedError) as error:
            raise ContractError(
                "Workload ZIP content is invalid or unsupported"
            ) from error
        finally:
            if not retained:
                target.unlink(missing_ok=True)

    def copy_project(
        self, installed: InstalledWorkload, target: Path, upload_id: UUID
    ) -> UploadedProjectReference:
        with (
            confined_file(self.root / "workloads", str(installed.object_key)) as source,
            target.open("xb") as output,
        ):
            size, digest, _ = copy_digest(source, output, MAX_INPUT_BYTES)
        if digest != installed.workload.sha256:
            raise ComputeError(409, "Installed workload is unavailable")
        return UploadedProjectReference(
            upload_id=upload_id, sha256=digest, size_bytes=size
        )
