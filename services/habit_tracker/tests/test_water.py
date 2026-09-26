import sqlite3
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from services.habit_tracker.main import Identity, create_app


def client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(tmp_path / "water.db", allow_dev_identity=True),
        headers={"X-Habit-Tracker-Dev-User": "member@example.test"},
    )


def test_health_does_not_require_identity(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "water.db")) as test_client:
        response = test_client.get("/health")

    assert response.status_code == 200
    assert response.json()["service"] == "habit-tracker"


def test_application_routes_require_proxy_identity(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "water.db")) as test_client:
        response = test_client.get("/api/today")

    assert response.status_code == 401


def test_logging_ui_has_compact_amount_and_attribute_controls(tmp_path: Path) -> None:
    with client(tmp_path) as test_client:
        response = test_client.get("/water")

    assert response.status_code == 200
    assert 'data-amount="250"' in response.text
    assert 'data-amount="500"' in response.text
    assert 'data-amount="350"' not in response.text
    assert 'data-temperature="hot"' in response.text
    assert 'data-temperature="normal"' in response.text
    assert 'data-temperature="iced"' in response.text
    assert 'data-sweetness="regular"' in response.text
    assert 'id="sweetness-group" hidden' not in response.text


def test_record_and_remove_a_drink(tmp_path: Path) -> None:
    with client(tmp_path) as test_client:
        empty = test_client.get("/api/today")
        created = test_client.post(
            "/api/drinks",
            json={
                "amount_ml": 350,
                "drink_type": "coffee",
                "temperature": "hot",
                "sweetness": "less",
            },
        )
        populated = test_client.get("/api/today")
        deleted = test_client.delete(f"/api/drinks/{created.json()['id']}")
        final = test_client.get("/api/today")

    assert empty.json()["total_ml"] == 0
    assert created.status_code == 201
    assert created.json()["drink_type"] == "coffee"
    assert created.json()["temperature"] == "hot"
    assert created.json()["sweetness"] == "less"
    assert populated.json()["total_ml"] == 350
    assert populated.json()["breakdown_ml"] == {"coffee": 350}
    assert deleted.status_code == 204
    assert final.json()["total_ml"] == 0


def test_identity_isolation(tmp_path: Path) -> None:
    with client(tmp_path) as first:
        drink = first.post("/api/drinks", json={"amount_ml": 500}).json()

    with TestClient(
        create_app(tmp_path / "water.db", allow_dev_identity=True),
        headers={"X-Habit-Tracker-Dev-User": "someone-else@example.test"},
    ) as second:
        today = second.get("/api/today")
        deletion = second.delete(f"/api/drinks/{drink['id']}")

    assert today.json()["total_ml"] == 0
    assert deletion.status_code == 404


def test_goal_validation_and_history(tmp_path: Path) -> None:
    with client(tmp_path) as test_client:
        updated = test_client.put("/api/settings", json={"daily_goal_ml": 2400})
        invalid = test_client.put("/api/settings", json={"daily_goal_ml": 100})
        test_client.post("/api/drinks", json={"amount_ml": 250, "drink_type": "tea"})
        history = test_client.get("/api/history?days=7")

    assert updated.json() == {"daily_goal_ml": 2400}
    assert invalid.status_code == 422
    assert len(history.json()["days"]) == 7
    assert history.json()["days"][-1]["goal_ml"] == 2400
    assert history.json()["days"][-1]["breakdown_ml"] == {"tea": 250}


