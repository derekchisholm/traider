"""Trading-session hours.

The bot only trades in the regular session. Hours come from a provider (Schwab's
market-hours endpoint in production, which knows about holidays and half days).
When the hours for today are not known, the market is treated as closed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Protocol

from traider.risk import SessionView
from traider.timeutil import ET, trading_date

log = logging.getLogger(__name__)

_CLOSED = SessionView(is_open=False, minutes_since_open=None, minutes_to_close=None)


@dataclass(frozen=True, slots=True)
class Session:
    day: date
    open: datetime | None  # None on a day with no regular session
    close: datetime | None

    def view(self, now: datetime) -> SessionView:
        if self.open is None or self.close is None or not self.open <= now < self.close:
            return _CLOSED
        return SessionView(
            is_open=True,
            minutes_since_open=(now - self.open).total_seconds() / 60,
            minutes_to_close=(self.close - now).total_seconds() / 60,
        )


class SessionProvider(Protocol):
    async def session_for(self, day: date) -> Session | None:
        """The session for a day, or None when it cannot be determined right now."""
        ...


class StaticSessionProvider:
    """09:30 to 16:00 New York on weekdays. No holidays or half days: for backtests
    and offline runs only."""

    async def session_for(self, day: date) -> Session | None:
        if day.weekday() >= 5:
            return Session(day, None, None)
        return Session(
            day,
            datetime.combine(day, time(9, 30), tzinfo=ET),
            datetime.combine(day, time(16, 0), tzinfo=ET),
        )


class SessionTracker:
    """Keeps today's session, fetching it once and retrying while it is unknown."""

    def __init__(self, provider: SessionProvider, *, retry_s: float = 60.0) -> None:
        self._provider = provider
        self._retry_s = retry_s
        self._session: Session | None = None
        self._attempt: tuple[date, datetime] | None = None

    @property
    def session(self) -> Session | None:
        return self._session

    async def refresh(self, now: datetime) -> None:
        day = trading_date(now)
        if self._session is not None and self._session.day == day:
            return
        if self._attempt is not None:
            attempt_day, attempted_at = self._attempt
            if attempt_day == day and (now - attempted_at).total_seconds() < self._retry_s:
                return
        self._attempt = (day, now)
        self._session = None  # yesterday's hours must never apply to today
        try:
            session = await self._provider.session_for(day)
        except Exception as exc:  # the calendar being down must not crash the bot
            log.warning("market-hours lookup failed, treating market as closed: %s", exc)
            return
        if session is None:
            log.warning("market hours for %s unknown, treating market as closed", day)
            return
        self._session = session
        log.info("session %s: open=%s close=%s", day, session.open, session.close)

    def view(self, now: datetime) -> SessionView:
        if self._session is None or self._session.day != trading_date(now):
            return _CLOSED
        return self._session.view(now)
