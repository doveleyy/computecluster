"""Per-request cost: indexed timetable lookups."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from services.common.identity import Identity
from services.transport import datamall, main
from services.transport.tests.test_api import NOW, timetable

TAILSCALE = {"Tailscale-User-Login": "Owner@Example.test"}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def resolving_client(
    tmp_path: Path, answers: list[object], clock: Clock
) -> tuple[TestClient, list[str]]:
    calls: list[str] = []

    def resolve(subject: str) -> Identity | None:
        calls.append(subject)
        answer = answers[min(len(calls), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[return-value]

    app = main.create_app(
        tmp_path / "transport.db",
        schedule_fetcher=lambda key: datamall.ScheduleDownload("x", timetable()),
        bus_fetcher=lambda key, stop: {"Services": []},
        now=lambda: NOW,
        identity_resolver=resolve,
        identity_clock=clock,
    )
    return TestClient(app, headers=TAILSCALE), calls


def test_station_and_last_train_lookups_use_indexes(tmp_path: Path) -> None:
    clock = Clock()
    browser, _ = resolving_client(
        tmp_path, [Identity("home-platform:7", "owner")], clock
    )
    with browser:
        assert browser.post("/api/train/refresh").status_code == 200
    with sqlite3.connect(tmp_path / "transport.db") as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert {"routes_short_name", "trips_route_direction", "stops_code"} <= names
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat1'"
        ).fetchone()
        # At the live timetable's size (19 lines, 17,576 trips) the planner
        # must start from the line, not scan every trip.
        connection.executemany(
            "INSERT INTO routes VALUES (?, ?, '', '', '')",
            [(f"R{i}", f"L{i}") for i in range(20)],
        )
        connection.executemany(
            "INSERT INTO trips VALUES (?, ?, 'S', ?, ?)",
            [(f"T{i}", f"R{i % 20}", f"H{i % 7}", i % 2) for i in range(18_000)],
        )
        connection.execute("ANALYZE")
        plan = " ".join(
            row[3]
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT trip_id FROM trips JOIN routes "
                "USING(route_id) WHERE routes.short_name = ? AND direction_id = ? "
                "AND headsign = ?",
                ("NS", 1, "Marina South Pier"),
            )
        )
        assert "SCAN" not in plan