def test_previous_day_analysis_breaks_down_drink_attributes(tmp_path: Path) -> None:
    database_path = tmp_path / "water.db"
    identity = "development:member@example.test"
    timezone = ZoneInfo("Asia/Singapore")
    previous_day = datetime.now(timezone).date() - timedelta(days=1)
    consumed_at = datetime.combine(previous_day, time(12), timezone).astimezone(UTC)
    with client(tmp_path) as test_client:
        assert test_client.get("/api/today").status_code == 200
        with sqlite3.connect(database_path) as connection:
            connection.executemany(
                """
                INSERT INTO drinks(
                    id, owner_identity, amount_ml, drink_type, temperature,
                    sweetness, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        "yesterday-tea",
                        identity,
                        500,
                        "tea",
                        "iced",
                        "less",
                        consumed_at.isoformat(),
                    ),
                    (
                        "yesterday-water",
                        identity,
                        250,
                        "water",
                        "normal",
                        None,
                        consumed_at.isoformat(),
                    ),
                ),
            )
        analysis = test_client.get(f"/api/water/day?date={previous_day}")
        future = test_client.get(
            f"/api/water/day?date={datetime.now(timezone).date() + timedelta(days=1)}"
        )
        page = test_client.get("/water")

    assert analysis.status_code == 200
    assert analysis.json()["total_ml"] == 750
    assert analysis.json()["entry_count"] == 2
    assert analysis.json()["breakdown_ml"] == {"tea": 500, "water": 250}
    assert analysis.json()["temperature_breakdown_ml"] == {
        "iced": 500,
        "normal": 250,
    }
    assert analysis.json()["sweetness_breakdown_ml"] == {"less": 500}
    assert future.status_code == 422
    assert 'id="analysis-date"' in page.text
    assert 'data-breakdown="drink_type"' in page.text
    assert 'data-breakdown="temperature"' in page.text
    assert "Selected day" not in page.text


def test_drink_type_defaults_to_water_and_rejects_unknown_type(
    tmp_path: Path,
) -> None:
    with client(tmp_path) as test_client:
        defaulted = test_client.post("/api/drinks", json={"amount_ml": 250})
        protein_shake = test_client.post(
            "/api/drinks",
            json={"amount_ml": 400, "drink_type": "protein_shake"},
        )
        invalid = test_client.post(
            "/api/drinks", json={"amount_ml": 250, "drink_type": "energy_potion"}
        )
        today = test_client.get("/api/today")
        page = test_client.get("/water")

    assert defaulted.status_code == 201
    assert defaulted.json()["drink_type"] == "water"
    assert defaulted.json()["temperature"] == "normal"
    assert defaulted.json()["sweetness"] is None
    assert protein_shake.status_code == 201
    assert protein_shake.json()["drink_type"] == "protein_shake"
    assert today.json()["breakdown_ml"] == {"protein_shake": 400, "water": 250}
    assert '"value": "protein_shake", "label": "Protein shake"' in page.text
    assert invalid.status_code == 422


def test_temperature_and_sweetness_validation(tmp_path: Path) -> None:
    with client(tmp_path) as test_client:
        iced_tea = test_client.post(
            "/api/drinks",
            json={
                "amount_ml": 500,
                "drink_type": "tea",
                "temperature": "iced",
                "sweetness": "none",
            },
        )
        invalid_temperature = test_client.post(
            "/api/drinks", json={"amount_ml": 250, "temperature": "frozen"}
        )
        sweetness_on_water = test_client.post(
            "/api/drinks",
            json={"amount_ml": 250, "drink_type": "water", "sweetness": "less"},
        )

    assert iced_tea.status_code == 201
    assert iced_tea.json()["temperature"] == "iced"
    assert iced_tea.json()["sweetness"] == "none"
    assert invalid_temperature.status_code == 422
    assert sweetness_on_water.status_code == 422


def test_soft_and_sports_drinks_accept_sweetness(tmp_path: Path) -> None:
    with client(tmp_path) as test_client:
        soft = test_client.post(
            "/api/drinks",
            json={
                "amount_ml": 330,
                "drink_type": "soft_drink",
                "sweetness": "regular",
            },
        )
        sports = test_client.post(
            "/api/drinks",
            json={
                "amount_ml": 500,
                "drink_type": "sports_drink",
                "temperature": "iced",
                "sweetness": "less",
            },
        )
        juice = test_client.post(
            "/api/drinks",
            json={"amount_ml": 200, "drink_type": "juice", "sweetness": "extra"},
        )
        page = test_client.get("/water")

    assert soft.status_code == 201
    assert soft.json()["sweetness"] == "regular"
    assert sports.status_code == 201
    assert sports.json()["sweetness"] == "less"
    # Still refused for drinks that are not sweetened by the drinker.
    assert juice.status_code == 422
    # The page tells the browser which types offer the control, so the UI and
    # the API validator cannot drift apart.
    assert (
        '"sweetenedTypes": ["coffee", "tea", "soft_drink", "sports_drink"]' in page.text
    )


def test_database_rejects_sweetness_on_an_unsweetened_type(tmp_path: Path) -> None:
    database_path = tmp_path / "water.db"
    with client(tmp_path) as test_client:
        assert test_client.get("/api/today").status_code == 200

    with (
        sqlite3.connect(database_path) as connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute(
            """
            INSERT INTO drinks(
                id, owner_identity, amount_ml, drink_type, sweetness, consumed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "bypass",
                "development:member@example.test",
                250,
                "milk",
                "extra",
                datetime.now(UTC).isoformat(),
            ),
        )


