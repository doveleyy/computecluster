"""Starting the control plane installs the routine access-log filter."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def access_line(path: str, status: int, method: str = "GET") -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("client", method, path, "1.1", status),
        None,
    )


def test_started_app_drops_routine_lines_and_keeps_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logger = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(logger, "filters", [])
    resolve = "/internal/service-identities/resolve"
    assert logger.filter(access_line(resolve, 200, "POST")), "filter pre-installed"

    with TestClient(create_app(tmp_path / "platform.db")):
        pass

    polled = access_line("/dashboard/api/services", 200)
    assert not logger.filter(polled), "routine poll still logged"
    assert not logger.filter(access_line(resolve, 200, "POST")), "resolve logged"
    assert logger.filter(access_line(resolve, 404, "POST")), "unlinked 404 hidden"
    assert logger.filter(access_line("/jobs", 200, "POST")), "job submit hidden"
