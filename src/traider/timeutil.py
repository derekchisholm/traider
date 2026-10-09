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
