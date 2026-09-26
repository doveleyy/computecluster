import json
import os
import sqlite3
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.transport import datamall
from services.transport.repository import TimetableError, TransportRepository

SERVICE_VERSION = "0.1.0"
SERVICE_DIR = Path(__file__).parent
DEFAULT_DATABASE_PATH = Path("data/transport.db")
TEMPLATE = (SERVICE_DIR / "templates" / "transport.html").read_text(encoding="utf-8")


@dataclass(frozen=True)
class Settings:
    database_path: Path
    base_path: str
    timezone: ZoneInfo
    account_key: str | None
    allow_dev_identity: bool
    identity_resolver_url: str | None
    identity_resolver_token: str | None


@dataclass(frozen=True)
class Identity:
    key: str
    display_name: str


class IdentityServiceUnavailableError(Exception):
    pass


class SavedBusCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stop_code: str = Field(min_length=5, max_length=5, pattern=r"^[0-9]{5}$")
    stop_name: str = Field(min_length=1, max_length=80)
    service_no: str = Field(min_length=1, max_length=8, pattern=r"^[A-Za-z0-9]+$")

    @field_validator("stop_name", "service_no")
    @classmethod
    def strip_value(cls, value: str) -> str:
        return value.strip()


def _secret_file(path_value: str) -> str | None:
    if not path_value:
        return None
    value = Path(path_value).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"secret file is empty: {path_value}")
    return value


def _local_dotenv_key() -> str | None:
    path = Path(".env")
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == "LTA_DATAMALL_KEY":
            return value.strip().strip("\"'") or None
    return None


def load_settings() -> Settings:
    base_path = os.environ.get("TRANSPORT_BASE_PATH", "/transport").rstrip("/")
    if base_path and not base_path.startswith("/"):
        raise RuntimeError("TRANSPORT_BASE_PATH must be an absolute URL path")
    account_key = _secret_file(os.environ.get("TRANSPORT_DATAMALL_KEY_FILE", ""))
    account_key = (
        account_key or os.environ.get("LTA_DATAMALL_KEY") or _local_dotenv_key()
    )
    return Settings(
        database_path=Path(
            os.environ.get("TRANSPORT_DB_PATH", str(DEFAULT_DATABASE_PATH))
        ),
        base_path=base_path,
        timezone=ZoneInfo(os.environ.get("TRANSPORT_TIMEZONE", "Asia/Singapore")),
        account_key=account_key,
        allow_dev_identity=os.environ.get(
            "TRANSPORT_ALLOW_DEV_IDENTITY", "false"
        ).lower()
        in {"1", "true", "yes"},
        identity_resolver_url=os.environ.get(
            "TRANSPORT_IDENTITY_RESOLVER_URL", ""
        ).strip()
        or None,
        identity_resolver_token=_secret_file(
            os.environ.get("TRANSPORT_IDENTITY_TOKEN_FILE", "")
        ),
    )


def _identity_resolver(settings: Settings) -> Callable[[str], Identity | None]:
    assert settings.identity_resolver_url and settings.identity_resolver_token
    resolver_url = settings.identity_resolver_url
    resolver_token = settings.identity_resolver_token

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
        return Identity(f"home-platform:{payload['id']}", str(payload["username"]))

    return resolve


def _clock_time(seconds: int) -> dict[str, object]:
    day_offset, within_day = divmod(seconds, 86_400)
    hours, remainder = divmod(within_day, 3_600)
    minutes = remainder // 60
    return {"time": f"{hours:02d}:{minutes:02d}", "day_offset": day_offset}


