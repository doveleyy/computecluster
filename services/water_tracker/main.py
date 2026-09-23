import json
import os
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.water_tracker.database import Drink, WaterRepository

SERVICE_VERSION = "0.3.1"
DEFAULT_DATABASE_PATH = Path("data/water-tracker.db")
INDEX_HTML = Path(__file__).with_name("index.html").read_text(encoding="utf-8")


@dataclass(frozen=True)
class Settings:
    database_path: Path
    base_path: str
    timezone: ZoneInfo
    allow_dev_identity: bool
    identity_resolver_url: str | None
    identity_resolver_token: str | None


@dataclass(frozen=True)
class Identity:
    key: str
    display_name: str


class IdentityServiceUnavailableError(Exception):
    pass


class DrinkType(StrEnum):
    WATER = "water"
    SUPPLEMENT_WATER = "supplement_water"
    COFFEE = "coffee"
    TEA = "tea"
    MILK = "milk"
    JUICE = "juice"
    SOFT_DRINK = "soft_drink"
    SPORTS_DRINK = "sports_drink"
    ALCOHOL = "alcohol"
    OTHER = "other"


class DrinkTemperature(StrEnum):
    HOT = "hot"
    NORMAL = "normal"
    ICED = "iced"


class Sweetness(StrEnum):
    NONE = "none"
    LESS = "less"
    REGULAR = "regular"
    EXTRA = "extra"


DRINK_TYPE_LABELS = {
    DrinkType.WATER: "Water",
    DrinkType.SUPPLEMENT_WATER: "Supplement water",
    DrinkType.COFFEE: "Coffee",
    DrinkType.TEA: "Tea",
    DrinkType.MILK: "Milk",
    DrinkType.JUICE: "Juice",
    DrinkType.SOFT_DRINK: "Soft drink",
    DrinkType.SPORTS_DRINK: "Sports drink",
    DrinkType.ALCOHOL: "Alcohol",
    DrinkType.OTHER: "Other",
}


class DrinkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_ml: int = Field(ge=10, le=2000)
    drink_type: DrinkType = DrinkType.WATER
    temperature: DrinkTemperature = DrinkTemperature.NORMAL
    sweetness: Sweetness | None = None

    @model_validator(mode="after")
    def validate_sweetness(self) -> "DrinkCreate":
        if self.sweetness is not None and self.drink_type not in {
            DrinkType.COFFEE,
            DrinkType.TEA,
        }:
            raise ValueError("sweetness is only available for coffee and tea")
        return self


class GoalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_goal_ml: int = Field(ge=250, le=10000)


class DrinkRead(BaseModel):
    id: str
    amount_ml: int
    drink_type: DrinkType
    temperature: DrinkTemperature
    sweetness: Sweetness | None
    consumed_at: str


def load_settings() -> Settings:
    base_path = os.environ.get("WATER_TRACKER_BASE_PATH", "/water").rstrip("/")
    if not base_path.startswith("/") or base_path == "":
        raise RuntimeError("WATER_TRACKER_BASE_PATH must be an absolute URL path")
    timezone_name = os.environ.get("WATER_TRACKER_TIMEZONE", "Asia/Singapore")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise RuntimeError(
            f"unknown WATER_TRACKER_TIMEZONE: {timezone_name}"
        ) from error
    return Settings(
        database_path=Path(
            os.environ.get("WATER_TRACKER_DB_PATH", str(DEFAULT_DATABASE_PATH))
        ),
        base_path=base_path,
        timezone=timezone,
        allow_dev_identity=os.environ.get("WATER_TRACKER_ALLOW_DEV_IDENTITY", "false")
        .strip()
        .lower()
        in {"1", "true", "yes"},
        identity_resolver_url=(
            os.environ.get("WATER_TRACKER_IDENTITY_RESOLVER_URL", "").strip() or None
        ),
        identity_resolver_token=_load_optional_secret(
            os.environ.get("WATER_TRACKER_IDENTITY_TOKEN_FILE", "").strip()
        ),
    )


