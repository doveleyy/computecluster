"""SQLite storage and GTFS queries for the personal transport dashboard."""

from __future__ import annotations

import csv
import io
import sqlite3
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

REQUIRED_GTFS_FILES = {
    "routes.txt",
    "stops.txt",
    "trips.txt",
    "stop_times.txt",
    "calendar.txt",
    "calendar_dates.txt",
}


class TimetableError(Exception):
    pass


@dataclass(frozen=True)
class SavedBus:
    id: str
    owner_identity: str
    stop_code: str
    stop_name: str
    service_no: str


def _rows(archive: zipfile.ZipFile, filename: str) -> Iterator[dict[str, str]]:
    with (
        archive.open(filename) as raw,
        io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text,
    ):
        yield from csv.DictReader(text)


def _chunks(
    rows: Iterable[tuple[object, ...]], size: int = 2_000
) -> Iterator[list[tuple[object, ...]]]:
    chunk: list[tuple[object, ...]] = []
    for row in rows:
        chunk.append(row)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _seconds(value: str) -> int:
    try:
        hours, minutes, seconds = (int(part) for part in value.split(":"))
    except (TypeError, ValueError) as error:
        raise TimetableError(f"invalid GTFS time: {value!r}") from error
    if hours < 0 or minutes not in range(60) or seconds not in range(60):
        raise TimetableError(f"invalid GTFS time: {value!r}")
    return hours * 3600 + minutes * 60 + seconds


class TransportRepository:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS users (
                    identity TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS timetable_metadata (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    published_at TEXT NOT NULL,
                    refreshed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS routes (
                    route_id TEXT PRIMARY KEY,
                    short_name TEXT NOT NULL,
                    long_name TEXT NOT NULL,
                    color TEXT NOT NULL,
                    text_color TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stops (
                    stop_id TEXT PRIMARY KEY,
                    stop_code TEXT NOT NULL,
                    stop_name TEXT NOT NULL,
                    parent_station TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trips (
                    trip_id TEXT PRIMARY KEY,
                    route_id TEXT NOT NULL REFERENCES routes(route_id),
                    service_id TEXT NOT NULL,
                    headsign TEXT NOT NULL,
                    direction_id INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stop_times (
                    trip_id TEXT NOT NULL REFERENCES trips(trip_id),
                    stop_id TEXT NOT NULL REFERENCES stops(stop_id),
                    arrival_seconds INTEGER NOT NULL,
                    departure_seconds INTEGER NOT NULL,
                    stop_sequence INTEGER NOT NULL,
                    PRIMARY KEY (trip_id, stop_sequence)
                );
                CREATE INDEX IF NOT EXISTS stop_times_stop ON stop_times(stop_id);
                CREATE TABLE IF NOT EXISTS service_calendar (
                    service_id TEXT PRIMARY KEY,
                    monday INTEGER NOT NULL, tuesday INTEGER NOT NULL,
                    wednesday INTEGER NOT NULL, thursday INTEGER NOT NULL,
                    friday INTEGER NOT NULL, saturday INTEGER NOT NULL,
                    sunday INTEGER NOT NULL,
                    start_date TEXT NOT NULL, end_date TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS calendar_dates (
                    service_id TEXT NOT NULL,
                    service_date TEXT NOT NULL,
                    exception_type INTEGER NOT NULL,
                    PRIMARY KEY (service_id, service_date)
                );
                CREATE TABLE IF NOT EXISTS saved_buses (
                    id TEXT PRIMARY KEY,
                    owner_identity TEXT NOT NULL REFERENCES users(identity),
                    stop_code TEXT NOT NULL,
                    stop_name TEXT NOT NULL,
                    service_no TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(owner_identity, stop_code, service_no)
                );
                """
            )

    def ready(self) -> bool:
        try:
            with self._connect() as connection:
                return (
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name = 'saved_buses'"
                    ).fetchone()
                    is not None
                )
        except sqlite3.Error:
            return False

    def ensure_user(self, identity: str, display_name: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users(identity, display_name, created_at) VALUES (?, ?, ?)
                ON CONFLICT(identity) DO UPDATE SET display_name = excluded.display_name
                """,
                (identity, display_name, datetime.now(UTC).isoformat()),
            )

    def adopt_identity(self, old: str, new: str, display_name: str) -> None:
        if old == new:
            self.ensure_user(new, display_name)
            return
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users(identity, display_name, created_at) VALUES (?, ?, ?)
                ON CONFLICT(identity) DO UPDATE SET display_name = excluded.display_name
                """,
                (new, display_name, datetime.now(UTC).isoformat()),
            )
            connection.execute(
                "UPDATE saved_buses SET owner_identity = ? WHERE owner_identity = ?",
                (new, old),
            )
            connection.execute("DELETE FROM users WHERE identity = ?", (old,))

    def import_timetable(self, archive_bytes: bytes, published_at: str) -> None:
        try:
            archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
        except zipfile.BadZipFile as error:
            raise TimetableError("LTA returned an invalid timetable archive") from error
        with archive:
            if not REQUIRED_GTFS_FILES.issubset(archive.namelist()):
                raise TimetableError("The timetable is missing required GTFS files")
            with self._connect() as connection:
                for table in (
                    "stop_times",
                    "trips",
                    "stops",
                    "routes",
                    "service_calendar",
                    "calendar_dates",
                ):
                    connection.execute(f"DELETE FROM {table}")
                self._insert_gtfs(connection, archive)
                connection.execute(
                    """
                    INSERT INTO timetable_metadata(
                        singleton, published_at, refreshed_at
                    )
                    VALUES (1, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET
                      published_at = excluded.published_at,
                      refreshed_at = excluded.refreshed_at
                    """,
                    (published_at, datetime.now(UTC).isoformat()),
                )

    def _insert_gtfs(
        self, connection: sqlite3.Connection, archive: zipfile.ZipFile
    ) -> None:
        mappings: list[tuple[str, str, str, Iterable[tuple[object, ...]]]] = [
            (
                "routes",
                "route_id, short_name, long_name, color, text_color",
                "?, ?, ?, ?, ?",
                (
                    (
                        row["route_id"],
                        row["route_short_name"],
                        row["route_long_name"],
                        row.get("route_color", ""),
                        row.get("route_text_color", ""),
                    )
                    for row in _rows(archive, "routes.txt")
                ),
            ),
            (
                "stops",
                "stop_id, stop_code, stop_name, parent_station",
                "?, ?, ?, ?",
                (
                    (
                        row["stop_id"],
                        row["stop_code"],
                        row["stop_name"],
                        row.get("parent_station", ""),
                    )
                    for row in _rows(archive, "stops.txt")
                ),
            ),
            (
                "trips",
                "trip_id, route_id, service_id, headsign, direction_id",
                "?, ?, ?, ?, ?",
                (
                    (
                        row["trip_id"],
                        row["route_id"],
                        row["service_id"],
                        row["trip_headsign"],
                        int(row["direction_id"]),
                    )
                    for row in _rows(archive, "trips.txt")
                ),
            ),
            (
                "stop_times",
                "trip_id, stop_id, arrival_seconds, departure_seconds, stop_sequence",
                "?, ?, ?, ?, ?",
                (
                    (
                        row["trip_id"],
                        row["stop_id"],
                        _seconds(row["arrival_time"]),
                        _seconds(row["departure_time"]),
                        int(row["stop_sequence"]),
                    )
                    for row in _rows(archive, "stop_times.txt")
                ),
            ),
            (
                "service_calendar",
                "service_id, monday, tuesday, wednesday, thursday, friday, "
                "saturday, sunday, start_date, end_date",
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?",
                (
                    (
                        row["service_id"],
                        *(
                            int(row[day])
                            for day in (
                                "monday",
                                "tuesday",
                                "wednesday",
                                "thursday",
                                "friday",
                                "saturday",
                                "sunday",
                            )
                        ),
                        row["start_date"],
                        row["end_date"],
                    )
                    for row in _rows(archive, "calendar.txt")
                ),
            ),
            (
                "calendar_dates",
                "service_id, service_date, exception_type",
                "?, ?, ?",
                (
                    (row["service_id"], row["date"], int(row["exception_type"]))
                    for row in _rows(archive, "calendar_dates.txt")
                ),
            ),
        ]
        try:
            for table, columns, placeholders, rows in mappings:
                for chunk in _chunks(rows):
                    connection.executemany(
                        f"INSERT INTO {table}({columns}) VALUES ({placeholders})", chunk
                    )
        except (KeyError, ValueError, sqlite3.Error) as error:
            raise TimetableError("The LTA timetable could not be imported") from error

    def metadata(self) -> dict[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM timetable_metadata WHERE singleton = 1"
            ).fetchone()
        return (
            None
            if row is None
            else {
                "published_at": str(row["published_at"]),
                "refreshed_at": str(row["refreshed_at"]),
            }
        )

    def lines(self) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT short_name, MIN(long_name) AS long_name,
                       MIN(color) AS color, MIN(text_color) AS text_color
                FROM routes GROUP BY short_name ORDER BY short_name
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def directions(self, line: str) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT trips.direction_id, trips.headsign
                FROM trips JOIN routes USING(route_id)
                WHERE routes.short_name = ? AND trips.headsign <> ''
                ORDER BY trips.direction_id, trips.headsign
                """,
                (line,),
            ).fetchall()
        return [dict(row) for row in rows]

    def stations(
        self, line: str, direction_id: int, headsign: str
    ) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT stops.stop_code, MIN(stops.stop_name) AS stop_name,
                       MIN(stop_times.stop_sequence) AS sequence
                FROM stops
                JOIN stop_times USING(stop_id)
                JOIN trips USING(trip_id)
                JOIN routes USING(route_id)
                WHERE routes.short_name = ? AND trips.direction_id = ?
                  AND trips.headsign = ? AND stops.stop_code <> ''
                GROUP BY stops.stop_code ORDER BY sequence
                """,
                (line, direction_id, headsign),
            ).fetchall()
        return [
            {"stop_code": str(row["stop_code"]), "stop_name": str(row["stop_name"])}
            for row in rows
        ]

    def last_train(
        self,
        service_date: date,
        line: str,
        direction_id: int,
        headsign: str,
        stop_code: str,
    ) -> dict[str, object] | None:
        compact = service_date.strftime("%Y%m%d")
        weekday = service_date.strftime("%A").lower()
        if weekday not in {
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        }:
            raise AssertionError("invalid weekday")
        query = f"""
            WITH active_services AS (
                SELECT service_id FROM service_calendar
                WHERE start_date <= ? AND end_date >= ? AND {weekday} = 1
                  AND NOT EXISTS (
                    SELECT 1 FROM calendar_dates AS exception
                    WHERE exception.service_id = service_calendar.service_id
                      AND exception.service_date = ? AND exception.exception_type = 2
                  )
                UNION
                SELECT service_id FROM calendar_dates
                WHERE service_date = ? AND exception_type = 1
            )
            SELECT MAX(stop_times.arrival_seconds) AS arrival_seconds,
                   MIN(stops.stop_name) AS stop_name
            FROM stop_times
            JOIN stops USING(stop_id)
            JOIN trips USING(trip_id)
            JOIN routes USING(route_id)
            JOIN active_services USING(service_id)
            WHERE routes.short_name = ? AND trips.direction_id = ?
              AND trips.headsign = ? AND stops.stop_code = ?
        """
        with self._connect() as connection:
            row = connection.execute(
                query,
                (
                    compact,
                    compact,
                    compact,
                    compact,
                    line,
                    direction_id,
                    headsign,
                    stop_code,
                ),
            ).fetchone()
        if row is None or row["arrival_seconds"] is None:
            return None
        return {
            "arrival_seconds": int(row["arrival_seconds"]),
            "stop_name": str(row["stop_name"]),
        }

    def add_bus(
        self, identity: str, stop_code: str, stop_name: str, service_no: str
    ) -> SavedBus:
        saved = SavedBus(str(uuid4()), identity, stop_code, stop_name, service_no)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO saved_buses(
                    id, owner_identity, stop_code, stop_name, service_no, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (*saved.__dict__.values(), datetime.now(UTC).isoformat()),
            )
        return saved

    def buses(self, identity: str) -> list[SavedBus]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, owner_identity, stop_code, stop_name, service_no
                FROM saved_buses WHERE owner_identity = ? ORDER BY created_at
                """,
                (identity,),
            ).fetchall()
        return [SavedBus(**dict(row)) for row in rows]

    def delete_bus(self, identity: str, saved_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM saved_buses WHERE id = ? AND owner_identity = ?",
                (saved_id, identity),
            )
        return cursor.rowcount == 1
