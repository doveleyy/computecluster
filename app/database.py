import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from app.migrations import apply_migrations


class Database:
    """SQLite access with one long-lived connection per thread.

    Connections are reused rather than opened per operation. That is not a
    micro-optimisation: in WAL mode SQLite runs a full checkpoint whenever the
    last connection to a database closes. Opening and closing a connection for
    every request therefore checkpointed the WAL, rewrote pages and fsynced on
    every claim, heartbeat and read — turning a 48 KB database into roughly
    21 GB of physical writes per day on an idle system, all of it landing on the
    Raspberry Pi's microSD card. Cards fail from write cycles, so this was
    slowly destroying the boot medium to record that nothing was happening.

    Holding the connection open lets the WAL accumulate and checkpoint on
    SQLite's own schedule instead.

    Connections are thread-local because FastAPI runs synchronous endpoints in a
    worker threadpool and SQLite connection objects are not safe to share across
    threads by default.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._local = threading.local()

    def _connection(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if connection is not None:
            return connection
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        # FULL (the default) fsyncs the WAL on every commit, and each fsync
        # drags an ext4 journal commit with it. At ~100 transactions a minute
        # that amplified 2.4 MB of application writes into ~10 MB reaching the
        # device. NORMAL is the setting SQLite documents as safe for WAL: a
        # power loss can lose recently committed transactions, but cannot
        # corrupt the database.
        #
        # That trade is acceptable here specifically because the system already
        # tolerates it. A lost completion leaves the job RUNNING with an expired
        # lease, which the recovery loop requeues — the same path taken when a
        # worker dies mid-job.
        connection.execute("PRAGMA synchronous = NORMAL")
        self._local.conn = connection
        return connection

    def _discard(self) -> None:
        """Drop this thread's connection so the next call reconnects.

        A connection that raised may be in an unusable state; keeping it would
        make one transient error permanent for that thread.
        """
        connection: sqlite3.Connection | None = getattr(self._local, "conn", None)
        self._local.conn = None
        if connection is not None:
            with suppress(sqlite3.Error):
                connection.close()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = self._connection()
        try:
            with connection:
                yield connection
        except sqlite3.Error:
            self._discard()
            raise

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            apply_migrations(connection)

    def ping(self) -> None:
        with self.connect() as connection:
            connection.execute("SELECT 1").fetchone()

    def close(self) -> None:
        """Close this thread's connection. Used at shutdown and in tests."""
        self._discard()
