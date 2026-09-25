import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest

from app.database import Database
from app.repository import JobRepository
from app.service import JobService, SchedulingCapacityError
from contracts.models import (
    FailureKind,
    JobCreate,
    JobStatus,
    JobType,
    PythonBatchParameters,
    SleepParameters,
    UploadedDatasetReference,
    UploadedScriptReference,
    WorkerClaim,
)


def enable_worker(
    repository: JobRepository,
    worker_id: str,
    now: datetime,
    supported_types: list[JobType] | None = None,
) -> None:
    """Register a worker and turn its scheduling on.

    Workers register with scheduling disabled, so any test that expects a
    claim to succeed has to enable the worker first.
    """
    repository.heartbeat(
        worker_id,
        supported_types or [JobType.SLEEP],
        now,
        now,
        None,
        None,
    )
    assert repository.set_worker_enabled(worker_id, True, now) is not None


def configure_capacity(
    repository: JobRepository,
    worker_id: str,
    now: datetime,
    cpu: float,
    memory_mb: int,
) -> None:
    assert repository.set_worker_capacity(worker_id, cpu, memory_mb, now) is not None


def python_batch(
    cpu: float,
    memory_mb: int,
    target_worker_id: str | None = None,
) -> JobCreate:
    return JobCreate(
        type=JobType.PYTHON_BATCH,
        target_worker_id=target_worker_id,
        parameters=PythonBatchParameters(
            script=UploadedScriptReference(
                upload_id=uuid4(), sha256="a" * 64, size_bytes=10
            ),
            dataset=UploadedDatasetReference(
                upload_id=uuid4(), sha256="b" * 64, size_bytes=10
            ),
            cpu_limit=cpu,
            memory_mb=memory_mb,
        ),
    )


@pytest.mark.parametrize("_attempt", range(10))
def test_only_one_worker_can_claim_one_job_under_contention(
    tmp_path: Path,
    _attempt: int,
) -> None:
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    JobService(JobRepository(database)).create(
        JobCreate(type=JobType.SLEEP, parameters=SleepParameters(seconds=1))
    )
    registry = JobRepository(database)
    registration_time = datetime.now(UTC)
    enable_worker(registry, "mac-one", registration_time)
    enable_worker(registry, "mac-two", registration_time)
    barrier = Barrier(2)

    def claim(worker_id: str):
        service = JobService(JobRepository(database))
        barrier.wait()
        return service.claim(WorkerClaim(worker_id=worker_id))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ["mac-one", "mac-two"]))

    claimed = [result for result in results if result is not None]
    assert len(claimed) == 1
    assert claimed[0].status.value == "RUNNING"


def test_targeted_job_can_only_be_claimed_by_its_worker(tmp_path: Path) -> None:
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    repository = JobRepository(database)
    now = datetime.now(UTC)
    enable_worker(repository, "mac-primary", now)
    enable_worker(repository, "windows-primary", now)
    service = JobService(repository)
    created = service.create(
        JobCreate(
            type=JobType.SLEEP,
            parameters=SleepParameters(seconds=1),
            target_worker_id="windows-primary",
        )
    )

    wrong_worker = service.claim(WorkerClaim(worker_id="mac-primary"))
    selected_worker = service.claim(WorkerClaim(worker_id="windows-primary"))

    assert wrong_worker is None
    assert selected_worker is not None
    assert selected_worker.id == created.id
    assert selected_worker.target_worker_id == "windows-primary"
    assert selected_worker.worker_id == "windows-primary"


