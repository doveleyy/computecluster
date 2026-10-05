"""Fewer outbound calls: one fetch per add, one rate per run."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from services.wishlist import pricing, refresh
from services.wishlist.repository import WishlistRepository
from services.wishlist.tests.test_api import SHOPIFY, URL, client


def counting_stub(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"shopify": 0, "rate": 0}

    def shopify(url: str, currency: str | None = None) -> pricing.PriceObservation:
        calls["shopify"] += 1
        return SHOPIFY

    def rate(base: str, quote: str) -> tuple[date, Decimal]:
        calls["rate"] += 1
        return date(2026, 9, 24), Decimal("1.2799")

    monkeypatch.setattr(pricing, "fetch_shopify", shopify)
    monkeypatch.setattr(pricing, "fetch_rate", rate)
    return calls


def test_adding_a_product_fetches_the_shop_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = counting_stub(monkeypatch)
    with client(tmp_path) as user:
        created = user.post("/api/products", json={"url": URL})
    assert created.status_code == 201
    assert created.json()["latest"]["display_cents"] == 38141
    assert calls == {"shopify": 1, "rate": 1}


def test_a_run_fetches_each_exchange_rate_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = counting_stub(monkeypatch)
    with client(tmp_path) as user:
        for index in range(3):
            url = f"https://shop.example/products/item-{index}"
            assert user.post("/api/products", json={"url": url}).status_code == 201
        calls.update(shopify=0, rate=0)
        refreshed = user.post("/api/refresh").json()
    assert refreshed["checked"] == 3
    assert calls == {"shopify": 3, "rate": 1}

    monkeypatch.setenv("WISHLIST_DB_PATH", str(tmp_path / "wishlist.db"))
    calls.update(shopify=0, rate=0)
    assert refresh.main(["--pause-seconds", "0"]) == 0
    assert calls == {"shopify": 3, "rate": 1}
    repository = WishlistRepository(tmp_path / "wishlist.db", ZoneInfo("UTC"))
    owner = "development:member@example.test"
    # Add, the web refresh and the timer refresh each recorded a reading.
    assert all(
        len(repository.history(owner, entry.product.id, 10)) == 3
        for entry in repository.entries(owner)
    )
