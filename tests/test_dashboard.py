from pathlib import Path

import pytest
from test_app_registry import document, registration

from app import app_health
from app.app_registry import AppRegistry
from app.database import Database
from app.system_health import collect_service_health


class AvailableService:
    def ping(self) -> None:
        return None


@pytest.fixture
def registry(tmp_path: Path) -> AppRegistry:
    database = Database(tmp_path / "platform.db")
    database.initialize()
    return AppRegistry(database)


def test_service_health_reports_unconfigured_synology(monkeypatch, registry) -> None:
    monkeypatch.delenv("HOME_PLATFORM_SYNOLOGY_HOST", raising=False)
    monkeypatch.setattr("app.system_health.tcp_reachable", lambda _host, _port: True)

    services = collect_service_health(AvailableService(), registry)  # type: ignore[arg-type]

    assert services["control_plane"]["state"] == "ONLINE"
    assert services["database"]["state"] == "ONLINE"
    assert services["synology_nas"] == {
        "state": "UNKNOWN",
        "configured": False,
        "host": None,
        "smb": "unreachable",
        "management": "unreachable",
        "role": "primary storage",
    }


def test_service_health_reports_synology_smb_independently(
    monkeypatch, registry
) -> None:
    monkeypatch.setenv("HOME_PLATFORM_SYNOLOGY_HOST", "storage.example.internal")
    monkeypatch.setattr(
        "app.system_health.tcp_reachable",
        lambda host, port: host == "storage.example.internal" and port in {445, 5001},
    )

    services = collect_service_health(AvailableService(), registry)  # type: ignore[arg-type]

    assert services["synology_nas"] == {
        "state": "ONLINE",
        "configured": True,
        "host": "storage.example.internal",
        "smb": "reachable",
        "management": "reachable",
        "role": "primary storage",
    }


def test_application_probe_requires_readiness_and_expected_service(
    monkeypatch, registry
) -> None:
    value = registration()
    value["installation"]["probe"]["readiness"] = "legacy-json"
    registry.register(document(value))
    application = registry.monitoring()[0]

    def ready_probe(_port: int, endpoint: str) -> dict[str, str]:
        return (
            {"status": "ready"}
            if endpoint == "/ready"
            else {"service": "notes", "version": "0.8.1"}
        )

    monkeypatch.setattr(app_health, "_get_json", ready_probe)
    ready = app_health.probe_application(application)
    assert ready["state"] == "ONLINE"
    assert ready["version"] == "0.8.1"
    assert isinstance(ready["latency_ms"], int)

    monkeypatch.setattr(
        app_health,
        "_get_json",
        lambda _port, endpoint: {"status": "ready"}
        if endpoint == "/ready"
        else {"service": "another-app", "version": "9.9"},
    )
    wrong_service = app_health.probe_application(application)
    assert wrong_service["state"] == "DEGRADED"
    assert wrong_service["version"] is None


def test_application_probe_distinguishes_unready_from_unreachable(
    monkeypatch, registry
) -> None:
    value = registration()
    value["installation"]["probe"]["readiness"] = "legacy-json"
    registry.register(document(value))
    application = registry.monitoring()[0]

    def unready_probe(_port: int, endpoint: str) -> dict[str, str]:
        if endpoint == "/ready":
            raise app_health.ProbeHTTPError
        return {"service": "notes", "version": "0.1.2"}

    monkeypatch.setattr(app_health, "_get_json", unready_probe)
    unready = app_health.probe_application(application)
    assert unready["state"] == "DEGRADED"
    assert unready["version"] == "0.1.2"

    def unreachable_probe(_port: int, _endpoint: str) -> dict[str, str]:
        raise ConnectionRefusedError

    monkeypatch.setattr(app_health, "_get_json", unreachable_probe)
    unreachable = app_health.probe_application(application)
    assert unreachable["state"] == "OFFLINE"
    assert unreachable["latency_ms"] is None
