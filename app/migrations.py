import json
import sqlite3
from collections.abc import Callable

from app.identity import ADMIN_USER_ID, ADMIN_USERNAME

Migration = Callable[[sqlite3.Connection], None]


def apply_migrations(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    pristine = (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' LIMIT 1"
        ).fetchone()
        is None
    )
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
            if pristine and version == 17:
                connection.execute(
                    "DELETE FROM app_registrations "
                    "WHERE app_id IN ('habits', 'wishlist', 'transport', 'sorter')"
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


def add_application_registry(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE app_registrations (
            app_id TEXT PRIMARY KEY,
            route TEXT NOT NULL UNIQUE,
            definition_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE app_releases (
            app_id TEXT NOT NULL REFERENCES app_registrations(app_id),
            manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
            manifest_bytes BLOB NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (app_id, manifest_sha256)
        ) WITHOUT ROWID
        """
    )
    connection.execute(
        """
        CREATE TRIGGER app_releases_no_replace
        BEFORE INSERT ON app_releases
        WHEN EXISTS (
            SELECT 1 FROM app_releases
            WHERE app_id = NEW.app_id AND manifest_sha256 = NEW.manifest_sha256
        )
        BEGIN
            SELECT RAISE(ABORT, 'application releases are immutable');
        END
        """
    )
    for operation in ("UPDATE", "DELETE"):
        connection.execute(
            f"""
            CREATE TRIGGER app_releases_no_{operation.lower()}
            BEFORE {operation} ON app_releases
            BEGIN
                SELECT RAISE(ABORT, 'application releases are immutable');
            END
            """
        )
    seeds = (
        (
            "habits",
            "Habits",
            "Habit Tracker",
            10,
            "#75d8a9",
            "Daily life",
            "Water, spending, funds, study, and daily analysis.",
            8100,
            "habit-tracker",
        ),
        (
            "wishlist",
            "Wishlist",
            "Wishlist",
            20,
            "#f2c96d",
            "Things to buy",
            "Saved products, current prices, and targets.",
            8101,
            "wishlist",
        ),
        (
            "transport",
            "Transport",
            "Transport",
            30,
            "#7fc7ef",
            "Getting around",
            "Today's last trains and saved bus arrivals.",
            8102,
            "transport",
        ),
        (
            "sorter",
            "Sorter",
            "File Sorter",
            60,
            "#c4a2ff",
            "File sorting",
            "Sort the dump folder into the tree, one file at a time.",
            8103,
            None,
        ),
    )
    for (
        key,
        label,
        health_name,
        position,
        accent,
        kicker,
        description,
        port,
        service,
    ) in seeds:
        definition: dict[str, object] = {
            "source": {"kind": "legacy", "owner": "current-platform-checkout"},
            "access": "administrator" if key == "sorter" else "linked-user",
            "capabilities": ["identity"],
            "presentation": {
                "label": label,
                "health_name": health_name,
                "position": position,
                "accent": accent,
                "kicker": kicker,
                "description": description,
            },
            "bindings": {
                "secret_files": [],
                "settings": [],
                "state_directory": None,
                "backup_directory": None,
                "integration_bindings": [],
                "resources": {
                    "cpu_limit": 0.5,
                    "memory_bytes": 268435456,
                    "pids_limit": 128,
                    "maintenance_timeout_seconds": 300,
                },
            },
            "installation": {
                "owner": "legacy-scripts",
                "host_port": port,
                "probe": {
                    "service": service,
                    "ready_path": "/ready",
                    "version_path": "/version",
                    "readiness": "legacy-json",
                }
                if service
                else None,
            },
        }
        connection.execute(
            "INSERT INTO app_registrations (app_id, route, definition_json) "
            "VALUES (?, ?, ?)",
            (
                key,
                f"/{key}",
                json.dumps(definition, sort_keys=True, separators=(",", ":")),
            ),
        )


def add_app_runtime_identity(connection: sqlite3.Connection) -> None:
    ledger = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'app_releases'"
    ).fetchone()[0]
    if "WITHOUT ROWID" not in ledger.upper():
        for operation in ("replace", "update", "delete"):
            connection.execute(f"DROP TRIGGER app_releases_no_{operation}")
        connection.execute("ALTER TABLE app_releases RENAME TO app_releases_old")
        connection.execute(
            "CREATE TABLE app_releases ("
            "app_id TEXT NOT NULL REFERENCES app_registrations(app_id), "
            "manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256) = 64), "
            "manifest_bytes BLOB NOT NULL, "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "PRIMARY KEY(app_id, manifest_sha256)) WITHOUT ROWID"
        )
        connection.execute("INSERT INTO app_releases SELECT * FROM app_releases_old")
        connection.execute("DROP TABLE app_releases_old")
        connection.execute(
            "CREATE TRIGGER app_releases_no_replace BEFORE INSERT ON app_releases "
            "WHEN EXISTS (SELECT 1 FROM app_releases WHERE app_id = NEW.app_id "
            "AND manifest_sha256 = NEW.manifest_sha256) BEGIN "
            "SELECT RAISE(ABORT, 'application releases are immutable'); END"
        )
        for operation in ("UPDATE", "DELETE"):
            connection.execute(
                f"CREATE TRIGGER app_releases_no_{operation.lower()} "
                f"BEFORE {operation} ON app_releases BEGIN "
                "SELECT RAISE(ABORT, 'application releases are immutable'); END"
            )
    connection.execute(
        "CREATE TABLE platform_instance "
        "(singleton INTEGER PRIMARY KEY CHECK(singleton = 1), "
        "environment TEXT NOT NULL, app_api_url TEXT NOT NULL, timezone TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TRIGGER platform_instance_no_replace "
        "BEFORE INSERT ON platform_instance "
        "WHEN EXISTS (SELECT 1 FROM platform_instance) BEGIN "
        "SELECT RAISE(ABORT, 'platform instance is immutable'); END"
    )
    for operation in ("UPDATE", "DELETE"):
        connection.execute(
            f"CREATE TRIGGER platform_instance_no_{operation.lower()} "
            f"BEFORE {operation} ON platform_instance BEGIN "
            "SELECT RAISE(ABORT, 'platform instance is immutable'); END"
        )
    connection.execute(
        "CREATE TABLE app_credentials (credential_id TEXT PRIMARY KEY, "
        "app_id TEXT NOT NULL REFERENCES app_registrations(app_id), "
        "environment TEXT NOT NULL, "
        "verifier TEXT NOT NULL CHECK(length(verifier) = 64), "
        "file_path TEXT NOT NULL, issued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "revoked_at TEXT) WITHOUT ROWID"
    )
    connection.execute(
        "CREATE UNIQUE INDEX app_credentials_active ON app_credentials(app_id) "
        "WHERE revoked_at IS NULL"
    )


def add_app_compute(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE app_staging_reservations (id TEXT PRIMARY KEY, "
        "app_id TEXT NOT NULL, owner_user_id TEXT NOT NULL, "
        "paths_json TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE app_workloads (id TEXT PRIMARY KEY, app_id TEXT NOT NULL, "
        "release_sha256 TEXT NOT NULL, workload_id TEXT NOT NULL, "
        "object_key TEXT NOT NULL, inputs_json TEXT NOT NULL, "
        "UNIQUE(app_id, release_sha256, workload_id), "
        "FOREIGN KEY(app_id, release_sha256) "
        "REFERENCES app_releases(app_id, manifest_sha256)) WITHOUT ROWID"
    )
    connection.execute(
        "CREATE TABLE app_inputs (id TEXT PRIMARY KEY, app_id TEXT NOT NULL, "
        "owner_user_id TEXT NOT NULL REFERENCES users(id), "
        "installation_id TEXT NOT NULL REFERENCES app_workloads(id), "
        "object_key TEXT, logical_path TEXT, fingerprint TEXT, "
        "size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, "
        "CHECK((object_key IS NOT NULL AND logical_path IS NULL) OR "
        "(object_key IS NULL AND logical_path IS NOT NULL))) WITHOUT ROWID"
    )
    connection.execute(
        "CREATE TABLE app_input_reservations (id TEXT PRIMARY KEY, "
        "app_id TEXT NOT NULL, "
        "owner_user_id TEXT NOT NULL, size_bytes INTEGER NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE app_runs (group_id TEXT PRIMARY KEY REFERENCES job_groups(id), "
        "app_id TEXT NOT NULL REFERENCES app_registrations(app_id), "
        "owner_user_id TEXT NOT NULL REFERENCES users(id), "
        "installation_id TEXT NOT NULL REFERENCES app_workloads(id), "
        "submission_key TEXT NOT NULL, payload_sha256 TEXT NOT NULL, "
        "inputs_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "UNIQUE(app_id, owner_user_id, submission_key)) WITHOUT ROWID"
    )
    unique_predicates = {
        "app_workloads": "id = NEW.id OR (app_id = NEW.app_id AND "
        "release_sha256 = NEW.release_sha256 AND workload_id = NEW.workload_id)",
        "app_inputs": "id = NEW.id",
        "app_runs": "group_id = NEW.group_id OR (app_id = NEW.app_id AND "
        "owner_user_id = NEW.owner_user_id AND submission_key = NEW.submission_key)",
    }
    for table in ("app_workloads", "app_inputs", "app_runs"):
        connection.execute(
            f"CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table} "
            f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {unique_predicates[table]}) "
            "BEGIN SELECT RAISE(ABORT, 'app compute provenance is immutable'); END"
        )
        for operation in ("UPDATE", "DELETE"):
            connection.execute(
                f"CREATE TRIGGER {table}_no_{operation.lower()} "
                f"BEFORE {operation} ON {table} BEGIN "
                "SELECT RAISE(ABORT, 'app compute provenance is immutable'); END"
            )


def add_app_release_discovery(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE app_discovery (app_id TEXT PRIMARY KEY "
        "REFERENCES app_registrations(app_id), "
        "etag TEXT, waiting_json TEXT NOT NULL DEFAULT '[]', latest_sha256 TEXT, "
        "last_success TEXT, error TEXT)"
    )
    connection.execute(
        "CREATE TABLE app_eligible_releases (app_id TEXT NOT NULL "
        "REFERENCES app_registrations(app_id), "
        "manifest_sha256 TEXT NOT NULL, release_id INTEGER NOT NULL, "
        "artifact_id INTEGER NOT NULL, "
        "artifact_sha256 TEXT NOT NULL, archive BLOB NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY "
        "KEY(app_id, manifest_sha256), "
        "UNIQUE(app_id, release_id)) WITHOUT ROWID"
    )
    for operation in ("UPDATE", "DELETE"):
        connection.execute(
            f"CREATE TRIGGER "
            f"app_eligible_releases_no_{operation.lower()} BEFORE {operation} "
            "ON app_eligible_releases BEGIN SELECT RAISE(ABORT, "
            "'release proof is immutable'); END"
        )
    connection.execute(
        "CREATE TRIGGER app_eligible_releases_no_replace BEFORE INSERT "
        "ON app_eligible_releases "
        "WHEN EXISTS (SELECT 1 FROM app_eligible_releases WHERE app_id "
        "= NEW.app_id AND "
        "(manifest_sha256 = NEW.manifest_sha256 OR release_id = NEW.release_id)) "
        "BEGIN SELECT RAISE(ABORT, 'release proof is immutable'); END"
    )
    connection.execute(
        "CREATE TABLE app_preview_attempts (attempt_id TEXT PRIMARY "
        "KEY, app_id TEXT NOT NULL, "
        "manifest_sha256 TEXT NOT NULL, bundle_sha256 TEXT NOT NULL, "
        "state TEXT NOT NULL, "
        "result_json TEXT, FOREIGN KEY(app_id, manifest_sha256) "
        "REFERENCES app_eligible_releases(app_id, manifest_sha256))"
    )
    connection.execute(
        "CREATE UNIQUE INDEX app_preview_active ON "
        "app_preview_attempts(app_id) WHERE state != 'retired'"
    )
    connection.execute(
        "CREATE TRIGGER app_preview_identity_immutable BEFORE UPDATE OF "
        "attempt_id, app_id, manifest_sha256, bundle_sha256 ON app_preview_attempts "
        "BEGIN SELECT RAISE(ABORT, 'preview identity is immutable'); END"
    )


def add_app_retention_controls(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE app_inputs ADD COLUMN deleted_at TEXT")
    connection.execute("DROP TRIGGER app_inputs_no_update")
    unchanged = " AND ".join(
        f"NEW.{column} IS OLD.{column}"
        for column in (
            "id",
            "app_id",
            "owner_user_id",
            "installation_id",
            "object_key",
            "logical_path",
            "fingerprint",
            "size_bytes",
            "sha256",
        )
    )
    connection.execute(
        "CREATE TRIGGER app_inputs_no_update BEFORE UPDATE ON app_inputs "
        "WHEN NOT (OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL AND "
        f"{unchanged}) "
        "BEGIN SELECT RAISE(ABORT, 'app compute provenance is immutable'); END"
    )
    connection.execute(
        "CREATE TABLE app_run_results (group_id TEXT PRIMARY KEY "
        "REFERENCES app_runs(group_id), outputs_json TEXT NOT NULL) WITHOUT ROWID"
    )
    for operation in ("UPDATE", "DELETE"):
        connection.execute(
            f"CREATE TRIGGER app_run_results_no_{operation.lower()} "
            f"BEFORE {operation} ON app_run_results BEGIN "
            "SELECT RAISE(ABORT, 'app run results are immutable'); END"
        )
    connection.execute(
        "CREATE TABLE app_release_attempts (app_id TEXT NOT NULL "
        "REFERENCES app_registrations(app_id), release_id INTEGER NOT NULL, "
        "attempts INTEGER NOT NULL, next_attempt REAL NOT NULL, "
        "PRIMARY KEY(app_id, release_id)) WITHOUT ROWID"
    )


def add_app_deployments(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE app_operations (operation_id TEXT PRIMARY KEY, "
        "request_id TEXT NOT NULL UNIQUE, "
        "app_id TEXT NOT NULL REFERENCES app_registrations(app_id), "
        "environment TEXT NOT NULL, "
        "kind TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN "
        "('queued','switching','succeeded','failed','recovered','blocked')), "
        "operation_json TEXT NOT NULL, receipt_json TEXT, created_at "
        "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, reconcile_error TEXT, "
        "reconcile_failed_at TEXT)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX app_operation_active ON "
        "app_operations(app_id, environment) "
        "WHERE state IN ('queued','switching')"
    )
    connection.execute(
        "CREATE UNIQUE INDEX app_operation_blocked ON "
        "app_operations(app_id, environment) WHERE state = 'blocked'"
    )
    connection.execute(
        "CREATE TABLE app_installed_history (operation_id TEXT "
        "PRIMARY KEY REFERENCES app_operations(operation_id), app_id "
        "TEXT NOT NULL, environment TEXT NOT NULL, generation INTEGER"
        " NOT NULL, snapshot_json TEXT NOT NULL, UNIQUE(app_id, "
        "environment, generation))"
    )
    connection.execute(
        "CREATE TABLE app_rejections (app_id TEXT NOT NULL, "
        "manifest_sha256 TEXT NOT NULL, administrator_id TEXT NOT "
        "NULL, PRIMARY KEY(app_id, manifest_sha256))"
    )
    connection.execute(
        "CREATE TABLE app_registration_changes (id INTEGER PRIMARY "
        "KEY, app_id TEXT NOT NULL, definition_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )


def add_release_commit_facts(connection: sqlite3.Connection) -> None:
    for column in ("commit_title", "committed_at"):
        connection.execute(
            f"ALTER TABLE app_eligible_releases ADD COLUMN {column} TEXT"
        )


def add_registration_proposals(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE app_registration_proposals (proposal_id INTEGER "
        "PRIMARY KEY, app_id TEXT NOT NULL REFERENCES app_registrations(app_id), "
        "environment TEXT NOT NULL, base_generation INTEGER NOT NULL, "
        "definition_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    connection.execute(
        "CREATE TABLE app_registration_rejections (proposal_id INTEGER "
        "PRIMARY KEY REFERENCES app_registration_proposals(proposal_id), "
        "administrator_id TEXT NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    for table in ("app_registration_proposals", "app_registration_rejections"):
        connection.execute(
            f"CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table} "
            f"WHEN EXISTS (SELECT 1 FROM {table} WHERE proposal_id = "
            "NEW.proposal_id) BEGIN "
            "SELECT RAISE(ABORT, 'registration proposals are immutable'); END"
        )
        for operation in ("UPDATE", "DELETE"):
            connection.execute(
                f"CREATE TRIGGER {table}_no_{operation.lower()} "
                f"BEFORE {operation} ON {table} BEGIN "
                "SELECT RAISE(ABORT, 'registration proposals are immutable'); END"
            )


def add_app_pins(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE app_pins (user_id TEXT NOT NULL REFERENCES users(id) "
        "ON DELETE CASCADE, app_id TEXT NOT NULL REFERENCES "
        "app_registrations(app_id), PRIMARY KEY (user_id, app_id))"
    )
    # Every bar looked like this before pins existed; only later registrations
    # start unpinned.
    connection.execute(
        "INSERT INTO app_pins (user_id, app_id) "
        "SELECT users.id, app_registrations.app_id "
        "FROM users CROSS JOIN app_registrations"
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
    (17, add_application_registry),
    (18, add_app_runtime_identity),
    (19, add_app_compute),
    (20, add_app_release_discovery),
    (21, add_app_retention_controls),
    (22, add_app_deployments),
    (23, add_release_commit_facts),
    (24, add_registration_proposals),
    (25, add_app_pins),
)