def create_app(
    database_path: Path | None = None,
    *,
    allow_dev_identity: bool | None = None,
    schedule_fetcher: Callable[
        [str], datamall.ScheduleDownload
    ] = datamall.fetch_train_schedule,
    bus_fetcher: Callable[[str, str], dict[str, Any]] = datamall.fetch_bus_arrivals,
    now: Callable[[], datetime] | None = None,
) -> FastAPI:
    settings = load_settings()
    settings = Settings(
        database_path=database_path or settings.database_path,
        base_path=settings.base_path,
        timezone=settings.timezone,
        account_key=settings.account_key,
        allow_dev_identity=settings.allow_dev_identity
        if allow_dev_identity is None
        else allow_dev_identity,
        identity_resolver_url=settings.identity_resolver_url,
        identity_resolver_token=settings.identity_resolver_token,
    )
    resolver = None
    if settings.identity_resolver_url:
        if not settings.identity_resolver_token:
            raise RuntimeError(
                "TRANSPORT_IDENTITY_TOKEN_FILE is required with the resolver"
            )
        resolver = _identity_resolver(settings)
    repository = TransportRepository(settings.database_path)
    now = now or (lambda: datetime.now(settings.timezone))

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        repository.initialize()
        application.state.repository = repository
        application.state.settings = settings
        yield

    app = FastAPI(
        title="Home Platform Transport",
        version=SERVICE_VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.mount("/static", StaticFiles(directory=SERVICE_DIR / "static"))

    def current_identity(
        request: Request,
        tailscale_login: Annotated[
            str | None, Header(alias="Tailscale-User-Login")
        ] = None,
        tailscale_name: Annotated[
            str | None, Header(alias="Tailscale-User-Name")
        ] = None,
        dev_user: Annotated[str | None, Header(alias="X-Transport-Dev-User")] = None,
    ) -> Identity:
        store: TransportRepository = request.app.state.repository
        if tailscale_login:
            normalized = tailscale_login.strip().lower()
            if resolver:
                try:
                    identity = resolver(normalized)
                except IdentityServiceUnavailableError as error:
                    raise HTTPException(
                        503, "Home Platform identity service is unavailable"
                    ) from error
                if identity is None:
                    raise HTTPException(
                        403,
                        "Link this Tailscale identity from your Job Desk account first",
                    )
                store.adopt_identity(
                    f"tailscale:{normalized}", identity.key, identity.display_name
                )
            else:
                identity = Identity(
                    f"tailscale:{normalized}",
                    (tailscale_name or tailscale_login).strip(),
                )
        elif request.app.state.settings.allow_dev_identity and dev_user:
            clean = dev_user.strip().lower()
            identity = Identity(f"development:{clean}", clean)
        else:
            raise HTTPException(
                401, "Open this service through the private Tailscale URL"
            )
        store.ensure_user(identity.key, identity.display_name)
        return identity

    CurrentUser = Annotated[Identity, Depends(current_identity)]

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "healthy", "service": "transport"}

    @app.get("/ready")
    def ready(request: Request) -> JSONResponse:
        ok = bool(request.app.state.repository.ready())
        return JSONResponse(
            {"status": "ready" if ok else "not_ready"}, status_code=200 if ok else 503
        )

    @app.get("/version")
    def version() -> dict[str, str]:
        return {"service": "transport", "version": SERVICE_VERSION}

    @app.get("/", response_class=HTMLResponse)
    def page(user: CurrentUser, request: Request) -> str:
        config = json.dumps(
            {
                "basePath": request.app.state.settings.base_path,
                "today": now().date().isoformat(),
                "displayName": user.display_name,
            }
        ).replace("<", "\\u003c")
        return (
            TEMPLATE.replace("__BASE_PATH__", escape(settings.base_path, quote=True))
            .replace("__TRANSPORT_CONFIG__", config)
            .replace("__VERSION__", SERVICE_VERSION)
        )

    @app.get("/api/train")
    def train_options(user: CurrentUser, request: Request) -> dict[str, object]:
        store: TransportRepository = request.app.state.repository
        return {
            "today": now().date().isoformat(),
            "snapshot": store.metadata(),
            "lines": store.lines(),
        }

    @app.post("/api/train/refresh")
    def refresh_train(user: CurrentUser, request: Request) -> dict[str, object]:
        key = request.app.state.settings.account_key
        if not key:
            raise HTTPException(503, "The DataMall key is not configured")
        try:
            download = schedule_fetcher(key)
            request.app.state.repository.import_timetable(
                download.archive, download.published_at
            )
        except (datamall.DataMallError, TimetableError) as error:
            raise HTTPException(502, str(error)) from error
        store: TransportRepository = request.app.state.repository
        return {"snapshot": store.metadata(), "lines": store.lines()}

    @app.get("/api/train/directions")
    def directions(
        user: CurrentUser,
        request: Request,
        line: str = Query(min_length=1, max_length=8),
    ) -> dict[str, object]:
        return {"directions": request.app.state.repository.directions(line)}

    @app.get("/api/train/stations")
    def stations(
        user: CurrentUser, request: Request, line: str, direction_id: int, headsign: str
    ) -> dict[str, object]:
        return {
            "stations": request.app.state.repository.stations(
                line, direction_id, headsign
            )
        }

    @app.get("/api/train/last")
    def last_train(
        user: CurrentUser,
        request: Request,
        line: str,
        direction_id: int,
        headsign: str,
        stop_code: str,
    ) -> dict[str, object]:
        today = now().date()
        result = request.app.state.repository.last_train(
            today, line, direction_id, headsign, stop_code
        )
        if result is None:
            raise HTTPException(404, "No scheduled train was found for today")
        return {
            "date": today.isoformat(),
            "line": line,
            "headsign": headsign,
            "stop_code": stop_code,
            **result,
            **_clock_time(int(result["arrival_seconds"])),
        }

    @app.get("/api/buses")
    def list_buses(user: CurrentUser, request: Request) -> dict[str, object]:
        return {
            "buses": [
                bus.__dict__ for bus in request.app.state.repository.buses(user.key)
            ]
        }

    @app.post("/api/buses", status_code=201)
    def add_bus(
        payload: SavedBusCreate, user: CurrentUser, request: Request
    ) -> dict[str, object]:
        store: TransportRepository = request.app.state.repository
        try:
            saved = store.add_bus(
                user.key,
                payload.stop_code,
                payload.stop_name,
                payload.service_no.upper(),
            )
        except sqlite3.IntegrityError as error:
            raise HTTPException(
                409, "That bus is already saved for this stop"
            ) from error
        return saved.__dict__

    @app.delete("/api/buses/{saved_id}", status_code=204)
    def delete_bus(saved_id: str, user: CurrentUser, request: Request) -> Response:
        if not request.app.state.repository.delete_bus(user.key, saved_id):
            raise HTTPException(404, "Saved bus not found")
        return Response(status_code=204)

    @app.post("/api/buses/refresh")
    def refresh_buses(user: CurrentUser, request: Request) -> dict[str, object]:
        key = request.app.state.settings.account_key
        if not key:
            raise HTTPException(503, "The DataMall key is not configured")
        buses = request.app.state.repository.buses(user.key)
        payloads: dict[str, dict[str, Any]] = {}
        for stop_code in dict.fromkeys(bus.stop_code for bus in buses):
            try:
                payloads[stop_code] = bus_fetcher(key, stop_code)
            except datamall.DataMallError as error:
                raise HTTPException(502, str(error)) from error
        observed_at = now()
        results: list[dict[str, object]] = []
        for saved in buses:
            services = payloads[saved.stop_code].get("Services", [])
            service = next(
                (
                    item
                    for item in services
                    if item.get("ServiceNo", "").upper() == saved.service_no.upper()
                ),
                None,
            )
            arrivals = (
                []
                if service is None
                else [
                    datamall.minutes_until(
                        str(service.get(name, {}).get("EstimatedArrival", "")),
                        observed_at,
                    )
                    for name in ("NextBus", "NextBus2", "NextBus3")
                ]
            )
            results.append({**saved.__dict__, "minutes": arrivals})
        return {"observed_at": observed_at.isoformat(), "buses": results}

    return app


app = create_app()