def create_app(
    database_path: Path | None = None,
    *,
    allow_dev_identity: bool | None = None,
    identity_resolver: Callable[[str], Identity | None] | None = None,
) -> FastAPI:
    settings = load_settings()
    if database_path is not None or allow_dev_identity is not None:
        settings = Settings(
            database_path=database_path or settings.database_path,
            base_path=settings.base_path,
            timezone=settings.timezone,
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
                "WATER_TRACKER_IDENTITY_TOKEN_FILE is required with the resolver"
            )
        identity_resolver = _http_identity_resolver(settings)
    repository = WaterRepository(settings.database_path, settings.timezone)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        repository.initialize()
        application.state.repository = repository
        application.state.settings = settings
        yield

    application = FastAPI(
        title="Home Platform Water Tracker",
        version=SERVICE_VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def current_identity(
        request: Request,
        tailscale_login: Annotated[
            str | None, Header(alias="Tailscale-User-Login")
        ] = None,
        tailscale_name: Annotated[
            str | None, Header(alias="Tailscale-User-Name")
        ] = None,
        dev_user: Annotated[
            str | None, Header(alias="X-Water-Tracker-Dev-User")
        ] = None,
    ) -> Identity:
        if tailscale_login:
            normalized_login = tailscale_login.strip().lower()
            legacy_key = f"tailscale:{normalized_login}"
            if identity_resolver is not None:
                try:
                    identity = identity_resolver(normalized_login)
                except IdentityServiceUnavailableError as error:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Home Platform identity service is unavailable",
                    ) from error
                if identity is None:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=(
                            "Link this Tailscale identity from your Job Desk "
                            "account first"
                        ),
                    )
                request.app.state.repository.adopt_identity(
                    legacy_key, identity.key, identity.display_name
                )
            else:
                identity = Identity(
                    key=legacy_key,
                    display_name=(tailscale_name or tailscale_login).strip(),
                )
        elif request.app.state.settings.allow_dev_identity and dev_user:
            clean_user = dev_user.strip().lower()
            identity = Identity(
                key=f"development:{clean_user}", display_name=clean_user
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Open this service through the private Tailscale URL",
            )
        request.app.state.repository.ensure_user(identity.key, identity.display_name)
        return identity

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "healthy", "service": "water-tracker"}

    @application.get("/ready")
    def ready(request: Request) -> JSONResponse:
        is_ready = bool(request.app.state.repository.ready())
        return JSONResponse(
            {"status": "ready" if is_ready else "not_ready"},
            status_code=200 if is_ready else 503,
        )

    @application.get("/version")
    def version() -> dict[str, str]:
        return {"service": "water-tracker", "version": SERVICE_VERSION}

    @application.get("/", response_class=HTMLResponse)
    def index(
        user: Annotated[Identity, Depends(current_identity)], request: Request
    ) -> str:
        configuration = json.dumps(
            {
                "basePath": request.app.state.settings.base_path,
                "displayName": user.display_name,
                "timezone": str(request.app.state.settings.timezone),
                "drinkTypes": [
                    {"value": drink_type.value, "label": label}
                    for drink_type, label in DRINK_TYPE_LABELS.items()
                ],
            }
        )
        return INDEX_HTML.replace("__WATER_TRACKER_CONFIG__", configuration)

    @application.get("/api/today")
    def today(
        user: Annotated[Identity, Depends(current_identity)], request: Request
    ) -> dict[str, object]:
        repository: WaterRepository = request.app.state.repository
        local_day = datetime.now(request.app.state.settings.timezone).date()
        drinks = repository.drinks_for_day(user.key, local_day)
        goal_ml = repository.goal(user.key)
        return {
            "date": local_day.isoformat(),
            "goal_ml": goal_ml,
            "total_ml": sum(drink.amount_ml for drink in drinks),
            "breakdown_ml": _breakdown(drinks),
            "drinks": [_drink_read(drink).model_dump() for drink in drinks],
        }

    @application.post("/api/drinks", response_model=DrinkRead, status_code=201)
    def add_drink(
        payload: DrinkCreate,
        user: Annotated[Identity, Depends(current_identity)],
        request: Request,
    ) -> DrinkRead:
        repository: WaterRepository = request.app.state.repository
        return _drink_read(
            repository.add_drink(
                user.key,
                payload.amount_ml,
                payload.drink_type.value,
                payload.temperature.value,
                payload.sweetness.value if payload.sweetness is not None else None,
            )
        )

    @application.delete("/api/drinks/{drink_id}", status_code=204)
    def delete_drink(
        drink_id: str,
        user: Annotated[Identity, Depends(current_identity)],
        request: Request,
    ) -> Response:
        repository: WaterRepository = request.app.state.repository
        if not repository.delete_drink(user.key, drink_id):
            raise HTTPException(status_code=404, detail="Drink not found")
        return Response(status_code=204)

    @application.get("/api/settings")
    def get_settings(
        user: Annotated[Identity, Depends(current_identity)], request: Request
    ) -> dict[str, int]:
        repository: WaterRepository = request.app.state.repository
        return {"daily_goal_ml": repository.goal(user.key)}

    @application.put("/api/settings")
    def update_settings(
        payload: GoalUpdate,
        user: Annotated[Identity, Depends(current_identity)],
        request: Request,
    ) -> dict[str, int]:
        repository: WaterRepository = request.app.state.repository
        repository.set_goal(user.key, payload.daily_goal_ml)
        return {"daily_goal_ml": payload.daily_goal_ml}

    @application.get("/api/history")
    def history(
        user: Annotated[Identity, Depends(current_identity)],
        request: Request,
        days: Annotated[int, Query(ge=1, le=90)] = 14,
    ) -> dict[str, object]:
        repository: WaterRepository = request.app.state.repository
        local_day = datetime.now(request.app.state.settings.timezone).date()
        summaries = repository.history(user.key, local_day, days)
        return {
            "days": [
                {
                    "date": summary.day.isoformat(),
                    "total_ml": summary.total_ml,
                    "goal_ml": summary.goal_ml,
                    "breakdown_ml": summary.breakdown_ml,
                }
                for summary in summaries
            ]
        }

    return application


