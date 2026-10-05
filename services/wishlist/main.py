import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from time import monotonic
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from services.common.identity import (
    CachedResolver,
    Identity,
    http_identity_resolver,
    identity_dependency,
    read_secret_file,
    tailscale_key,
)
from services.common.web import cache_versioned_assets
from services.wishlist import pricing
from services.wishlist.observe import RateCache, observe
from services.wishlist.repository import WishlistEntry, WishlistRepository

SERVICE_VERSION = "0.1.1"
DEFAULT_DATABASE_PATH = Path("data/wishlist.db")
SERVICE_DIR = Path(__file__).parent
TEMPLATES = {
    page: (SERVICE_DIR / "templates" / f"{page}.html").read_text(encoding="utf-8")
    for page in ("wishlist",)
}


def render_page(page: str, configuration: dict[str, object]) -> str:
    base = str(configuration["basePath"])
    # JSON is embedded in HTML: escape tags even inside JSON strings.
    encoded = json.dumps(configuration).replace("<", "\\u003c")
    return (
        TEMPLATES[page]
        .replace("__BASE_PATH__", escape(base, quote=True))
        .replace("__VERSION__", SERVICE_VERSION)
        .replace("__WISHLIST_CONFIG__", encoded)
    )


@dataclass(frozen=True)
class Settings:
    database_path: Path
    base_path: str
    timezone: ZoneInfo
    display_currency: str
    allow_dev_identity: bool
    identity_resolver_url: str | None
    identity_resolver_token: str | None


class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=8, max_length=2048)
    target_cents: int | None = Field(default=None, ge=1, le=100_000_000)
    variant_label: str | None = Field(default=None, max_length=64)


class TargetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_cents: int | None = Field(default=None, ge=1, le=100_000_000)


def load_settings() -> Settings:
    base_path = os.environ.get("WISHLIST_BASE_PATH", "/wishlist").rstrip("/")
    if base_path and not base_path.startswith("/"):
        raise RuntimeError("WISHLIST_BASE_PATH must be an absolute URL path")
    timezone_name = os.environ.get("WISHLIST_TIMEZONE", "Asia/Singapore")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise RuntimeError(f"unknown WISHLIST_TIMEZONE: {timezone_name}") from error
    return Settings(
        database_path=Path(
            os.environ.get("WISHLIST_DB_PATH", str(DEFAULT_DATABASE_PATH))
        ),
        base_path=base_path,
        timezone=timezone,
        display_currency=os.environ.get("WISHLIST_DISPLAY_CURRENCY", "SGD").upper(),
        allow_dev_identity=os.environ.get("WISHLIST_ALLOW_DEV_IDENTITY", "false")
        .strip()
        .lower()
        in {"1", "true", "yes"},
        identity_resolver_url=(
            os.environ.get("WISHLIST_IDENTITY_RESOLVER_URL", "").strip() or None
        ),
        identity_resolver_token=read_secret_file(
            os.environ.get("WISHLIST_IDENTITY_TOKEN_FILE", "").strip()
        ),
    )


def _entry_read(entry: WishlistEntry, display_currency: str) -> dict[str, object]:
    product, latest = entry.product, entry.latest
    return {
        "id": product.id,
        "name": product.name,
        "url": product.url,
        "source": product.source,
        "base_currency": product.base_currency,
        "display_currency": product.display_currency or display_currency,
        "target_cents": product.target_cents,
        "variant_label": product.variant_label,
        "met_target": entry.met_target,
        "change_cents": entry.change_cents,
        "latest": None
        if latest is None
        else {
            "price_cents": latest.price_cents,
            "currency": latest.currency,
            "display_cents": latest.display_cents,
            "in_stock": latest.in_stock,
            "variant_available": latest.variant_available,
            "fx_rate": None if latest.fx_rate is None else str(latest.fx_rate),
            "fx_date": None if latest.fx_date is None else latest.fx_date.isoformat(),
            "method": latest.method,
            "observed_at": latest.observed_at.isoformat(),
        },
    }


