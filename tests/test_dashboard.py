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