def _drink_read(drink: Drink) -> DrinkRead:
    return DrinkRead(
        id=drink.id,
        amount_ml=drink.amount_ml,
        drink_type=DrinkType(drink.drink_type),
        temperature=DrinkTemperature(drink.temperature),
        sweetness=Sweetness(drink.sweetness) if drink.sweetness is not None else None,
        consumed_at=drink.consumed_at.isoformat(),
    )


def _breakdown(drinks: list[Drink]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for drink in drinks:
        totals[drink.drink_type] = totals.get(drink.drink_type, 0) + drink.amount_ml
    return totals


def _load_optional_secret(path_value: str) -> str | None:
    if not path_value:
        return None
    value = Path(path_value).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"identity token file is empty: {path_value}")
    return value


def _http_identity_resolver(
    settings: Settings,
) -> Callable[[str], Identity | None]:
    resolver_url = settings.identity_resolver_url
    resolver_token = settings.identity_resolver_token
    assert resolver_url is not None
    assert resolver_token is not None

    def resolve(subject: str) -> Identity | None:
        request = urllib.request.Request(
            resolver_url,
            data=json.dumps({"provider": "tailscale", "subject": subject}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Service-Identity-Token": resolver_token,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise IdentityServiceUnavailableError from error
        except (OSError, ValueError) as error:
            raise IdentityServiceUnavailableError from error
        return Identity(
            key=f"home-platform:{payload['id']}",
            display_name=str(payload["username"]),
        )

    return resolve


app = create_app()
