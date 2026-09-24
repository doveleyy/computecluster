import json
import os
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum
from html import escape
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.habit_tracker.budget import BudgetRepository, BudgetTransaction
from services.habit_tracker.water import Drink, WaterRepository

SERVICE_VERSION = "0.5.0"
DEFAULT_DATABASE_PATH = Path("data/habit-tracker.db")
SERVICE_DIR = Path(__file__).parent
TEMPLATES = {
    page: (SERVICE_DIR / "templates" / f"{page}.html").read_text(encoding="utf-8")
    for page in ("dashboard", "water", "budget")
}


def render_page(page: str, configuration: dict[str, object]) -> str:
    base = str(configuration["basePath"])
    navigation = '<nav class="tabs" aria-label="Habit Tracker">'
    for name, label, path in (
        ("dashboard", "Overview", "/"),
        ("water", "Water", "/water"),
        ("budget", "Budget", "/budget"),
    ):
        active = " active" if page == name else ""
        current = ' aria-current="page"' if page == name else ""
        navigation += (
            f'<a class="tab{active}" id="{name}-link"'
            f' href="{escape(base + path, quote=True)}"{current}>{label}</a>'
        )
    navigation += "</nav>"
    # JSON is embedded in HTML: escape tags even inside JSON strings.
    encoded = json.dumps(configuration).replace("<", "\\u003c")
    return (
        TEMPLATES[page]
        .replace("__BASE_PATH__", escape(base, quote=True))
        .replace("__VERSION__", SERVICE_VERSION)
        .replace("__NAVIGATION__", navigation)
        .replace("__HABIT_TRACKER_CONFIG__", encoded)
    )


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


class BudgetTransactionKind(StrEnum):
    DAILY_SPEND = "daily_spend"
    FUND_REDEMPTION = "fund_redemption"
    FUND_CONTRIBUTION = "fund_contribution"


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


class DailyBudgetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_cents: int = Field(ge=0, le=1_000_000)


class SavingsGoalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_cents: int | None = Field(default=None, ge=1, le=100_000_000)


class BudgetTransactionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: BudgetTransactionKind
    amount_cents: int = Field(ge=1, le=100_000_000)
    description: str = Field(default="", max_length=120)


class BudgetTransactionRead(BaseModel):
    id: str
    kind: BudgetTransactionKind
    amount_cents: int
    description: str
    occurred_at: str


