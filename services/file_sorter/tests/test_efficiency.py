"""Per-request cost: cached sessions, compressed text, untouched file bytes."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.file_sorter import main
from services.file_sorter.repository import Seed

P = "/api/projects/1"
ADMIN_ID = "00000000-0000-0000-0000-000000000001"
TAILNET = {"Tailscale-User-Login": "owner@example.com"}


@pytest.fixture
def library(tmp_path: Path) -> Path:
    dump = tmp_path / "lib" / "dump"
    (tmp_path / "lib" / "sorted" / "work").mkdir(parents=True)
    dump.mkdir(parents=True)
    (dump / "notes.txt").write_text("plain text " * 400)
    (dump / "report.pdf").write_bytes(b"%PDF-1.4 " + b"x" * 8000)
    return tmp_path / "lib"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_admin_session_is_validated_once_per_ttl(tmp_path: Path, library: Path) -> None:
    clock = Clock()
    seen: list[str] = []

    def validate(session: str) -> main.Identity | None:
        seen.append(session)
        if session == "admin.sig":
            return main.Identity(ADMIN_ID, "admin", "ADMIN")
        return None

    app = main.create_app(
        tmp_path / "s.db",
        library_root=library,
        seed=Seed("Downloads", "dump", "sorted"),
        allow_dev_identity=False,
        admin_account_id=ADMIN_ID,
        session_validator=validate,
        background_index=False,
        session_clock=clock,
    )
    admin = {**TAILNET, "Cookie": "home_platform_dashboard=admin.sig"}
    expired = {**TAILNET, "Cookie": "home_platform_dashboard=expired.sig"}
    with TestClient(app) as client:
        for _ in range(4):
            assert client.get(P + "/current", headers=admin).status_code == 200
        assert seen == ["admin.sig"]
        # A refused session is asked about every time, never remembered.
        assert client.get(P + "/current", headers=expired).status_code == 401
        assert client.get(P + "/current", headers=expired).status_code == 401
        assert seen.count("expired.sig") == 2
        clock.now += main.SESSION_CACHE_SECONDS + 1
        assert client.get(P + "/current", headers=admin).status_code == 200
        assert seen.count("admin.sig") == 2


def test_unavailable_sign_in_is_never_cached(tmp_path: Path, library: Path) -> None:
    answers: list[main.Identity | None] = []

    def flaky(session: str) -> main.Identity | None:
        if not answers:
            answers.append(None)
            raise main.IdentityServiceUnavailableError
        return main.Identity(ADMIN_ID, "admin", "ADMIN")

    app = main.create_app(
        tmp_path / "s.db",
        library_root=library,
        seed=Seed("Downloads", "dump", "sorted"),
        allow_dev_identity=False,
        admin_account_id=ADMIN_ID,
        session_validator=flaky,
        background_index=False,
    )
    admin = {**TAILNET, "Cookie": "home_platform_dashboard=admin.sig"}
    with TestClient(app) as client:
        assert client.get(P + "/current", headers=admin).status_code == 503
        assert client.get(P + "/current", headers=admin).status_code == 200


@pytest.fixture
def dev(tmp_path: Path, library: Path) -> Iterator[TestClient]:
    app = main.create_app(
        tmp_path / "d.db",
        library_root=library,
        seed=Seed("Downloads", "dump", "sorted"),
        allow_dev_identity=True,
        background_index=False,
    )
    with TestClient(app, headers={"Accept-Encoding": "gzip"}) as client:
        yield client


def test_text_is_compressed_but_file_bytes_never_are(dev: TestClient) -> None:
    preview = dev.get(P + "/preview", params={"path": "notes.txt"})
    assert preview.status_code == 200
    assert preview.headers["content-encoding"] == "gzip"
    raw = dev.get(P + "/raw", params={"path": "report.pdf"})
    assert raw.status_code == 200
    assert "content-encoding" not in raw.headers
    assert raw.content.startswith(b"%PDF-1.4")
    # Byte ranges, which video seeking needs, still work.
    ranged = dev.get(
        P + "/raw", params={"path": "report.pdf"}, headers={"Range": "bytes=0-7"}
    )
    assert ranged.status_code == 206
    assert ranged.content == b"%PDF-1.4"
