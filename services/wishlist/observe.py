"""One price reading, recorded the same way by the web app and the timer.

The price that matters is the pinned variant's when one is tracked, otherwise
the shop's headline price. Deciding that here, once, keeps the add route, the
web refresh and the scheduled refresh from drifting apart.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from services.wishlist import pricing
from services.wishlist.repository import WishlistRepository

RateCache = dict[tuple[str, str], tuple[date, Decimal]]


def observe(
    repository: WishlistRepository,
    product_id: str,
    url: str,
    base_currency: str,
    display_currency: str,
    variant_label: str | None,
    *,
    observation: pricing.PriceObservation | None = None,
    rates: RateCache | None = None,
) -> None:
    """Fetch one product and store what was seen, rate and all.

    `observation` reuses a fetch already made (adding a product), and `rates`
    shares exchange rates across one run: ECB publishes once a day, so every
    product in a run converts at the same rate anyway. A tracked variant the
    shop no longer offers raises `PriceSourceError`, so nothing misleading is
    recorded and the run reports it like any other failed product.
    """
    if observation is None:
        observation = pricing.fetch_shopify(url, currency=base_currency)
    variant = None if variant_label is None else observation.variant(variant_label)
    pair = (observation.currency.upper(), display_currency.upper())
    if rates is not None and pair in rates:
        fx_date, rate = rates[pair]
    else:
        fx_date, rate = pricing.fetch_rate(*pair)
        if rates is not None:
            rates[pair] = (fx_date, rate)
    price_cents = observation.price_cents if variant is None else variant.price_cents
    repository.record(
        product_id,
        price_cents=price_cents,
        currency=observation.currency,
        display_cents=pricing.convert_cents(price_cents, rate),
        in_stock=observation.in_stock,
        variant_available=None if variant is None else variant.available,
        fx_rate=rate,
        fx_date=fx_date,
        method=observation.method,
    )
