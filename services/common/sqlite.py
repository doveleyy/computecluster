"""SQLite connections for the application services."""

import sqlite3
from pathlib import Path


def connect(
    database_path: Path, *, row_factory: type[sqlite3.Row] | None = None
) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, timeout=10)
    if row_factory is not None:
        connection.row_factory = row_factory
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    # Safe with WAL: a power cut can lose the last commits, never corrupt.
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection
