"""Small, explicit LTA DataMall client.

Nothing in this module schedules work. Callers decide when a refresh happens.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

SCHEDULE_URL = "https://datamall2.mytransport.sg/ltaodataservice/GTFSScheduleTrain"
BUS_ARRIVAL_URL = "https://datamall2.mytransport.sg/ltaodataservice/v3/BusArrival"
MAX_SCHEDULE_BYTES = 64 * 1024 * 1024


class DataMallError(Exception):
    """A safe, user-facing DataMall failure."""


@dataclass(frozen=True)
class ScheduleDownload:
    published_at: str
    archive: bytes


def _request_json(request: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            value = json.load(response)
    except urllib.error.HTTPError as error:
        raise DataMallError(f"DataMall returned HTTP {error.code}") from error
    except (OSError, ValueError) as error:
        raise DataMallError(
            "DataMall could not be reached or returned invalid data"
        ) from error
    if not isinstance(value, dict):
        raise DataMallError("DataMall returned an unexpected response")
    return value


def _authenticated_request(url: str, account_key: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={"AccountKey": account_key, "Accept": "application/json"},
    )


def fetch_train_schedule(account_key: str) -> ScheduleDownload:
    index = _request_json(_authenticated_request(SCHEDULE_URL, account_key))
    entries = index.get("value")
    if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
        raise DataMallError("DataMall did not provide a train timetable")
    link = entries[0].get("link")
    published_at = entries[0].get("timestamp")
    if not isinstance(link, str) or urllib.parse.urlparse(link).scheme != "https":
        raise DataMallError("DataMall returned an invalid timetable link")
    if not isinstance(published_at, str):
        raise DataMallError("DataMall returned a timetable without a timestamp")
    try:
        with urllib.request.urlopen(link, timeout=60) as response:
            archive = response.read(MAX_SCHEDULE_BYTES + 1)
    except (urllib.error.HTTPError, OSError) as error:
        raise DataMallError("The LTA timetable download failed") from error
    if len(archive) > MAX_SCHEDULE_BYTES:
        raise DataMallError("The LTA timetable was unexpectedly large")
    return ScheduleDownload(published_at=published_at, archive=archive)


def fetch_bus_arrivals(account_key: str, stop_code: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"BusStopCode": stop_code})
    return _request_json(
        _authenticated_request(f"{BUS_ARRIVAL_URL}?{query}", account_key)
    )


def minutes_until(value: str, now: datetime) -> int | None:
    if not value:
        return None
    try:
        arrival = datetime.fromisoformat(value)
    except ValueError:
        return None
    if arrival.tzinfo is None or now.tzinfo is None:
        return None
    seconds = (arrival - now).total_seconds()
    return max(0, int((seconds + 59) // 60))
