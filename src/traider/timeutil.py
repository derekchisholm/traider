"""Clocks and exchange-calendar helpers.

Every timestamp inside the bot is a timezone-aware UTC ``datetime``. The clock
is injected everywhere so the backtester and the tests can control time.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """A clock that only moves when told to. Used by tests and the backtester."""

    def __init__(self, start: datetime) -> None:
        self._now = _require_aware(start)

    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        self._now = _require_aware(when)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def _require_aware(when: datetime) -> datetime:
    if when.tzinfo is None:
        raise ValueError("naive datetime: every timestamp must carry a timezone")
    return when.astimezone(UTC)


def trading_date(when: datetime) -> date:
    """The New York calendar date, which is what a 'trading day' means for US equities."""
    return when.astimezone(ET).date()


def trades_without_settling(day: date) -> bool:
    """True on the two days a year when US stock markets are open but banks are shut,
    so nothing settles: Columbus Day (second Monday of October) and Veterans Day
    (11 November, or the Monday after when that is a Sunday). Money from a sale on
    the trading day before is still unsettled on such a day."""
    if day.month == 10:
        return day.weekday() == 0 and 8 <= day.day <= 14
    if day.month == 11:
        if day.day == 11:
            return day.weekday() < 5
        return day.day == 12 and day.weekday() == 0
    return False


def previous_weekday(day: date) -> date:
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def next_weekday(day: date) -> date:
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def weekdays_after(day: date, n: int) -> date:
    """The ``n``-th weekday after ``day``. Holidays are not known here."""
    if n < 0:
        raise ValueError("n must not be negative")
    for _ in range(n):
        day = next_weekday(day)
    return day


def weekdays_between(start: date, end: date) -> int:
    """Weekdays ``d`` with ``start < d <= end``; negative when ``end`` is before ``start``."""
    if end < start:
        return -weekdays_between(end, start)
    count = 0
    day = start
    while day < end:
        day += timedelta(days=1)
        if day.weekday() < 5:
            count += 1
    return count


def weekdays_from(start: date, end: date) -> list[date]:
    """The weekdays from ``start`` to ``end``, both included, oldest first. Holidays are
    not known here."""
    days: list[date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days
