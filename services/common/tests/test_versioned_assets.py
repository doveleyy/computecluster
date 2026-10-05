"""Only an asset linked with the running version is cached, and text is gzipped."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.common.tests.apps import ASSET_SERVICES, ServiceApp, plain_app


@pytest.mark.parametrize("service", ASSET_SERVICES, ids=lambda s: s.name)
def test_only_versioned_assets_are_cached_and_text_is_compressed(
    service: ServiceApp, tmp_path: Path
) -> None:
    asset = f"/static/{service.asset}"
    with TestClient(plain_app(service, tmp_path)) as browser:
        versioned = browser.get(
            f"{asset}?v={service.version}", headers={"Accept-Encoding": "gzip"}
        )
        assert versioned.status_code == 200
        assert "immutable" in versioned.headers["cache-control"]
        assert versioned.headers["content-encoding"] == "gzip"
        assert "cache-control" not in browser.get(asset).headers
        assert "cache-control" not in browser.get(f"{asset}?v=0.0.1").headers