def test_cancelled_running_job_is_not_requeued_when_lease_expires(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    repository = JobRepository(database)
    now = datetime.now(UTC)
    enable_worker(repository, "mac-one", now)
    service = JobService(repository, lease_seconds=1)
    created = service.create(
        JobCreate(type=JobType.SLEEP, parameters=SleepParameters(seconds=30))
    )
    claimed = service.claim(WorkerClaim(worker_id="mac-one"))
    assert claimed is not None
    service.cancel(created.id)

    repository.recover_expired(now + timedelta(seconds=2))
    recovered = repository.get(created.id)

    assert recovered is not None
    assert recovered.status is JobStatus.FAILED
    assert recovered.failure_kind is FailureKind.CANCELLED_BY_USER
    assert recovered.cancellation_requested is True


def test_batch_best_fit_is_deterministic_and_capacity_gated(tmp_path: Path) -> None:
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    repository = JobRepository(database)
    now = datetime.now(UTC)
    enable_worker(repository, "mac-primary", now, [JobType.PYTHON_BATCH])
    enable_worker(repository, "windows-primary", now, [JobType.PYTHON_BATCH])
    configure_capacity(repository, "windows-primary", now, 4, 3072)
    configure_capacity(repository, "mac-primary", now, 6, 8192)
    created = JobService(repository).create(python_batch(2, 2048))

    # Mac polls first, but may not steal a job that fits the smaller node.
    assert (
        repository.claim(
            "mac-primary",
            [JobType.PYTHON_BATCH],
            now,
            uuid4(),
            now + timedelta(seconds=15),
        )
        is None
    )
    claimed = repository.claim(
        "windows-primary",
        [JobType.PYTHON_BATCH],
        now,
        uuid4(),
        now + timedelta(seconds=15),
    )

    assert claimed is not None
    assert claimed.id == created.id
    assert claimed.worker_id == "windows-primary"


def test_large_batch_spills_to_larger_worker(tmp_path: Path) -> None:
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    repository = JobRepository(database)
    now = datetime.now(UTC)
    enable_worker(repository, "mac-primary", now, [JobType.PYTHON_BATCH])
    enable_worker(repository, "windows-primary", now, [JobType.PYTHON_BATCH])
    configure_capacity(repository, "windows-primary", now, 4, 3072)
    configure_capacity(repository, "mac-primary", now, 6, 8192)
    created = JobService(repository).create(python_batch(4, 4096))

    assert (
        repository.claim(
            "windows-primary",
            [JobType.PYTHON_BATCH],
            now,
            uuid4(),
            now + timedelta(seconds=15),
        )
        is None
    )
    claimed = repository.claim(
        "mac-primary",
        [JobType.PYTHON_BATCH],
        now,
        uuid4(),
        now + timedelta(seconds=15),
    )
    assert claimed is not None
    assert claimed.id == created.id


def test_busy_best_fit_worker_causes_next_job_to_spill(tmp_path: Path) -> None:
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    repository = JobRepository(database)
    now = datetime.now(UTC)
    enable_worker(repository, "mac-primary", now, [JobType.PYTHON_BATCH])
    enable_worker(repository, "windows-primary", now, [JobType.PYTHON_BATCH])
    configure_capacity(repository, "windows-primary", now, 4, 3072)
    configure_capacity(repository, "mac-primary", now, 6, 8192)
    service = JobService(repository)
    first = service.create(python_batch(2, 2048))
    second = service.create(python_batch(2, 2048))

    windows_job = repository.claim(
        "windows-primary",
        [JobType.PYTHON_BATCH],
        now,
        uuid4(),
        now + timedelta(seconds=15),
    )
    mac_job = repository.claim(
        "mac-primary",
        [JobType.PYTHON_BATCH],
        now,
        uuid4(),
        now + timedelta(seconds=15),
    )

    assert windows_job is not None
    assert mac_job is not None
    assert windows_job.id == first.id
    assert mac_job.id == second.id


def test_stale_best_fit_worker_does_not_block_fallback(tmp_path: Path) -> None:
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    repository = JobRepository(database)
    registered = datetime(2026, 1, 1, tzinfo=UTC)
    enable_worker(repository, "mac-primary", registered, [JobType.PYTHON_BATCH])
    enable_worker(repository, "windows-primary", registered, [JobType.PYTHON_BATCH])
    configure_capacity(repository, "windows-primary", registered, 4, 3072)
    configure_capacity(repository, "mac-primary", registered, 6, 8192)
    created = JobService(repository).create(python_batch(2, 2048))
    later = registered + timedelta(seconds=30)

    claimed = repository.claim(
        "mac-primary",
        [JobType.PYTHON_BATCH],
        later,
        uuid4(),
        later + timedelta(seconds=15),
        # State the cutoff rather than inheriting the caller's default, so this
        # keeps testing "a stale best fit is skipped" if that default moves.
        stale_before=later - timedelta(seconds=20),
    )

    assert claimed is not None
    assert claimed.id == created.id
    assert claimed.worker_id == "mac-primary"


def test_targeting_does_not_bypass_capacity(tmp_path: Path) -> None:
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    repository = JobRepository(database)
    now = datetime.now(UTC)
    enable_worker(repository, "windows-primary", now, [JobType.PYTHON_BATCH])
    configure_capacity(repository, "windows-primary", now, 2, 2048)

    with pytest.raises(SchedulingCapacityError, match="not configured to accept"):
        JobService(repository).create(
            python_batch(4, 4096, target_worker_id="windows-primary")
        )


def test_existing_database_is_migrated_without_losing_job(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.db"
    job_id = UUID("00000000-0000-0000-0000-000000000001")
    timestamp = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(job_id),
                "sleep",
                json.dumps({"seconds": 1}),
                "QUEUED",
                timestamp,
                timestamp,
            ),
        )

    database = Database(database_path)
    database.initialize()
    database.initialize()

    preserved = JobRepository(database).get(job_id)
    with sqlite3.connect(database_path) as connection:
        versions = {
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(jobs)").fetchall()
        }

    assert preserved is not None
    assert preserved.status.value == "QUEUED"
    assert preserved.name is None
    assert preserved.attempt == 0
    assert preserved.max_attempts == 3
    assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16}
    assert preserved.target_worker_id is None
    assert preserved.failure_kind is None
    assert preserved.cancellation_requested is False
    assert "jobs_scheduling_order" in indexes
    assert "jobs_queue_order" in indexes
    assert "jobs_owner_idempotency_key" in indexes

    with sqlite3.connect(database_path) as connection:
        owner = connection.execute(
            "SELECT owner_user_id FROM jobs WHERE id = ?", (str(job_id),)
        ).fetchone()
        administrator = connection.execute(
            "SELECT username, role FROM users WHERE id = ?",
            ("00000000-0000-0000-0000-000000000001",),
        ).fetchone()
    assert owner == ("00000000-0000-0000-0000-000000000001",)
    assert administrator == ("administrator", "ADMIN")

    with (
        sqlite3.connect(database_path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
    ):
        connection.execute(
            "UPDATE jobs SET owner_user_id = NULL WHERE id = ?", (str(job_id),)
        )


def test_worker_enabled_state_survives_database_reinitialization(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    repository = JobRepository(database)
    now = datetime.now(UTC)
    repository.claim(
        "mac-one",
        [JobType.SLEEP],
        now,
        uuid4(),
        now + timedelta(seconds=15),
    )

    enabled = repository.set_worker_enabled("mac-one", True, now)
    database.initialize()
    repository.heartbeat("mac-one", [JobType.SLEEP], now, now, None, None)
    workers = repository.list_workers(now)

    assert enabled is not None
    assert enabled.enabled is True
    assert workers[0].enabled is True


def test_new_worker_registers_with_scheduling_disabled(tmp_path: Path) -> None:
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    repository = JobRepository(database)
    JobService(repository).create(
        JobCreate(type=JobType.SLEEP, parameters=SleepParameters(seconds=1))
    )
    now = datetime.now(UTC)

    first_claim = repository.claim(
        "mac-brand-new",
        [JobType.SLEEP],
        now,
        uuid4(),
        now + timedelta(seconds=15),
    )
    workers = repository.list_workers(now)

    assert first_claim is None
    assert [worker.id for worker in workers] == ["mac-brand-new"]
    assert workers[0].enabled is False


def test_expired_leases_requeue_then_fail_at_attempt_limit(tmp_path: Path) -> None:
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    repository = JobRepository(database)
    service = JobService(repository, max_attempts=2)
    created = service.create(
        JobCreate(type=JobType.SLEEP, parameters=SleepParameters(seconds=1))
    )
    first_time = datetime(2026, 1, 1, tzinfo=UTC)
    first_token = uuid4()
    enable_worker(repository, "mac-one", first_time)
    enable_worker(repository, "mac-two", first_time)
    first = repository.claim(
        "mac-one",
        [JobType.SLEEP],
        first_time,
        first_token,
        first_time + timedelta(seconds=1),
    )
    assert first is not None

    recovered = repository.recover_expired(first_time + timedelta(seconds=2))
    assert repository.set_worker_enabled("mac-one", False, first_time) is not None
    queued = repository.get(created.id)

    assert recovered == 1
    assert queued is not None
    assert queued.status.value == "QUEUED"
    assert queued.attempt == 1
    assert queued.lease_token is None

    second_time = first_time + timedelta(seconds=3)
    second = repository.claim(
        "mac-two",
        [JobType.SLEEP],
        second_time,
        uuid4(),
        second_time + timedelta(seconds=1),
    )
    assert second is not None
    assert second.attempt == 2
    assert second.lease_token != first_token

    repository.recover_expired(second_time + timedelta(seconds=2))
    failed = repository.get(created.id)

    assert failed is not None
    assert failed.status.value == "FAILED"
    assert failed.attempt == 2
    assert "maximum attempts" in (failed.error or "")
    assert failed.failure_kind is not None
    assert failed.failure_kind.value == "WORKER_LOST"
    assert (
        repository.complete(
            created.id,
            "mac-one",
            first_token,
            {"slept_seconds": 1},
            second_time,
        )
        is None
    )


def test_repeated_claim_returns_workers_existing_active_job(tmp_path: Path) -> None:
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    repository = JobRepository(database)
    service = JobService(repository)
    service.create(JobCreate(type=JobType.SLEEP, parameters=SleepParameters(seconds=1)))
    service.create(JobCreate(type=JobType.SLEEP, parameters=SleepParameters(seconds=2)))
    enable_worker(repository, "mac-one", datetime.now(UTC))

    first = service.claim(WorkerClaim(worker_id="mac-one"))
    repeated = service.claim(WorkerClaim(worker_id="mac-one"))

    assert first is not None
    assert repeated is not None
    assert repeated.id == first.id
    assert repeated.lease_token == first.lease_token
    assert len([job for job in repository.list() if job.status.value == "QUEUED"]) == 1


def test_connections_are_reused_within_a_thread(tmp_path: Path) -> None:
    """Reuse is the point of the change, so assert it directly.

    Opening a connection per operation made SQLite checkpoint the WAL on every
    close, which is what turned a tiny database into gigabytes of daily writes.
    """
    database = Database(tmp_path / "jobs.db")
    database.initialize()

    with database.connect() as first:
        first_id = id(first)
    with database.connect() as second:
        assert id(second) == first_id, "a new connection was opened per operation"

    database.close()
    with database.connect() as after_close:
        assert id(after_close) != first_id, "close() should force a reconnect"


def test_each_thread_gets_its_own_connection(tmp_path: Path) -> None:
    """SQLite connections are not safe to share across threads by default,
    and FastAPI runs synchronous endpoints in a worker threadpool."""
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    seen: list[int] = []
    barrier = Barrier(2)

    def record() -> None:
        barrier.wait()
        with database.connect() as connection:
            seen.append(id(connection))

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: record(), range(2)))

    assert len(set(seen)) == 2, "threads shared a connection"


