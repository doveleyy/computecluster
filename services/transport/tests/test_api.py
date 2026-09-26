from __future__ import annotations

import io
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from services.transport import datamall, main

NOW = datetime(2026, 9, 26, 14, 0, tzinfo=ZoneInfo("Asia/Singapore"))


def timetable() -> bytes:
    files = {
        "routes.txt": (
            "route_id,agency_id,route_short_name,route_long_name,route_type,"
            "route_color,route_text_color\n"
            "NSL,SMRT,NS,North-South Line,1,D62821,FFFFFF\n"
        ),
        "stops.txt": (
            "stop_id,stop_code,stop_name,stop_lat,stop_lon,location_type,"
            "platform_code,parent_station\n"
            "NS2,NS2,Bukit Batok,1.0,103.0,1,,\n"
            "NS2_A,NS2,Bukit Batok,1.0,103.0,0,A,NS2\n"
        ),
        "trips.txt": (
            "route_id,service_id,trip_id,trip_headsign,direction_id,block_id\n"
            "NSL,SERVICE_WE,NS_WE_1,Marina South Pier,1,\n"
            "NSL,SERVICE_WE,NS_WE_2,Marina South Pier,1,\n"
            "NSL,SERVICE_WD,NS_WD_1,Marina South Pier,1,\n"
        ),
        "stop_times.txt": (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence,"
            "stop_headsign\n"
            "NS_WE_1,23:55:00,23:55:30,NS2_A,1,\n"
            "NS_WE_2,24:15:00,24:15:30,NS2_A,1,\n"
            "NS_WD_1,23:59:00,23:59:30,NS2_A,1,\n"
        ),
        "calendar.txt": (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,"
            "sunday,start_date,end_date\n"
            "SERVICE_WD,1,1,1,1,1,0,0,20260101,20261231\n"
            "SERVICE_WE,0,0,0,0,0,1,0,20260101,20261231\n"
        ),
        "calendar_dates.txt": "service_id,date,exception_type\n",
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, contents in files.items():
            archive.writestr(name, contents)
    return output.getvalue()


def client(
    tmp_path: Path,
    *,
    schedule_fetcher: object = None,
    bus_fetcher: object = None,
) -> TestClient:
    schedule = schedule_fetcher or (
        lambda key: datamall.ScheduleDownload("2026-09-23T18:37:00+08:00", timetable())
    )
    bus = bus_fetcher or (lambda key, stop: {"Services": []})
    return TestClient(
        main.create_app(
            tmp_path / "transport.db",
            allow_dev_identity=True,
            schedule_fetcher=schedule,  # type: ignore[arg-type]
            bus_fetcher=bus,  # type: ignore[arg-type]
            now=lambda: NOW,
        ),
        headers={"X-Transport-Dev-User": "owner@example.test"},
    )


def test_private_routes_require_identity(tmp_path: Path) -> None:
    with TestClient(main.create_app(tmp_path / "transport.db")) as anonymous:
        assert anonymous.get("/").status_code == 401
        assert anonymous.get("/api/train").status_code == 401
        assert anonymous.get("/health").status_code == 200
        assert anonymous.get("/ready").status_code == 200


def test_opening_dashboard_does_not_call_datamall(tmp_path: Path) -> None:
    def unexpected(*args: str) -> object:
        raise AssertionError("opening the dashboard must not call DataMall")

    with client(
        tmp_path, schedule_fetcher=unexpected, bus_fetcher=unexpected
    ) as browser:
        assert browser.get("/").status_code == 200
        assert browser.get("/api/train").json()["snapshot"] is None
        assert browser.get("/api/buses").json()["buses"] == []


def test_manual_train_refresh_populates_selectors_and_todays_last_train(
    tmp_path: Path,
) -> None:
    calls = 0

    def fetch(key: str) -> datamall.ScheduleDownload:
        nonlocal calls
        calls += 1
        assert key
        return datamall.ScheduleDownload("2026-09-23T18:37:00+08:00", timetable())

    with client(tmp_path, schedule_fetcher=fetch) as browser:
        refreshed = browser.post("/api/train/refresh")
        directions = browser.get("/api/train/directions", params={"line": "NS"})
        stations = browser.get(
            "/api/train/stations",
            params={
                "line": "NS",
                "direction_id": 1,
                "headsign": "Marina South Pier",
            },
        )
        result = browser.get(
            "/api/train/last",
            params={
                "line": "NS",
                "direction_id": 1,
                "headsign": "Marina South Pier",
                "stop_code": "NS2",
            },
        )

    assert calls == 1
    assert refreshed.status_code == 200
    assert refreshed.json()["lines"][0]["short_name"] == "NS"
    assert directions.json()["directions"] == [
        {"direction_id": 1, "headsign": "Marina South Pier"}
    ]
    assert stations.json()["stations"] == [
        {"stop_code": "NS2", "stop_name": "Bukit Batok"}
    ]
    assert result.json()["date"] == "2026-09-26"
    assert result.json()["time"] == "00:15"
    assert result.json()["day_offset"] == 1


def test_saved_bus_refresh_calls_each_stop_once(tmp_path: Path) -> None:
    calls: list[str] = []

    def fetch(key: str, stop_code: str) -> dict[str, object]:
        calls.append(stop_code)
        return {
            "Services": [
                {
                    "ServiceNo": "176",
                    "NextBus": {"EstimatedArrival": "2026-09-26T14:02:10+08:00"},
                    "NextBus2": {"EstimatedArrival": "2026-09-26T14:11:00+08:00"},
                    "NextBus3": {"EstimatedArrival": ""},
                },
                {
                    "ServiceNo": "30",
                    "NextBus": {"EstimatedArrival": "2026-09-26T14:00:00+08:00"},
                    "NextBus2": {"EstimatedArrival": ""},
                    "NextBus3": {"EstimatedArrival": ""},
                },
            ]
        }

    with client(tmp_path, bus_fetcher=fetch) as browser:
        for service in ("176", "30"):
            response = browser.post(
                "/api/buses",
                json={
                    "stop_code": "20251",
                    "stop_name": "Home",
                    "service_no": service,
                },
            )
            assert response.status_code == 201
        refreshed = browser.post("/api/buses/refresh").json()

    assert calls == ["20251"]
    assert [bus["minutes"] for bus in refreshed["buses"]] == [
        [3, 11, None],
        [0, None, None],
    ]


def test_saved_buses_are_owner_scoped(tmp_path: Path) -> None:
    with client(tmp_path) as owner:
        saved = owner.post(
            "/api/buses",
            json={
                "stop_code": "20251",
                "stop_name": "Home",
                "service_no": "176",
            },
        ).json()
    with TestClient(
        main.create_app(tmp_path / "transport.db", allow_dev_identity=True),
        headers={"X-Transport-Dev-User": "someone-else@example.test"},
    ) as other:
        assert other.get("/api/buses").json()["buses"] == []
        assert other.delete(f"/api/buses/{saved['id']}").status_code == 404
