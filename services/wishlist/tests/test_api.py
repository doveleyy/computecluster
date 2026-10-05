from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.wishlist import main, pricing

SHOPIFY = pricing.PriceObservation(
    title="RIVET PANTS - BLACK",
    currency="USD",
    price_cents=29800,
    in_stock=True,
    variants=(
        pricing.VariantPrice("28", 29800, True),
        pricing.VariantPrice("32", 29800, False),
    ),
    method="shopify_js",
)
URL = "https://shop.example/products/rivet-pants-black"


def client(tmp_path: Path, user: str = "member@example.test") -> TestClient:
    return TestClient(
        main.create_app(tmp_path / "wishlist.db", allow_dev_identity=True),
        headers={"X-Wishlist-Dev-User": user},
    )


def stub(
    monkeypatch: pytest.MonkeyPatch, observation: pricing.PriceObservation = SHOPIFY
) -> None:
    monkeypatch.setattr(
        pricing, "fetch_shopify", lambda url, currency=None: observation
    )
    monkeypatch.setattr(
        pricing,
        "fetch_rate",
        lambda base, quote: (date(2026, 9, 24), Decimal("1.2799")),
    )


def test_data_routes_require_proxy_identity(tmp_path: Path) -> None:
    with TestClient(main.create_app(tmp_path / "wishlist.db")) as anonymous:
        for route in ("/", "/api/products"):
            assert anonymous.get(route).status_code == 401
        assert anonymous.post("/api/products", json={"url": URL}).status_code == 401


def test_health_and_version_need_no_identity(tmp_path: Path) -> None:
    with TestClient(main.create_app(tmp_path / "wishlist.db")) as anonymous:
        assert anonymous.get("/health").json()["service"] == "wishlist"
        assert anonymous.get("/ready").status_code == 200
        assert anonymous.get("/version").json()["service"] == "wishlist"


def test_tracking_a_product_records_base_price_and_converted_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub(monkeypatch)
    with client(tmp_path) as user:
        created = user.post(
            "/api/products",
            json={"url": URL, "variant_label": "32", "target_cents": 35000},
        )
        listed = user.get("/api/products").json()["products"]

    assert created.status_code == 201
    assert listed[0]["name"] == "RIVET PANTS - BLACK"
    assert listed[0]["base_currency"] == "USD"
    assert listed[0]["display_currency"] == "SGD"
    latest = listed[0]["latest"]
    assert latest["price_cents"] == 29800
    assert latest["display_cents"] == 38141
    assert latest["fx_rate"] == "1.2799"
    # The pinned size is sold out even though the product is "in stock".
    assert latest["in_stock"] is True
    assert latest["variant_available"] is False
    assert listed[0]["met_target"] is False


def test_an_unreachable_shop_is_a_422_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(url: str, currency: str | None = None) -> pricing.PriceObservation:
        raise pricing.PriceSourceError("shop.example returned HTTP 403")

    monkeypatch.setattr(pricing, "fetch_shopify", explode)
    with client(tmp_path) as user:
        response = user.post("/api/products", json={"url": URL})

    assert response.status_code == 422
    assert "403" in response.json()["detail"]


def test_refresh_records_a_new_observation_and_reports_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub(monkeypatch)
    with client(tmp_path) as user:
        user.post("/api/products", json={"url": URL})
        # The shop raises its price; the rate is unchanged.
        stub(
            monkeypatch,
            pricing.PriceObservation(**{**SHOPIFY.__dict__, "price_cents": 31000}),
        )
        result = user.post("/api/refresh").json()
        listed = user.get("/api/products").json()["products"][0]
        history = user.get(f"/api/products/{listed['id']}/history").json()[
            "observations"
        ]

    assert result["checked"] == 1 and result["failed"] == []
    assert listed["latest"]["price_cents"] == 31000
    # 31000 * 1.2799 = 39676.9, rounded half-up to 39677 cents.
    assert listed["latest"]["display_cents"] == 39677
    assert listed["change_cents"] == 39677 - 38141
    assert [o["price_cents"] for o in history] == [31000, 29800]


def test_products_are_owner_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub(monkeypatch)
    with client(tmp_path) as first:
        created = first.post("/api/products", json={"url": URL}).json()
    with client(tmp_path, "someone-else@example.test") as second:
        assert second.get("/api/products").json()["products"] == []
        assert second.delete(f"/api/products/{created['id']}").status_code == 404
        assert (
            second.put(
                f"/api/products/{created['id']}/target", json={"target_cents": 100}
            ).status_code
            == 404
        )


def test_target_can_be_set_and_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub(monkeypatch)
    with client(tmp_path) as user:
        created = user.post("/api/products", json={"url": URL}).json()
        user.put(f"/api/products/{created['id']}/target", json={"target_cents": 40000})
        met = user.get("/api/products").json()["products"][0]
        user.put(f"/api/products/{created['id']}/target", json={"target_cents": None})
        cleared = user.get("/api/products").json()["products"][0]

    assert met["met_target"] is True
    assert cleared["target_cents"] is None
    assert cleared["met_target"] is False


