"""The bot's view of research, refreshed by polling the research table.

Fail closed: until a read succeeds, and once reads have failed for longer than
``max_stale_s``, there are no live picks and the posture is "stand aside". Exits never
depend on any of this; it only narrows and shrinks entries.

Today's posture (``todays_posture``) starts from the newest posture of an ok run; only
when there is none, and ``accept_partial_runs`` is set, from the earliest of a partial run.
Without one, or with any unreadable posture item today, the bot stands aside. A posture
written at or after the starting one, by a run of any status (ok, partial, running or
failed), can only make it stricter, never looser (``strictest_since``). The intraday
research run starts from the same rule, so neither the bot nor research ever loosens the
day on a partial run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from traider.config import ResearchSettings
from traider.research.models import Horizon, Pick, Posture, PostureLevel, RunMeta, RunStatus
from traider.research.store import DayResearch, ResearchStore
from traider.timeutil import previous_weekday, trading_date

log = logging.getLogger(__name__)

STRICTNESS: Mapping[PostureLevel, int] = {
    PostureLevel.TRADE: 0,
    PostureLevel.REDUCED: 1,
    PostureLevel.STAND_ASIDE: 2,
}


def strictest_since(start: Posture, postures: Iterable[Posture]) -> Posture:
    """``start``, or the strictest posture written at or after it, whatever its run's
    status: a newer posture can tighten the day but never loosen it. On a tie the earlier
    one stands (``start`` first)."""
    result = start
    for p in sorted((p for p in postures if p.at >= start.at), key=lambda p: p.at):
        if STRICTNESS[p.level] > STRICTNESS[result.level]:
            result = p
    return result


@dataclass(frozen=True, slots=True)
class ResearchView:
    picks: Mapping[str, Pick] = field(default_factory=dict)
    posture: Posture | None = None
    as_of: datetime | None = None
    stale: bool = False

    @property
    def level(self) -> PostureLevel:
        if self.stale or self.posture is None:
            return PostureLevel.STAND_ASIDE
        return self.posture.level

    def pick(self, symbol: str, now: datetime) -> Pick | None:
        found = self.picks.get(symbol)
        return found if found is not None and now < found.expires_at else None

    def live_picks(self, now: datetime) -> dict[str, Pick]:
        return {s: p for s, p in self.picks.items() if now < p.expires_at}


@dataclass(frozen=True, slots=True)
class ResearchUpdate:
    kind: Literal["stale", "restored"]
    detail: str


def todays_posture(research: DayResearch, *, accept_partial_runs: bool) -> Posture | None:
    """The day's posture, or None (stand aside): see the module docstring. The bot calls
    it with its ``accept_partial_runs``; the intraday run with False."""
    if research.invalid_postures:
        return None  # an unreadable posture may be the newest one

    def of(status: RunStatus) -> list[Posture]:
        return [
            p
            for p in research.postures
            if (run := research.runs.get(p.run_id)) is not None and run.status is status
        ]

    ok, partial = of(RunStatus.OK), of(RunStatus.PARTIAL)
    start = max(ok, key=lambda p: p.at) if ok else None
    if start is None and accept_partial_runs and partial:
        # The earliest: every partial posture after it then counts, so two partial runs
        # never loosen each other.
        start = min(partial, key=lambda p: p.at)
    return strictest_since(start, research.postures) if start is not None else None


def _usable(run: RunMeta | None, settings: ResearchSettings) -> bool:
    if run is None:
        return False
    return run.status is RunStatus.OK or (
        run.status is RunStatus.PARTIAL and settings.accept_partial_runs
    )


class ResearchSource:
    def __init__(self, store: ResearchStore, settings: Callable[[], ResearchSettings]) -> None:
        self._store = store
        self._settings = settings
        self.view = ResearchView()
        self._last_ok: datetime | None = None
        self._first_try: datetime | None = None
        self._stale_reported = False
        # The last settings read that worked, for judging staleness when they cannot be read.
        self._last_settings: ResearchSettings | None = None

    def _days(self, now: datetime, settings: ResearchSettings) -> list[date]:
        today = trading_date(now)
        days = [today]
        for _ in range(settings.swing_lookback_days):
            days.append(previous_weekday(days[-1]))
        return days

    async def refresh(self, now: datetime) -> list[ResearchUpdate]:
        if self._first_try is None:
            self._first_try = now
        # Any error counts as a failed read, so it counts towards staleness.
        try:
            settings = self._settings()
            self._last_settings = settings
            results = [await self._store.day(d.isoformat()) for d in self._days(now, settings)]
            # Built before anything counts as a success: a view that cannot be built is
            # no better than a table that cannot be read.
            view = self._build(now, results, settings)
        except Exception as exc:
            fallback = self._last_settings or ResearchSettings()
            return self._failed(now, fallback, f"{type(exc).__name__}: {exc}")
        updates: list[ResearchUpdate] = []
        if self._stale_reported:
            updates.append(ResearchUpdate("restored", "research table readable again"))
        self._stale_reported = False
        self._last_ok = now
        self.view = view
        return updates

    def _failed(
        self, now: datetime, settings: ResearchSettings, detail: str
    ) -> list[ResearchUpdate]:
        if self.view.as_of is not None and trading_date(self.view.as_of) != trading_date(now):
            # Yesterday's posture and intraday picks must not carry into a new trading day.
            self.view = ResearchView()
        since = self._last_ok or self._first_try or now
        if (now - since).total_seconds() <= settings.max_stale_s:
            log.warning("research read failed, keeping the last view: %s", detail)
            return []
        self.view = ResearchView(as_of=self.view.as_of, stale=True)
        if self._stale_reported:
            return []
        self._stale_reported = True
        return [ResearchUpdate("stale", detail)]

    def _build(
        self, now: datetime, results: list[DayResearch], settings: ResearchSettings
    ) -> ResearchView:
        today = trading_date(now).isoformat()
        runs: dict[str, RunMeta] = {}
        for result in results:
            runs.update(result.runs)
        best: dict[str, Pick] = {}
        for result in results:
            for p in result.picks:
                if not _usable(runs.get(p.run_id), settings):
                    continue
                if now >= p.expires_at or p.score < settings.min_score:
                    continue
                if result.day != today and p.horizon is not Horizon.SWING:
                    continue
                current = best.get(p.symbol)
                key = (p.score, p.run_id, -p.rank)
                if current is None or key > (current.score, current.run_id, -current.rank):
                    best[p.symbol] = p
        todays = next((r for r in results if r.day == today), None)
        posture: Posture | None = None
        if todays is not None:
            if todays.invalid_postures:
                # A posture we cannot read may be the newest one. Refuse the day's posture.
                log.warning(
                    "research posture for %s has %d unreadable item(s), standing aside",
                    today,
                    todays.invalid_postures,
                )
            posture = todays_posture(todays, accept_partial_runs=settings.accept_partial_runs)
        return ResearchView(picks=best, posture=posture, as_of=now, stale=False)
