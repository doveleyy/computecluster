from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import TypedDict
from uuid import uuid4
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class StudySession:
    id: str
    activity: str
    duration_minutes: int
    started_at: datetime
    planned_end_at: datetime
    ended_at: datetime | None
    status: str

    @property
    def elapsed_seconds(self) -> int:
        if self.ended_at is None:
            return 0
        return max(0, int((self.ended_at - self.started_at).total_seconds()))


class StudySummary(TypedDict):
    date: str
    server_now: str
    today_seconds: int
    active: StudySession | None
    recent: list[StudySession]


class StudyDaySummary(TypedDict):
    date: str
    total_seconds: int


class ActiveStudySessionError(Exception):
    pass


class StudySessionNotActiveError(Exception):
    pass


class StudyRepository:
    def __init__(self, database_path: Path, timezone: ZoneInfo) -> None:
        self._database_path = database_path
        self._timezone = timezone

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS study_sessions (
                    id TEXT PRIMARY KEY,
                    owner_identity TEXT NOT NULL REFERENCES users(identity),
                    activity TEXT NOT NULL CHECK (length(activity) BETWEEN 1 AND 120),
                    duration_minutes INTEGER NOT NULL
                        CHECK (duration_minutes BETWEEN 1 AND 180),
                    started_at TEXT NOT NULL,
                    planned_end_at TEXT NOT NULL,
                    ended_at TEXT,
                    status TEXT NOT NULL CHECK (
                        status IN ('active', 'stopped', 'completed')
                    ),
                    CHECK ((status = 'active') = (ended_at IS NULL))
                );
                CREATE UNIQUE INDEX IF NOT EXISTS study_one_active_per_owner
                    ON study_sessions(owner_identity) WHERE status = 'active';
                CREATE INDEX IF NOT EXISTS study_owner_recent
                    ON study_sessions(owner_identity, started_at DESC);
                """
            )

    def ready(self) -> bool:
        try:
            with self._connect() as connection:
                return (
                    connection.execute(
                        "SELECT 1 FROM study_sessions LIMIT 1"
                    ).description
                    is not None
                )
        except sqlite3.Error:
            return False

    def start(
        self,
        owner: str,
        activity: str,
        duration_minutes: int,
        now: datetime | None = None,
    ) -> StudySession:
        now = now or datetime.now(UTC)
        session = StudySession(
            id=str(uuid4()),
            activity=activity,
            duration_minutes=duration_minutes,
            started_at=now,
            planned_end_at=now + timedelta(minutes=duration_minutes),
            ended_at=None,
            status="active",
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._complete_expired(connection, owner, now)
            if connection.execute(
                "SELECT 1 FROM study_sessions WHERE owner_identity = ? "
                "AND status = 'active'",
                (owner,),
            ).fetchone():
                raise ActiveStudySessionError
            connection.execute(
                """
                INSERT INTO study_sessions (
                    id, owner_identity, activity, duration_minutes, started_at,
                    planned_end_at, ended_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, 'active')
                """,
                (
                    session.id,
                    owner,
                    activity,
                    duration_minutes,
                    now.isoformat(),
                    session.planned_end_at.isoformat(),
                ),
            )
        return session

    def finish(
        self,
        owner: str,
        session_id: str,
        action: str,
        now: datetime | None = None,
    ) -> StudySession | None:
        now = now or datetime.now(UTC)
        session: StudySession | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._complete_expired(connection, owner, now)
            row = connection.execute(
                "SELECT * FROM study_sessions WHERE id = ? AND owner_identity = ?",
                (session_id, owner),
            ).fetchone()
            if row is not None:
                session = self._session(row)
            if (
                session is not None
                and session.status == "active"
                and action == "cancel"
            ):
                connection.execute(
                    "DELETE FROM study_sessions WHERE id = ?", (session_id,)
                )
                return None
            if session is not None and session.status == "active" and action == "stop":
                connection.execute(
                    "UPDATE study_sessions SET status = 'stopped', ended_at = ? "
                    "WHERE id = ?",
                    (now.isoformat(), session_id),
                )
        if session is None:
            raise KeyError(session_id)
        if session.status != "active":
            raise StudySessionNotActiveError
        if action != "stop":
            raise ValueError("unknown study action")
        return StudySession(
            id=session.id,
            activity=session.activity,
            duration_minutes=session.duration_minutes,
            started_at=session.started_at,
            planned_end_at=session.planned_end_at,
            ended_at=now,
            status="stopped",
        )

    def summary(self, owner: str, now: datetime | None = None) -> StudySummary:
        now = now or datetime.now(UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._complete_expired(connection, owner, now)
            active_row = connection.execute(
                "SELECT * FROM study_sessions WHERE owner_identity = ? "
                "AND status = 'active'",
                (owner,),
            ).fetchone()
            day_start, day_end = self._utc_bounds(now.astimezone(self._timezone).date())
            total_rows = connection.execute(
                """
                SELECT * FROM study_sessions
                WHERE owner_identity = ? AND status != 'active'
                    AND started_at < ? AND ended_at > ?
                """,
                (owner, day_end.isoformat(), day_start.isoformat()),
            ).fetchall()
            recent_rows = connection.execute(
                """
                SELECT * FROM study_sessions
                WHERE owner_identity = ? AND status != 'active'
                ORDER BY ended_at DESC LIMIT 5
                """,
                (owner,),
            ).fetchall()
        today_seconds = 0
        for row in total_rows:
            session = self._session(row)
            assert session.ended_at is not None
            overlap_start = max(session.started_at, day_start)
            overlap_end = min(session.ended_at, day_end)
            today_seconds += max(0, int((overlap_end - overlap_start).total_seconds()))
        return {
            "date": now.astimezone(self._timezone).date().isoformat(),
            "server_now": now.isoformat(),
            "today_seconds": today_seconds,
            "active": self._session(active_row) if active_row else None,
            "recent": [self._session(row) for row in recent_rows],
        }

    def history(
        self, owner: str, end_day: date, days: int, now: datetime | None = None
    ) -> list[StudyDaySummary]:
        now = now or datetime.now(UTC)
        first_day = end_day - timedelta(days=days - 1)
        range_start, _ = self._utc_bounds(first_day)
        _, range_end = self._utc_bounds(end_day)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._complete_expired(connection, owner, now)
            rows = connection.execute(
                """
                SELECT * FROM study_sessions
                WHERE owner_identity = ? AND status != 'active'
                    AND started_at < ? AND ended_at > ?
                """,
                (owner, range_end.isoformat(), range_start.isoformat()),
            ).fetchall()
        sessions = [self._session(row) for row in rows]
        result: list[StudyDaySummary] = []
        for index in range(days):
            day = first_day + timedelta(days=index)
            day_start, day_end = self._utc_bounds(day)
            total_seconds = 0
            for session in sessions:
                assert session.ended_at is not None
                overlap_start = max(session.started_at, day_start)
                overlap_end = min(session.ended_at, day_end)
                total_seconds += max(
                    0, int((overlap_end - overlap_start).total_seconds())
                )
            result.append({"date": day.isoformat(), "total_seconds": total_seconds})
        return result

    @staticmethod
    def _complete_expired(
        connection: sqlite3.Connection, owner: str, now: datetime
    ) -> None:
        connection.execute(
            """
            UPDATE study_sessions
            SET status = 'completed', ended_at = planned_end_at
            WHERE owner_identity = ? AND status = 'active' AND planned_end_at <= ?
            """,
            (owner, now.isoformat()),
        )

    def _utc_bounds(self, day: date) -> tuple[datetime, datetime]:
        start = datetime.combine(day, time.min, self._timezone)
        return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)

    @staticmethod
    def _session(row: sqlite3.Row) -> StudySession:
        return StudySession(
            id=row["id"],
            activity=row["activity"],
            duration_minutes=row["duration_minutes"],
            started_at=datetime.fromisoformat(row["started_at"]),
            planned_end_at=datetime.fromisoformat(row["planned_end_at"]),
            ended_at=(
                datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None
            ),
            status=row["status"],
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection
