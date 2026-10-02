"""SQLite state for the sorter: projects, skips, hashes, folder notes and
decisions.

A *project* pairs one dump folder with one tree folder, both relative to the
library root. Several projects may share a tree; everything that describes a
tree — its decisions' labels, folder moves and folder descriptions — is keyed
by that tree, so every project feeding it sees one taxonomy.

Decisions are append-only label truth. Undo only stamps `undone_at`; the row
stays, so the log also records what the owner reconsidered.

Reorganizing a tree never rewrites a decision. Each folder move is logged
with the last decision it applies to, and decisions are read through every
later move in the same tree: a file sorted into `school/DSA3102` reads as
`school/data_science/DSA3102` once that folder is grouped, while the
original choice stays available as `decided_label`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('top', 'files')),
    created_at TEXT NOT NULL,
    archived_at TEXT
);
CREATE TABLE IF NOT EXISTS skips (
    project_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    skipped_at TEXT NOT NULL,
    PRIMARY KEY (project_id, path)
);
CREATE TABLE IF NOT EXISTS hashes (
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (path, size, mtime_ns)
);
CREATE TABLE IF NOT EXISTS folders (
    target TEXT NOT NULL,
    path TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (target, path)
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decided_at TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('sort', 'discard')),
    kind TEXT NOT NULL CHECK (kind IN ('file', 'folder')),
    original_name TEXT NOT NULL,
    final_name TEXT NOT NULL,
    label TEXT NOT NULL,
    destination TEXT NOT NULL,
    sha256 TEXT,
    size INTEGER,
    undone_at TEXT,
    project_id INTEGER NOT NULL,
    target TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_sha256 ON decisions (sha256);
CREATE TABLE IF NOT EXISTS folder_moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    moved_at TEXT NOT NULL,
    old_path TEXT NOT NULL,
    new_path TEXT NOT NULL,
    -- Decisions up to and including this ID were made under the old path.
    through_decision_id INTEGER NOT NULL,
    target TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS duplicate_resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decided_at TEXT NOT NULL,
    project_id INTEGER NOT NULL,
    target TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    completed_at TEXT,
    cancelled_at TEXT
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS file_index (
    path TEXT PRIMARY KEY,
    base TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    document_id INTEGER NOT NULL,
    decision_id INTEGER,
    size INTEGER,
    mtime_ns TEXT,
    sha256 TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    present INTEGER NOT NULL DEFAULT 1,
    provenance TEXT NOT NULL,
    reviewed INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    checked_at TEXT
);
CREATE INDEX IF NOT EXISTS file_index_hash ON file_index(base, sha256);
CREATE TABLE IF NOT EXISTS document_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    target TEXT NOT NULL,
    path TEXT NOT NULL,
    document_id INTEGER NOT NULL,
    previous_document_id INTEGER,
    event TEXT NOT NULL,
    sha256 TEXT
);
CREATE TABLE IF NOT EXISTS index_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    epoch INTEGER NOT NULL DEFAULT 0,
    last_completed_at TEXT,
    error TEXT,
    checked INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    running INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO index_state(id) VALUES(1);
CREATE TABLE IF NOT EXISTS index_roots (
    base TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Project:
    id: int
    name: str
    source: str
    target: str
    mode: str
    created_at: str
    archived_at: str | None


@dataclass(frozen=True)
class Decision:
    id: int
    decided_at: str
    action: str
    kind: str
    # The entry's path inside its dump: a plain name in `top` mode.
    original_name: str
    final_name: str
    label: str
    destination: str
    sha256: str | None
    size: int | None
    undone_at: str | None
    project_id: int
    target: str
    decided_label: str
    replaces_id: int | None
    previous_destination: str | None
    origin_project_id: int | None
    document_id: int | None
    duplicate_group_id: int | None
    duplicate_of_document_id: int | None
    review_type: str | None
    file_mtime_ns: str | None


@dataclass(frozen=True)
class Seed:
    """The project created on first start, and owner of pre-project rows."""

    name: str
    source: str
    target: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def moved_path(path: str, old: str, new: str) -> str:
    if path == old:
        return new
    if path.startswith(f"{old}/"):
        return new + path[len(old) :]
    return path


class SorterRepository:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self, seed: Seed | None = None) -> None:
        """Create or upgrade the schema in one transaction, then seed."""
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self._database_path, timeout=30, isolation_level=None
        )
        try:
            # Explicit transaction: SQLite schema changes are transactional,
            # so a failed upgrade leaves the old database exactly as it was.
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("This sorter database needs a newer service")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if version == 0 and "decisions" in tables:
                self._migrate_from_single_library(connection, tables, seed)
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(decisions)")
            }
            for name, kind in (
                ("replaces_id", "INTEGER"),
                ("previous_destination", "TEXT"),
                ("origin_project_id", "INTEGER"),
                ("document_id", "INTEGER"),
                ("duplicate_group_id", "INTEGER"),
                ("duplicate_of_document_id", "INTEGER"),
                ("review_type", "TEXT"),
                ("file_mtime_ns", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE decisions ADD COLUMN {name} {kind}"
                    )
                    if name == "document_id":
                        connection.execute("UPDATE decisions SET document_id = id")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS decisions_replaces "
                "ON decisions (replaces_id)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO documents(id, created_at) "
                "SELECT COALESCE(document_id,id), MIN(decided_at) "
                "FROM decisions GROUP BY COALESCE(document_id,id)"
            )
            connection.execute("UPDATE index_state SET running = 0 WHERE id = 1")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            if seed and not connection.execute("SELECT 1 FROM projects").fetchone():
                connection.execute(
                    "INSERT INTO projects (name, source, target, mode, created_at) "
                    "VALUES (?, ?, ?, 'top', ?)",
                    (seed.name, seed.source, seed.target, _now()),
                )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @staticmethod
    def _migrate_from_single_library(
        connection: sqlite3.Connection, tables: set[str], seed: Seed | None
    ) -> None:
        """Version 0 held one dump/tree pair; give its rows to a project."""
        if seed is None:
            raise RuntimeError(
                "This database predates projects: set SORTER_SEED_SOURCE and "
                "SORTER_SEED_TARGET to the folders it was created for"
            )
        connection.execute(
            "CREATE TABLE projects (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL UNIQUE, source TEXT NOT NULL, target TEXT NOT NULL, "
            "mode TEXT NOT NULL CHECK (mode IN ('top', 'files')), "
            "created_at TEXT NOT NULL, archived_at TEXT)"
        )
        project_id = connection.execute(
            "INSERT INTO projects (name, source, target, mode, created_at) "
            "VALUES (?, ?, ?, 'top', ?)",
            (seed.name, seed.source, seed.target, _now()),
        ).lastrowid
        connection.execute(
            "ALTER TABLE decisions ADD COLUMN project_id INTEGER NOT NULL DEFAULT 0"
        )
        connection.execute(
            "ALTER TABLE decisions ADD COLUMN target TEXT NOT NULL DEFAULT ''"
        )
        connection.execute(
            "UPDATE decisions SET project_id = ?, target = ?",
            (project_id, seed.target),
        )
        if "folder_moves" in tables:
            connection.execute(
                "ALTER TABLE folder_moves ADD COLUMN target TEXT NOT NULL DEFAULT ''"
            )
            connection.execute("UPDATE folder_moves SET target = ?", (seed.target,))
        connection.execute(
            "CREATE TABLE folders_v1 (target TEXT NOT NULL, path TEXT NOT NULL, "
            "description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, "
            "PRIMARY KEY (target, path))"
        )
        if "folders" in tables:
            connection.execute(
                "INSERT INTO folders_v1 SELECT ?, path, description, created_at "
                "FROM folders",
                (seed.target,),
            )
            connection.execute("DROP TABLE folders")
        connection.execute("ALTER TABLE folders_v1 RENAME TO folders")
        connection.execute(
            "CREATE TABLE skips_v1 (project_id INTEGER NOT NULL, path TEXT NOT NULL, "
            "skipped_at TEXT NOT NULL, PRIMARY KEY (project_id, path))"
        )
        if "skips" in tables:
            connection.execute(
                "INSERT INTO skips_v1 SELECT ?, name, skipped_at FROM skips",
                (project_id,),
            )
            connection.execute("DROP TABLE skips")
        connection.execute("ALTER TABLE skips_v1 RENAME TO skips")
        # Hashes were keyed by dump name only; they are a cache, so restart it.
        if "hashes" in tables:
            connection.execute("DROP TABLE hashes")

    def ready(self) -> bool:
        try:
            with self._connect() as connection:
                version: int = connection.execute("PRAGMA user_version").fetchone()[0]
                return version == SCHEMA_VERSION
        except sqlite3.Error:
            return False

    # ---------- projects ----------

    def projects(self, include_archived: bool = False) -> list[Project]:
        query = "SELECT * FROM projects"
        if not include_archived:
            query += " WHERE archived_at IS NULL"
        with self._connect() as connection:
            return [
                Project(**dict(row))
                for row in connection.execute(query + " ORDER BY name COLLATE NOCASE")
            ]

    def project(self, project_id: int) -> Project | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ? AND archived_at IS NULL",
                (project_id,),
            ).fetchone()
        return Project(**dict(row)) if row else None

    def create_project(self, name: str, source: str, target: str, mode: str) -> Project:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO projects (name, source, target, mode, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, source, target, mode, _now()),
            )
            connection.execute("UPDATE index_state SET epoch=epoch+1 WHERE id=1")
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return Project(**dict(row))

    def rename_project(self, project_id: int, name: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE projects SET name = ? WHERE id = ?", (name, project_id)
            )

    def archive_project(self, project_id: int) -> None:
        """Hide a project. Its decisions stay: they are labels for its tree."""
        with self._connect() as connection:
            connection.execute(
                "UPDATE projects SET archived_at = ?, name = name || ' (archived ' "
                "|| id || ')' WHERE id = ?",
                (_now(), project_id),
            )

    # ---------- queue state ----------

    def skips(self, project_id: int) -> dict[str, str]:
        with self._connect() as connection:
            return {
                row["path"]: row["skipped_at"]
                for row in connection.execute(
                    "SELECT path, skipped_at FROM skips WHERE project_id = ?",
                    (project_id,),
                )
            }

    def skip(self, project_id: int, path: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO skips (project_id, path, skipped_at) VALUES (?, ?, ?) "
                "ON CONFLICT (project_id, path) "
                "DO UPDATE SET skipped_at = excluded.skipped_at",
                (project_id, path, datetime.now(UTC).isoformat()),
            )

    def cached_hash(self, path: str, size: int, mtime_ns: int) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT sha256 FROM hashes "
                "WHERE path = ? AND size = ? AND mtime_ns = ?",
                (path, size, mtime_ns),
            ).fetchone()
        return row["sha256"] if row else None

    def remember_hash(self, path: str, size: int, mtime_ns: int, sha256: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO hashes VALUES (?, ?, ?, ?)",
                (path, size, mtime_ns, sha256),
            )

    # ---------- trees ----------

    def folder_notes(self, target: str) -> dict[str, str]:
        with self._connect() as connection:
            return {
                row["path"]: row["description"]
                for row in connection.execute(
                    "SELECT path, description FROM folders WHERE target = ?",
                    (target,),
                )
            }

    def add_folder(self, target: str, path: str, description: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO folders VALUES (?, ?, ?, ?)",
                (target, path, description, _now()),
            )

    def record_folder_move(self, target: str, old_path: str, new_path: str) -> None:
        """Log a moved category folder and carry its descriptions along."""
        with self._connect() as connection:
            through = connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM decisions"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO folder_moves "
                "(moved_at, old_path, new_path, through_decision_id, target) "
                "VALUES (?, ?, ?, ?, ?)",
                (_now(), old_path, new_path, through, target),
            )

            # The destination did not exist on disk before this rename, so any
            # rows already filed under it describe things that are gone (a
            # folder deleted or renamed outside the sorter). Retire them first;
            # otherwise rewriting the moved rows collides with them and the
            # whole log write fails after the folder has already moved.
            def under_destination(path: str) -> bool:
                return path == new_path or path.startswith(f"{new_path}/")

            rows = connection.execute(
                "SELECT path FROM folders WHERE target = ?", (target,)
            ).fetchall()
            connection.executemany(
                "DELETE FROM folders WHERE target = ? AND path = ?",
                [(target, path) for (path,) in rows if under_destination(path)],
            )
            for (path,) in rows:
                moved = moved_path(path, old_path, new_path)
                if moved != path:
                    connection.execute(
                        "UPDATE folders SET path = ? WHERE target = ? AND path = ?",
                        (moved, target, path),
                    )
            from services.file_sorter.index import event

            stale = connection.execute(
                "SELECT * FROM file_index WHERE base = ?", (target,)
            ).fetchall()
            for row in stale:
                if under_destination(row["relative_path"]):
                    connection.execute(
                        "DELETE FROM file_index WHERE path=?", (row["path"],)
                    )
                    # History keeps the trace: the document's last known place
                    # was reused by a moved folder.
                    event(
                        connection,
                        target,
                        row["path"],
                        row["document_id"],
                        "location_reused",
                        row["sha256"],
                    )

            indexed = connection.execute(
                "SELECT * FROM file_index WHERE base = ?", (target,)
            ).fetchall()
            changes = [
                (row, moved_path(row["relative_path"], old_path, new_path))
                for row in indexed
                if moved_path(row["relative_path"], old_path, new_path)
                != row["relative_path"]
            ]
            for row, _ in changes:
                connection.execute(
                    "DELETE FROM file_index WHERE path=?", (row["path"],)
                )
            for row, moved in changes:
                values = dict(row)
                values.update(path=f"{target}/{moved}", relative_path=moved)
                connection.execute(
                    "INSERT INTO file_index VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(values.values()),
                )
                event(
                    connection,
                    target,
                    values["path"],
                    row["document_id"],
                    "folder_move",
                    row["sha256"],
                )
            connection.execute("UPDATE index_state SET epoch=epoch+1 WHERE id=1")

    # ---------- decisions ----------

    def record(
        self,
        *,
        project_id: int,
        target: str,
        action: str,
        kind: str,
        original_name: str,
        final_name: str,
        label: str,
        destination: str,
        sha256: str | None,
        size: int | None,
        replaces_id: int | None = None,
        previous_destination: str | None = None,
        origin_project_id: int | None = None,
        document_id: int | None = None,
        duplicate_group_id: int | None = None,
        duplicate_of_document_id: int | None = None,
        review_type: str | None = None,
        file_mtime_ns: str | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> Decision:
        if _connection is None:
            with self._connect() as connection:
                return self.record(
                    project_id=project_id,
                    target=target,
                    action=action,
                    kind=kind,
                    original_name=original_name,
                    final_name=final_name,
                    label=label,
                    destination=destination,
                    sha256=sha256,
                    size=size,
                    replaces_id=replaces_id,
                    previous_destination=previous_destination,
                    origin_project_id=origin_project_id,
                    document_id=document_id,
                    duplicate_group_id=duplicate_group_id,
                    duplicate_of_document_id=duplicate_of_document_id,
                    review_type=review_type,
                    file_mtime_ns=file_mtime_ns,
                    _connection=connection,
                )
        connection = _connection
        from services.file_sorter.index import allocate, bind_decision

        if document_id is None:
            document_id = allocate(connection)
        else:
            connection.execute(
                "INSERT OR IGNORE INTO documents(id,created_at) VALUES(?,?)",
                (document_id, _now()),
            )
        cursor = connection.execute(
            "INSERT INTO decisions (decided_at, action, kind, original_name, "
            "final_name, label, destination, sha256, size, project_id, target, "
            "replaces_id, previous_destination, origin_project_id, document_id, "
            "duplicate_group_id, duplicate_of_document_id, review_type, file_mtime_ns) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _now(),
                action,
                kind,
                original_name,
                final_name,
                label,
                destination,
                sha256,
                size,
                project_id,
                target,
                replaces_id,
                previous_destination,
                origin_project_id,
                document_id,
                duplicate_group_id,
                duplicate_of_document_id,
                review_type,
                file_mtime_ns,
            ),
        )
        if previous_destination is None:
            connection.execute(
                "DELETE FROM skips WHERE project_id = ? AND path = ?",
                (project_id, original_name),
            )
        row = connection.execute(
            "SELECT * FROM decisions WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        result = Decision(**dict(row), decided_label=row["label"])
        bind_decision(connection, result)
        return result

    def prepare_duplicates(
        self,
        project_id: int,
        target: str,
        digest: str,
        plan_json: str,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO duplicate_resolutions "
                "(decided_at, project_id, target, sha256, plan_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (_now(), project_id, target, digest, plan_json),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def pending_duplicates(self) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM duplicate_resolutions WHERE completed_at IS NULL "
                    "AND cancelled_at IS NULL LIMIT 1"
                ).fetchone()
                is not None
            )

    def cancel_duplicates(self, group_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE duplicate_resolutions SET cancelled_at = ? WHERE id = ?",
                (_now(), group_id),
            )

    def record_duplicates(
        self,
        group_id: int,
        keeper: dict[str, Any],
        others: list[dict[str, Any]],
    ) -> Decision:
        """One transaction records the keeper and every redundant copy."""
        with self._connect() as connection:
            kept = self.record(
                **keeper,
                duplicate_group_id=group_id,
                _connection=connection,
            )
            for record in others:
                self.record(
                    **record,
                    duplicate_group_id=group_id,
                    duplicate_of_document_id=kept.document_id,
                    _connection=connection,
                )
            connection.execute(
                "UPDATE duplicate_resolutions SET completed_at = ? WHERE id = ?",
                (_now(), group_id),
            )
        return kept

    def incomplete_duplicates(self, project_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM duplicate_resolutions WHERE project_id = ? "
                    "AND completed_at IS NULL AND cancelled_at IS NULL ORDER BY id",
                    (project_id,),
                )
            ]

    def duplicate_group(self, group_id: int) -> list[Decision]:
        return self._read("WHERE duplicate_group_id = ? ORDER BY id", (group_id,))

    def undo_duplicate_group(self, group_id: int, operation_id: int) -> None:
        with self._connect() as connection:
            decisions = self._current(
                connection.execute(
                    "SELECT * FROM decisions WHERE duplicate_group_id=? ORDER BY id",
                    (group_id,),
                ).fetchall(),
                connection.execute("SELECT * FROM folder_moves ORDER BY id").fetchall(),
            )
            connection.execute(
                "UPDATE decisions SET undone_at = ? WHERE duplicate_group_id = ?",
                (_now(), group_id),
            )
            connection.execute(
                "UPDATE duplicate_resolutions SET completed_at = ? WHERE id = ?",
                (_now(), operation_id),
            )
            self._restore_index(connection, decisions)

    def _current(
        self, rows: list[sqlite3.Row], moves: list[sqlite3.Row]
    ) -> list[Decision]:
        decisions = []
        for row in rows:
            label, destination = row["label"], row["destination"]
            previous = row["previous_destination"]
            for move in moves:
                if (
                    move["target"] == row["target"]
                    and row["id"] <= move["through_decision_id"]
                ):
                    label = moved_path(label, move["old_path"], move["new_path"])
                    destination = moved_path(
                        destination, move["old_path"], move["new_path"]
                    )
                    if previous is not None:
                        previous = moved_path(
                            previous, move["old_path"], move["new_path"]
                        )
            values = {
                **dict(row),
                "label": label,
                "destination": destination,
                "previous_destination": previous,
            }
            decisions.append(Decision(**values, decided_label=row["label"]))
        return decisions

    def _read(self, where: str, parameters: tuple[object, ...] = ()) -> list[Decision]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM decisions {where}", parameters
            ).fetchall()
            moves = connection.execute(
                "SELECT * FROM folder_moves ORDER BY id"
            ).fetchall()
        return self._current(rows, moves)

    def tree_decisions(self, target: str) -> list[Decision]:
        """Active decisions of every project that feeds this tree."""
        return self._read(
            f"WHERE target = ? AND {self._active()} ORDER BY id", (target,)
        )

    @staticmethod
    def _active() -> str:
        # A correction supersedes its predecessor without rewriting it. Undo
        # stamps only the correction, which makes the prior decision active again.
        return (
            "undone_at IS NULL AND NOT EXISTS (SELECT 1 FROM decisions AS correction "
            "WHERE correction.replaces_id = decisions.id "
            "AND correction.undone_at IS NULL)"
        )

    def decision(self, decision_id: int) -> Decision | None:
        found = self._read("WHERE id = ?", (decision_id,))
        return found[0] if found else None

    def last_active(self, project_id: int) -> Decision | None:
        found = self._read(
            f"WHERE project_id = ? AND {self._active()} ORDER BY id DESC LIMIT 1",
            (project_id,),
        )
        return found[0] if found else None

    def mark_undone(self, decision_id: int) -> None:
        with self._connect() as connection:
            decisions = self._current(
                connection.execute(
                    "SELECT * FROM decisions WHERE id=?", (decision_id,)
                ).fetchall(),
                connection.execute("SELECT * FROM folder_moves ORDER BY id").fetchall(),
            )
            connection.execute(
                "UPDATE decisions SET undone_at = ? WHERE id = ?",
                (_now(), decision_id),
            )
            self._restore_index(connection, decisions)

    def _restore_index(
        self, connection: sqlite3.Connection, decisions: list[Decision]
    ) -> None:
        from services.file_sorter.index import restore_binding

        moves = connection.execute("SELECT * FROM folder_moves ORDER BY id").fetchall()
        # Remove all group destinations before restoring names that may overlap.
        bindings = connection.execute("SELECT * FROM file_index").fetchall()
        for decision in decisions:
            connection.execute(
                "DELETE FROM file_index WHERE path=?",
                (f"{decision.target}/{decision.destination}",),
            )
        for decision in decisions:
            row = next(
                (
                    r
                    for r in bindings
                    if r["path"] == f"{decision.target}/{decision.destination}"
                ),
                None,
            )
            if row:
                connection.execute(
                    "INSERT OR REPLACE INTO file_index "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(row),
                )
                prior = self._current(
                    connection.execute(
                        "SELECT * FROM decisions WHERE id=?", (decision.replaces_id,)
                    ).fetchall(),
                    moves,
                )
                restore_binding(connection, decision, prior[0] if prior else None)

    def sorted_with_hash(self, sha256: str) -> str | None:
        """Where identical content was sorted, in any tree."""
        found = self._read(
            f"WHERE sha256 = ? AND {self._active()} ORDER BY id DESC LIMIT 1",
            (sha256,),
        )
        return f"{found[0].target}/{found[0].destination}" if found else None

    def counts(self) -> dict[int, dict[str, int]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT project_id, action, COUNT(*) AS total FROM decisions "
                f"WHERE {self._active()} GROUP BY project_id, action"
            ).fetchall()
        totals: dict[int, dict[str, int]] = {}
        for row in rows:
            key = "sorted" if row["action"] == "sort" else "discarded"
            totals.setdefault(row["project_id"], {"sorted": 0, "discarded": 0})[key] = (
                row["total"]
            )
        return totals

    def decision_log(self, target: str) -> list[dict[str, Any]]:
        """Lossless tree history, including superseded and undone decisions.

        Read one SQLite snapshot so the export cannot lose a move that falls
        between its decision rows and its folder-move rows.
        """
        with self._connect() as connection:
            connection.execute("BEGIN")
            rows: list[dict[str, Any]] = [
                {
                    "record_type": "schema",
                    "schema_version": SCHEMA_VERSION,
                    "tree": target,
                }
            ]
            for table, record_type in (
                ("projects", "project"),
                ("decisions", "decision"),
                ("folder_moves", "folder_move"),
                ("folders", "folder_note"),
                ("duplicate_resolutions", "duplicate_resolution"),
            ):
                rows.extend(
                    {"record_type": record_type, **dict(row)}
                    for row in connection.execute(
                        f"SELECT * FROM {table} WHERE target = ?", (target,)
                    )
                )
            bases = {
                target,
                *(
                    row["source"]
                    for row in connection.execute(
                        "SELECT source FROM projects WHERE target=?", (target,)
                    )
                ),
            }
            for base in sorted(bases):
                rows.extend(
                    {"record_type": "document_event", **dict(row)}
                    for row in connection.execute(
                        "SELECT * FROM document_events WHERE target=? ORDER BY id",
                        (base,),
                    )
                )
                rows.extend(
                    {"record_type": "indexed_file", **dict(row)}
                    for row in connection.execute(
                        "SELECT * FROM file_index WHERE base=? ORDER BY path", (base,)
                    )
                )
        return rows