def load_settings() -> Settings:
    base_path = os.environ.get("HABIT_TRACKER_BASE_PATH", "/habits").rstrip("/")
    if base_path and not base_path.startswith("/"):
        raise RuntimeError("HABIT_TRACKER_BASE_PATH must be an absolute URL path")
    timezone_name = os.environ.get(
        "HABIT_TRACKER_TIMEZONE",
        "Asia/Singapore",
    )
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise RuntimeError(
            f"unknown HABIT_TRACKER_TIMEZONE: {timezone_name}"
        ) from error
    return Settings(
        database_path=Path(
            os.environ.get(
                "HABIT_TRACKER_DB_PATH",
                str(DEFAULT_DATABASE_PATH),
            )
        ),
        base_path=base_path,
        timezone=timezone,
        allow_dev_identity=os.environ.get(
            "HABIT_TRACKER_ALLOW_DEV_IDENTITY",
            "false",
        )
        .strip()
        .lower()
        in {"1", "true", "yes"},
        identity_resolver_url=(
            os.environ.get(
                "HABIT_TRACKER_IDENTITY_RESOLVER_URL",
                "",
            ).strip()
            or None
        ),
        identity_resolver_token=_load_optional_secret(
            os.environ.get(
                "HABIT_TRACKER_IDENTITY_TOKEN_FILE",
                "",
            ).strip()
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
                "HABIT_TRACKER_IDENTITY_TOKEN_FILE is required with the resolver"
            )
        identity_resolver = _http_identity_resolver(settings)
    repository = WaterRepository(settings.database_path, settings.timezone)
    budget_repository = BudgetRepository(settings.database_path, settings.timezone)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        repository.initialize()
        budget_repository.initialize()
        application.state.repository = repository
        application.state.budget_repository = budget_repository
        application.state.settings = settings
        yield

    application = FastAPI(
        title="Home Platform Habit Tracker",
        version=SERVICE_VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.mount("/static", StaticFiles(directory=SERVICE_DIR / "static"))

    def current_identity(
        request: Request,
        tailscale_login: Annotated[
            str | None, Header(alias="Tailscale-User-Login")
        ] = None,
        tailscale_name: Annotated[
            str | None, Header(alias="Tailscale-User-Name")
        ] = None,
        dev_user: Annotated[
            str | None, Header(alias="X-Habit-Tracker-Dev-User")
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
                local_day = datetime.now(request.app.state.settings.timezone).date()
                request.app.state.budget_repository.adopt_identity(
                    legacy_key,
                    identity.key,
                    identity.display_name,
                    local_day,
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
        local_day = datetime.now(request.app.state.settings.timezone).date()
        request.app.state.budget_repository.ensure_user(identity.key, local_day)
        return identity

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "healthy", "service": "habit-tracker"}

    @application.get("/ready")
    def ready(request: Request) -> JSONResponse:
        is_ready = bool(
            request.app.state.repository.ready()
            and request.app.state.budget_repository.ready()
        )
        return JSONResponse(
            {"status": "ready" if is_ready else "not_ready"},
            status_code=200 if is_ready else 503,
        )

    @application.get("/version")
    def version() -> dict[str, str]:
        return {"service": "habit-tracker", "version": SERVICE_VERSION}

    @application.get("/", response_class=HTMLResponse)
    def dashboard_page(
        user: Annotated[Identity, Depends(current_identity)], request: Request
    ) -> str:
        return render_page(
            "dashboard",
            {
                "basePath": request.app.state.settings.base_path,
                "displayName": user.display_name,
                "timezone": str(request.app.state.settings.timezone),
                "currency": "SGD",
            },
        )

    @application.get("/water", response_class=HTMLResponse)
    def water_page(
        user: Annotated[Identity, Depends(current_identity)], request: Request
    ) -> str:
        configuration = {
            "basePath": request.app.state.settings.base_path,
            "displayName": user.display_name,
            "timezone": str(request.app.state.settings.timezone),
            "drinkTypes": [
                {"value": drink_type.value, "label": label}
                for drink_type, label in DRINK_TYPE_LABELS.items()
            ],
        }
        return render_page("water", configuration)

    @application.get("/budget", response_class=HTMLResponse)
    def budget_page(
        user: Annotated[Identity, Depends(current_identity)], request: Request
    ) -> str:
        configuration = {
            "basePath": request.app.state.settings.base_path,
            "displayName": user.display_name,
            "timezone": str(request.app.state.settings.timezone),
            "currency": "SGD",
        }
        return render_page("budget", configuration)

    @application.get("/api/today")
    @application.get("/api/water/today")
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
    @application.post("/api/water/drinks", response_model=DrinkRead, status_code=201)
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
    @application.delete("/api/water/drinks/{drink_id}", status_code=204)
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
    @application.get("/api/water/settings")
    def get_settings(
        user: Annotated[Identity, Depends(current_identity)], request: Request
    ) -> dict[str, int]:
        repository: WaterRepository = request.app.state.repository
        return {"daily_goal_ml": repository.goal(user.key)}

    @application.put("/api/settings")
    @application.put("/api/water/settings")
    def update_settings(
        payload: GoalUpdate,
        user: Annotated[Identity, Depends(current_identity)],
        request: Request,
    ) -> dict[str, int]:
        repository: WaterRepository = request.app.state.repository
        repository.set_goal(user.key, payload.daily_goal_ml)
        return {"daily_goal_ml": payload.daily_goal_ml}

    @application.get("/api/history")
    @application.get("/api/water/history")
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

    @application.get("/api/budget/summary")
    def budget_summary(
        user: Annotated[Identity, Depends(current_identity)], request: Request
    ) -> dict[str, object]:
        local_now = datetime.now(request.app.state.settings.timezone)
        repository: BudgetRepository = request.app.state.budget_repository
        summary = repository.summary(user.key, local_now.date())
        next_reset = datetime.combine(
            local_now.date() + timedelta(days=1),
            time.min,
            request.app.state.settings.timezone,
        )
        return {
            "date": summary.day.isoformat(),
            "currency": summary.currency,
            "savings_goal_cents": repository.savings_goal(user.key),
            "daily_budget_cents": summary.daily_budget_cents,
            "daily_spent_cents": summary.daily_spent_cents,
            "daily_remaining_cents": summary.daily_remaining_cents,
            "settled_fund_cents": summary.settled_fund_cents,
            "fund_balance_cents": summary.fund_balance_cents,
            "pending_surplus_cents": summary.pending_surplus_cents,
            "next_reset_at": next_reset.isoformat(),
            "transactions": [
                _budget_transaction_read(transaction).model_dump()
                for transaction in summary.transactions
            ],
        }

    @application.put("/api/budget/daily-budget")
    def update_daily_budget(
        payload: DailyBudgetUpdate,
        user: Annotated[Identity, Depends(current_identity)],
        request: Request,
    ) -> dict[str, object]:
        local_day = datetime.now(request.app.state.settings.timezone).date()
        repository: BudgetRepository = request.app.state.budget_repository
        repository.set_daily_budget(user.key, local_day, payload.amount_cents)
        return {
            "effective_date": local_day.isoformat(),
            "amount_cents": payload.amount_cents,
        }

    @application.put("/api/budget/savings-goal")
    def update_savings_goal(
        payload: SavingsGoalUpdate,
        user: Annotated[Identity, Depends(current_identity)],
        request: Request,
    ) -> dict[str, int | None]:
        repository: BudgetRepository = request.app.state.budget_repository
        repository.set_savings_goal(user.key, payload.target_cents)
        return {"savings_goal_cents": payload.target_cents}

    @application.post(
        "/api/budget/transactions",
        response_model=BudgetTransactionRead,
        status_code=201,
    )
    def add_budget_transaction(
        payload: BudgetTransactionCreate,
        user: Annotated[Identity, Depends(current_identity)],
        request: Request,
    ) -> BudgetTransactionRead:
        repository: BudgetRepository = request.app.state.budget_repository
        descriptions = {
            BudgetTransactionKind.DAILY_SPEND: "Daily spending",
            BudgetTransactionKind.FUND_REDEMPTION: "Fund redemption",
            BudgetTransactionKind.FUND_CONTRIBUTION: "Fund contribution",
        }
        description = payload.description.strip() or descriptions[payload.kind]
        transaction = repository.add_transaction(
            user.key, payload.kind.value, payload.amount_cents, description
        )
        return _budget_transaction_read(transaction)

    @application.delete("/api/budget/transactions/{transaction_id}", status_code=204)
    def delete_budget_transaction(
        transaction_id: str,
        user: Annotated[Identity, Depends(current_identity)],
        request: Request,
    ) -> Response:
        repository: BudgetRepository = request.app.state.budget_repository
        if not repository.delete_transaction(user.key, transaction_id):
            raise HTTPException(status_code=404, detail="Transaction not found")
        return Response(status_code=204)

    @application.get("/api/budget/history")
    def budget_history(
        user: Annotated[Identity, Depends(current_identity)],
        request: Request,
        days: Annotated[int, Query(ge=1, le=90)] = 14,
    ) -> dict[str, object]:
        local_day = datetime.now(request.app.state.settings.timezone).date()
        repository: BudgetRepository = request.app.state.budget_repository
        return {
            "days": [
                {
                    "date": day.day.isoformat(),
                    "budget_cents": day.budget_cents,
                    "spent_cents": day.spent_cents,
                    "net_cents": day.net_cents,
                    "settled": day.day < local_day,
                }
                for day in repository.history(user.key, local_day, days)
            ]
        }

    @application.get("/api/budget/ledger")
    def budget_ledger(
        user: Annotated[Identity, Depends(current_identity)],
        request: Request,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, object]:
        local_day = datetime.now(request.app.state.settings.timezone).date()
        repository: BudgetRepository = request.app.state.budget_repository
        return {
            "entries": [
                {
                    "key": entry.key,
                    "date": entry.entry_date.isoformat(),
                    "kind": entry.kind,
                    "amount_cents": entry.amount_cents,
                    "description": entry.description,
                    "occurred_at": (
                        entry.occurred_at.isoformat()
                        if entry.occurred_at is not None
                        else None
                    ),
                    "previous_amount_cents": entry.previous_amount_cents,
                    "new_amount_cents": entry.new_amount_cents,
                }
                for entry in repository.fund_ledger(user.key, local_day, limit)
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


def _budget_transaction_read(
    transaction: BudgetTransaction,
) -> BudgetTransactionRead:
    return BudgetTransactionRead(
        id=transaction.id,
        kind=BudgetTransactionKind(transaction.kind),
        amount_cents=transaction.amount_cents,
        description=transaction.description,
        occurred_at=transaction.occurred_at.isoformat(),
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
