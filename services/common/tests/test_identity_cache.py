"""A linked identity is resolved once per TTL; refusals and outages never stick."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.common.identity import (
    IDENTITY_CACHE_SECONDS,
    Identity,
    IdentityServiceUnavailableError,
)
from services.common.tests.apps import IDENTITY_SERVICES, ServiceApp

TAILSCALE = {"Tailscale-User-Login": "Member@Example.test"}
MEMBER = Identity(key="home-platform:42", display_name="member")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def resolving(
    service: ServiceApp, tmp_path: Path, answers: list[object], clock: Clock
) -> tuple[TestClient, list[str]]:
    calls: list[str] = []

    def resolve(subject: str) -> Identity | None:
        calls.append(subject)
        answer = answers[min(len(calls), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[return-value]

    app = service.build(tmp_path, resolve, clock)
    return TestClient(app, headers=TAILSCALE), calls


@pytest.mark.parametrize("service", IDENTITY_SERVICES, ids=lambda s: s.name)
def test_identity_is_resolved_once_per_ttl_and_reads_commit_nothing(
    service: ServiceApp, tmp_path: Path
) -> None:
    clock = Clock()
    browser, calls = resolving(service, tmp_path, [MEMBER], clock)
    with browser:
        assert browser.get(service.reads[0]).status_code == 200
        watcher = sqlite3.connect(tmp_path / service.database)
        before = watcher.execute("PRAGMA data_version").fetchone()[0]
        for _ in range(5):
            for path in service.reads:
                assert browser.get(path).status_code == 200
        after = watcher.execute("PRAGMA data_version").fetchone()[0]
        watcher.close()
        assert calls == ["member@example.test"]
        assert before == after, "reading pages must not commit identity writes"
        clock.now += IDENTITY_CACHE_SECONDS + 1
        assert browser.get(service.reads[0]).status_code == 200
        assert len(calls) == 2


@pytest.mark.parametrize("service", IDENTITY_SERVICES, ids=lambda s: s.name)
def test_unlinked_and_unavailable_answers_are_never_cached(
    service: ServiceApp, tmp_path: Path
) -> None:
    clock = Clock()
    outage = IdentityServiceUnavailableError()
    browser, calls = resolving(service, tmp_path, [None, outage, MEMBER], clock)
    path = service.reads[0]
    with browser:
        assert browser.get(path).status_code == 403
        assert browser.get(path).status_code == 503
        for _ in range(4):
            assert browser.get(path).status_code == 200
        assert len(calls) == 3
        clock.now += IDENTITY_CACHE_SECONDS + 1
        assert browser.get(path).status_code == 200
        assert len(calls) == 4
