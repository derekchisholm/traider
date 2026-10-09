"""The bot's view of research, refreshed by polling the research table.

Fail closed: until a read succeeds, and once reads have failed for longer than
``max_stale_s``, there are no live picks and the posture is "stand aside". Exits never
depend on any of this; it only narrows and shrinks entries.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from traider.config import ResearchSettings
from traider.research.models import Horizon, Pick, Posture, PostureLevel, RunMeta, RunStatus
from traider.research.store import DayResearch, ResearchStore
from traider.timeutil import previous_weekday, trading_date

log = logging.getLogger(__name__)


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

    def _days(self, now: datetime) -> list[date]:
        today = trading_date(now)
        days = [today]
        for _ in range(self._settings().swing_lookback_days):
            days.append(previous_weekday(days[-1]))
        return days

    async def refresh(self, now: datetime) -> list[ResearchUpdate]:
        settings = self._settings()
        if self._first_try is None:
            self._first_try = now
        try:
            results = [await self._store.day(d.isoformat()) for d in self._days(now)]
        except Exception as exc:
            return self._failed(now, settings, f"{type(exc).__name__}: {exc}")
        updates: list[ResearchUpdate] = []
        if self._stale_reported:
            updates.append(ResearchUpdate("restored", "research table readable again"))
        self._stale_reported = False
        self._last_ok = now
        self.view = self._build(now, results, settings)
        return updates

    def _failed(
        self, now: datetime, settings: ResearchSettings, detail: str
    ) -> list[ResearchUpdate]:
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
        postures = (
            [p for p in results[0].postures if _usable(runs.get(p.run_id), settings)]
            if results and results[0].day == today
            else []
        )
        posture = max(postures, key=lambda p: p.at) if postures else None
        return ResearchView(picks=best, posture=posture, as_of=now, stale=False)
