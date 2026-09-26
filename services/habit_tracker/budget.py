from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

TRANSACTION_KINDS = {"daily_spend", "fund_redemption", "fund_contribution"}
SPEND_CATEGORIES = (
    "food",
    "drinks",
    "transport",
    "shopping",
    "bills",
    "entertainment",
    "health",
    "other",
)
_CATEGORY_SQL_LIST = ", ".join(f"'{category}'" for category in SPEND_CATEGORIES)


@dataclass(frozen=True)
class BudgetTransaction:
    id: str
    kind: str
    amount_cents: int
    description: str
    occurred_at: datetime
    category: str = "other"


@dataclass(frozen=True)
class BudgetDay:
    day: date
    budget_cents: int
    spent_cents: int

    @property
    def net_cents(self) -> int:
        return self.budget_cents - self.spent_cents


@dataclass(frozen=True)
class FundEntry:
    key: str
    entry_date: date
    kind: str
    amount_cents: int
    description: str
    occurred_at: datetime | None = None
    previous_amount_cents: int | None = None
    new_amount_cents: int | None = None


@dataclass(frozen=True)
class BudgetSummary:
    day: date
    currency: str
    daily_budget_cents: int
    daily_spent_cents: int
    daily_remaining_cents: int
    settled_fund_cents: int
    fund_balance_cents: int
    pending_surplus_cents: int
    transactions: list[BudgetTransaction]


