"""A failed legacy-row adoption is retried on the next request, not hidden."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from services.common.identity import Identity
from services.transport import main
from services.transport.tests.test_api import NOW

LOGIN = {"Tailscale-User-Login": "owner@example.test"}
BUS = {"stop_code": "20251", "stop_name": "Home", "service_no": "176"}


def test_adoption_failure_is_retried_before_the_cache_expires(tmp_path: Path) -> None:
    database = tmp_path / "transport.db"
    with TestClient(
        main.create_app(database, now=lambda: NOW), headers=LOGIN
    ) as legacy:
        assert legacy.post("/api/buses", json=BUS).status_code == 201

    linked = Identity("home-platform:7", "owner")
    app = main.create_app(
        database, now=lambda: NOW, identity_resolver=lambda subject: linked
    )
    with TestClient(app, headers=LOGIN, raise_server_exceptions=False) as owner:
        store = app.state.repository
        real = store.adopt_identity
        attempts: list[int] = []

        def adopt(*args: Any) -> None:
            attempts.append(1)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("database is locked")
            real(*args)

        store.adopt_identity = adopt
        assert owner.get("/api/buses").status_code == 500
        listed = owner.get("/api/buses")
    assert listed.status_code == 200
    assert [bus["service_no"] for bus in listed.json()["buses"]] == ["176"]
