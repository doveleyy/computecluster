import hashlib
import json
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from app import migrations
from app.app_contracts import (
    ContractError,
    Registration,
    ReleaseManifest,
    parse_document,
)
from app.app_registry import (
    CORE_ROUTE_TREES,
    AppRegistry,
    RegistrationConflict,
    decode_registration,
    routes_overlap,
)
from app.database import Database
from app.main import create_app
from app.platform_apps import Role, render_launcher_tiles


def registration(app_id: str = "notes", route: str = "/notes") -> dict[str, Any]:
    return {
        "app_id": app_id,
        "route": route,
        "source": {
            "kind": "published",
            "repository": "example/notes",
            "branch": "main",
            "workflow": ".github/workflows/publish.yml",
            "image_repository": "ghcr.io/example/notes",
        },
        "access": "linked-user",
        "capabilities": ["identity", "jobs"],
        "presentation": {
            "label": "Notes <test>",
            "health_name": "Notes",
            "position": 35,
            "accent": "#123456",
        },
        "bindings": {
            "secret_files": [
                {"name": "provider-key", "file_path": "/private/test-key"}
            ],
            "settings": [{"name": "NOTES_THEME", "value": "dark"}],
            "state_directory": "/private/test-state",
        },
        "installation": {
            "owner": "legacy-scripts",
            "host_port": 19001,
            "probe": {"service": app_id, "readiness": "v1-status"},
        },
    }


def manifest() -> dict[str, Any]:
    return {
        "contract_version": 1,
        "app_id": "notes",
        "source_commit": "a" * 40,
        "image": "ghcr.io/example/notes@sha256:" + "b" * 64,
        "runtime": {
            "port": 8080,
            "health_path": "/health",
            "ready_path": "/ready",
            "version_path": "/version.json",
        },
        "capabilities": ["identity", "jobs"],
        "state": {"sqlite_file": ".notes.db"},
        "secrets": ["provider-key"],
        "maintenance": [
            {
                "name": "cleanup",
                "argv": ["python", "-m", "notes.cleanup"],
                "interval_hours": 6,
                "timeout_seconds": 300,
            }
        ],
        "workloads": [
            {
                "id": "report",
                "asset": "report.tar.gz",
                "sha256": "c" * 64,
                "entrypoint": "scripts/.report.py",
                "runtime": "scientific-python:1",
            }
        ],
    }


def document(value: object) -> bytes:
    return json.dumps(value).encode()


@pytest.fixture
def registry(tmp_path: Path) -> AppRegistry:
    database = Database(tmp_path / "platform.db")
    database.initialize()
    return AppRegistry(database)


@pytest.mark.parametrize(
    "prelude",
    [
        "",
        "PRAGMA user_version = 42",
        "CREATE TABLE transient (id INTEGER PRIMARY KEY AUTOINCREMENT); "
        "DROP TABLE transient",
    ],
)
def test_fresh_core_starts_without_external_apps_and_explicit_registration_survives(
    tmp_path, prelude
):
    path = tmp_path / "fresh.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(prelude)
    database = Database(path)
    database.initialize()
    registry = AppRegistry(database)

    assert registry.summaries() == ()
    assert registry.monitoring() == ()
    assert [app.key for app in registry.navigation(Role.MEMBER)] == ["home", "jobs"]
    assert [app.key for app in registry.navigation(Role.ADMINISTRATOR)] == [
        "home",
        "dashboard",
    ]
    tiles = render_launcher_tiles(registry.navigation(Role.ADMINISTRATOR))
    assert 'href="/dashboard"' in tiles
    for route in ("/habits", "/wishlist", "/transport", "/sorter"):
        assert f'href="{route}' not in tiles

    assert registry.register(document(registration())) == "recorded"
    registry.ingest_release("notes", document(manifest()))
    before = registry._registrations(), registry.summaries()
    database.initialize()

    assert (registry._registrations(), registry.summaries()) == before
    assert [app.key for app in registry.navigation(Role.MEMBER)] == [
        "home",
        "notes",
        "jobs",
    ]
    assert [target.key for target in registry.monitoring()] == ["notes"]
    assert 'href="/notes/"' in render_launcher_tiles(registry.navigation(Role.MEMBER))
    database.close()


