"""Bounded, read-only readiness probes for the platform's own applications."""

from __future__ import annotations

import http.client
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Application:
    key: str
    name: str
    path: str
    port: int
    service: str


APPLICATIONS = (
    Application("habits", "Habit Tracker", "/habits", 8100, "habit-tracker"),
    Application("wishlist", "Wishlist", "/wishlist", 8101, "wishlist"),
    Application("transport", "Transport", "/transport", 8102, "transport"),
)
PROBE_TIMEOUT_SECONDS = 1
MAX_RESPONSE_BYTES = 1024


class ProbeHTTPError(Exception):
    """The local service responded with a non-success status."""


def _get_json(port: int, endpoint: str) -> dict[str, Any]:
    connection = http.client.HTTPConnection(
        "127.0.0.1", port, timeout=PROBE_TIMEOUT_SECONDS
    )
    try:
        connection.request(
            "GET", f"/{endpoint}", headers={"Accept": "application/json"}
        )
        response = connection.getresponse()
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if response.status != 200:
            raise ProbeHTTPError
    finally:
        connection.close()
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError("Oversized application probe response")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Invalid application probe response")
    return value


def probe_application(application: Application) -> dict[str, object]:
    started = time.monotonic()
    try:
        ready = _get_json(application.port, "ready")
    except ProbeHTTPError:
        state = "DEGRADED"
        detail = "Application responds but is not ready"
    except (OSError, ValueError, http.client.HTTPException):
        state = "OFFLINE"
        detail = "No valid response from local service"
    else:
        state = "ONLINE" if ready.get("status") == "ready" else "DEGRADED"
        detail = (
            "Application and database ready"
            if state == "ONLINE"
            else "Application responds but is not ready"
        )
    latency_ms = round((time.monotonic() - started) * 1000)

    version = None
    if state != "OFFLINE":
        try:
            reported = _get_json(application.port, "version")
            reported_version = reported.get("version")
            if (
                reported.get("service") == application.service
                and isinstance(reported_version, str)
                and reported_version
            ):
                version = reported_version
            else:
                state = "DEGRADED"
                detail = "Unexpected or unversioned service on local port"
        except (ProbeHTTPError, OSError, ValueError, http.client.HTTPException):
            state = "DEGRADED"
            detail = "Application version could not be checked"

    return {
        "key": application.key,
        "name": application.name,
        "path": application.path,
        "state": state,
        "detail": detail,
        "version": version,
        "latency_ms": latency_ms if state != "OFFLINE" else None,
    }


def collect_application_health() -> list[dict[str, object]]:
    with ThreadPoolExecutor(max_workers=len(APPLICATIONS)) as pool:
        return list(pool.map(probe_application, APPLICATIONS))
