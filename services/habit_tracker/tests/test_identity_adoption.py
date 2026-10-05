"""Legacy rows follow a linked identity: all tables, all-or-nothing, retried."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from services.common.identity import Identity
from services.habit_tracker import main

LOGIN = {"Tailscale-User-Login": "member@example.test"}
LEGACY = "tailscale:member@example.test"
LINKED = Identity(key="home-platform:42", display_name="member")
OWNER_TABLES = (
    ("users", "identity"),
    ("user_settings", "identity"),
    ("drinks", "owner_identity"),
    ("budget_settings", "identity"),
    ("daily_budget_changes", "identity"),
    ("budget_transactions", "owner_identity"),
    ("budget_adjustments", "owner_identity"),
    ("study_sessions", "owner_identity"),
)


def owners(database: Path) -> dict[str, set[str]]:
    with sqlite3.connect(database) as connection:
        return {
            table: {
                str(row[0])
                for row in connection.execute(
                    f"SELECT DISTINCT {column} FROM {table}"
                ).fetchall()
            }
            for table, column in OWNER_TABLES
        }


def legacy_era(database: Path) -> None:
    with TestClient(main.create_app(database), headers=LOGIN) as member:
        assert (
            member.put("/api/water/settings", json={"daily_goal_ml": 3000}).status_code
            == 200
        )
        assert (
            member.put(
                "/api/budget/daily-budget", json={"amount_cents": 2500}
            ).status_code
            == 200
        )
        assert (
            member.post("/api/water/drinks", json={"amount_ml": 250}).status_code == 201
        )
        assert (
            member.post(
                "/api/budget/transactions",
                json={"kind": "daily_spend", "amount_cents": 500},
            ).status_code
            == 201
        )
        started = member.post(
            "/api/study/sessions", json={"activity": "Reading", "duration_minutes": 25}
        )
        assert started.status_code == 201
        assert (
            member.post(f"/api/study/sessions/{started.json()['id']}/stop").status_code
            == 200
        )
    assert owners(database)["study_sessions"] == {LEGACY}


def linked_app(database: Path) -> tuple[Any, list[str]]:
    calls: list[str] = []

    def resolve(subject: str) -> Identity:
        calls.append(subject)
        return LINKED

    return main.create_app(database, identity_resolver=resolve), calls


def failing_once(real: Callable[..., None]) -> Callable[..., None]:
    attempts: list[int] = []

    def adopt(*args: Any, **kwargs: Any) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        real(*args, **kwargs)

    return adopt


def test_every_table_moves_to_the_linked_identity(tmp_path: Path) -> None:
    database = tmp_path / "habits.db"
    legacy_era(database)
    app, _ = linked_app(database)
    with TestClient(app, headers=LOGIN) as member:
        today = member.get("/api/water/today")
        study = member.get("/api/study/summary")
    assert today.status_code == 200
    assert today.json()["total_ml"] == 250
    assert study.status_code == 200
    assert [session["activity"] for session in study.json()["recent"]] == ["Reading"]
    assert owners(database) == {table: {LINKED.key} for table, _ in OWNER_TABLES}


def test_adoption_retried_after_one_failure_keeps_legacy_settings(
    tmp_path: Path,
) -> None:
    database = tmp_path / "habits.db"
    legacy_era(database)
    app, calls = linked_app(database)
    with TestClient(app, headers=LOGIN, raise_server_exceptions=False) as member:
        budget = app.state.budget_repository
        budget.adopt_identity = failing_once(budget.adopt_identity)
        assert member.get("/api/water/today").status_code == 500
        assert member.get("/api/water/today").status_code == 200
        goal = member.get("/api/water/settings").json()
        summary = member.get("/api/budget/summary").json()
    assert goal["daily_goal_ml"] == 3000
    assert summary["daily_budget_cents"] == 2500
    assert owners(database)["budget_settings"] == {LINKED.key}
    assert calls == ["member@example.test"] * 2, "a failed miss is not cached"


def test_a_partial_adoption_failure_moves_nothing(tmp_path: Path) -> None:
    database = tmp_path / "habits.db"
    legacy_era(database)
    before = owners(database)
    app, _ = linked_app(database)
    with TestClient(app, headers=LOGIN, raise_server_exceptions=False) as member:
        study = app.state.study_repository
        study.adopt_identity = failing_once(study.adopt_identity)
        assert member.get("/api/water/today").status_code == 500
        assert owners(database) == before, "nothing moves until every table moves"
        assert member.get("/api/water/today").json()["total_ml"] == 250
    assert owners(database) == {table: {LINKED.key} for table, _ in OWNER_TABLES}
