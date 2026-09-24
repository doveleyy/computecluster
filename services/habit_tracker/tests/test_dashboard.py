import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from services.habit_tracker.main import create_app


def client(path: Path, user: str = "member@example.test") -> TestClient:
    return TestClient(
        create_app(path, allow_dev_identity=True),
        headers={"X-Habit-Tracker-Dev-User": user},
    )


def test_navigation_assets_and_private_dashboard(tmp_path: Path) -> None:
    path = tmp_path / "habits.db"
    with TestClient(create_app(path)) as anonymous:
        for route in ("/", "/water", "/budget", "/api/budget/summary"):
            assert anonymous.get(route).status_code == 401
        assert (
            anonymous.put(
                "/api/budget/savings-goal", json={"target_cents": 10000}
            ).status_code
            == 401
        )
    with client(path) as signed_in:
        for route in ("/", "/water", "/budget"):
            page = signed_in.get(route)
            assert page.status_code == 200
            for href in ("/habits/", "/habits/water", "/habits/budget"):
                assert f'href="{href}"' in page.text
            assert page.text.count('aria-current="page"') == 1
        for asset in ("dashboard", "water", "budget", "shell"):
            assert signed_in.get(f"/static/{asset}.css").status_code == 200
        for asset in ("dashboard", "water", "budget"):
            assert signed_in.get(f"/static/{asset}.js").status_code == 200
        assert (
            signed_in.get("/api/water/today").json()
            == signed_in.get("/api/today").json()
        )


def test_savings_goal_is_durable_scoped_and_does_not_change_money(
    tmp_path: Path,
) -> None:
    path = tmp_path / "habits.db"
    with client(path) as first:
        assert first.get("/api/budget/summary").json()["savings_goal_cents"] is None
        first.post(
            "/api/budget/transactions",
            json={
                "kind": "fund_contribution",
                "amount_cents": 15000,
            },
        )
        for value in (50000, 25000, 25000):
            assert (
                first.put(
                    "/api/budget/savings-goal",
                    json={
                        "target_cents": value,
                    },
                ).status_code
                == 200
            )
        assert (
            first.put(
                "/api/budget/savings-goal",
                json={
                    "target_cents": 0,
                },
            ).status_code
            == 422
        )
    with client(path) as reopened:
        summary = reopened.get("/api/budget/summary").json()
        assert summary["savings_goal_cents"] == 25000
        assert summary["fund_balance_cents"] == 15000
        assert summary["daily_budget_cents"] == 1000
        assert (
            reopened.put(
                "/api/budget/savings-goal",
                json={
                    "target_cents": None,
                },
            ).status_code
            == 200
        )
        assert reopened.get("/api/budget/summary").json()["savings_goal_cents"] is None
    with client(path, "second@example.test") as other:
        summary = other.get("/api/budget/summary").json()
        assert summary["savings_goal_cents"] is None
        assert summary["fund_balance_cents"] == 0
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT previous_target_cents, target_cents, currency, occurred_at "
            "FROM savings_goal_changes ORDER BY sequence"
        ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [
        (None, 50000),
        (50000, 25000),
        (25000, None),
    ]
    assert all(row[2] == "SGD" and row[3] for row in rows)


def test_explicit_zero_budget_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "habits.db"
    with client(path) as first:
        first.put("/api/budget/daily-budget", json={"amount_cents": 0})
    with client(path) as restarted:
        assert restarted.get("/api/budget/summary").json()["daily_budget_cents"] == 0


def test_embedded_identity_cannot_close_configuration_script(tmp_path: Path) -> None:
    with client(tmp_path / "habits.db", "</script><script>alert(1)</script>") as user:
        page = user.get("/")
    assert page.status_code == 200
    assert "</script><script>alert(1)</script>" not in page.text
    assert "\\u003c/script>" in page.text
