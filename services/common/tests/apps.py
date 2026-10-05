"""Every service that uses the shared mechanism, built the way its tests do."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from fastapi import FastAPI

from services.common.identity import Identity
from services.file_sorter import main as sorter
from services.file_sorter.repository import Seed
from services.habit_tracker import main as habits
from services.transport import datamall
from services.transport import main as transport
from services.transport.tests.test_api import NOW, timetable
from services.wishlist import main as wishlist

Resolver = Callable[[str], Identity | None]
Clock = Callable[[], float]


@dataclass(frozen=True)
class ServiceApp:
    name: str
    version: str
    database: str
    asset: str
    reads: tuple[str, ...]
    build: Callable[[Path, Resolver | None, Clock], FastAPI]


def _habits(tmp_path: Path, resolver: Resolver | None, clock: Clock) -> FastAPI:
    return habits.create_app(
        tmp_path / "habits.db", identity_resolver=resolver, identity_clock=clock
    )


def _wishlist(tmp_path: Path, resolver: Resolver | None, clock: Clock) -> FastAPI:
    return wishlist.create_app(
        tmp_path / "wishlist.db", identity_resolver=resolver, identity_clock=clock
    )


def _transport(tmp_path: Path, resolver: Resolver | None, clock: Clock) -> FastAPI:
    return transport.create_app(
        tmp_path / "transport.db",
        schedule_fetcher=lambda key: datamall.ScheduleDownload("x", timetable()),
        bus_fetcher=lambda key, stop: {"Services": []},
        now=lambda: NOW,
        identity_resolver=resolver,
        identity_clock=clock,
    )


def _sorter(tmp_path: Path, resolver: Resolver | None, clock: Clock) -> FastAPI:
    library = tmp_path / "lib"
    (library / "dump").mkdir(parents=True)
    (library / "sorted").mkdir(parents=True)
    return sorter.create_app(
        tmp_path / "sorter.db",
        library_root=library,
        seed=Seed("Downloads", "dump", "sorted"),
        allow_dev_identity=True,
        background_index=False,
    )


IDENTITY_SERVICES = [
    ServiceApp(
        "habit_tracker",
        habits.SERVICE_VERSION,
        "habits.db",
        "water.js",
        ("/api/today", "/api/budget/summary"),
        _habits,
    ),
    ServiceApp(
        "wishlist",
        wishlist.SERVICE_VERSION,
        "wishlist.db",
        "wishlist.js",
        ("/api/products",),
        _wishlist,
    ),
    ServiceApp(
        "transport",
        transport.SERVICE_VERSION,
        "transport.db",
        "transport.js",
        ("/api/buses",),
        _transport,
    ),
]

# The sorter gates on the administrator session, not on linked identities,
# so it shares only the asset cache.
ASSET_SERVICES = [
    *IDENTITY_SERVICES,
    ServiceApp(
        "file_sorter", sorter.SERVICE_VERSION, "sorter.db", "sorter.js", (), _sorter
    ),
]


def plain_app(service: ServiceApp, tmp_path: Path) -> FastAPI:
    return service.build(tmp_path, None, monotonic)
