from app import app_health
from app.dashboard import collect_service_health


class AvailableService:
    def ping(self) -> None:
        return None


def test_service_health_reports_unconfigured_synology(monkeypatch) -> None:
    monkeypatch.delenv("HOME_PLATFORM_SYNOLOGY_HOST", raising=False)
    monkeypatch.setattr("app.dashboard.tcp_reachable", lambda _host, _port: True)

    services = collect_service_health(AvailableService())  # type: ignore[arg-type]

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


def test_service_health_reports_synology_smb_independently(monkeypatch) -> None:
    monkeypatch.setenv("HOME_PLATFORM_SYNOLOGY_HOST", "storage.example.internal")
    monkeypatch.setattr(
        "app.dashboard.tcp_reachable",
        lambda host, port: host == "storage.example.internal" and port in {445, 5001},
    )

    services = collect_service_health(AvailableService())  # type: ignore[arg-type]

    assert services["synology_nas"] == {
        "state": "ONLINE",
        "configured": True,
        "host": "storage.example.internal",
        "smb": "reachable",
        "management": "reachable",
        "role": "primary storage",
    }


def test_application_probe_requires_readiness_and_expected_service(monkeypatch) -> None:
    application = app_health.APPLICATIONS[0]

    def ready_probe(_port: int, endpoint: str) -> dict[str, str]:
        return (
            {"status": "ready"}
            if endpoint == "ready"
            else {"service": "habit-tracker", "version": "0.8.1"}
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
        if endpoint == "ready"
        else {"service": "another-app", "version": "9.9"},
    )
    wrong_service = app_health.probe_application(application)
    assert wrong_service["state"] == "DEGRADED"
    assert wrong_service["version"] is None


def test_application_probe_distinguishes_unready_from_unreachable(monkeypatch) -> None:
    application = app_health.APPLICATIONS[2]

    def unready_probe(_port: int, endpoint: str) -> dict[str, str]:
        if endpoint == "ready":
            raise app_health.ProbeHTTPError
        return {"service": "transport", "version": "0.1.2"}

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
