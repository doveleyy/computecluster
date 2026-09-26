"""Wishlist persistence: things you want, and what they have cost over time.

Two numbers move independently. The shop sets a price in its own currency; the
exchange rate moves on its own. Storing only the converted figure would make
"the seller put it up" and "the dollar moved" indistinguishable, so an
observation records the base price, the rate, the rate's publication date, and
the converted figure it produced.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

SOURCES = ("shopify",)


@dataclass(frozen=True)
class WishlistProduct:
    id: str
    name: str
    url: str
    source: str
    base_currency: str
    display_currency: str
    target_cents: int | None
    variant_label: str | None
    created_at: datetime


@dataclass(frozen=True)
class Observation:
    product_id: str
    price_cents: int
    currency: str
    display_cents: int
    in_stock: bool
    variant_available: bool | None
    fx_rate: Decimal | None
    fx_date: date | None
    method: str
    observed_at: datetime


@dataclass(frozen=True)
class WishlistEntry:
    product: WishlistProduct
    latest: Observation | None
    previous_display_cents: int | None

    @property
    def met_target(self) -> bool:
        if self.latest is None or self.product.target_cents is None:
            return False
        return self.latest.display_cents <= self.product.target_cents

    @property
    def change_cents(self) -> int | None:
        if self.latest is None or self.previous_display_cents is None:
            return None
        return self.latest.display_cents - self.previous_display_cents


class WishlistRepository:
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

                -- This service owns its own database, so it owns its own copy
                -- of the identity it was given. It shares the *pattern* with
                -- the habit tracker, never a table.
                CREATE TABLE IF NOT EXISTS users (
                    identity TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS wishlist_products (
                    id TEXT PRIMARY KEY,
                    owner_identity TEXT NOT NULL REFERENCES users(identity),
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'shopify',
                    base_currency TEXT NOT NULL,
                    display_currency TEXT NOT NULL DEFAULT 'SGD',
                    target_cents INTEGER
                        CHECK (target_cents IS NULL OR target_cents > 0),
                    variant_label TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE (owner_identity, url)
                );

                CREATE TABLE IF NOT EXISTS wishlist_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id TEXT NOT NULL
                        REFERENCES wishlist_products(id) ON DELETE CASCADE,
                    price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
                    currency TEXT NOT NULL,
                    display_cents INTEGER NOT NULL CHECK (display_cents >= 0),
                    in_stock INTEGER NOT NULL CHECK (in_stock IN (0, 1)),
                    variant_available INTEGER CHECK (
                        variant_available IN (0, 1) OR variant_available IS NULL
                    ),
                    fx_rate TEXT,
                    fx_date TEXT,
                    method TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS wishlist_observations_product_time
                    ON wishlist_observations(product_id, observed_at DESC);
                """
            )

    def ready(self) -> bool:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'wishlist_products'"
                ).fetchone()
                return row is not None
        except sqlite3.Error:
            return False

    def ensure_user(self, identity: str, display_name: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users(identity, display_name, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(identity)
                DO UPDATE SET display_name = excluded.display_name
                """,
                (identity, display_name, now),
            )

    def adopt_identity(
        self, old_identity: str, new_identity: str, display_name: str
    ) -> None:
        """Move rows from a network-derived key onto the stable platform UUID."""
        if old_identity == new_identity:
            self.ensure_user(new_identity, display_name)
            return
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users(identity, display_name, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(identity)
                DO UPDATE SET display_name = excluded.display_name
                """,
                (new_identity, display_name, now),
            )
            connection.execute(
                "UPDATE wishlist_products SET owner_identity = ? "
                "WHERE owner_identity = ?",
                (new_identity, old_identity),
            )
            connection.execute("DELETE FROM users WHERE identity = ?", (old_identity,))

    def add_product(
        self,
        identity: str,
        *,
        name: str,
        url: str,
        source: str,
        base_currency: str,
        display_currency: str,
        target_cents: int | None,
        variant_label: str | None,
    ) -> WishlistProduct:
        if source not in SOURCES:
            raise ValueError("unsupported wishlist source")
        product = WishlistProduct(
            id=str(uuid4()),
            name=name,
            url=url,
            source=source,
            base_currency=base_currency.upper(),
            display_currency=display_currency.upper(),
            target_cents=target_cents,
            variant_label=variant_label,
            created_at=datetime.now(UTC),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO wishlist_products(
                    id, owner_identity, name, url, source, base_currency,
                    display_currency, target_cents, variant_label, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    product.id,
                    identity,
                    product.name,
                    product.url,
                    product.source,
                    product.base_currency,
                    product.display_currency,
                    product.target_cents,
                    product.variant_label,
                    product.created_at.isoformat(),
                ),
            )
        return product

    def delete_product(self, identity: str, product_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM wishlist_products WHERE id = ? AND owner_identity = ?",
                (product_id, identity),
            )
        return cursor.rowcount == 1

    def set_target(
        self, identity: str, product_id: str, target_cents: int | None
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE wishlist_products SET target_cents = ?
                WHERE id = ? AND owner_identity = ?
                """,
                (target_cents, product_id, identity),
            )
        return cursor.rowcount == 1

    def products(self, identity: str | None = None) -> list[WishlistProduct]:
        query = "SELECT * FROM wishlist_products"
        parameters: tuple[str, ...] = ()
        if identity is not None:
            query += " WHERE owner_identity = ?"
            parameters = (identity,)
        query += " ORDER BY created_at"
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, parameters).fetchall()
        return [self._product(row) for row in rows]

    def record(
        self,
        product_id: str,
        *,
        price_cents: int,
        currency: str,
        display_cents: int,
        in_stock: bool,
        variant_available: bool | None,
        fx_rate: Decimal | None,
        fx_date: date | None,
        method: str,
    ) -> Observation:
        observation = Observation(
            product_id=product_id,
            price_cents=price_cents,
            currency=currency.upper(),
            display_cents=display_cents,
            in_stock=in_stock,
            variant_available=variant_available,
            fx_rate=fx_rate,
            fx_date=fx_date,
            method=method,
            observed_at=datetime.now(UTC),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO wishlist_observations(
                    product_id, price_cents, currency, display_cents, in_stock,
                    variant_available, fx_rate, fx_date, method, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.product_id,
                    observation.price_cents,
                    observation.currency,
                    observation.display_cents,
                    int(observation.in_stock),
                    None if variant_available is None else int(variant_available),
                    None if fx_rate is None else str(fx_rate),
                    None if fx_date is None else fx_date.isoformat(),
                    observation.method,
                    observation.observed_at.isoformat(),
                ),
            )
        return observation

    def entries(self, identity: str) -> list[WishlistEntry]:
        """Each tracked product with its latest observation and prior price."""
        entries: list[WishlistEntry] = []
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            for row in connection.execute(
                "SELECT * FROM wishlist_products WHERE owner_identity = ? "
                "ORDER BY created_at",
                (identity,),
            ).fetchall():
                recent = connection.execute(
                    """
                    SELECT * FROM wishlist_observations
                    WHERE product_id = ? ORDER BY observed_at DESC LIMIT 2
                    """,
                    (row["id"],),
                ).fetchall()
                entries.append(
                    WishlistEntry(
                        product=self._product(row),
                        latest=self._observation(recent[0]) if recent else None,
                        previous_display_cents=(
                            int(recent[1]["display_cents"]) if len(recent) > 1 else None
                        ),
                    )
                )
        return entries

    def history(self, identity: str, product_id: str, limit: int) -> list[Observation]:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT observations.* FROM wishlist_observations AS observations
                JOIN wishlist_products AS products
                  ON products.id = observations.product_id
                WHERE observations.product_id = ? AND products.owner_identity = ?
                ORDER BY observations.observed_at DESC
                LIMIT ?
                """,
                (product_id, identity, limit),
            ).fetchall()
        return [self._observation(row) for row in rows]

    @staticmethod
    def _product(row: sqlite3.Row) -> WishlistProduct:
        return WishlistProduct(
            id=str(row["id"]),
            name=str(row["name"]),
            url=str(row["url"]),
            source=str(row["source"]),
            base_currency=str(row["base_currency"]),
            display_currency=str(row["display_currency"]),
            target_cents=(
                None if row["target_cents"] is None else int(row["target_cents"])
            ),
            variant_label=(
                None if row["variant_label"] is None else str(row["variant_label"])
            ),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    @staticmethod
    def _observation(row: sqlite3.Row) -> Observation:
        return Observation(
            product_id=str(row["product_id"]),
            price_cents=int(row["price_cents"]),
            currency=str(row["currency"]),
            display_cents=int(row["display_cents"]),
            in_stock=bool(row["in_stock"]),
            variant_available=(
                None
                if row["variant_available"] is None
                else bool(row["variant_available"])
            ),
            fx_rate=None if row["fx_rate"] is None else Decimal(str(row["fx_rate"])),
            fx_date=(
                None
                if row["fx_date"] is None
                else date.fromisoformat(str(row["fx_date"]))
            ),
            method=str(row["method"]),
            observed_at=datetime.fromisoformat(str(row["observed_at"])),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=10)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection
