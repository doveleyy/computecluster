import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from services.habit_tracker.budget import BudgetRepository
from services.habit_tracker.main import create_app
from services.habit_tracker.water import WaterRepository


def client(tmp_path: Path, user: str = "member@example.test") -> TestClient:
    return TestClient(
        create_app(tmp_path / "habits.db", allow_dev_identity=True),
        headers={"X-Habit-Tracker-Dev-User": user},
    )


def test_budget_page_is_a_tab_in_the_habit_tracker(tmp_path: Path) -> None:
    with client(tmp_path) as test_client:
        water = test_client.get("/water")
        budget = test_client.get("/budget")

    assert water.status_code == 200
    assert budget.status_code == 200
    assert 'id="budget-link"' in water.text
    assert 'id="water-link"' in budget.text
    assert "Sinking fund" in budget.text


def test_daily_spending_and_fund_movements_are_separate(tmp_path: Path) -> None:
    with client(tmp_path) as test_client:
        initial = test_client.get("/api/budget/summary")
        budget = test_client.put(
            "/api/budget/daily-budget", json={"amount_cents": 2000}
        )
        spending = test_client.post(
            "/api/budget/transactions",
            json={
                "kind": "daily_spend",
                "amount_cents": 800,
                "description": "Lunch",
            },
        )
        redemption = test_client.post(
            "/api/budget/transactions",
            json={
                "kind": "fund_redemption",
                "amount_cents": 500,
                "description": "Game",
            },
        )
        contribution = test_client.post(
            "/api/budget/transactions",
            json={
                "kind": "fund_contribution",
                "amount_cents": 1000,
                "description": "Opening contribution",
            },
        )
        summary = test_client.get("/api/budget/summary")

    assert initial.json()["daily_budget_cents"] == 1000
    assert budget.status_code == 200
    assert spending.status_code == 201
    assert redemption.status_code == 201
    assert contribution.status_code == 201
    assert summary.json()["daily_budget_cents"] == 2000
    assert summary.json()["daily_spent_cents"] == 800
    assert summary.json()["daily_remaining_cents"] == 1200
    assert summary.json()["pending_surplus_cents"] == 1200
    assert summary.json()["settled_fund_cents"] == 500
    assert summary.json()["fund_balance_cents"] == 500


def test_today_overage_reduces_fund_immediately(tmp_path: Path) -> None:
    with client(tmp_path) as test_client:
        test_client.put("/api/budget/daily-budget", json={"amount_cents": 1000})
        test_client.post(
            "/api/budget/transactions",
            json={"kind": "fund_contribution", "amount_cents": 700},
        )
        test_client.post(
            "/api/budget/transactions",
            json={"kind": "daily_spend", "amount_cents": 1400},
        )
        summary = test_client.get("/api/budget/summary")

    assert summary.json()["daily_remaining_cents"] == -400
    assert summary.json()["pending_surplus_cents"] == 0
    assert summary.json()["settled_fund_cents"] == 700
    assert summary.json()["fund_balance_cents"] == 300


def test_budget_transaction_validation_and_owner_isolation(tmp_path: Path) -> None:
    with client(tmp_path) as first:
        created = first.post(
            "/api/budget/transactions",
            json={"kind": "fund_redemption", "amount_cents": 250},
        ).json()
        invalid = first.post(
            "/api/budget/transactions",
            json={"kind": "unknown", "amount_cents": 250},
        )

    with client(tmp_path, "someone-else@example.test") as second:
        summary = second.get("/api/budget/summary")
        deletion = second.delete(f"/api/budget/transactions/{created['id']}")

    assert invalid.status_code == 422
    assert summary.json()["fund_balance_cents"] == 0
    assert summary.json()["transactions"] == []
    assert deletion.status_code == 404


def test_completed_days_settle_using_their_historical_budget(tmp_path: Path) -> None:
    database_path = tmp_path / "habits.db"
    timezone = ZoneInfo("Asia/Singapore")
    water = WaterRepository(database_path, timezone)
    budget = BudgetRepository(database_path, timezone)
    water.initialize()
    budget.initialize()
    identity = "development:member@example.test"
    water.ensure_user(identity, "member")
    budget.ensure_user(identity, date(2026, 9, 22))
    budget.set_daily_budget(identity, date(2026, 9, 22), 2000)
    budget.set_daily_budget(identity, date(2026, 9, 24), 2500)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO budget_transactions(
                id, owner_identity, kind, amount_cents, description, occurred_at
            ) VALUES (?, ?, 'daily_spend', ?, ?, ?)
            """,
            (
                "day-one-spend",
                identity,
                500,
                "Day one",
                datetime(2026, 9, 22, 4, tzinfo=UTC).isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO budget_transactions(
                id, owner_identity, kind, amount_cents, description, occurred_at
            ) VALUES (?, ?, 'daily_spend', ?, ?, ?)
            """,
            (
                "today-spend",
                identity,
                1000,
                "Today",
                datetime(2026, 9, 24, 4, tzinfo=UTC).isoformat(),
            ),
        )

    summary = budget.summary(identity, date(2026, 9, 24))

    assert summary.settled_fund_cents == 3500
    assert summary.daily_budget_cents == 2500
    assert summary.daily_spent_cents == 1000
    assert summary.pending_surplus_cents == 1500
    assert summary.fund_balance_cents == 3500


def test_daily_budget_carries_forward_until_explicitly_changed(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "habits.db"
    timezone = ZoneInfo("Asia/Singapore")
    water = WaterRepository(database_path, timezone)
    budget = BudgetRepository(database_path, timezone)
    water.initialize()
    budget.initialize()
    identity = "development:member@example.test"
    water.ensure_user(identity, "member")
    budget.ensure_user(identity, date(2026, 9, 22))
    budget.set_daily_budget(identity, date(2026, 9, 22), 2000)

    budget.ensure_user(identity, date(2026, 9, 23))
    next_day = budget.summary(identity, date(2026, 9, 23))

    assert next_day.daily_budget_cents == 2000
    assert next_day.settled_fund_cents == 2000


def test_every_same_day_budget_adjustment_is_preserved_in_the_ledger(
    tmp_path: Path,
) -> None:
    with client(tmp_path) as test_client:
        first = test_client.put("/api/budget/daily-budget", json={"amount_cents": 800})
        second = test_client.put("/api/budget/daily-budget", json={"amount_cents": 650})
        unchanged = test_client.put(
            "/api/budget/daily-budget", json={"amount_cents": 650}
        )
        summary = test_client.get("/api/budget/summary").json()
        history = test_client.get("/api/budget/history?days=1").json()
        ledger = test_client.get("/api/budget/ledger?limit=20").json()["entries"]

    assert first.status_code == 200
    assert second.status_code == 200
    assert unchanged.status_code == 200
    assert summary["daily_budget_cents"] == 650
    assert history["days"][-1]["budget_cents"] == 650
    adjustments = [
        entry for entry in ledger if entry["kind"] == "daily_budget_adjustment"
    ]
    assert [
        (
            entry["previous_amount_cents"],
            entry["new_amount_cents"],
            entry["amount_cents"],
        )
        for entry in adjustments
    ] == [(800, 650, -150), (1000, 800, -200), (0, 1000, 1000)]


def test_spending_carries_a_category(tmp_path: Path) -> None:
    with client(tmp_path) as test_client:
        lunch = test_client.post(
            "/api/budget/transactions",
            json={
                "kind": "daily_spend",
                "amount_cents": 800,
                "description": "Lunch",
                "category": "food",
            },
        )
        bus = test_client.post(
            "/api/budget/transactions",
            json={"kind": "daily_spend", "amount_cents": 200, "category": "transport"},
        )
        drink = test_client.post(
            "/api/budget/transactions",
            json={"kind": "daily_spend", "amount_cents": 300, "category": "drinks"},
        )
        default = test_client.post(
            "/api/budget/transactions",
            json={"kind": "daily_spend", "amount_cents": 100},
        )
        unknown = test_client.post(
            "/api/budget/transactions",
            json={"kind": "daily_spend", "amount_cents": 100, "category": "crypto"},
        )
        page = test_client.get("/budget")
        summary = test_client.get("/api/budget/summary").json()

    assert lunch.json()["category"] == "food"
    assert lunch.json()["description"] == "Lunch"
    assert bus.json()["category"] == "transport"
    assert drink.json()["category"] == "drinks"
    assert default.json()["category"] == "other"
    assert unknown.status_code == 422
    assert "Category" in page.text
    assert '"value": "drinks", "label": "Drinks"' in page.text
    assert {t["category"] for t in summary["transactions"]} == {
        "food",
        "drinks",
        "transport",
        "other",
    }


def test_previous_day_budget_analysis_breaks_down_spending_categories(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "habits.db"
    timezone = ZoneInfo("Asia/Singapore")
    identity = "development:member@example.test"
    previous_day = datetime.now(timezone).date() - timedelta(days=1)
    occurred_at = datetime.combine(previous_day, time(12), timezone).astimezone(UTC)
    with client(tmp_path) as test_client:
        assert test_client.get("/api/budget/summary").status_code == 200
        with sqlite3.connect(database_path) as connection:
            connection.executemany(
                """
                INSERT INTO budget_transactions(
                    id, owner_identity, kind, amount_cents, description,
                    occurred_at, category
                ) VALUES (?, ?, 'daily_spend', ?, ?, ?, ?)
                """,
                (
                    (
                        "yesterday-food",
                        identity,
                        450,
                        "Lunch",
                        occurred_at.isoformat(),
                        "food",
                    ),
                    (
                        "yesterday-drink",
                        identity,
                        250,
                        "Coffee",
                        occurred_at.isoformat(),
                        "drinks",
                    ),
                ),
            )
        analysis = test_client.get(f"/api/budget/day?date={previous_day}")
        future = test_client.get(
            f"/api/budget/day?date={datetime.now(timezone).date() + timedelta(days=1)}"
        )
        page = test_client.get("/budget")

    assert analysis.status_code == 200
    assert analysis.json()["spent_cents"] == 700
    assert analysis.json()["entry_count"] == 2
    assert analysis.json()["category_breakdown_cents"] == {
        "drinks": 250,
        "food": 450,
    }
    assert future.status_code == 422
    assert 'id="category-breakdown"' in page.text


def test_only_daily_spending_carries_a_category(tmp_path: Path) -> None:
    with client(tmp_path) as test_client:
        redemption = test_client.post(
            "/api/budget/transactions",
            json={
                "kind": "fund_redemption",
                "amount_cents": 500,
                "category": "entertainment",
            },
        )
        contribution = test_client.post(
            "/api/budget/transactions",
            json={
                "kind": "fund_contribution",
                "amount_cents": 100,
                "category": "food",
            },
        )
        plain_redemption = test_client.post(
            "/api/budget/transactions",
            json={"kind": "fund_redemption", "amount_cents": 500},
        )

    # Fund movements are not spending, so they carry no category to analyse.
    assert redemption.status_code == 422
    assert contribution.status_code == 422
    assert plain_redemption.status_code == 201
    assert plain_redemption.json()["category"] == "other"


def test_existing_transactions_migrate_to_the_other_category(tmp_path: Path) -> None:
    database_path = tmp_path / "habits.db"
    timezone = ZoneInfo("Asia/Singapore")
    water = WaterRepository(database_path, timezone)
    water.initialize()
    identity = "development:legacy@example.test"
    water.ensure_user(identity, "legacy")
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE budget_transactions (
                id TEXT PRIMARY KEY,
                owner_identity TEXT NOT NULL REFERENCES users(identity),
                kind TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                description TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO budget_transactions VALUES (?, ?, 'daily_spend', ?, ?, ?)",
            ("old", identity, 450, "Before categories", datetime.now(UTC).isoformat()),
        )

    budget = BudgetRepository(database_path, timezone)
    budget.initialize()
    budget.ensure_user(identity, date.today())
    rows = budget.transactions_for_day(identity, date.today())

    assert [row.category for row in rows] == ["other"]


def test_new_account_starts_with_ten_dollar_daily_budget(tmp_path: Path) -> None:
    with client(tmp_path) as test_client:
        summary = test_client.get("/api/budget/summary").json()
        history = test_client.get("/api/budget/history?days=1").json()

    assert summary["daily_budget_cents"] == 1000
    assert summary["daily_remaining_cents"] == 1000
    assert history["days"][-1]["budget_cents"] == 1000


def test_untouched_zero_placeholder_migrates_to_default_with_audit_event(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "habits.db"
    timezone = ZoneInfo("Asia/Singapore")
    water = WaterRepository(database_path, timezone)
    budget = BudgetRepository(database_path, timezone)
    water.initialize()
    budget.initialize()
    identity = "development:legacy@example.test"
    water.ensure_user(identity, "legacy")
    budget.ensure_user(identity, date(2026, 9, 24))
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE daily_budget_changes SET amount_cents = 0 WHERE identity = ?",
            (identity,),
        )
        connection.execute(
            "DELETE FROM budget_adjustments WHERE owner_identity = ?", (identity,)
        )

    budget.initialize()
    summary = budget.summary(identity, date(2026, 9, 24))
    adjustments = [
        entry
        for entry in budget.fund_ledger(identity, date(2026, 9, 24), 20)
        if entry.kind == "daily_budget_adjustment"
    ]

    assert summary.daily_budget_cents == 1000
    assert len(adjustments) == 1
    assert adjustments[0].previous_amount_cents == 0
    assert adjustments[0].new_amount_cents == 1000