def create_app(
    database_path: Path | None = None,
    *,
    allow_dev_identity: bool | None = None,
    identity_resolver: Callable[[str], Identity | None] | None = None,
    identity_clock: Callable[[], float] = monotonic,
) -> FastAPI:
    settings = load_settings()
    if database_path is not None or allow_dev_identity is not None:
        settings = Settings(
            database_path=database_path or settings.database_path,
            base_path=settings.base_path,
            timezone=settings.timezone,
            display_currency=settings.display_currency,
            allow_dev_identity=(
                settings.allow_dev_identity
                if allow_dev_identity is None
                else allow_dev_identity
            ),
            identity_resolver_url=settings.identity_resolver_url,
            identity_resolver_token=settings.identity_resolver_token,
        )
    if identity_resolver is None and settings.identity_resolver_url is not None:
        if settings.identity_resolver_token is None:
            raise RuntimeError(
                "WISHLIST_IDENTITY_TOKEN_FILE is required with the resolver"
            )
        identity_resolver = http_identity_resolver(
            settings.identity_resolver_url, settings.identity_resolver_token
        )
    repository = WishlistRepository(settings.database_path, settings.timezone)

    def adopt_legacy_rows(subject: str, identity: Identity) -> None:
        repository.adopt_identity(
            tailscale_key(subject), identity.key, identity.display_name
        )

    cached_resolver = (
        CachedResolver(identity_resolver, adopt_legacy_rows, clock=identity_clock)
        if identity_resolver
        else None
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        repository.initialize()
        application.state.repository = repository
        application.state.settings = settings
        yield

    application = FastAPI(
        title="Home Platform Wishlist",
        version=SERVICE_VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.add_middleware(GZipMiddleware, minimum_size=1000)
    application.mount("/static", StaticFiles(directory=SERVICE_DIR / "static"))
    cache_versioned_assets(application, SERVICE_VERSION)
    current_identity = identity_dependency(
        dev_header="X-Wishlist-Dev-User",
        allow_dev_identity=settings.allow_dev_identity,
        resolver=cached_resolver,
        ensure_user=lambda identity: repository.ensure_user(
            identity.key, identity.display_name
        ),
    )
    CurrentUser = Annotated[Identity, Depends(current_identity)]

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "healthy", "service": "wishlist"}

    @application.get("/ready")
    def ready(request: Request) -> JSONResponse:
        is_ready = bool(request.app.state.repository.ready())
        return JSONResponse(
            {"status": "ready" if is_ready else "not_ready"},
            status_code=200 if is_ready else 503,
        )

    @application.get("/version")
    def version() -> dict[str, str]:
        return {"service": "wishlist", "version": SERVICE_VERSION}

    @application.get("/", response_class=HTMLResponse)
    def page(user: CurrentUser, request: Request) -> str:
        settings_now = request.app.state.settings
        return render_page(
            "wishlist",
            {
                "basePath": settings_now.base_path,
                "displayName": user.display_name,
                "timezone": str(settings_now.timezone),
                "currency": settings_now.display_currency,
            },
        )

    @application.get("/api/products")
    def list_products(user: CurrentUser, request: Request) -> dict[str, object]:
        store: WishlistRepository = request.app.state.repository
        currency = request.app.state.settings.display_currency
        return {
            "products": [
                _entry_read(entry, currency) for entry in store.entries(user.key)
            ]
        }

    @application.post("/api/products", status_code=201)
    def add_product(
        payload: ProductCreate, user: CurrentUser, request: Request
    ) -> dict[str, object]:
        store: WishlistRepository = request.app.state.repository
        currency = request.app.state.settings.display_currency
        if any(p.url == payload.url for p in store.products(user.key)):
            raise HTTPException(status_code=409, detail="That URL is already tracked")
        try:
            observation = pricing.fetch_shopify(payload.url)
            if payload.variant_label is not None:
                observation.variant(payload.variant_label)
        except pricing.PriceSourceError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        product = store.add_product(
            user.key,
            name=observation.title or payload.url,
            url=payload.url,
            source="shopify",
            base_currency=observation.currency,
            display_currency=currency,
            target_cents=payload.target_cents,
            variant_label=payload.variant_label,
        )
        # The product is tracked either way; a first reading can wait for the
        # timer rather than failing the whole add.
        with suppress(pricing.PriceSourceError):
            observe(
                store,
                product.id,
                product.url,
                product.base_currency,
                currency,
                product.variant_label,
                observation=observation,
            )
        entries = [e for e in store.entries(user.key) if e.product.id == product.id]
        return _entry_read(entries[0], currency)

    @application.delete("/api/products/{product_id}", status_code=204)
    def delete_product(
        product_id: str, user: CurrentUser, request: Request
    ) -> Response:
        store: WishlistRepository = request.app.state.repository
        if not store.delete_product(user.key, product_id):
            raise HTTPException(status_code=404, detail="Product not found")
        return Response(status_code=204)

    @application.put("/api/products/{product_id}/target")
    def update_target(
        product_id: str, payload: TargetUpdate, user: CurrentUser, request: Request
    ) -> dict[str, object]:
        store: WishlistRepository = request.app.state.repository
        if not store.set_target(user.key, product_id, payload.target_cents):
            raise HTTPException(status_code=404, detail="Product not found")
        return {"target_cents": payload.target_cents}

    @application.get("/api/products/{product_id}/history")
    def history(
        product_id: str,
        user: CurrentUser,
        request: Request,
        limit: Annotated[int, Query(ge=1, le=500)] = 90,
    ) -> dict[str, object]:
        store: WishlistRepository = request.app.state.repository
        return {
            "observations": [
                {
                    "price_cents": o.price_cents,
                    "currency": o.currency,
                    "display_cents": o.display_cents,
                    "in_stock": o.in_stock,
                    "variant_available": o.variant_available,
                    "fx_rate": None if o.fx_rate is None else str(o.fx_rate),
                    "observed_at": o.observed_at.isoformat(),
                }
                for o in store.history(user.key, product_id, limit)
            ]
        }

    @application.post("/api/refresh")
    def refresh_now(user: CurrentUser, request: Request) -> dict[str, object]:
        store: WishlistRepository = request.app.state.repository
        currency = request.app.state.settings.display_currency
        checked, failed = 0, []
        rates: RateCache = {}
        for entry in store.entries(user.key):
            product = entry.product
            try:
                observe(
                    store,
                    product.id,
                    product.url,
                    product.base_currency,
                    currency,
                    product.variant_label,
                    rates=rates,
                )
                checked += 1
            except pricing.PriceSourceError as error:
                failed.append({"id": product.id, "error": str(error)})
        return {
            "checked": checked,
            "failed": failed,
            "at": datetime.now(request.app.state.settings.timezone).isoformat(),
        }

    return application


app = create_app()