def test_existing_database_entries_migrate_to_water(tmp_path: Path) -> None:
    database_path = tmp_path / "water.db"
    identity = "development:member@example.test"
    timestamp = datetime.now(UTC).isoformat()
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE users (
                identity TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE user_settings (
                identity TEXT PRIMARY KEY REFERENCES users(identity),
                daily_goal_ml INTEGER NOT NULL
            );
            CREATE TABLE drinks (
                id TEXT PRIMARY KEY,
                owner_identity TEXT NOT NULL REFERENCES users(identity),
                amount_ml INTEGER NOT NULL,
                consumed_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO users VALUES (?, ?, ?)", (identity, "member", timestamp)
        )
        connection.execute("INSERT INTO user_settings VALUES (?, ?)", (identity, 2000))
        connection.execute(
            "INSERT INTO drinks VALUES (?, ?, ?, ?)",
            ("legacy-drink", identity, 300, timestamp),
        )

    with client(tmp_path) as test_client:
        today = test_client.get("/api/today")

    assert today.status_code == 200
    assert today.json()["drinks"][0]["drink_type"] == "water"
    assert today.json()["drinks"][0]["temperature"] == "normal"
    assert today.json()["drinks"][0]["sweetness"] is None
    assert today.json()["breakdown_ml"] == {"water": 300}


def test_sparkling_water_entries_migrate_to_supplement_water(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "water.db"
    with client(tmp_path) as test_client:
        assert test_client.get("/api/today").status_code == 200

    identity = "development:member@example.test"
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER drinks_valid_type_insert")
        connection.execute(
            """
            INSERT INTO drinks(
                id, owner_identity, amount_ml, drink_type, consumed_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "old-sparkling-drink",
                identity,
                400,
                "sparkling_water",
                datetime.now(UTC).isoformat(),
            ),
        )

    with client(tmp_path) as test_client:
        today = test_client.get("/api/today")
        rejected = test_client.post(
            "/api/drinks",
            json={"amount_ml": 250, "drink_type": "sparkling_water"},
        )

    assert today.status_code == 200
    assert today.json()["drinks"][0]["drink_type"] == "supplement_water"
    assert today.json()["breakdown_ml"] == {"supplement_water": 400}
    assert rejected.status_code == 422


def test_linked_home_platform_identity_adopts_legacy_tailscale_data(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "water.db"
    with TestClient(
        create_app(database_path),
        headers={"Tailscale-User-Login": "member@example.test"},
    ) as legacy:
        assert legacy.post("/api/drinks", json={"amount_ml": 500}).status_code == 201

    with TestClient(
        create_app(
            database_path,
            identity_resolver=lambda _subject: Identity(
                key="home-platform:00000000-0000-0000-0000-000000000123",
                display_name="member",
            ),
        ),
        headers={"Tailscale-User-Login": "member@example.test"},
    ) as linked:
        today = linked.get("/api/today")

    assert today.status_code == 200
    assert today.json()["total_ml"] == 500


def test_unlinked_tailscale_identity_is_refused(tmp_path: Path) -> None:
    with TestClient(
        create_app(tmp_path / "water.db", identity_resolver=lambda _subject: None),
        headers={"Tailscale-User-Login": "unknown@example.test"},
    ) as test_client:
        response = test_client.get("/api/today")

    assert response.status_code == 403
    assert "Link this Tailscale identity" in response.json()["detail"]
