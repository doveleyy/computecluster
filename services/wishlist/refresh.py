"""Scheduled price check.

Runs as its own process on a timer rather than inside the web application.
Fetching from other people's servers is the part of this service that can hang,
get rate-limited, or be blocked outright, and none of that should be able to
occupy a request worker.

Polite by construction: one request per product per run, a real User-Agent, and
a pause between products.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from services.wishlist import pricing
from services.wishlist.main import load_settings, observe
from services.wishlist.repository import WishlistRepository

DEFAULT_PAUSE_SECONDS = 2.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh tracked wishlist prices")
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=DEFAULT_PAUSE_SECONDS,
        help="delay between products, so a shop sees a trickle not a burst",
    )
    arguments = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    settings = load_settings()
    repository = WishlistRepository(settings.database_path, settings.timezone)
    repository.initialize()

    products = repository.products()
    if not products:
        logging.info("no tracked products")
        return 0

    failures = 0
    for index, product in enumerate(products):
        if index:
            time.sleep(max(0.0, arguments.pause_seconds))
        try:
            observe(
                repository,
                product.id,
                product.url,
                product.base_currency,
                product.display_currency or settings.display_currency,
                product.variant_label,
            )
            logging.info("checked %s", product.name)
        except pricing.PriceSourceError as error:
            failures += 1
            logging.warning("failed %s: %s", product.name, error)

    logging.info("checked %d product(s), %d failed", len(products), failures)
    # A shop being unreachable is normal and must not make the timer look broken.
    # Only a total failure is worth a non-zero exit.
    return 1 if failures == len(products) else 0


if __name__ == "__main__":
    sys.exit(main())
