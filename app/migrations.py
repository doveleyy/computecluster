import sqlite3
from collections.abc import Callable

from app.identity import ADMIN_USER_ID, ADMIN_USERNAME

Migration = Callable[[sqlite3.Connection], None]


def apply_migrations(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    applied = {
        row["version"]
        for row in connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
    }
    for version, migration in MIGRATIONS:
        if version not in applied:
            migration(connection)
            connection.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)",
                (version,),
            )


def create_jobs_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            parameters_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def add_execution_columns(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    new_columns = {
        "worker_id": "TEXT",
        "started_at": "TEXT",
        "finished_at": "TEXT",
        "result_json": "TEXT",
        "error": "TEXT",
    }
    for name, column_type in new_columns.items():
        if name not in existing_columns:
            connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {column_type}")


def add_queue_index(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS jobs_queue_order
        ON jobs (status, type, created_at)
        """
    )


def add_leases_and_workers(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    new_columns = {
        "attempt": "INTEGER NOT NULL DEFAULT 0",
        "max_attempts": "INTEGER NOT NULL DEFAULT 3",
        "lease_token": "TEXT",
        "lease_expires_at": "TEXT",
    }
    for name, column_type in new_columns.items():
        if name not in existing_columns:
            connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {column_type}")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS workers (
            id TEXT PRIMARY KEY,
            supported_types_json TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            current_job_id TEXT
        )
        """
    )


def add_submission_idempotency(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    if "idempotency_key" not in existing_columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN idempotency_key TEXT")
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency_key
        ON jobs (idempotency_key) WHERE idempotency_key IS NOT NULL
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS jobs_expired_leases
        ON jobs (status, lease_expires_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS workers_last_seen
        ON workers (last_seen)
        """
    )


def add_worker_metrics(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(workers)").fetchall()
    }
    if "metrics_json" not in existing_columns:
        connection.execute("ALTER TABLE workers ADD COLUMN metrics_json TEXT")


def add_worker_scheduling_control(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(workers)").fetchall()
    }
    if "enabled" not in existing_columns:
        connection.execute(
            "ALTER TABLE workers ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
        )


def add_job_names(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    if "name" not in existing_columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN name TEXT")


def add_scheduling_and_failure_details(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    if "target_worker_id" not in existing_columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN target_worker_id TEXT")
    if "failure_kind" not in existing_columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN failure_kind TEXT")
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS jobs_scheduling_order
        ON jobs (status, target_worker_id, type, created_at, id)
        """
    )


def add_worker_capacity_limits(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(workers)").fetchall()
    }
    if "max_job_cpu" not in existing_columns:
        connection.execute("ALTER TABLE workers ADD COLUMN max_job_cpu REAL")
    if "max_job_memory_mb" not in existing_columns:
        connection.execute("ALTER TABLE workers ADD COLUMN max_job_memory_mb INTEGER")


def add_job_cancellation(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    if "cancellation_requested" not in existing_columns:
        connection.execute(
            "ALTER TABLE jobs ADD COLUMN cancellation_requested "
            "INTEGER NOT NULL DEFAULT 0"
        )


def add_job_groups(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS job_groups (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            request_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            idempotency_key TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS job_groups_idempotency_key
        ON job_groups (idempotency_key) WHERE idempotency_key IS NOT NULL
        """
    )
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    new_columns = {
        "group_id": "TEXT",
        "task_id": "TEXT",
        "task_index": "INTEGER",
    }
    for name, column_type in new_columns.items():
        if name not in existing_columns:
            connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {column_type}")
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS jobs_group_task_id
        ON jobs (group_id, task_id) WHERE group_id IS NOT NULL
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS jobs_group_task_index
        ON jobs (group_id, task_index) WHERE group_id IS NOT NULL
        """
    )


def add_ownership_foundation(connection: sqlite3.Connection) -> None:
    """Create stable ownership without exposing an incomplete member boundary.

    SQLite cannot add a non-null foreign-key column with a non-null default to
    an existing table. Add the columns as nullable, backfill them in the same
    migration transaction, and make every application insert explicit.
    """
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL CHECK (role IN ('MEMBER', 'ADMIN')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        """
        INSERT INTO users (id, username, role)
        VALUES (?, ?, 'ADMIN')
        ON CONFLICT DO NOTHING
        """,
        (ADMIN_USER_ID, ADMIN_USERNAME),
    )

    for table in ("jobs", "job_groups"):
        columns = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if "owner_user_id" not in columns:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN owner_user_id TEXT "
                "REFERENCES users(id)"
            )
        connection.execute(
            f"UPDATE {table} SET owner_user_id = ? WHERE owner_user_id IS NULL",
            (ADMIN_USER_ID,),
        )
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS {table}_owner_created "
            f"ON {table} (owner_user_id, created_at DESC)"
        )


def add_member_authentication(connection: sqlite3.Connection) -> None:
    user_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(users)").fetchall()
    }
    if "password_hash" not in user_columns:
        connection.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
    if "disabled" not in user_columns:
        connection.execute(
            "ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0"
        )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS uploads (
            id TEXT PRIMARY KEY,
            owner_user_id TEXT NOT NULL REFERENCES users(id),
            kind TEXT NOT NULL
                CHECK (kind IN ('dataset', 'script', 'project', 'input')),
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS uploads_owner_created "
        "ON uploads (owner_user_id, created_at DESC)"
    )
    connection.execute("DROP INDEX IF EXISTS jobs_idempotency_key")
    connection.execute("DROP INDEX IF EXISTS job_groups_idempotency_key")
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS jobs_owner_idempotency_key
        ON jobs (owner_user_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS job_groups_owner_idempotency_key
        ON job_groups (owner_user_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL
        """
    )
    for table in ("jobs", "job_groups"):
        connection.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_owner_required
            BEFORE INSERT ON {table}
            WHEN NEW.owner_user_id IS NULL
            BEGIN
                SELECT RAISE(ABORT, 'owner_user_id is required');
            END
            """
        )
        connection.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_owner_immutable
            BEFORE UPDATE OF owner_user_id ON {table}
            WHEN NEW.owner_user_id IS NOT OLD.owner_user_id
            BEGIN
                SELECT RAISE(ABORT, 'owner_user_id is immutable');
            END
            """
        )


def add_session_version(connection: sqlite3.Connection) -> None:
    user_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(users)").fetchall()
    }
    if "session_version" not in user_columns:
        connection.execute(
            "ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 1"
        )


def add_external_identities(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS external_identities (
            provider TEXT NOT NULL,
            subject TEXT NOT NULL,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            display_name TEXT NOT NULL,
            linked_at TEXT NOT NULL,
            PRIMARY KEY (provider, subject),
            UNIQUE (user_id, provider)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS external_identities_user
        ON external_identities(user_id, provider)
        """
    )


MIGRATIONS: tuple[tuple[int, Migration], ...] = (
    (1, create_jobs_table),
    (2, add_execution_columns),
    (3, add_queue_index),
    (4, add_leases_and_workers),
    (5, add_submission_idempotency),
    (6, add_worker_metrics),
    (7, add_worker_scheduling_control),
    (8, add_job_names),
    (9, add_scheduling_and_failure_details),
    (10, add_worker_capacity_limits),
    (11, add_job_cancellation),
    (12, add_job_groups),
    (13, add_ownership_foundation),
    (14, add_member_authentication),
    (15, add_session_version),
    (16, add_external_identities),
)
