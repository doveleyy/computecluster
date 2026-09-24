from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Drink:
    id: str
    amount_ml: int
    drink_type: str
    temperature: str
    sweetness: str | None
    consumed_at: datetime


@dataclass(frozen=True)
class DaySummary:
    day: date
    total_ml: int
    goal_ml: int
    breakdown_ml: dict[str, int]


class WaterRepository:
    def __init__(self, database_path: Path, timezone: ZoneInfo) -> None:
        self._database_path = database_path
        self._timezone = timezone

    def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS users (
                    identity TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_settings (
                    identity TEXT PRIMARY KEY REFERENCES users(identity),
                    daily_goal_ml INTEGER NOT NULL
                        CHECK (daily_goal_ml BETWEEN 250 AND 10000)
                );

                CREATE TABLE IF NOT EXISTS drinks (
                    id TEXT PRIMARY KEY,
                    owner_identity TEXT NOT NULL REFERENCES users(identity),
                    amount_ml INTEGER NOT NULL CHECK (amount_ml BETWEEN 10 AND 2000),
                    drink_type TEXT NOT NULL DEFAULT 'water',
                    temperature TEXT NOT NULL DEFAULT 'normal',
                    sweetness TEXT,
                    consumed_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS drinks_owner_consumed
                    ON drinks(owner_identity, consumed_at DESC);
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(drinks)").fetchall()
            }
            if "drink_type" not in columns:
                connection.execute(
                    "ALTER TABLE drinks ADD COLUMN drink_type TEXT NOT NULL "
                    "DEFAULT 'water'"
                )
            if "temperature" not in columns:
                connection.execute(
                    "ALTER TABLE drinks ADD COLUMN temperature TEXT NOT NULL "
                    "DEFAULT 'normal'"
                )
            if "sweetness" not in columns:
                connection.execute("ALTER TABLE drinks ADD COLUMN sweetness TEXT")
            connection.executescript(
                """
                DROP TRIGGER IF EXISTS drinks_valid_type_insert;
                DROP TRIGGER IF EXISTS drinks_valid_type_update;
                DROP TRIGGER IF EXISTS drinks_valid_temperature_insert;
                DROP TRIGGER IF EXISTS drinks_valid_temperature_update;
                DROP TRIGGER IF EXISTS drinks_valid_sweetness_insert;
                DROP TRIGGER IF EXISTS drinks_valid_sweetness_update;

                UPDATE drinks
                SET drink_type = 'supplement_water'
                WHERE drink_type = 'sparkling_water';

                CREATE INDEX IF NOT EXISTS drinks_owner_type_consumed
                    ON drinks(owner_identity, drink_type, consumed_at DESC);

                CREATE TRIGGER drinks_valid_type_insert
                BEFORE INSERT ON drinks
                WHEN NEW.drink_type NOT IN (
                    'water', 'supplement_water', 'coffee', 'tea', 'milk',
                    'juice', 'soft_drink', 'sports_drink', 'alcohol', 'other'
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid drink type');
                END;

                CREATE TRIGGER drinks_valid_type_update
                BEFORE UPDATE OF drink_type ON drinks
                WHEN NEW.drink_type NOT IN (
                    'water', 'supplement_water', 'coffee', 'tea', 'milk',
                    'juice', 'soft_drink', 'sports_drink', 'alcohol', 'other'
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid drink type');
                END;

                CREATE TRIGGER drinks_valid_temperature_insert
                BEFORE INSERT ON drinks
                WHEN NEW.temperature NOT IN ('hot', 'normal', 'iced')
                BEGIN
                    SELECT RAISE(ABORT, 'invalid drink temperature');
                END;

                CREATE TRIGGER drinks_valid_temperature_update
                BEFORE UPDATE OF temperature ON drinks
                WHEN NEW.temperature NOT IN ('hot', 'normal', 'iced')
                BEGIN
                    SELECT RAISE(ABORT, 'invalid drink temperature');
                END;

                CREATE TRIGGER drinks_valid_sweetness_insert
                BEFORE INSERT ON drinks
                WHEN NEW.sweetness IS NOT NULL AND (
                    NEW.sweetness NOT IN ('none', 'less', 'regular', 'extra')
                    OR NEW.drink_type NOT IN ('coffee', 'tea')
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid drink sweetness');
                END;

                CREATE TRIGGER drinks_valid_sweetness_update
                BEFORE UPDATE OF sweetness, drink_type ON drinks
                WHEN NEW.sweetness IS NOT NULL AND (
                    NEW.sweetness NOT IN ('none', 'less', 'regular', 'extra')
                    OR NEW.drink_type NOT IN ('coffee', 'tea')
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid drink sweetness');
                END;
                """
            )

    def ready(self) -> bool:
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT 1").fetchone()
                return row is not None and int(row[0]) == 1
        except sqlite3.Error:
            return False

    def ensure_user(self, identity: str, display_name: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users(identity, display_name, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(identity) DO UPDATE SET display_name = excluded.display_name
                """,
                (identity, display_name, now),
            )
            connection.execute(
                """
                INSERT INTO user_settings(identity, daily_goal_ml)
                VALUES (?, 2000)
                ON CONFLICT(identity) DO NOTHING
                """,
                (identity,),
            )

    def adopt_identity(
        self, old_identity: str, new_identity: str, display_name: str
    ) -> None:
        if old_identity == new_identity:
            self.ensure_user(new_identity, display_name)
            return
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            old_goal = connection.execute(
                "SELECT daily_goal_ml FROM user_settings WHERE identity = ?",
                (old_identity,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO users(identity, display_name, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(identity) DO UPDATE SET display_name = excluded.display_name
                """,
                (new_identity, display_name, now),
            )
            connection.execute(
                """
                INSERT INTO user_settings(identity, daily_goal_ml)
                VALUES (?, ?)
                ON CONFLICT(identity) DO NOTHING
                """,
                (new_identity, int(old_goal[0]) if old_goal else 2000),
            )
            connection.execute(
                """
                UPDATE drinks SET owner_identity = ? WHERE owner_identity = ?
                """,
                (new_identity, old_identity),
            )
            connection.execute(
                "DELETE FROM user_settings WHERE identity = ?", (old_identity,)
            )
            connection.execute("DELETE FROM users WHERE identity = ?", (old_identity,))

    def goal(self, identity: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT daily_goal_ml FROM user_settings WHERE identity = ?",
                (identity,),
            ).fetchone()
        if row is None:
            raise LookupError("water tracker user is not initialized")
        return int(row[0])

    def set_goal(self, identity: str, daily_goal_ml: int) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE user_settings
                SET daily_goal_ml = ?
                WHERE identity = ?
                """,
                (daily_goal_ml, identity),
            )
        if cursor.rowcount != 1:
            raise LookupError("water tracker user is not initialized")

    def add_drink(
        self,
        identity: str,
        amount_ml: int,
        drink_type: str,
        temperature: str,
        sweetness: str | None,
    ) -> Drink:
        drink = Drink(
            id=str(uuid4()),
            amount_ml=amount_ml,
            drink_type=drink_type,
            temperature=temperature,
            sweetness=sweetness,
            consumed_at=datetime.now(UTC),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO drinks(
                    id, owner_identity, amount_ml, drink_type, temperature,
                    sweetness, consumed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    drink.id,
                    identity,
                    drink.amount_ml,
                    drink.drink_type,
                    drink.temperature,
                    drink.sweetness,
                    drink.consumed_at.isoformat(),
                ),
            )
        return drink

    def delete_drink(self, identity: str, drink_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM drinks WHERE id = ? AND owner_identity = ?",
                (drink_id, identity),
            )
        return cursor.rowcount == 1

    def drinks_for_day(self, identity: str, day: date) -> list[Drink]:
        start, end = self._utc_bounds(day)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, amount_ml, drink_type, temperature, sweetness, consumed_at
                FROM drinks
                WHERE owner_identity = ?
                  AND consumed_at >= ?
                  AND consumed_at < ?
                ORDER BY consumed_at DESC
                """,
                (identity, start.isoformat(), end.isoformat()),
            ).fetchall()
        return [
            Drink(
                id=str(row[0]),
                amount_ml=int(row[1]),
                drink_type=str(row[2]),
                temperature=str(row[3]),
                sweetness=str(row[4]) if row[4] is not None else None,
                consumed_at=datetime.fromisoformat(str(row[5])),
            )
            for row in rows
        ]

    def history(self, identity: str, end_day: date, days: int) -> list[DaySummary]:
        first_day = end_day - timedelta(days=days - 1)
        start, _ = self._utc_bounds(first_day)
        _, end = self._utc_bounds(end_day)
        totals: dict[date, int] = {}
        breakdowns: dict[date, dict[str, int]] = {}
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT amount_ml, drink_type, consumed_at
                FROM drinks
                WHERE owner_identity = ?
                  AND consumed_at >= ?
                  AND consumed_at < ?
                """,
                (identity, start.isoformat(), end.isoformat()),
            ).fetchall()
        for amount_ml, drink_type, consumed_at in rows:
            local_day = (
                datetime.fromisoformat(str(consumed_at))
                .astimezone(self._timezone)
                .date()
            )
            totals[local_day] = totals.get(local_day, 0) + int(amount_ml)
            day_breakdown = breakdowns.setdefault(local_day, {})
            category = str(drink_type)
            day_breakdown[category] = day_breakdown.get(category, 0) + int(amount_ml)
        goal_ml = self.goal(identity)
        return [
            DaySummary(
                day=first_day + timedelta(days=index),
                total_ml=totals.get(first_day + timedelta(days=index), 0),
                goal_ml=goal_ml,
                breakdown_ml=breakdowns.get(first_day + timedelta(days=index), {}),
            )
            for index in range(days)
        ]

    def _utc_bounds(self, day: date) -> tuple[datetime, datetime]:
        start = datetime.combine(day, datetime.min.time(), self._timezone)
        end = start + timedelta(days=1)
        return start.astimezone(UTC), end.astimezone(UTC)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection
