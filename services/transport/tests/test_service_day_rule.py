"""The pure service-day rule behind the last-train card, without HTTP."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from services.transport import main
from services.transport.repository import LastTrain

SINGAPORE = ZoneInfo("Asia/Singapore")


def at(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=SINGAPORE)


def test_rule_keeps_previous_service_day_while_its_late_train_is_pending() -> None:
    schedule = {
        date(2026, 9, 26): LastTrain(
            arrival_seconds=24 * 3_600 + 15 * 60, stop_name="A"
        ),
        date(2026, 9, 27): LastTrain(arrival_seconds=23 * 3_600, stop_name="B"),
    }
    sunday = date(2026, 9, 27)

    assert main.catchable_last_train(at(2026, 9, 27, 0, 5), schedule.get) == (
        date(2026, 9, 26),
        schedule[date(2026, 9, 26)],
    )
    assert main.catchable_last_train(at(2026, 9, 27, 0, 15), schedule.get) == (
        date(2026, 9, 26),
        schedule[date(2026, 9, 26)],
    )
    assert main.catchable_last_train(at(2026, 9, 27, 0, 16), schedule.get) == (
        sunday,
        schedule[sunday],
    )
    assert main.catchable_last_train(at(2026, 9, 27, 14, 0), schedule.get) == (
        sunday,
        schedule[sunday],
    )


def test_rule_ignores_a_previous_day_whose_last_train_left_before_midnight() -> None:
    schedule = {
        date(2026, 9, 28): LastTrain(
            arrival_seconds=23 * 3_600 + 59 * 60, stop_name="A"
        ),
        date(2026, 9, 29): LastTrain(arrival_seconds=23 * 3_600, stop_name="B"),
    }

    assert main.catchable_last_train(at(2026, 9, 29, 0, 5), schedule.get) == (
        date(2026, 9, 29),
        schedule[date(2026, 9, 29)],
    )
    assert main.catchable_last_train(at(2026, 9, 29, 0, 5), lambda _: None) is None
