import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from uuid import UUID

from app.app_assets import (
    ComputeError,
    confined_directory,
    confined_file,
    copy_digest,
    fingerprint,
)
from app.app_compute import AppCompute
from app.app_identity import AppContext
from app.artifacts import job_artifact_relative_path
from contracts.models import BatchResult, JobStatus


@dataclass(frozen=True)
class ResultDownload:
    body: Iterator[bytes]
    close: Callable[[], None]


class RunResults:
    def __init__(self, compute: AppCompute) -> None:
        self.compute = compute

    def directories(
        self, context: AppContext, run_id: UUID
    ) -> list[tuple[int, Path, str, list[str]]]:
        group, installed = self.compute.run(context, run_id)
        settings = self.compute.settings
        root = settings.artifact_directory
        if settings.artifact_requires_mount:
            probe = root if root.exists() else root.parent
            if probe.stat().st_dev == Path("/").stat().st_dev:
                raise ComputeError(409, "Results are unavailable")
        directories = []
        for task in group.tasks:
            result = task.result
            if (
                task.status != JobStatus.COMPLETED
                or not isinstance(result, BatchResult)
                or result.project_sha256 != installed.workload.sha256
            ):
                raise ComputeError(409, "Results are not complete")
            names = result.output_files
            if len(set(names)) != len(names) or any(
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) is None
                or ".." in name
                for name in names
            ):
                raise ComputeError(409, "Results are not complete")
            relative = job_artifact_relative_path(
                task, group_name=group.name
            ).as_posix()
            if settings.artifact_owner_scoped:
                relative = str(context.user.id) + "/" + relative
            try:
                with confined_directory(root, relative) as directory:
                    if set(os.listdir(directory)) != set(names):
                        raise ComputeError(409, "Results are not complete")
            except FileNotFoundError:
                if names:
                    raise ComputeError(409, "Results are not complete") from None
            directories.append((task.task_index or 0, root, relative, names))
        return directories

    def published(
        self, context: AppContext, run_id: UUID
    ) -> tuple[list[tuple[int, Path, str, list[str]]], list[dict[str, Any]]]:
        database = self.compute.database
        query = "SELECT outputs_json FROM app_run_results WHERE group_id = ?"
        try:
            directories = self.directories(context, run_id)
            with database.connect() as connection:
                row = connection.execute(query, (str(run_id),)).fetchone()
            if row is None:
                outputs = []
                for index, root, directory, names in directories:
                    for name in sorted(names):
                        with confined_file(root, directory + "/" + name) as source:
                            size, digest, _ = copy_digest(
                                source, None, self.compute.settings.max_artifact_bytes
                            )
                        outputs.append(
                            {
                                "index": index,
                                "name": name,
                                "size_bytes": size,
                                "sha256": digest,
                                "download_url": (
                                    f"/api/apps/v1/runs/{run_id}/results/"
                                    f"{index}/{name}?sha256={digest}"
                                ),
                            }
                        )
                with database.connect() as connection:
                    connection.execute(
                        "INSERT OR IGNORE INTO app_run_results VALUES (?, ?)",
                        (str(run_id), json.dumps(outputs)),
                    )
                    row = connection.execute(query, (str(run_id),)).fetchone()
        except (OSError, ComputeError) as error:
            if isinstance(error, ComputeError) and error.status in {403, 404}:
                raise
            raise ComputeError(409, "Results are unavailable or incomplete") from error
        return directories, json.loads(row["outputs_json"])

    def manifest(self, context: AppContext, run_id: UUID) -> dict[str, object]:
        return {"outputs": self.published(context, run_id)[1]}

    def download(
        self, context: AppContext, run_id: UUID, index: int, name: str, digest: str
    ) -> ResultDownload:
        stack = ExitStack()
        try:
            directories, outputs = self.published(context, run_id)
            recorded = next(
                (
                    item["sha256"]
                    for item in outputs
                    if item["index"] == index and item["name"] == name
                ),
                None,
            )
            if recorded is None:
                raise ComputeError(404, "Result not found")
            selected: BinaryIO | None = None
            for task_index, root, directory, names in directories:
                if index == task_index and name in names:
                    selected = stack.enter_context(
                        confined_file(root, directory + "/" + name)
                    )
                    break
            if selected is None:
                raise ComputeError(404, "Result not found")
            _, actual, stamp = copy_digest(
                selected, None, self.compute.settings.max_artifact_bytes
            )
            if actual != digest or actual != recorded:
                raise ComputeError(409, "Result changed after the manifest was read")
            selected.seek(0)
        except BaseException:
            stack.close()
            raise

        def chunks() -> Iterator[bytes]:
            with stack:
                if fingerprint(selected) != stamp:
                    raise ComputeError(409, "Result changed before streaming")
                yield from iter(lambda: selected.read(1024 * 1024), b"")

        return ResultDownload(chunks(), stack.close)
