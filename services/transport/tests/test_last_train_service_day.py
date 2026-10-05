"""The last-train card answers for the GTFS service day, not the calendar date.

GTFS files a train leaving after midnight under the previous day with a time
past 24:00. The fixture timetable has a Saturday 24:15 train and a weekday
24:10 train at Bukit Batok (NS2).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from services.transport.tests.test_api import client

SINGAPORE = ZoneInfo("Asia/Singapore")
QUERY: dict[str, str | int] = {
    "line": "NS",
    "direction_id": 1,
    "headsign": "Marina South Pier",
    "stop_code": "NS2",
}


def at(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=SINGAPORE)


def last_train_at(tmp_path: Path, now: datetime) -> tuple[int, dict[str, object]]:
    with client(tmp_path, now=now) as browser:
        assert browser.post("/api/train/refresh").status_code == 200
        response = browser.get("/api/train/last", params=QUERY)
    return response.status_code, response.json()


def test_saturday_late_train_is_still_offered_early_sunday(tmp_path: Path) -> None:
    status, body = last_train_at(tmp_path, at(2026, 9, 27, 0, 5))

    assert status == 200
    assert (body["date"], body["time"], body["day_offset"]) == (
        "2026-09-26",
        "00:15",
        1,
    )


def test_sunday_without_service_has_no_train_once_saturdays_has_left(
    tmp_path: Path,
) -> None:
    status, body = last_train_at(tmp_path, at(2026, 9, 27, 0, 20))

    assert status == 404
    assert body == {"detail": "No scheduled train was found for today"}


def test_weekday_after_midnight_offers_last_nights_train_not_tonights(
    tmp_path: Path,
) -> None:
    pending_status, pending = last_train_at(tmp_path, at(2026, 9, 29, 0, 5))
    gone_status, gone = last_train_at(tmp_path, at(2026, 9, 29, 0, 12))

    assert pending_status == gone_status == 200
    assert (pending["date"], pending["time"], pending["day_offset"]) == (
        "2026-09-28",
        "00:10",
        1,
    )
    assert (gone["date"], gone["time"], gone["day_offset"]) == (
        "2026-09-29",
        "00:10",
        1,
    )


def test_daytime_answer_is_unchanged(tmp_path: Path) -> None:
    status, body = last_train_at(tmp_path, at(2026, 9, 29, 14, 0))

    assert status == 200
    assert (body["date"], body["time"], body["day_offset"]) == (
        "2026-09-29",
        "00:10",
        1,
    )