def test_a_failed_statement_does_not_poison_the_connection(tmp_path: Path) -> None:
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    repository = JobRepository(database)

    with pytest.raises(sqlite3.Error), database.connect() as connection:
        connection.execute("SELECT * FROM a_table_that_does_not_exist")

    # The thread must still be usable afterwards.
    assert repository.list() == []


def test_disabled_worker_claim_refreshes_liveness_without_becoming_enabled(
    tmp_path: Path,
) -> None:
    """One idle claim replaces the former claim-plus-heartbeat request pair."""
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    repository = JobRepository(database)
    registered_at = datetime(2026, 1, 1, tzinfo=UTC)

    # First contact registers the worker even though it cannot claim.
    assert (
        repository.claim(
            "mac-one",
            [JobType.SLEEP],
            registered_at,
            uuid4(),
            registered_at + timedelta(seconds=15),
        )
        is None
    )
    first = repository.list_workers(registered_at)
    assert [w.id for w in first] == ["mac-one"]
    assert first[0].enabled is False

    # A later claim refreshes liveness and metrics but cannot enable or schedule
    # the worker.
    much_later = registered_at + timedelta(hours=1)
    assert (
        repository.claim(
            "mac-one",
            [JobType.SLEEP],
            much_later,
            uuid4(),
            much_later + timedelta(seconds=15),
        )
        is None
    )
    refreshed = repository.list_workers(registered_at)[0]
    assert refreshed.last_seen > first[0].last_seen
    assert refreshed.enabled is False


