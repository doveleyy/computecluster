from __future__ import annotations

import json
import sqlite3
from builtins import list as list_type
from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from app.database import Database
from app.identity import ADMIN_USER_ID
from contracts.models import (
    BatchParameters,
    DatasetScriptParameters,
    FailureKind,
    JobGroupRead,
    JobParameters,
    JobRead,
    JobStatus,
    JobType,
    PythonBatchParameters,
    SleepParameters,
    WorkerMetrics,
    WorkerRead,
    WorkerState,
)


class JobRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add(
        self,
        job: JobRead,
        idempotency_key: str | None = None,
        owner_user_id: str = ADMIN_USER_ID,
    ) -> JobRead:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO jobs (
                    id, type, parameters_json, status, created_at, updated_at,
                    attempt, max_attempts, idempotency_key, name, target_worker_id,
                    group_id, task_id, task_index, owner_user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    str(job.id),
                    job.type.value,
                    json.dumps(job.parameters.model_dump(mode="json")),
                    job.status.value,
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                    job.attempt,
                    job.max_attempts,
                    idempotency_key,
                    job.name,
                    job.target_worker_id,
                    str(job.group_id) if job.group_id is not None else None,
                    job.task_id,
                    job.task_index,
                    owner_user_id,
                ),
            )
            if cursor.rowcount == 1:
                return job
            row = connection.execute(
                "SELECT * FROM jobs WHERE owner_user_id = ? AND idempotency_key = ?",
                (owner_user_id, idempotency_key),
            ).fetchone()
        if row is None:
            raise RuntimeError("idempotent insert did not return a job")
        return self._row_to_job(row)

    def add_group(
        self,
        group: JobGroupRead,
        request_json: str,
        idempotency_key: str | None,
        owner_user_id: str = ADMIN_USER_ID,
    ) -> tuple[JobGroupRead, str]:
        """Atomically insert a group and all of its schedulable child jobs."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO job_groups (
                    id, name, request_json, created_at, updated_at, idempotency_key,
                    owner_user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    str(group.id),
                    group.name,
                    request_json,
                    group.created_at.isoformat(),
                    group.updated_at.isoformat(),
                    idempotency_key,
                    owner_user_id,
                ),
            )
            if cursor.rowcount == 0:
                row = connection.execute(
                    """
                    SELECT id, request_json FROM job_groups
                    WHERE owner_user_id = ? AND idempotency_key = ?
                    """,
                    (owner_user_id, idempotency_key),
                ).fetchone()
                if row is None:
                    raise RuntimeError("idempotent group insert did not return a group")
                existing = self._get_group(connection, UUID(row["id"]), owner_user_id)
                if existing is None:
                    raise RuntimeError("stored group has no readable record")
                return existing, str(row["request_json"])

            for job in group.tasks:
                connection.execute(
                    """
                    INSERT INTO jobs (
                        id, type, parameters_json, status, created_at, updated_at,
                        attempt, max_attempts, name, target_worker_id,
                        group_id, task_id, task_index, owner_user_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(job.id),
                        job.type.value,
                        json.dumps(job.parameters.model_dump(mode="json")),
                        job.status.value,
                        job.created_at.isoformat(),
                        job.updated_at.isoformat(),
                        job.attempt,
                        job.max_attempts,
                        job.name,
                        job.target_worker_id,
                        str(group.id),
                        job.task_id,
                        job.task_index,
                        owner_user_id,
                    ),
                )
        return group, request_json

    def get_group(
        self, group_id: UUID, owner_user_id: str | None = None
    ) -> JobGroupRead | None:
        with self.database.connect() as connection:
            return self._get_group(connection, group_id, owner_user_id)

    def list_groups(self, owner_user_id: str | None = None) -> list[JobGroupRead]:
        with self.database.connect() as connection:
            if owner_user_id is None:
                rows = connection.execute(
                    "SELECT id FROM job_groups ORDER BY created_at DESC, id DESC"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT id FROM job_groups WHERE owner_user_id = ?
                    ORDER BY created_at DESC, id DESC
                    """,
                    (owner_user_id,),
                ).fetchall()
            groups = [
                self._get_group(connection, UUID(row["id"]), owner_user_id)
                for row in rows
            ]
        return [group for group in groups if group is not None]

    def get(self, job_id: UUID, owner_user_id: str | None = None) -> JobRead | None:
        with self.database.connect() as connection:
            if owner_user_id is None:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE id = ?", (str(job_id),)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE id = ? AND owner_user_id = ?",
                    (str(job_id), owner_user_id),
                ).fetchone()
        return self._row_to_job(row) if row is not None else None

    def owner_user_id(self, job_id: UUID) -> str | None:
        """Return the immutable owner used to place one job's artifacts."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT owner_user_id FROM jobs WHERE id = ?", (str(job_id),)
            ).fetchone()
        return str(row["owner_user_id"]) if row is not None else None

    def list(self, owner_user_id: str | None = None) -> list[JobRead]:
        with self.database.connect() as connection:
            if owner_user_id is None:
                rows = connection.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC, id DESC"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM jobs WHERE owner_user_id = ?
                    ORDER BY created_at DESC, id DESC
                    """,
                    (owner_user_id,),
                ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def ping(self) -> None:
        self.database.ping()

    def worker_exists(self, worker_id: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM workers WHERE id = ?", (worker_id,)
            ).fetchone()
        return row is not None

    def get_worker(self, worker_id: str, stale_before: datetime) -> WorkerRead | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM workers WHERE id = ?", (worker_id,)
            ).fetchone()
        return self._row_to_worker(row, stale_before) if row is not None else None

    def claim(
        self,
        worker_id: str,
        supported_types: Sequence[JobType],
        claimed_at: datetime,
        lease_token: UUID,
        lease_expires_at: datetime,
        metrics: WorkerMetrics | None = None,
        stale_before: datetime | None = None,
    ) -> JobRead | None:
        supported_json = json.dumps([job_type.value for job_type in supported_types])
        metrics_json = json.dumps(metrics.model_dump(mode="json")) if metrics else None
        with self.database.connect() as connection:
            self._recover_expired(connection, claimed_at)

            worker = connection.execute(
                "SELECT enabled FROM workers WHERE id = ?", (worker_id,)
            ).fetchone()
            if worker is None:
                # First contact: register the worker (disabled) so it becomes
                # visible to an operator, then decline — a brand-new worker has
                # nothing to claim anyway.
                self._upsert_worker(
                    connection,
                    worker_id,
                    supported_json,
                    claimed_at,
                    current_job_id=None,
                    metrics_json=metrics_json,
                )
                return None
            # Claim already carries the worker's capabilities and metrics. Use
            # it as the idle liveness update too, instead of following every
            # empty claim with a second heartbeat request and transaction.
            self._upsert_worker(
                connection,
                worker_id,
                supported_json,
                claimed_at,
                current_job_id=None,
                metrics_json=metrics_json,
            )
            if not bool(worker["enabled"]):
                return None

            active = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status = ? AND worker_id = ? AND lease_expires_at > ?
                ORDER BY started_at ASC LIMIT 1
                """,
                (JobStatus.RUNNING.value, worker_id, claimed_at.isoformat()),
            ).fetchone()
            if active is not None:
                connection.execute(
                    "UPDATE workers SET current_job_id = ? WHERE id = ?",
                    (active["id"], worker_id),
                )
                return self._row_to_job(active)

            placeholders = ", ".join("?" for _ in supported_types)
            queued = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE status = ? AND type IN ({placeholders})
                    AND (target_worker_id IS NULL OR target_worker_id = ?)
                ORDER BY created_at ASC, COALESCE(task_index, -1) ASC, id ASC
                """,
                (
                    JobStatus.QUEUED.value,
                    *(job_type.value for job_type in supported_types),
                    worker_id,
                ),
            ).fetchall()
            workers = connection.execute("SELECT * FROM workers").fetchall()
            cutoff = stale_before or claimed_at - timedelta(seconds=90)
            row = None
            for candidate in queued:
                if self._preferred_worker_id(candidate, workers, cutoff) != worker_id:
                    continue
                row = connection.execute(
                    """
                UPDATE jobs
                SET status = ?, worker_id = ?, started_at = ?, updated_at = ?,
                    attempt = attempt + 1, lease_token = ?, lease_expires_at = ?,
                    finished_at = NULL, result_json = NULL, error = NULL,
                    failure_kind = NULL
                WHERE id = ? AND status = ?
                RETURNING *
                    """,
                    (
                        JobStatus.RUNNING.value,
                        worker_id,
                        claimed_at.isoformat(),
                        claimed_at.isoformat(),
                        str(lease_token),
                        lease_expires_at.isoformat(),
                        candidate["id"],
                        JobStatus.QUEUED.value,
                    ),
                ).fetchone()
                if row is not None:
                    break
            if row is not None:
                connection.execute(
                    "UPDATE workers SET current_job_id = ? WHERE id = ?",
                    (row["id"], worker_id),
                )
        return self._row_to_job(row) if row is not None else None

    def heartbeat(
        self,
        worker_id: str,
        supported_types: Sequence[JobType],
        seen_at: datetime,
        lease_expires_at: datetime,
        current_job_id: UUID | None,
        lease_token: UUID | None,
        metrics: WorkerMetrics | None = None,
    ) -> bool | None:
        supported_json = json.dumps([job_type.value for job_type in supported_types])
        metrics_json = json.dumps(metrics.model_dump(mode="json")) if metrics else None
        with self.database.connect() as connection:
            if current_job_id is not None and lease_token is not None:
                renewed = connection.execute(
                    """
                    UPDATE jobs SET lease_expires_at = ?, updated_at = ?
                    WHERE id = ? AND status = ? AND worker_id = ?
                        AND lease_token = ? AND lease_expires_at > ?
                    RETURNING cancellation_requested
                    """,
                    (
                        lease_expires_at.isoformat(),
                        seen_at.isoformat(),
                        str(current_job_id),
                        JobStatus.RUNNING.value,
                        worker_id,
                        str(lease_token),
                        seen_at.isoformat(),
                    ),
                )
                row = renewed.fetchone()
                if row is None:
                    return None
                current = str(current_job_id)
                cancellation_requested = bool(row["cancellation_requested"])
            else:
                current = None
                cancellation_requested = False
            self._upsert_worker(
                connection,
                worker_id,
                supported_json,
                seen_at,
                current,
                metrics_json,
            )
        return cancellation_requested

    def cancel(self, job_id: UUID, requested_at: datetime) -> JobRead | None:
        """Cancel queued work now, or ask the current worker to stop running work."""
        now = requested_at.isoformat()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (str(job_id),)
            ).fetchone()
            if row is None:
                return None
            if row["status"] == JobStatus.QUEUED.value:
                row = connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, cancellation_requested = 1,
                        failure_kind = ?, error = 'cancelled by user',
                        finished_at = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    RETURNING *
                    """,
                    (
                        JobStatus.FAILED.value,
                        FailureKind.CANCELLED_BY_USER.value,
                        now,
                        now,
                        str(job_id),
                        JobStatus.QUEUED.value,
                    ),
                ).fetchone()
            elif row["status"] == JobStatus.RUNNING.value:
                row = connection.execute(
                    """
                    UPDATE jobs SET cancellation_requested = 1, updated_at = ?
                    WHERE id = ? AND status = ?
                    RETURNING *
                    """,
                    (now, str(job_id), JobStatus.RUNNING.value),
                ).fetchone()
            else:
                return self._row_to_job(row)
        return self._row_to_job(row) if row is not None else None

    def complete(
        self,
        job_id: UUID,
        worker_id: str,
        lease_token: UUID,
        result: dict[str, object],
        finished_at: datetime,
    ) -> JobRead | None:
        return self._finish(
            job_id,
            worker_id,
            lease_token,
            finished_at,
            JobStatus.COMPLETED,
            result=result,
            allow_cancellation=False,
        )

    def fail(
        self,
        job_id: UUID,
        worker_id: str,
        lease_token: UUID,
        error: str,
        failure_kind: FailureKind,
        finished_at: datetime,
    ) -> JobRead | None:
        return self._finish(
            job_id,
            worker_id,
            lease_token,
            finished_at,
            JobStatus.FAILED,
            error=error,
            failure_kind=failure_kind,
        )

    def _finish(
        self,
        job_id: UUID,
        worker_id: str,
        lease_token: UUID,
        finished_at: datetime,
        status: JobStatus,
        *,
        result: dict[str, object] | None = None,
        error: str | None = None,
        failure_kind: FailureKind | None = None,
        allow_cancellation: bool = True,
    ) -> JobRead | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                UPDATE jobs
                SET status = ?, result_json = ?, error = ?, failure_kind = ?,
                    finished_at = ?, updated_at = ?, lease_token = NULL,
                    lease_expires_at = NULL
                WHERE id = ? AND status = ? AND worker_id = ?
                    AND lease_token = ? AND lease_expires_at > ?
                    AND (? OR cancellation_requested = 0)
                RETURNING *
                """,
                (
                    status.value,
                    json.dumps(result) if result is not None else None,
                    error,
                    failure_kind.value if failure_kind is not None else None,
                    finished_at.isoformat(),
                    finished_at.isoformat(),
                    str(job_id),
                    JobStatus.RUNNING.value,
                    worker_id,
                    str(lease_token),
                    finished_at.isoformat(),
                    int(allow_cancellation),
                ),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE workers SET current_job_id = NULL WHERE id = ?",
                    (worker_id,),
                )
        return self._row_to_job(row) if row is not None else None

    def recover_expired(self, recovered_at: datetime) -> int:
        with self.database.connect() as connection:
            return self._recover_expired(connection, recovered_at)

    def list_workers(self, stale_before: datetime) -> list_type[WorkerRead]:
        with self.database.connect() as connection:
            rows = connection.execute(
                # Order by identity, not recency. Ordering by last_seen means
                # the list reshuffles every few seconds as workers heartbeat in
                # turn, which makes the dashboard jump under the reader's eye
                # and makes "the second row" meaningless. Recency is already
                # visible as a column.
                "SELECT * FROM workers ORDER BY id"
            ).fetchall()
        return [self._row_to_worker(row, stale_before) for row in rows]

    def set_worker_enabled(
        self, worker_id: str, enabled: bool, stale_before: datetime
    ) -> WorkerRead | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "UPDATE workers SET enabled = ? WHERE id = ? RETURNING *",
                (int(enabled), worker_id),
            ).fetchone()
        return self._row_to_worker(row, stale_before) if row is not None else None

    def set_worker_capacity(
        self,
        worker_id: str,
        max_job_cpu: float,
        max_job_memory_mb: int,
        stale_before: datetime,
    ) -> WorkerRead | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                UPDATE workers SET max_job_cpu = ?, max_job_memory_mb = ?
                WHERE id = ? RETURNING *
                """,
                (max_job_cpu, max_job_memory_mb, worker_id),
            ).fetchone()
        return self._row_to_worker(row, stale_before) if row is not None else None

    @staticmethod
    def _preferred_worker_id(
        job: sqlite3.Row,
        workers: Sequence[sqlite3.Row],
        stale_before: datetime,
    ) -> str | None:
        job_type = JobType(job["type"])
        target = job["target_worker_id"]
        parameters = json.loads(job["parameters_json"])
        eligible: list[sqlite3.Row] = []
        for worker in workers:
            if not bool(worker["enabled"]):
                continue
            if datetime.fromisoformat(worker["last_seen"]) < stale_before:
                continue
            if worker["current_job_id"] is not None:
                continue
            if target is not None and worker["id"] != target:
                continue
            supported = json.loads(worker["supported_types_json"])
            if job_type.value not in supported:
                continue
            if job_type in {JobType.PYTHON_BATCH, JobType.BATCH}:
                max_cpu = worker["max_job_cpu"]
                max_memory = worker["max_job_memory_mb"]
                if max_cpu is None or max_memory is None:
                    continue
                if parameters["cpu_limit"] > max_cpu:
                    continue
                if parameters["memory_mb"] > max_memory:
                    continue
            eligible.append(worker)
        if not eligible:
            return None
        if job_type in {JobType.PYTHON_BATCH, JobType.BATCH}:
            # Best-fit keeps the larger machine available for work that truly
            # needs it. Identity is the stable final tie-breaker.
            selected = min(
                eligible,
                key=lambda worker: (
                    worker["max_job_memory_mb"],
                    worker["max_job_cpu"],
                    worker["id"],
                ),
            )
        else:
            selected = min(eligible, key=lambda worker: worker["id"])
        return str(selected["id"])

    @staticmethod
    def _upsert_worker(
        connection: sqlite3.Connection,
        worker_id: str,
        supported_types_json: str,
        seen_at: datetime,
        current_job_id: str | None,
        metrics_json: str | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO workers (
                id, supported_types_json, registered_at, last_seen, current_job_id,
                metrics_json, enabled
            ) VALUES (?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(id) DO UPDATE SET
                supported_types_json = excluded.supported_types_json,
                last_seen = excluded.last_seen,
                current_job_id = excluded.current_job_id,
                metrics_json = COALESCE(excluded.metrics_json, workers.metrics_json)
            """,
            (
                worker_id,
                supported_types_json,
                seen_at.isoformat(),
                seen_at.isoformat(),
                current_job_id,
                metrics_json,
            ),
        )

    @staticmethod
    def _row_to_worker(row: sqlite3.Row, stale_before: datetime) -> WorkerRead:
        last_seen = datetime.fromisoformat(row["last_seen"])
        if last_seen < stale_before:
            state = WorkerState.STALE
        elif row["current_job_id"] is not None:
            state = WorkerState.BUSY
        else:
            state = WorkerState.ONLINE
        return WorkerRead(
            id=row["id"],
            enabled=bool(row["enabled"]),
            supported_types=[
                JobType(value) for value in json.loads(row["supported_types_json"])
            ],
            registered_at=datetime.fromisoformat(row["registered_at"]),
            last_seen=last_seen,
            current_job_id=(
                UUID(row["current_job_id"])
                if row["current_job_id"] is not None
                else None
            ),
            max_job_cpu=row["max_job_cpu"],
            max_job_memory_mb=row["max_job_memory_mb"],
            metrics=(
                WorkerMetrics.model_validate(json.loads(row["metrics_json"]))
                if row["metrics_json"] is not None
                else None
            ),
            state=state,
        )

    @staticmethod
    def _recover_expired(connection: sqlite3.Connection, recovered_at: datetime) -> int:
        now = recovered_at.isoformat()
        expired_workers = connection.execute(
            """
            SELECT DISTINCT worker_id FROM jobs
            WHERE status = ? AND lease_expires_at <= ? AND worker_id IS NOT NULL
            """,
            (JobStatus.RUNNING.value, now),
        ).fetchall()
        cancelled = connection.execute(
            """
            UPDATE jobs SET status = ?, finished_at = ?, updated_at = ?,
                error = ?,
                failure_kind = ?, lease_token = NULL, lease_expires_at = NULL
            WHERE status = ? AND lease_expires_at <= ? AND cancellation_requested = 1
            """,
            (
                JobStatus.FAILED.value,
                now,
                now,
                "cancelled by user; worker lease expired before acknowledgement",
                FailureKind.CANCELLED_BY_USER.value,
                JobStatus.RUNNING.value,
                now,
            ),
        ).rowcount
        failed = connection.execute(
            """
            UPDATE jobs SET status = ?, finished_at = ?, updated_at = ?,
                error = 'worker lease expired; maximum attempts reached',
                failure_kind = ?,
                lease_token = NULL, lease_expires_at = NULL
            WHERE status = ? AND lease_expires_at <= ? AND attempt >= max_attempts
                AND cancellation_requested = 0
            """,
            (
                JobStatus.FAILED.value,
                now,
                now,
                FailureKind.WORKER_LOST.value,
                JobStatus.RUNNING.value,
                now,
            ),
        ).rowcount
        requeued = connection.execute(
            """
            UPDATE jobs SET status = ?, worker_id = NULL, started_at = NULL,
                updated_at = ?, error = 'worker lease expired; job requeued',
                failure_kind = NULL, lease_token = NULL, lease_expires_at = NULL
            WHERE status = ? AND lease_expires_at <= ?
                AND cancellation_requested = 0
            """,
            (JobStatus.QUEUED.value, now, JobStatus.RUNNING.value, now),
        ).rowcount
        for row in expired_workers:
            connection.execute(
                "UPDATE workers SET current_job_id = NULL WHERE id = ?",
                (row["worker_id"],),
            )
        return cancelled + failed + requeued

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobRead:
        result_json = row["result_json"]
        job_type = JobType(row["type"])
        parameters_json = json.loads(row["parameters_json"])
        parameters: JobParameters
        if job_type is JobType.SLEEP:
            parameters = SleepParameters.model_validate(parameters_json)
        elif job_type is JobType.DATASET_SCRIPT:
            parameters = DatasetScriptParameters.model_validate(parameters_json)
        elif job_type is JobType.PYTHON_BATCH:
            parameters = PythonBatchParameters.model_validate(parameters_json)
        else:
            parameters = BatchParameters.model_validate(parameters_json)
        return JobRead(
            id=UUID(row["id"]),
            name=row["name"],
            type=job_type,
            parameters=parameters,
            target_worker_id=row["target_worker_id"],
            group_id=row["group_id"],
            task_id=row["task_id"],
            task_index=row["task_index"],
            status=JobStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            worker_id=row["worker_id"],
            started_at=(
                datetime.fromisoformat(row["started_at"]) if row["started_at"] else None
            ),
            finished_at=(
                datetime.fromisoformat(row["finished_at"])
                if row["finished_at"]
                else None
            ),
            result=json.loads(result_json) if result_json else None,
            error=row["error"],
            failure_kind=(
                FailureKind(row["failure_kind"]) if row["failure_kind"] else None
            ),
            cancellation_requested=bool(row["cancellation_requested"]),
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            lease_token=UUID(row["lease_token"]) if row["lease_token"] else None,
            lease_expires_at=(
                datetime.fromisoformat(row["lease_expires_at"])
                if row["lease_expires_at"]
                else None
            ),
        )

    @classmethod
    def _get_group(
        cls,
        connection: sqlite3.Connection,
        group_id: UUID,
        owner_user_id: str | None = None,
    ) -> JobGroupRead | None:
        if owner_user_id is None:
            group_row = connection.execute(
                "SELECT * FROM job_groups WHERE id = ?", (str(group_id),)
            ).fetchone()
        else:
            group_row = connection.execute(
                "SELECT * FROM job_groups WHERE id = ? AND owner_user_id = ?",
                (str(group_id), owner_user_id),
            ).fetchone()
        if group_row is None:
            return None
        task_rows = connection.execute(
            """
            SELECT * FROM jobs WHERE group_id = ?
            ORDER BY task_index ASC, id ASC
            """,
            (str(group_id),),
        ).fetchall()
        tasks = [cls._row_to_job(row) for row in task_rows]
        statuses = {task.status for task in tasks}
        if statuses == {JobStatus.QUEUED}:
            status = JobStatus.QUEUED
        elif statuses == {JobStatus.COMPLETED}:
            status = JobStatus.COMPLETED
        elif statuses <= {JobStatus.COMPLETED, JobStatus.FAILED}:
            status = JobStatus.FAILED
        else:
            status = JobStatus.RUNNING
        updated_at = max(
            [datetime.fromisoformat(group_row["updated_at"])]
            + [task.updated_at for task in tasks]
        )
        return JobGroupRead(
            id=group_id,
            name=group_row["name"],
            status=status,
            created_at=datetime.fromisoformat(group_row["created_at"]),
            updated_at=updated_at,
            tasks=tasks,
        )
