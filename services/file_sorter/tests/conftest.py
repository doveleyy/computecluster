"""Unit tests drive index scans explicitly instead of racing a background timer."""

from typing import Any

import pytest

from services.file_sorter.index import FileIndex


@pytest.fixture(autouse=True)
def deterministic_index(monkeypatch: Any) -> None:
    monkeypatch.setattr(FileIndex, "start", lambda self: None)
    monkeypatch.setattr(FileIndex, "settle_seconds", 0.0)