def test_worker_order_is_stable_across_heartbeats(tmp_path: Path) -> None:
    """The list must not reshuffle as workers report in.

    Ordering by last_seen meant the dashboard reordered every few seconds as
    each worker heartbeat landed, so rows jumped under the reader and "the
    second worker" meant nothing. Recency is a column, not an ordering.
    """
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    repository = JobRepository(database)
    start = datetime(2026, 1, 1, tzinfo=UTC)

    for worker_id in ("windows-primary", "mac-primary"):
        repository.heartbeat(worker_id, [JobType.SLEEP], start, start, None, None)
    first = [w.id for w in repository.list_workers(start)]

    # Heartbeat them in the opposite order, repeatedly, as really happens.
    for index in range(1, 6):
        moment = start + timedelta(seconds=index)
        for worker_id in ("mac-primary", "windows-primary"):
            repository.heartbeat(worker_id, [JobType.SLEEP], moment, moment, None, None)
        assert [w.id for w in repository.list_workers(start)] == first

    assert first == ["mac-primary", "windows-primary"], "expected stable id order"


def test_claim_order_is_total_when_timestamps_tie(tmp_path: Path) -> None:
    """Two jobs created in the same instant must still claim deterministically."""
    database = Database(tmp_path / "jobs.db")
    database.initialize()
    repository = JobRepository(database)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    identical = now.isoformat()

    ids = sorted(str(UUID(int=n)) for n in (1, 2, 3))
    with database.connect() as connection:
        for job_id in ids:
            connection.execute(
                "INSERT INTO jobs (id, type, parameters_json, status, created_at,"
                " updated_at, attempt, max_attempts, owner_user_id) "
                "VALUES (?,?,?,?,?,?,0,3,?)",
                (
                    job_id,
                    "sleep",
                    json.dumps({"seconds": 1}),
                    "QUEUED",
                    identical,
                    identical,
                    "00000000-0000-0000-0000-000000000001",
                ),
            )

    enable_worker(repository, "mac-one", now)
    claimed = repository.claim(
        "mac-one", [JobType.SLEEP], now, uuid4(), now + timedelta(seconds=15)
    )
    assert claimed is not None
    # With created_at tied, the lowest id wins rather than whichever row
    # SQLite happened to visit first.
    assert str(claimed.id) == ids[0]
