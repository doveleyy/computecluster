"""A failed legacy-row adoption is retried on the next request, not hidden."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.common.identity import Identity
from services.wishlist import main
from services.wishlist.tests.test_api import URL, stub

LOGIN = {"Tailscale-User-Login": "member@example.test"}


def test_adoption_failure_is_retried_before_the_cache_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub(monkeypatch)
    database = tmp_path / "wishlist.db"
    with TestClient(main.create_app(database), headers=LOGIN) as legacy:
        assert legacy.post("/api/products", json={"url": URL}).status_code == 201

    linked = Identity(key="home-platform:42", display_name="member")
    app = main.create_app(database, identity_resolver=lambda subject: linked)
    with TestClient(app, headers=LOGIN, raise_server_exceptions=False) as member:
        store = app.state.repository
        real = store.adopt_identity
        attempts: list[int] = []

        def adopt(*args: Any) -> None:
            attempts.append(1)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("database is locked")
            real(*args)

        store.adopt_identity = adopt
        assert member.get("/api/products").status_code == 500
        listed = member.get("/api/products")
    assert listed.status_code == 200
    assert [product["url"] for product in listed.json()["products"]] == [URL]
