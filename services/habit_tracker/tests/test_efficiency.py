"""Per-request cost: study reads stay read-only."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from services.habit_tracker import main
from services.habit_tracker.study import StudyRepository

OWNER = "development:member@example.test"


def test_study_reads_commit_nothing_unless_a_session_expired(tmp_path: Path) -> None:
    database = tmp_path / "habits.db"
    with TestClient(
        main.create_app(database, allow_dev_identity=True),
        headers={"X-Habit-Tracker-Dev-User": "member@example.test"},
    ) as member:
        assert member.get("/api/study/summary").status_code == 200
    repository = StudyRepository(database, ZoneInfo("Asia/Singapore"))
    start = datetime(2026, 9, 28, 4, tzinfo=UTC)
    session = repository.start(OWNER, "Reading", 25, start)
    watcher = sqlite3.connect(database)
    version = watcher.execute("PRAGMA data_version").fetchone()[0]
    assert repository.summary(OWNER, start + timedelta(minutes=10))["active"]
    repository.history(OWNER, start.date(), 7, start + timedelta(minutes=10))
    assert watcher.execute("PRAGMA data_version").fetchone()[0] == version
    completed = repository.summary(OWNER, start + timedelta(minutes=30))
    assert completed["active"] is None
    assert completed["recent"][0].ended_at == session.planned_end_at
    assert watcher.execute("PRAGMA data_version").fetchone()[0] != version
    watcher.close()