@pytest.mark.parametrize(
    "prelude",
    [
        "CREATE TABLE marker (value TEXT)",
        "CREATE VIEW marker AS SELECT 1 AS value",
        "CREATE TABLE sqliteXmarker (value TEXT)",
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
        "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
    ],
)
def test_existing_schema_without_migration_rows_keeps_historical_upgrade_seeds(
    tmp_path, prelude
):
    path = tmp_path / "existing.db"
    with sqlite3.connect(path) as connection:
        connection.execute(prelude)
    database = Database(path)
    database.initialize()
    registry = AppRegistry(database)
    assert {item.app_id for item in registry.summaries()} == {
        "habits",
        "wishlist",
        "transport",
        "sorter",
    }
    database.close()


def test_failed_fresh_initialization_rolls_back_schema_and_retries(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "fresh-retry.db")

    def interrupted(connection):
        assert (
            connection.execute("SELECT count(*) FROM app_registrations").fetchone()[0]
            == 0
        )
        raise sqlite3.OperationalError("injected bootstrap failure")

    with monkeypatch.context() as failure:
        failure.setattr(
            migrations, "MIGRATIONS", (*migrations.MIGRATIONS, (26, interrupted))
        )
        with pytest.raises(sqlite3.OperationalError, match="injected bootstrap"):
            database.initialize()
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
            ).fetchall()
            == []
        )
    database.initialize()
    registry = AppRegistry(database)
    assert registry.summaries() == ()
    assert registry.register(document(registration())) == "recorded"
    assert [item.app_id for item in registry.summaries()] == ["notes"]
    database.close()


def test_fresh_bootstrap_preserves_registrations_added_after_historical_seeds(
    tmp_path, monkeypatch
):
    value = registration("habits", "/habits")

    def register_explicit_app(connection):
        definition = {
            key: item for key, item in value.items() if key not in {"app_id", "route"}
        }
        connection.execute(
            "INSERT INTO app_registrations (app_id, route, definition_json) "
            "VALUES (?, ?, ?)",
            ("habits", "/habits", json.dumps(definition)),
        )

    monkeypatch.setattr(
        migrations, "MIGRATIONS", (*migrations.MIGRATIONS, (26, register_explicit_app))
    )
    database = Database(tmp_path / "explicit-migration.db")
    database.initialize()
    registry = AppRegistry(database)
    assert [item.app_id for item in registry.summaries()] == ["habits"]
    assert registry._registrations()[0] == parse_document(Registration, document(value))
    database.close()


def test_register_retries_semantically_equal_definitions_and_rejects_changes(registry):
    value = registration()
    assert registry.register(document(value)) == "recorded"
    assert registry.register(json.dumps(value, indent=2).encode()) == "unchanged"
    value["presentation"]["label"] = "Changed"
    with pytest.raises(RegistrationConflict):
        registry.register(document(value))
    assert (
        next(item.label for item in registry.summaries() if item.app_id == "notes")
        == "Notes <test>"
    )


def test_dynamic_installed_and_catalog_records_feed_both_projections_without_paths(
    registry,
):
    value = registration()
    registry.register(document(value))
    member = registry.navigation(Role.MEMBER)
    assert [app.key for app in member] == [
        "home",
        "notes",
        "jobs",
    ]
    assert [app.key for app in registry.navigation(Role.ADMINISTRATOR)] == [
        "home",
        "notes",
        "dashboard",
    ]
    assert [target.key for target in registry.monitoring()] == [
        "notes",
    ]
    catalog = registration("catalog", "/catalog")
    catalog["installation"] = None
    registry.register(document(catalog))
    assert "catalog" not in [app.key for app in registry.navigation()]
    assert "catalog" not in [target.key for target in registry.monitoring()]
    safe = repr((member, registry.monitoring(), registry.summaries()))
    assert "/private/" not in safe
    assert "provider-key" not in safe


def test_release_keeps_exact_bytes_is_immutable_and_does_not_change_installation(
    registry,
):
    registry.register(document(registration()))
    before = registry.navigation(), registry.monitoring(), registry._registrations()
    raw = document(manifest())
    receipt = registry.ingest_release("notes", raw)
    assert receipt.outcome == "recorded"
    assert receipt.manifest_sha256 == hashlib.sha256(raw).hexdigest()
    assert registry.ingest_release("notes", raw).outcome == "unchanged"
    assert registry.ingest_release("notes", raw + b"\n").outcome == "recorded"
    assert before == (
        registry.navigation(),
        registry.monitoring(),
        registry._registrations(),
    )
    with registry.database.connect() as connection:
        row = connection.execute(
            "SELECT manifest_bytes FROM app_releases WHERE manifest_sha256 = ?",
            (receipt.manifest_sha256,),
        ).fetchone()
        assert bytes(row[0]) == raw
    for sql in (
        "UPDATE app_releases SET manifest_bytes = X'7b7d'",
        "UPDATE app_releases SET manifest_sha256 = 'changed'",
        "DELETE FROM app_releases",
    ):
        with (
            pytest.raises(sqlite3.IntegrityError, match="immutable"),
            registry.database.connect() as connection,
        ):
            connection.execute(sql)
    with (
        pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"),
        registry.database.connect() as connection,
    ):
        connection.execute(
            "INSERT INTO app_releases VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            ("missing", "f" * 64, raw),
        )


@pytest.mark.parametrize("insert", ["INSERT OR REPLACE", "REPLACE"])
def test_release_replacement_preserves_original_bytes_and_timestamp(registry, insert):
    registry.register(document(registration()))
    raw = document(manifest())
    receipt = registry.ingest_release("notes", raw)
    key = ("notes", receipt.manifest_sha256)
    with registry.database.connect() as connection:
        assert connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        original = tuple(
            connection.execute(
                "SELECT manifest_bytes, created_at FROM app_releases "
                "WHERE app_id = ? AND manifest_sha256 = ?",
                key,
            ).fetchone()
        )
    replacement = manifest()
    replacement["source_commit"] = "d" * 40
    changed = document(replacement)
    with (
        pytest.raises(
            sqlite3.IntegrityError, match="application releases are immutable"
        ),
        registry.database.connect() as connection,
    ):
        connection.execute(
            f"{insert} INTO app_releases "
            "(app_id, manifest_sha256, manifest_bytes, created_at) VALUES (?, ?, ?, ?)",
            (*key, changed, "1900-01-01 00:00:00"),
        )
    with registry.database.connect() as connection:
        retained = tuple(
            connection.execute(
                "SELECT manifest_bytes, created_at FROM app_releases "
                "WHERE app_id = ? AND manifest_sha256 = ?",
                key,
            ).fetchone()
        )
    assert retained == original
    assert bytes(retained[0]) == raw
    assert registry.ingest_release("notes", raw).outcome == "unchanged"
    assert registry.ingest_release("notes", changed).outcome == "recorded"
    assert registry.ingest_release("notes", changed).outcome == "unchanged"


@pytest.mark.parametrize(
    "app_id,route",
    [
        ("home", "/new"),
        ("jobs", "/new"),
        ("dashboard", "/new"),
        *[("notes", tree) for tree in CORE_ROUTE_TREES],
        ("notes", "/jobs/child"),
        ("notes", "/habits/child"),
    ],
)
def test_core_and_existing_route_trees_are_reserved(registry, app_id, route):
    registry.register(document(registration("habits", "/habits")))
    with pytest.raises(RegistrationConflict):
        registry.register(document(registration(app_id, route)))


def test_every_actual_core_route_is_reserved():
    for route in create_app().routes:
        path = getattr(route, "path", "/")
        assert path == "/" or any(
            routes_overlap(path, tree) for tree in CORE_ROUTE_TREES
        ), path


def test_route_parent_collides_but_segment_neighbors_do_not(registry):
    registry.register(document(registration("child", "/custom/child")))
    with pytest.raises(RegistrationConflict):
        registry.register(document(registration("parent", "/custom")))
    assert (
        registry.register(document(registration("neighbor", "/customized")))
        == "recorded"
    )