class BudgetRepository:
    def __init__(self, database_path: Path, timezone: ZoneInfo) -> None:
        self._database_path = database_path
        self._timezone = timezone

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS budget_settings (
                    identity TEXT PRIMARY KEY REFERENCES users(identity),
                    currency TEXT NOT NULL DEFAULT 'SGD',
                    plan_start_date TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS daily_budget_changes (
                    identity TEXT NOT NULL REFERENCES users(identity),
                    effective_date TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL
                        CHECK (amount_cents BETWEEN 0 AND 1000000),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (identity, effective_date)
                );

                CREATE TABLE IF NOT EXISTS budget_adjustments (
                    id TEXT PRIMARY KEY,
                    owner_identity TEXT NOT NULL REFERENCES users(identity),
                    effective_date TEXT NOT NULL,
                    previous_amount_cents INTEGER NOT NULL
                        CHECK (previous_amount_cents BETWEEN 0 AND 1000000),
                    new_amount_cents INTEGER NOT NULL
                        CHECK (new_amount_cents BETWEEN 0 AND 1000000),
                    occurred_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS budget_adjustments_owner_time
                    ON budget_adjustments(owner_identity, occurred_at DESC);

                CREATE TABLE IF NOT EXISTS savings_goal_changes (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_identity TEXT NOT NULL REFERENCES users(identity),
                    previous_target_cents INTEGER,
                    target_cents INTEGER CHECK (
                        target_cents IS NULL OR
                        target_cents BETWEEN 1 AND 100000000
                    ),
                    currency TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS savings_goal_owner_sequence
                    ON savings_goal_changes(owner_identity, sequence DESC);

                CREATE TABLE IF NOT EXISTS budget_transactions (
                    id TEXT PRIMARY KEY,
                    owner_identity TEXT NOT NULL REFERENCES users(identity),
                    kind TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL
                        CHECK (amount_cents BETWEEN 1 AND 100000000),
                    description TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'other'
                );

                CREATE INDEX IF NOT EXISTS budget_transactions_owner_time
                    ON budget_transactions(owner_identity, occurred_at DESC);

                CREATE TRIGGER IF NOT EXISTS budget_transaction_kind_insert
                BEFORE INSERT ON budget_transactions
                WHEN NEW.kind NOT IN (
                    'daily_spend', 'fund_redemption', 'fund_contribution'
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid budget transaction kind');
                END;

                CREATE TRIGGER IF NOT EXISTS budget_transaction_kind_update
                BEFORE UPDATE OF kind ON budget_transactions
                WHEN NEW.kind NOT IN (
                    'daily_spend', 'fund_redemption', 'fund_contribution'
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid budget transaction kind');
                END;
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(budget_transactions)"
                ).fetchall()
            }
            if "category" not in columns:
                connection.execute(
                    "ALTER TABLE budget_transactions ADD COLUMN category TEXT "
                    "NOT NULL DEFAULT 'other'"
                )
            # Recreated every start so the vocabulary can widen without a
            # bespoke migration, matching the drink triggers in water.py.
            connection.executescript(
                f"""
                DROP TRIGGER IF EXISTS budget_category_insert;
                DROP TRIGGER IF EXISTS budget_category_update;

                CREATE TRIGGER budget_category_insert
                BEFORE INSERT ON budget_transactions
                WHEN NEW.category NOT IN ({_CATEGORY_SQL_LIST})
                BEGIN
                    SELECT RAISE(ABORT, 'invalid budget category');
                END;

                CREATE TRIGGER budget_category_update
                BEFORE UPDATE OF category ON budget_transactions
                WHEN NEW.category NOT IN ({_CATEGORY_SQL_LIST})
                BEGIN
                    SELECT RAISE(ABORT, 'invalid budget category');
                END;
                """
            )
            self._migrate_initial_budget(connection)

    def ready(self) -> bool:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'budget_transactions'"
                ).fetchone()
                return row is not None
        except sqlite3.Error:
            return False

    def ensure_user(self, identity: str, local_day: date) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO budget_settings(identity, currency, plan_start_date)
                VALUES (?, 'SGD', ?)
                ON CONFLICT(identity) DO NOTHING
                """,
                (identity, local_day.isoformat()),
            )
            connection.execute(
                """
                INSERT INTO daily_budget_changes(
                    identity, effective_date, amount_cents, created_at
                )
                SELECT ?, ?, 1000, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM daily_budget_changes WHERE identity = ?
                )
                """,
                (identity, local_day.isoformat(), now, identity),
            )
            connection.execute(
                """
                INSERT INTO budget_adjustments(
                    id, owner_identity, effective_date,
                    previous_amount_cents, new_amount_cents, occurred_at
                )
                SELECT ?, ?, ?, 0, 1000, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM budget_adjustments WHERE owner_identity = ?
                )
                """,
                (str(uuid4()), identity, local_day.isoformat(), now, identity),
            )

    def adopt_identity(
        self,
        old_identity: str,
        new_identity: str,
        display_name: str,
        local_day: date,
    ) -> None:
        if old_identity == new_identity:
            return
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users(identity, display_name, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(identity) DO UPDATE SET display_name = excluded.display_name
                """,
                (new_identity, display_name, now),
            )
            old_setting = connection.execute(
                """
                SELECT currency, plan_start_date
                FROM budget_settings WHERE identity = ?
                """,
                (old_identity,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO budget_settings(identity, currency, plan_start_date)
                VALUES (?, ?, ?)
                ON CONFLICT(identity) DO NOTHING
                """,
                (
                    new_identity,
                    str(old_setting[0]) if old_setting else "SGD",
                    str(old_setting[1]) if old_setting else local_day.isoformat(),
                ),
            )
            old_changes = connection.execute(
                """
                SELECT effective_date, amount_cents, created_at
                FROM daily_budget_changes WHERE identity = ?
                """,
                (old_identity,),
            ).fetchall()
            for effective_date, amount_cents, created_at in old_changes:
                connection.execute(
                    """
                    INSERT INTO daily_budget_changes(
                        identity, effective_date, amount_cents, created_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(identity, effective_date) DO NOTHING
                    """,
                    (new_identity, effective_date, amount_cents, created_at),
                )
            connection.execute(
                """
                UPDATE budget_transactions
                SET owner_identity = ? WHERE owner_identity = ?
                """,
                (new_identity, old_identity),
            )
            connection.execute(
                """
                UPDATE budget_adjustments
                SET owner_identity = ? WHERE owner_identity = ?
                """,
                (new_identity, old_identity),
            )
            connection.execute(
                "UPDATE savings_goal_changes SET owner_identity = ? "
                "WHERE owner_identity = ?",
                (new_identity, old_identity),
            )
            connection.execute(
                "DELETE FROM daily_budget_changes WHERE identity = ?",
                (old_identity,),
            )
            connection.execute(
                "DELETE FROM budget_settings WHERE identity = ?",
                (old_identity,),
            )

    def savings_goal(self, identity: str) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT target_cents FROM savings_goal_changes "
                "WHERE owner_identity = ? ORDER BY sequence DESC LIMIT 1",
                (identity,),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def set_savings_goal(self, identity: str, target_cents: int | None) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT target_cents FROM savings_goal_changes "
                "WHERE owner_identity = ? ORDER BY sequence DESC LIMIT 1",
                (identity,),
            ).fetchone()
            previous = int(row[0]) if row and row[0] is not None else None
            if previous == target_cents:
                return
            connection.execute(
                """
                INSERT INTO savings_goal_changes(
                    owner_identity, previous_target_cents, target_cents,
                    currency, occurred_at
                ) SELECT ?, ?, ?, currency, ? FROM budget_settings WHERE identity = ?
                """,
                (
                    identity,
                    previous,
                    target_cents,
                    datetime.now(UTC).isoformat(),
                    identity,
                ),
            )

    def set_daily_budget(
        self, identity: str, effective_date: date, amount_cents: int
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            # Serialize the read-before-write pair so concurrent edits each
            # preserve the value immediately preceding that specific change.
            connection.execute("BEGIN IMMEDIATE")
            previous_amount = self._budget_on_date(connection, identity, effective_date)
            if previous_amount == amount_cents:
                return
            connection.execute(
                """
                INSERT INTO daily_budget_changes(
                    identity, effective_date, amount_cents, created_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(identity, effective_date)
                DO UPDATE SET amount_cents = excluded.amount_cents,
                              created_at = excluded.created_at
                """,
                (identity, effective_date.isoformat(), amount_cents, now),
            )
            connection.execute(
                """
                INSERT INTO budget_adjustments(
                    id, owner_identity, effective_date,
                    previous_amount_cents, new_amount_cents, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    identity,
                    effective_date.isoformat(),
                    previous_amount,
                    amount_cents,
                    now,
                ),
            )

    def add_transaction(
        self,
        identity: str,
        kind: str,
        amount_cents: int,
        description: str,
        category: str = "other",
    ) -> BudgetTransaction:
        if kind not in TRANSACTION_KINDS:
            raise ValueError("invalid budget transaction kind")
        if category not in SPEND_CATEGORIES:
            raise ValueError("invalid budget category")
        transaction = BudgetTransaction(
            id=str(uuid4()),
            kind=kind,
            amount_cents=amount_cents,
            description=description,
            occurred_at=datetime.now(UTC),
            category=category,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO budget_transactions(
                    id, owner_identity, kind, amount_cents,
                    description, occurred_at, category
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transaction.id,
                    identity,
                    transaction.kind,
                    transaction.amount_cents,
                    transaction.description,
                    transaction.occurred_at.isoformat(),
                    transaction.category,
                ),
            )
        return transaction

    def delete_transaction(self, identity: str, transaction_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM budget_transactions
                WHERE id = ? AND owner_identity = ?
                """,
                (transaction_id, identity),
            )
        return cursor.rowcount == 1

    def summary(self, identity: str, local_day: date) -> BudgetSummary:
        days = self._days(identity, local_day)
        today = days[-1]
        transactions = self.transactions_for_day(identity, local_day)
        contribution_cents, redemption_cents = self._fund_movements(identity)
        completed_net = sum(day.net_cents for day in days[:-1])
        settled_fund = completed_net + contribution_cents - redemption_cents
        today_overage = min(today.net_cents, 0)
        return BudgetSummary(
            day=local_day,
            currency=self.currency(identity),
            daily_budget_cents=today.budget_cents,
            daily_spent_cents=today.spent_cents,
            daily_remaining_cents=today.net_cents,
            settled_fund_cents=settled_fund,
            fund_balance_cents=settled_fund + today_overage,
            pending_surplus_cents=max(today.net_cents, 0),
            transactions=transactions,
        )

    def history(self, identity: str, local_day: date, days: int) -> list[BudgetDay]:
        all_days = self._days(identity, local_day)
        return all_days[-days:]

    def day(self, identity: str, local_day: date) -> BudgetDay:
        return self._days(identity, local_day)[-1]

    def fund_ledger(
        self, identity: str, local_day: date, limit: int
    ) -> list[FundEntry]:
        entries = [
            FundEntry(
                key=f"settlement:{day.day.isoformat()}",
                entry_date=day.day,
                kind="daily_settlement",
                amount_cents=day.net_cents,
                description="Daily surplus" if day.net_cents >= 0 else "Daily overage",
            )
            for day in self._days(identity, local_day)[:-1]
            if day.net_cents != 0
        ]
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, kind, amount_cents, description, occurred_at
                FROM budget_transactions
                WHERE owner_identity = ?
                  AND kind IN ('fund_redemption', 'fund_contribution')
                """,
                (identity,),
            ).fetchall()
            adjustment_rows = connection.execute(
                """
                SELECT id, effective_date, previous_amount_cents,
                       new_amount_cents, occurred_at
                FROM budget_adjustments
                WHERE owner_identity = ?
                """,
                (identity,),
            ).fetchall()
        for row in rows:
            occurred_at = datetime.fromisoformat(str(row[4]))
            kind = str(row[1])
            amount = int(row[2])
            entries.append(
                FundEntry(
                    key=f"transaction:{row[0]}",
                    entry_date=occurred_at.astimezone(self._timezone).date(),
                    kind=kind,
                    amount_cents=amount if kind == "fund_contribution" else -amount,
                    description=str(row[3]),
                    occurred_at=occurred_at,
                )
            )
        for row in adjustment_rows:
            previous_amount = int(row[2])
            new_amount = int(row[3])
            entries.append(
                FundEntry(
                    key=f"adjustment:{row[0]}",
                    entry_date=date.fromisoformat(str(row[1])),
                    kind="daily_budget_adjustment",
                    amount_cents=new_amount - previous_amount,
                    description="Daily budget changed",
                    occurred_at=datetime.fromisoformat(str(row[4])),
                    previous_amount_cents=previous_amount,
                    new_amount_cents=new_amount,
                )
            )
        entries.sort(
            key=lambda entry: (
                entry.entry_date,
                entry.occurred_at or datetime.min.replace(tzinfo=UTC),
                entry.key,
            ),
            reverse=True,
        )
        return entries[:limit]

    @staticmethod
    def _budget_on_date(
        connection: sqlite3.Connection, identity: str, effective_date: date
    ) -> int:
        row = connection.execute(
            """
            SELECT amount_cents
            FROM daily_budget_changes
            WHERE identity = ? AND effective_date <= ?
            ORDER BY effective_date DESC
            LIMIT 1
            """,
            (identity, effective_date.isoformat()),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    @staticmethod
    def _migrate_initial_budget(connection: sqlite3.Connection) -> None:
        """Turn the pre-0.4.1 zero placeholder into the documented default.

        The old build created exactly one zero change for untouched accounts.
        Accounts with transactions or multiple changes are left unchanged.
        """
        connection.execute(
            """
            UPDATE daily_budget_changes
            SET amount_cents = 1000
            WHERE amount_cents = 0
              AND NOT EXISTS (
                  SELECT 1 FROM budget_adjustments
                  WHERE owner_identity = daily_budget_changes.identity
              )
              AND identity IN (
                  SELECT changes.identity
                  FROM daily_budget_changes AS changes
                  LEFT JOIN budget_transactions AS transactions
                    ON transactions.owner_identity = changes.identity
                  GROUP BY changes.identity
                  HAVING COUNT(DISTINCT changes.effective_date) = 1
                     AND COUNT(transactions.id) = 0
              )
            """
        )
        connection.execute(
            """
            INSERT INTO budget_adjustments(
                id, owner_identity, effective_date,
                previous_amount_cents, new_amount_cents, occurred_at
            )
            SELECT lower(hex(randomblob(4))) || '-' ||
                   lower(hex(randomblob(2))) || '-4' ||
                   substr(lower(hex(randomblob(2))), 2) || '-' ||
                   substr('89ab', abs(random()) % 4 + 1, 1) ||
                   substr(lower(hex(randomblob(2))), 2) || '-' ||
                   lower(hex(randomblob(6))),
                   changes.identity, changes.effective_date, 0,
                   changes.amount_cents, changes.created_at
            FROM daily_budget_changes AS changes
            WHERE NOT EXISTS (
                SELECT 1 FROM budget_adjustments AS adjustments
                WHERE adjustments.owner_identity = changes.identity
            )
            """
        )

    def transactions_for_day(
        self, identity: str, local_day: date
    ) -> list[BudgetTransaction]:
        start, end = self._utc_bounds(local_day)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, kind, amount_cents, description, occurred_at, category
                FROM budget_transactions
                WHERE owner_identity = ?
                  AND occurred_at >= ?
                  AND occurred_at < ?
                ORDER BY occurred_at DESC
                """,
                (identity, start.isoformat(), end.isoformat()),
            ).fetchall()
        return [
            BudgetTransaction(
                id=str(row[0]),
                kind=str(row[1]),
                amount_cents=int(row[2]),
                description=str(row[3]),
                occurred_at=datetime.fromisoformat(str(row[4])),
                category=str(row[5]),
            )
            for row in rows
        ]

    def currency(self, identity: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT currency FROM budget_settings WHERE identity = ?",
                (identity,),
            ).fetchone()
        if row is None:
            raise LookupError("budget user is not initialized")
        return str(row[0])

    def _days(self, identity: str, end_day: date) -> list[BudgetDay]:
        with self._connect() as connection:
            setting = connection.execute(
                "SELECT plan_start_date FROM budget_settings WHERE identity = ?",
                (identity,),
            ).fetchone()
            changes = connection.execute(
                """
                SELECT effective_date, amount_cents
                FROM daily_budget_changes
                WHERE identity = ? AND effective_date <= ?
                ORDER BY effective_date
                """,
                (identity, end_day.isoformat()),
            ).fetchall()
            transaction_rows = connection.execute(
                """
                SELECT amount_cents, occurred_at
                FROM budget_transactions
                WHERE owner_identity = ? AND kind = 'daily_spend'
                """,
                (identity,),
            ).fetchall()
        if setting is None:
            raise LookupError("budget user is not initialized")
        start_day = date.fromisoformat(str(setting[0]))
        if start_day > end_day:
            start_day = end_day
        spent_by_day: dict[date, int] = {}
        for amount_cents, occurred_at in transaction_rows:
            transaction_day = (
                datetime.fromisoformat(str(occurred_at))
                .astimezone(self._timezone)
                .date()
            )
            if start_day <= transaction_day <= end_day:
                spent_by_day[transaction_day] = spent_by_day.get(
                    transaction_day, 0
                ) + int(amount_cents)
        parsed_changes = [
            (date.fromisoformat(str(change_day)), int(amount))
            for change_day, amount in changes
        ]
        result: list[BudgetDay] = []
        change_index = 0
        current_budget = 0
        day = start_day
        while day <= end_day:
            while (
                change_index < len(parsed_changes)
                and parsed_changes[change_index][0] <= day
            ):
                current_budget = parsed_changes[change_index][1]
                change_index += 1
            result.append(
                BudgetDay(
                    day=day,
                    budget_cents=current_budget,
                    spent_cents=spent_by_day.get(day, 0),
                )
            )
            day += timedelta(days=1)
        return result

    def _fund_movements(self, identity: str) -> tuple[int, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT kind, COALESCE(SUM(amount_cents), 0)
                FROM budget_transactions
                WHERE owner_identity = ?
                  AND kind IN ('fund_redemption', 'fund_contribution')
                GROUP BY kind
                """,
                (identity,),
            ).fetchall()
        totals = {str(kind): int(amount) for kind, amount in rows}
        return (
            totals.get("fund_contribution", 0),
            totals.get("fund_redemption", 0),
        )

    def _utc_bounds(self, local_day: date) -> tuple[datetime, datetime]:
        start = datetime.combine(local_day, datetime.min.time(), self._timezone)
        end = start + timedelta(days=1)
        return start.astimezone(UTC), end.astimezone(UTC)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection
