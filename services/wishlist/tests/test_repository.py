import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from services.wishlist import pricing
from services.wishlist.repository import WishlistRepository

SHOPIFY_PRODUCT = {
    "title": "RIVET PANTS - BLACK",
    "price": 29800,
    "available": True,
    "variants": [
        {"title": "28", "price": 29800, "available": True},
        {"title": "32", "price": 29800, "available": False},
    ],
}


def repository(tmp_path: Path) -> tuple[WishlistRepository, str]:
    wishlist = WishlistRepository(tmp_path / "wishlist.db", ZoneInfo("Asia/Singapore"))
    wishlist.initialize()
    identity = "development:member@example.test"
    wishlist.ensure_user(identity, "member")
    return wishlist, identity


def test_shopify_endpoints_are_derived_not_guessed() -> None:
    product, meta = pricing.shopify_endpoints(
        "https://shop.example/products/rivet-pants-black"
    )
    assert product == "https://shop.example/products/rivet-pants-black.js"
    assert meta == "https://shop.example/meta.json"
    # A URL already pointing at the JSON endpoint must not gain a second suffix.
    assert pricing.shopify_endpoints("https://shop.example/products/x.js")[0] == (
        "https://shop.example/products/x.js"
    )
    for bad in ("https://shop.example/collections/all", "/products/relative"):
        with pytest.raises(pricing.PriceSourceError):
            pricing.shopify_endpoints(bad)


def test_shopify_observation_reads_price_and_per_variant_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pricing, "_get_json", lambda url: SHOPIFY_PRODUCT)
    observation = pricing.fetch_shopify(
        "https://shop.example/products/rivet-pants-black", currency="USD"
    )

    assert observation.price_cents == 29800
    assert observation.currency == "USD"
    assert observation.in_stock is True
    assert observation.method == "shopify_js"
    # The size you want being sold out is the signal, not the headline price.
    sold_out = observation.variant("32")
    in_stock = observation.variant("28")
    assert sold_out is not None and sold_out.available is False
    assert in_stock is not None and in_stock.available is True
    assert observation.variant("99") is None


def test_conversion_rounds_half_up_to_the_cent() -> None:
    assert pricing.convert_cents(29800, Decimal("1.2799")) == 38141
    assert pricing.convert_cents(1, Decimal("1.5")) == 2
    assert pricing.convert_cents(100, Decimal(1)) == 100


def test_same_currency_needs_no_exchange_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(url: str) -> object:
        raise AssertionError("must not call the FX service for SGD->SGD")

    monkeypatch.setattr(pricing, "_get_json", explode)
    _, rate = pricing.fetch_rate("SGD", "SGD")
    assert rate == Decimal(1)


def test_price_and_exchange_rate_are_recorded_separately(tmp_path: Path) -> None:
    wishlist, identity = repository(tmp_path)
    product = wishlist.add_product(
        identity,
        name="RIVET PANTS - BLACK",
        url="https://shop.example/products/rivet-pants-black",
        source="shopify",
        base_currency="USD",
        display_currency="SGD",
        target_cents=35000,
        variant_label="32",
    )

    # Same USD price on both days; only the exchange rate moved.
    for rate in (Decimal("1.2799"), Decimal("1.3100")):
        wishlist.record(
            product.id,
            price_cents=29800,
            currency="USD",
            display_cents=pricing.convert_cents(29800, rate),
            in_stock=True,
            variant_available=False,
            fx_rate=rate,
            fx_date=date(2026, 9, 24),
            method="shopify_js",
        )

    entry = wishlist.entries(identity)[0]
    assert entry.latest is not None
    assert entry.latest.price_cents == 29800
    assert entry.latest.display_cents == 39038
    assert entry.previous_display_cents == 38141
    # The seller changed nothing; the shopper still pays S$8.97 more.
    assert entry.change_cents == 897
    assert entry.met_target is False
    history = wishlist.history(identity, product.id, 10)
    assert {o.price_cents for o in history} == {29800}
    assert [o.fx_rate for o in history] == [Decimal("1.3100"), Decimal("1.2799")]


def test_target_is_met_against_the_converted_price(tmp_path: Path) -> None:
    wishlist, identity = repository(tmp_path)
    product = wishlist.add_product(
        identity,
        name="Thing",
        url="https://shop.example/products/thing",
        source="shopify",
        base_currency="USD",
        display_currency="SGD",
        target_cents=40000,
        variant_label=None,
    )
    wishlist.record(
        product.id,
        price_cents=29800,
        currency="USD",
        display_cents=38141,
        in_stock=True,
        variant_available=None,
        fx_rate=Decimal("1.2799"),
        fx_date=date(2026, 9, 24),
        method="shopify_js",
    )
    assert wishlist.entries(identity)[0].met_target is True


def test_products_are_owner_scoped_and_deletable(tmp_path: Path) -> None:
    wishlist, identity = repository(tmp_path)
    product = wishlist.add_product(
        identity,
        name="Thing",
        url="https://shop.example/products/thing",
        source="shopify",
        base_currency="USD",
        display_currency="SGD",
        target_cents=None,
        variant_label=None,
    )

    assert (
        wishlist.delete_product("development:someone-else@example.test", product.id)
        is False
    )
    assert wishlist.entries(identity) != []
    assert wishlist.delete_product(identity, product.id) is True
    assert wishlist.entries(identity) == []


def test_unsupported_source_is_refused(tmp_path: Path) -> None:
    wishlist, identity = repository(tmp_path)
    with pytest.raises(ValueError):
        wishlist.add_product(
            identity,
            name="Thing",
            url="https://shop.example/thing",
            source="handwritten",
            base_currency="USD",
            display_currency="SGD",
            target_cents=None,
            variant_label=None,
        )


def test_the_same_url_cannot_be_tracked_twice(tmp_path: Path) -> None:
    import sqlite3

    wishlist, identity = repository(tmp_path)

    def add() -> None:
        wishlist.add_product(
            identity,
            name="Thing",
            url="https://shop.example/products/thing",
            source="shopify",
            base_currency="USD",
            display_currency="SGD",
            target_cents=None,
            variant_label=None,
        )

    add()
    with pytest.raises(sqlite3.IntegrityError):
        add()


def test_recorded_json_shape_is_stable(tmp_path: Path) -> None:
    """Guard the fields a UI and any later export depend on."""
    wishlist, identity = repository(tmp_path)
    product = wishlist.add_product(
        identity,
        name="Thing",
        url="https://shop.example/products/thing",
        source="shopify",
        base_currency="USD",
        display_currency="SGD",
        target_cents=1,
        variant_label="32",
    )
    wishlist.record(
        product.id,
        price_cents=1,
        currency="USD",
        display_cents=2,
        in_stock=False,
        variant_available=False,
        fx_rate=Decimal("1.2799"),
        fx_date=date(2026, 9, 24),
        method="shopify_js",
    )
    observation = wishlist.history(identity, product.id, 1)[0]
    assert observation.fx_date is not None
    assert (
        json.loads(
            json.dumps(
                {
                    "price_cents": observation.price_cents,
                    "display_cents": observation.display_cents,
                    "in_stock": observation.in_stock,
                    "variant_available": observation.variant_available,
                    "fx_rate": str(observation.fx_rate),
                    "fx_date": observation.fx_date.isoformat(),
                    "method": observation.method,
                }
            )
        )["fx_rate"]
        == "1.2799"
    )