def test_concurrent_overlapping_registration_has_exactly_one_winner(registry):
    barrier = threading.Barrier(2)

    def register_pair(pair):
        try:
            barrier.wait()
            return registry.register(document(registration(*pair)))
        except RegistrationConflict:
            return "conflict"
        finally:
            registry.database.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(register_pair, [("parent", "/race"), ("child", "/race/child")])
        )
    assert sorted(results) == ["conflict", "recorded"]
    assert (
        len([item for item in registry.summaries() if item.route.startswith("/race")])
        == 1
    )


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/notes/",
        "/notes//x",
        "/notes/../x",
        "/notes%2fx",
        "/notes?q=x",
        "https://evil/x",
        "/notes#x",
    ],
)
def test_registration_rejects_noncanonical_routes(registry, path):
    with pytest.raises(ContractError):
        registry.register(document(registration(route=path)))


@pytest.mark.parametrize(
    "field,value",
    [
        ("contract_version", True),
        ("contract_version", 1.0),
        ("contract_version", "1"),
        ("contract_version", 2),
        ("app_id", "other"),
        ("source_commit", "short"),
        ("image", "ghcr.io/example/notes:latest"),
        ("image", "ghcr.io/example/notes@garbage@sha256:" + "b" * 64),
        ("image", "ghcr.io/example/notes:tag@sha256:" + "b" * 64),
        ("image", "ghcr.io/evil/notes@sha256:" + "b" * 64),
        ("capabilities", ["identity", "files"]),
        ("capabilities", ["identity", "identity"]),
        ("capabilities", ["jobs"]),
        ("secrets", ["unknown"]),
        ("secrets", ["provider-key", "provider_key"]),
        ("state", {"sqlite_file": "../escape.db"}),
        ("state", {"sqlite_file": "db/name.db"}),
        ("state", None),
        ("state", {"sqlite_file": "notes.db", "host_path": "/private/test-state"}),
        ("privileged", True),
        ("bindings", {"secrets": "credential"}),
    ],
)
def test_release_rejects_untrusted_or_coerced_fields_without_insertion(
    registry, field, value
):
    registry.register(document(registration()))
    release = manifest()
    release[field] = value
    with pytest.raises(ContractError):
        registry.ingest_release("notes", document(release))
    assert all(item.release_count == 0 for item in registry.summaries())


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("runtime", "port", True),
        ("runtime", "port", "8080"),
        ("runtime", "port", 65536),
        ("runtime", "ready_path", "//evil/ready"),
        ("runtime", "health_path", "/%2e%2e/health"),
        ("runtime", "version_path", "/version?token=secret"),
        ("runtime", "extra", "private"),
        ("maintenance", "timeout_seconds", 301),
        ("maintenance", "interval_hours", 1.0),
        ("maintenance", "argv", []),
        ("maintenance", "extra", "private"),
        ("workloads", "asset", "../report.tar.gz"),
        ("workloads", "sha256", "bad"),
        ("workloads", "entrypoint", "./report.py"),
        ("workloads", "entrypoint", "scripts/%2e%2e/report.py"),
        ("workloads", "runtime", "arbitrary"),
        ("workloads", "extra", "private"),
    ],
)
def test_nested_release_contract_is_strict(registry, section, key, value):
    registry.register(document(registration()))
    release = manifest()
    nested = release[section]
    (nested[0] if isinstance(nested, list) else nested)[key] = value
    with pytest.raises(ContractError):
        registry.ingest_release("notes", document(release))


@pytest.mark.parametrize(
    "name", ["APP_ID", "TZ", "APP_CREDENTIAL_FILE", "APP_SECRET_UNKNOWN_FILE"]
)
def test_reserved_environment_names_are_not_operator_settings(registry, name):
    value = registration()
    value["bindings"]["settings"][0]["name"] = name
    with pytest.raises(ContractError):
        registry.register(document(value))


def test_duplicate_secret_variables_and_unknown_private_fields_fail_safely(registry):
    value = registration()
    value["bindings"]["secret_files"].append(
        {"name": "provider_key", "file_path": "/private/other"}
    )
    with pytest.raises(ContractError):
        registry.register(document(value))
    value = registration()
    value["bindings"]["credential"] = "never-print-this-credential"
    with pytest.raises(ContractError) as error:
        registry.register(document(value))
    assert "never-print" not in str(error.value)
    assert "/private/" not in str(error.value)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"app_id":"notes","app_id":"other"}',
        b'{"bindings":{"x":1,"x":2}}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b"\xff",
        b"[" * 10000,
        b" " * 65537,
    ],
)
def test_bounded_json_rejects_duplicates_nonfinite_and_invalid_input(registry, payload):
    with pytest.raises(ContractError):
        registry.register(payload)


def test_legacy_and_unregistered_sources_cannot_admit_releases(registry):
    legacy = registration("legacy", "/legacy")
    legacy["source"] = {"kind": "legacy"}
    registry.register(document(legacy))
    with pytest.raises(ContractError, match="Legacy"):
        registry.ingest_release("legacy", document(manifest()))
    with pytest.raises(ContractError, match="not registered"):
        registry.ingest_release("missing", document(manifest()))


def test_release_can_omit_sqlite_state(registry):
    registry.register(document(registration()))
    value = manifest()
    del value["state"]
    assert registry.ingest_release("notes", document(value)).outcome == "recorded"


def test_concurrent_identical_release_retry_retains_one_record(registry):
    registry.register(document(registration()))
    barrier = threading.Barrier(2)
    raw = document(manifest())

    def ingest():
        try:
            barrier.wait()
            return registry.ingest_release("notes", raw).outcome
        finally:
            registry.database.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ingest) for _ in range(2)]
        assert sorted(future.result() for future in futures) == [
            "recorded",
            "unchanged",
        ]
    assert (
        next(
            item.release_count
            for item in registry.summaries()
            if item.app_id == "notes"
        )
        == 1
    )


def test_registration_models_and_nested_objects_are_frozen():
    parsed = parse_document(Registration, document(registration()))
    with pytest.raises(ValueError):
        parsed.app_id = "changed"
    with pytest.raises(ValueError):
        parsed.presentation.label = "changed"


def test_migration_17_preserves_previous_tables_and_does_not_reseed(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "old.db")
    with monkeypatch.context() as before:
        before.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:16])
        database.initialize()
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO workers "
            "(id, supported_types_json, registered_at, last_seen, enabled) "
            "VALUES ('disabled-worker', '[]', 'before', 'before', 0)"
        )
        connection.execute(
            "INSERT INTO jobs "
            "(id,type,parameters_json,status,created_at,updated_at,owner_user_id) "
            "VALUES ('old-job','sleep','{}','QUEUED','before','before',?)",
            ("00000000-0000-0000-0000-000000000001",),
        )
        connection.execute(
            "INSERT INTO job_groups "
            "(id,name,request_json,created_at,updated_at,owner_user_id) "
            "VALUES ('old-group','before','{}','before','before',?)",
            ("00000000-0000-0000-0000-000000000001",),
        )
        connection.execute(
            "INSERT INTO uploads (id,owner_user_id,kind,created_at) "
            "VALUES ('old-upload',?,'input','before')",
            ("00000000-0000-0000-0000-000000000001",),
        )
        connection.execute(
            "INSERT INTO external_identities "
            "(provider,subject,user_id,display_name,linked_at) "
            "VALUES ('tailscale','before@example.test',?,'before','before')",
            ("00000000-0000-0000-0000-000000000001",),
        )
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            if row[0] != "schema_migrations"
        ]
        old = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
    database.initialize()
    registry = AppRegistry(database)
    legacy = next(item for item in registry._registrations() if item.app_id == "habits")
    assert registry.register(legacy.model_dump_json().encode()) == "unchanged"
    with database.connect() as connection:
        assert old == {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
        row = connection.execute(
            "SELECT * FROM app_registrations WHERE app_id='habits'"
        ).fetchone()
        value = decode_registration(row["app_id"], row["route"], row["definition_json"])
        changed = value.model_copy(
            update={
                "installation": value.installation.model_copy(
                    update={"host_port": 19100}
                )
            }
        )
        connection.execute(
            "UPDATE app_registrations SET definition_json=? WHERE app_id='habits'",
            (changed.model_dump_json(exclude={"app_id", "route"}),),
        )
    database.initialize()
    assert registry.monitoring()[0].port == 19100
    assert len(registry.summaries()) == 4
    assert all(
        item.source == "legacy" and item.release_count == 0
        for item in registry.summaries()
    )
    assert "sorter" not in [target.key for target in registry.monitoring()]


def test_operator_cli_records_retries_lists_and_sanitizes_errors(tmp_path):
    import sys

    definition = tmp_path / "registration.json"
    definition.write_bytes(document(registration()))
    release = tmp_path / "manifest.json"
    release.write_bytes(document(manifest()))
    database = Database(tmp_path / "cli.db")
    database.initialize()
    database.close()
    argv = [sys.executable, "-m", "ops.apps", "--database", str(database.path)]

    def run(*command):
        return subprocess.run([*argv, *command], capture_output=True, text=True)

    assert json.loads(run("register", str(definition)).stdout)["outcome"] == "recorded"
    assert json.loads(run("register", str(definition)).stdout)["outcome"] == "unchanged"
    assert (
        json.loads(run("ingest", "notes", str(release)).stdout)["outcome"] == "recorded"
    )
    assert (
        json.loads(run("ingest", "notes", str(release)).stdout)["outcome"]
        == "unchanged"
    )
    listing = run("list")
    assert listing.returncode == 0
    assert "/private/" not in listing.stdout
    assert (
        next(item for item in json.loads(listing.stdout) if item["app_id"] == "notes")[
            "release_count"
        ]
        == 1
    )
    definition.write_bytes(b'{"secret":"never-print-credential"}')
    invalid = run("register", str(definition))
    assert invalid.returncode == 2
    assert "never-print" not in invalid.stderr
    assert (
        str(tmp_path)
        not in run("register", str(tmp_path / "missing-private-file")).stderr
    )


def test_migration_17_rolls_back_partial_ddl_and_retries_on_schema_16(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "migration-retry.db")
    all_migrations = migrations.MIGRATIONS
    with monkeypatch.context() as before:
        before.setattr(migrations, "MIGRATIONS", all_migrations[:16])
        database.initialize()

    class FailSecondTable:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            if "CREATE TABLE app_releases" in sql:
                raise sqlite3.OperationalError("injected migration failure")
            return self.connection.execute(sql, *args)

    def interrupted(connection):
        migrations.add_application_registry(FailSecondTable(connection))

    with monkeypatch.context() as failure:
        failure.setattr(
            migrations, "MIGRATIONS", (*all_migrations[:16], (17, interrupted))
        )
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            database.initialize()
    with database.connect() as connection:
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'app_%'"
        ).fetchall()
        assert (
            connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[
                0
            ]
            == 16
        )
    database.initialize()
    assert len(AppRegistry(database).summaries()) == 4
    assert not AppRegistry(database).summaries()[0].release_count


def test_migration_21_rolls_back_dropped_trigger_and_retries_on_schema_20(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "migration-21-retry.db")
    all_migrations = migrations.MIGRATIONS
    with monkeypatch.context() as before:
        before.setattr(migrations, "MIGRATIONS", all_migrations[:20])
        database.initialize()

    class FailAfterDrop:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            if sql.startswith("CREATE TRIGGER app_inputs_no_update"):
                raise sqlite3.OperationalError("injected migration failure")
            return self.connection.execute(sql, *args)

    def interrupted(connection):
        migrations.add_app_retention_controls(FailAfterDrop(connection))

    with monkeypatch.context() as failure:
        failure.setattr(
            migrations, "MIGRATIONS", (*all_migrations[:20], (21, interrupted))
        )
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            database.initialize()
    with database.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'app_inputs_no_update'"
        ).fetchone()
        assert "deleted_at" not in {
            row["name"]
            for row in connection.execute("PRAGMA table_info(app_inputs)").fetchall()
        }
        assert (
            connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[
                0
            ]
            == 20
        )
    database.initialize()
    with database.connect() as connection:
        assert (
            connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[
                0
            ]
            == all_migrations[-1][0]
        )