def test_removing_a_product_removes_its_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub(monkeypatch)
    with client(tmp_path) as user:
        created = user.post("/api/products", json={"url": URL}).json()
        assert user.delete(f"/api/products/{created['id']}").status_code == 204
        assert user.get("/api/products").json()["products"] == []
        # Cascade means the observations went with it.
        assert (
            user.get(f"/api/products/{created['id']}/history").json()["observations"]
            == []
        )


def test_page_embeds_escaped_configuration(tmp_path: Path) -> None:
    with client(tmp_path, "</script><script>alert(1)</script>") as user:
        page = user.get("/")
    assert page.status_code == 200
    assert "</script><script>alert(1)</script>" not in page.text
    assert "\\u003c/script>" in page.text


def _shop(monkeypatch: pytest.MonkeyPatch, payloads: dict[str, object]) -> None:
    def get_json(url: str) -> object:
        if url in payloads:
            return payloads[url]
        raise pricing.PriceSourceError(f"{url} unreachable")

    monkeypatch.setattr(pricing, "_get_json", get_json)
    monkeypatch.setattr(
        pricing,
        "fetch_rate",
        lambda base, quote: (date(2026, 9, 24), Decimal("1.2799")),
    )


def test_a_tracked_variant_is_priced_at_its_own_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dearer = pricing.PriceObservation(
        **{
            **SHOPIFY.__dict__,
            "variants": (
                pricing.VariantPrice("28", 29800, True),
                pricing.VariantPrice("32", 45000, True),
            ),
        }
    )
    stub(monkeypatch, dearer)
    with client(tmp_path) as user:
        created = user.post(
            "/api/products",
            json={"url": URL, "variant_label": "32", "target_cents": 35000},
        ).json()
        history = user.get(f"/api/products/{created['id']}/history").json()

    assert created["latest"]["price_cents"] == 45000
    # 45000 * 1.2799 = 57595.5, rounded half-up.
    assert created["latest"]["display_cents"] == 57596
    assert created["met_target"] is False
    assert [o["price_cents"] for o in history["observations"]] == [45000]


def test_a_variant_the_shop_does_not_offer_is_refused_with_the_offered_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub(monkeypatch)
    with client(tmp_path) as user:
        refused = user.post("/api/products", json={"url": URL, "variant_label": "33"})
        listed = user.get("/api/products").json()["products"]

    assert refused.status_code == 422
    assert "33" in refused.json()["detail"]
    assert "28" in refused.json()["detail"] and "32" in refused.json()["detail"]
    assert listed == []


def test_a_variant_that_disappears_is_a_reported_failure_not_a_headline_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub(monkeypatch)
    with client(tmp_path) as user:
        created = user.post(
            "/api/products", json={"url": URL, "variant_label": "32"}
        ).json()
        stub(
            monkeypatch,
            pricing.PriceObservation(
                **{**SHOPIFY.__dict__, "variants": (SHOPIFY.variants[0],)}
            ),
        )
        result = user.post("/api/refresh").json()
        listed = user.get("/api/products").json()["products"][0]

    assert result["checked"] == 0
    assert [f["id"] for f in result["failed"]] == [created["id"]]
    assert "32" in result["failed"][0]["error"]
    assert listed["latest"]["variant_available"] is False
    assert listed["latest"]["observed_at"] == created["latest"]["observed_at"]


def test_adding_a_url_already_tracked_is_a_409_without_asking_the_shop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetches: list[str] = []

    def shopify(url: str, currency: str | None = None) -> pricing.PriceObservation:
        fetches.append(url)
        return SHOPIFY

    stub(monkeypatch)
    monkeypatch.setattr(pricing, "fetch_shopify", shopify)
    with client(tmp_path) as user:
        assert user.post("/api/products", json={"url": URL}).status_code == 201
        duplicate = user.post("/api/products", json={"url": URL})
        listed = user.get("/api/products").json()["products"]

    assert duplicate.status_code == 409
    assert fetches == [URL]
    assert len(listed) == 1


def test_a_malformed_shop_price_fails_that_product_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    product_js = "https://shop.example/products/rivet-pants-black.js"
    other = "https://shop.example/products/other"
    payloads: dict[str, object] = {
        product_js: {"title": "Rivet", "price": 29800, "available": True},
        f"{other}.js": {"title": "Other", "price": 1000, "available": True},
        "https://shop.example/meta.json": {"currency": "USD"},
    }
    _shop(monkeypatch, payloads)
    with client(tmp_path) as user:
        assert user.post("/api/products", json={"url": URL}).status_code == 201
        assert user.post("/api/products", json={"url": other}).status_code == 201
        payloads[product_js] = {"title": "Rivet", "price": "298.00"}
        result = user.post("/api/refresh").json()
        listed = user.get("/api/products").json()["products"]

    assert result["checked"] == 1
    assert [f["id"] for f in result["failed"]] == [listed[0]["id"]]
    assert "price" in result["failed"][0]["error"]
    assert listed[0]["latest"]["price_cents"] == 29800
