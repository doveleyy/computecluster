"""Price and exchange-rate sources for the wishlist.

Deliberately free of database and web-framework concerns so the awkward part —
talking to someone else's server — can be tested without either.

Nothing here scrapes HTML. A Shopify storefront publishes the same product data
it renders from, as JSON, at a documented path. Parsing that is exact; parsing
the rendered page would be guesswork against a template that changes without
warning.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

USER_AGENT = (
    "home-platform-wishlist/1.0 "
    "(personal price tracking; one request per product per run)"
)
REQUEST_TIMEOUT = 20
# ECB reference rates, free and key-less. api.frankfurter.app now redirects, so
# the versioned .dev host is the one to call.
FX_ENDPOINT = "https://api.frankfurter.dev/v1"


class PriceSourceError(Exception):
    """The source could not be reached or did not return what it promised."""


@dataclass(frozen=True)
class VariantPrice:
    label: str
    price_cents: int
    available: bool


@dataclass(frozen=True)
class PriceObservation:
    title: str
    currency: str
    price_cents: int
    in_stock: bool
    variants: tuple[VariantPrice, ...]
    method: str

    def variant(self, label: str) -> VariantPrice | None:
        for candidate in self.variants:
            if candidate.label == label:
                return candidate
        return None


def _get_json(url: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise PriceSourceError(f"{url} returned HTTP {error.code}") from error
    except (OSError, ValueError) as error:
        raise PriceSourceError(f"{url} unreachable or not JSON: {error}") from error


def shopify_endpoints(product_url: str) -> tuple[str, str]:
    """Map a Shopify product page to its JSON endpoint and the shop's meta."""
    parsed = urlparse(product_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PriceSourceError("product URL must be absolute http(s)")
    path = parsed.path.rstrip("/")
    if "/products/" not in path:
        raise PriceSourceError(
            "not a Shopify product path (expected /products/<handle>)"
        )
    for suffix in (".js", ".json"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    root = f"{parsed.scheme}://{parsed.netloc}"
    return f"{root}{path}.js", f"{root}/meta.json"


def shop_currency(meta_url: str) -> str:
    """The store's base currency. What a visitor sees may be converted."""
    payload = _get_json(meta_url)
    if not isinstance(payload, dict) or not payload.get("currency"):
        raise PriceSourceError(f"{meta_url} did not report a shop currency")
    return str(payload["currency"]).upper()


def fetch_shopify(product_url: str, *, currency: str | None = None) -> PriceObservation:
    """Read a Shopify product's live price and per-variant availability.

    `/products/<handle>.js` reports money as integer minor units already, which
    matches how the rest of this application stores money.
    """
    product_endpoint, meta_endpoint = shopify_endpoints(product_url)
    payload = _get_json(product_endpoint)
    if not isinstance(payload, dict) or "price" not in payload:
        raise PriceSourceError(
            f"{product_endpoint} did not look like a Shopify product"
        )

    variants: list[VariantPrice] = []
    for raw in payload.get("variants") or []:
        if not isinstance(raw, dict) or raw.get("price") is None:
            continue
        variants.append(
            VariantPrice(
                label=str(raw.get("title") or "").strip() or "default",
                price_cents=int(raw["price"]),
                available=bool(raw.get("available")),
            )
        )

    return PriceObservation(
        title=str(payload.get("title") or "").strip(),
        currency=currency or shop_currency(meta_endpoint),
        price_cents=int(payload["price"]),
        in_stock=bool(payload.get("available")),
        variants=tuple(variants),
        method="shopify_js",
    )


def fetch_rate(base: str, quote: str) -> tuple[date, Decimal]:
    """Latest published rate. Returns the rate's own date, not today's.

    ECB publishes once per working day, so a Sunday lookup legitimately returns
    Friday's rate. Recording the rate's date keeps a stored price honest about
    which rate it was converted at.
    """
    base, quote = base.upper(), quote.upper()
    if base == quote:
        return date.today(), Decimal(1)
    payload = _get_json(f"{FX_ENDPOINT}/latest?base={base}&symbols={quote}")
    if not isinstance(payload, dict):
        raise PriceSourceError("exchange rate service returned an unexpected shape")
    rates = payload.get("rates")
    if not isinstance(rates, dict) or quote not in rates:
        raise PriceSourceError(f"no {base}->{quote} rate published")
    try:
        return date.fromisoformat(str(payload["date"])), Decimal(str(rates[quote]))
    except (InvalidOperation, ValueError, KeyError) as error:
        raise PriceSourceError(f"unusable {base}->{quote} rate") from error


def convert_cents(price_cents: int, rate: Decimal) -> int:
    """Convert minor units at `rate`, rounding half-up to the nearest cent."""
    converted = (Decimal(price_cents) * rate).quantize(
        Decimal(1), rounding="ROUND_HALF_UP"
    )
    return int(converted)