@pytest.mark.parametrize("legacy", [False, True])
def test_concurrent_initialization_applies_each_migration_once(
    tmp_path, monkeypatch, legacy
):
    path = tmp_path / "concurrent-init.db"
    all_migrations = migrations.MIGRATIONS
    if legacy:
        with monkeypatch.context() as before:
            before.setattr(migrations, "MIGRATIONS", all_migrations[:16])
            Database(path).initialize()

    first_started = threading.Event()
    release_first = threading.Event()

    def held_registry(connection):
        if not first_started.is_set():
            first_started.set()
            assert release_first.wait(timeout=5)
        migrations.add_application_registry(connection)

    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        tuple(
            (version, held_registry if version == 17 else migration)
            for version, migration in all_migrations
        ),
    )
    errors: list[BaseException] = []

    def initialize(database):
        try:
            database.initialize()
        except BaseException as error:
            errors.append(error)
        finally:
            database.close()

    first = threading.Thread(target=initialize, args=(Database(path),))
    first.start()
    assert first_started.wait(timeout=5)
    second = threading.Thread(target=initialize, args=(Database(path),))
    second.start()
    second.join(timeout=0.3)
    release_first.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not errors
    with sqlite3.connect(path) as connection:
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
    assert versions == [version for version, _ in all_migrations]
    assert {item.app_id for item in AppRegistry(Database(path)).summaries()} == (
        {"habits", "wishlist", "transport", "sorter"} if legacy else set()
    )


def test_operator_cli_refuses_missing_or_unmigrated_database_without_migrating(
    tmp_path, monkeypatch
):
    import sys

    definition = tmp_path / "registration.json"
    definition.write_bytes(document(registration()))
    missing = tmp_path / "typo.db"
    old = Database(tmp_path / "old.db")
    with monkeypatch.context() as before:
        before.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:16])
        old.initialize()
    old.close()

    for path in (missing, old.path):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ops.apps",
                "--database",
                str(path),
                "register",
                str(definition),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert result.stdout == ""
        assert "control plane" in result.stderr
        assert str(tmp_path) not in result.stderr
    assert not missing.exists()
    with sqlite3.connect(old.path) as connection:
        assert (
            connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[
                0
            ]
            == 16
        )
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'app_%'"
        ).fetchall()


def test_contract_errors_name_the_failing_field_without_echoing_input():
    def failure(model, value):
        with pytest.raises(ContractError) as error:
            parse_document(model, document(value))
        return str(error.value)

    privileged = manifest()
    privileged["runtime"]["privileged"] = "sk-live-never-print"
    assert failure(ReleaseManifest, privileged) == (
        "Invalid JSON contract document: runtime.privileged: "
        "Extra inputs are not permitted"
    )
    unsupported = manifest()
    unsupported["contract_version"] = 2
    assert failure(ReleaseManifest, unsupported).startswith(
        "Invalid JSON contract document: contract_version: "
    )
    unpinned = manifest()
    unpinned["image"] = "ghcr.io/example/notes:sk-live-never-print"
    message = failure(ReleaseManifest, unpinned)
    assert message.startswith("Invalid JSON contract document: image: ")
    assert "sk-live-never-print" not in message
    tagged = registration()
    tagged["source"]["kind"] = "sk-live-never-print"
    message = failure(Registration, tagged)
    assert message.startswith("Invalid JSON contract document: source: ")
    assert "sk-live-never-print" not in message


def test_registration_route_must_be_lowercase(registry):
    with pytest.raises(ContractError):
        registry.register(document(registration(route="/Notes")))
    assert "/Notes" not in [item.route for item in registry.summaries()]
    assert registry.register(document(registration(route="/notes"))) == "recorded"


def test_release_cannot_be_replaced_through_hidden_rowid(registry):
    registry.register(document(registration()))
    raw = document(manifest())
    registry.ingest_release("notes", raw)
    with registry.database.connect() as connection:
        before = [
            tuple(row) for row in connection.execute("SELECT * FROM app_releases")
        ]
        with pytest.raises(sqlite3.Error):
            connection.execute(
                "INSERT OR REPLACE INTO app_releases "
                "(rowid, app_id, manifest_sha256, manifest_bytes) VALUES (1, ?, ?, ?)",
                ("notes", "d" * 64, b"replaced"),
            )
        assert before == [
            tuple(row) for row in connection.execute("SELECT * FROM app_releases")
        ]
