"""The intraday run, every 30 minutes from 10:00 to 15:00 New York time on weekdays.

    lock(intraday) -> session open now? -> after last_start? -> an ok posture today?
      -> META running -> collect (context quotes, SPY bars, movers, the earnings calendar)
      -> posture = stricter(today's latest ok posture, code rules on current metrics)
      -> stand_aside? yes -> write the posture, no picks, ok
      -> candidates = movers - picked today - held - pinned -> screen -> top K
      -> deep-dives (intraday_model, intraday_run_usd) -> rank (every pick intraday)
      -> write picks + posture + META -> alert if there are picks, the posture tightened,
         or the run is partial

It never rescues a day: without an ok posture today (from the morning run, or an earlier
intraday run) it writes nothing and exits ``skipped``. Its posture can only be stricter
than the one it starts from, and there is no model review of it. It always writes the
posture, changed or not, so the day's record is continuous. A swing idea is made
intraday (with a note): every pick expires at today's close. "Held" is the bot's ledger,
read-only; without it the run fails.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Final

from traider.research.botstate import held_symbols
from traider.research.dive import Assessment
from traider.research.events import EarningsEvent, EventsUnavailable
from traider.research.job_settings import DiveSettings, ScreenSettings
from traider.research.models import (
    Pick,
    Posture,
    PostureLevel,
    RunKind,
    RunMeta,
    RunStatus,
)
from traider.research.posture import (
    REASON_MAX_CHARS,
    PostureDecision,
    code_posture,
    posture_metrics,
    stricter,
)
from traider.research.rank import RankInput, RankResult, rank_and_validate
from traider.research.run import (
    CONTEXT_SYMBOLS,
    EMPTY_CALENDAR_WEEKDAYS,
    EXIT_OK,
    HISTORY_DAYS,
    MAX_NOTES,
    MOVER_INDEXES,
    MOVER_SORTS,
    RunDeps,
    RunOutcome,
    Snapshot,
    _events_for,
    _Run,
    _stale,
    new_run_id,
    run_locked,
)
from traider.research.screen import build_candidates
from traider.research.scrub import LIMIT, scrub
from traider.research.store import DayResearch
from traider.timeutil import ET, weekdays_between

log = logging.getLogger(__name__)

KIND: Final = "intraday"
# A scheduled start lands a little after its time (the task takes a minute or two to
# start), so last_start allows this much.
START_GRACE_S: Final = 300
# The alert: the summary line (at most rank.max_picks = 25 picks, well under SUMMARY_MAX)
# and "; notes: " plus the notes, which RunBase.note bounds to MAX_NOTES of at most LIMIT
# characters each. Scrubbed again as a whole, never cut short.
SUMMARY_MAX: Final = 1000
MESSAGE_LIMIT: Final = SUMMARY_MAX + MAX_NOTES * (LIMIT + 2) + len("; notes: ")


class NoBotState(Exception):
    """An intraday run cannot tell what the bot holds."""


async def run_intraday(
    deps: RunDeps, now: datetime, *, dry_run: bool = False, force: bool = False
) -> RunOutcome:
    """One intraday run. ``dry_run`` makes every call but writes nothing to the research
    table and sends no alert. ``force`` (and a dry run) ignores ``last_start``; nothing
    ignores the need for an ok posture today."""
    now = now.astimezone(UTC)
    run_id = new_run_id(now, KIND)
    jobs = deps.settings.research_jobs
    if not jobs.enabled or not jobs.intraday.enabled:
        log.info("intraday research is switched off; nothing to do")
        detail = "research_jobs.enabled or research_jobs.intraday.enabled is false"
        return RunOutcome("disabled", EXIT_OK, run_id, detail=detail)
    return await run_locked(IntradayRun(deps, now, run_id, dry_run=dry_run), force=force)


def latest_ok_posture(research: DayResearch) -> Posture | None:
    """Today's newest posture from an ok run, or None. A day with an unreadable posture
    item has none: that item may be the newest."""
    if research.invalid_postures:
        return None
    usable = [
        p
        for p in research.postures
        if (run := research.runs.get(p.run_id)) is not None and run.status is RunStatus.OK
    ]
    return max(usable, key=lambda p: p.at) if usable else None


class IntradayRun(_Run):
    kind: ClassVar[RunKind] = KIND
    lock_name: ClassVar[str] = KIND
    title: ClassVar[str] = "Research intraday"
    intraday_dives: ClassVar[bool] = True

    def __init__(self, deps: RunDeps, now: datetime, run_id: str, *, dry_run: bool) -> None:
        super().__init__(deps, now, run_id, dry_run=dry_run)
        self.tightened = False

    def time_limit(self) -> float:
        return self.jobs.intraday.max_run_s

    def models(self) -> tuple[str, ...]:
        return (self.jobs.dive.intraday_model,)

    def screen_settings(self) -> ScreenSettings:
        return self.jobs.screen.model_copy(
            update={"deep_dive_count": self.jobs.intraday.deep_dive_count}
        )

    def dive_settings(self) -> DiveSettings:
        return self.jobs.dive.model_copy(update={"model": self.jobs.dive.intraday_model})

    def alert_wanted(self, status: RunStatus, posture: Posture, picks: Sequence[Pick]) -> bool:
        return bool(picks) or self.tightened or status is RunStatus.PARTIAL

    def summary(self, posture: Posture, picks: Sequence[Pick], meta: RunMeta) -> str:
        return scrub(super().summary(posture, picks, meta), MESSAGE_LIMIT)

    def after_last_start(self) -> bool:
        last = datetime.combine(self.today, self.jobs.intraday.last_start, tzinfo=ET)
        return self.now > last + timedelta(seconds=START_GRACE_S)

    async def _execute(self, *, force: bool) -> RunOutcome:
        deps, day = self.deps, self.today.isoformat()
        self.stage = "market_hours"
        session = await deps.market.market_session(self.today)
        if session.open is None or session.close is None:
            log.info("no regular session on %s; nothing to research", day)
            return RunOutcome("closed", EXIT_OK, self.run_id, detail=f"market closed on {day}")
        if not session.open <= self.now < session.close:
            log.info("the session is not open; nothing to research")
            return RunOutcome("closed", EXIT_OK, self.run_id, detail="the session is not open")
        if not force and self.after_last_start():
            last = self.jobs.intraday.last_start.strftime("%H:%M")
            log.info("after research_jobs.intraday.last_start (%s); skipping", last)
            return RunOutcome(
                "skipped", EXIT_OK, self.run_id, detail=f"after last_start ({last} New York)"
            )
        self.stage = "morning_posture"
        research = await deps.store.day(day)
        base = latest_ok_posture(research)
        if base is None:
            # A failed morning means standing aside all day: code rules alone never
            # rescue it.
            log.info("no ok posture today; an intraday run never rescues the day")
            return RunOutcome("skipped", EXIT_OK, self.run_id, detail="no ok posture today")
        await self.begin(self.jobs.budget.intraday_run_usd)

        self.stage = "held"
        if deps.state is None:
            raise NoBotState("no state table: the run cannot tell what the bot holds")
        held = await held_symbols(deps.state)
        picked = {p.symbol for p in research.picks}

        self.stage = "collect"
        snapshot = await self.collect_intraday(picked=picked, held=held)
        await self.put_trail("snapshot.json", snapshot)

        self.stage = "posture"
        decision = self.tighten(snapshot, base)
        posture = Posture(
            level=decision.level,
            reasons=tuple(scrub(r, REASON_MAX_CHARS) for r in decision.reasons),
            run_id=self.run_id,
            at=self.now,
            metrics=decision.metrics.as_dict(),
        )
        await self.put_trail(
            "posture.json",
            {"posture": posture, "from": base, "notes": [scrub(n) for n in decision.notes]},
        )
        if decision.level is PostureLevel.STAND_ASIDE:
            self.stage = "write"
            return await self.finish(posture, RankResult((), ()), {})
        if deps.monotonic() >= self.started + self.max_run_s:
            self.note("deadline passed before the screen: no deep-dives", partial=True)
            self.stage = "write"
            return await self.finish(posture, RankResult((), ()), {})

        self.stage = "screen"
        top, quotes, bars, profiles = await self.screen(snapshot)

        self.stage = "dive"
        results = await self.dive(
            snapshot, decision=decision, top=top, quotes=quotes, bars=bars, profiles=profiles
        )

        self.stage = "rank"
        by_symbol = {row.symbol: row for row in top}
        inputs = [
            RankInput(
                symbol=r.symbol,
                assessment=self.intraday_only(r.symbol, r.assessment),
                pre_score=by_symbol[r.symbol].pre_score,
                features=by_symbol[r.symbol].features.as_dict(),
                atr=by_symbol[r.symbol].atr,
                sector=p.industry if (p := profiles.get(r.symbol)) else None,
                earnings=_events_for(snapshot.earnings, r.symbol),
            )
            for r in results
            if r.assessment is not None
        ]
        self.counts["assessed"] = len(inputs)
        ranked = await rank_and_validate(
            inputs,
            market=deps.market,
            run_id=self.run_id,
            today=self.today,
            close=session.close,
            earnings_ok=snapshot.earnings_ok,
            calendar_end=self.calendar_end,
            settings=self.jobs.rank,
        )
        if ranked.chain_failures:
            self.counts["chain_failures"] = ranked.chain_failures
            self.note(
                f"put chain reads failed for {ranked.chain_failures} name(s); counted as illiquid"
            )
        self.stage = "write"
        assessments = {i.symbol: i.assessment.model_dump(mode="json") for i in inputs}
        return await self.finish(posture, ranked, assessments)

    def intraday_only(self, symbol: str, assessment: Assessment) -> Assessment:
        """Every intraday pick is flat by today's close: a swing idea is made intraday."""
        if assessment.horizon != "swing":
            return assessment
        self.counts["coerced_to_intraday"] = self.counts.get("coerced_to_intraday", 0) + 1
        self.note(f"{symbol}: a swing idea was made intraday")
        return assessment.model_copy(update={"horizon": "intraday", "swing_days": None})

    def tighten(self, snapshot: Snapshot, base: Posture) -> PostureDecision:
        """The stricter of the posture the run starts from and the code rules on the
        market now. No model review, so it can only tighten."""
        spy_bars = snapshot.spy_bars
        notes: list[str] = []
        if spy_bars and _stale(spy_bars, self.today):
            note = f"SPY daily history is stale (last bar {spy_bars[-1].day.isoformat()})"
            self.note(note)
            notes.append(note)
            spy_bars = []
        metrics = posture_metrics(snapshot.context, spy_bars)
        code_level, code_reasons = code_posture(metrics, self.today, self.jobs.posture)
        level = stricter(base.level, code_level)
        self.tightened = level is not base.level
        reasons = [
            f"intraday: at least {base.level.value}, from {base.run_id}",
            *(f"code: {r}" for r in code_reasons),
        ]
        return PostureDecision(level, tuple(reasons), metrics, notes=tuple(notes))

    async def collect_intraday(self, *, picked: set[str], held: set[str]) -> Snapshot:
        deps, jobs, today = self.deps, self.jobs, self.today
        context = (await deps.market.quotes(CONTEXT_SYMBOLS)).quotes
        spy_bars = await deps.market.daily_bars("SPY", today, HISTORY_DAYS)
        movers: dict[str, list[str]] = {}
        for index in MOVER_INDEXES:
            for sort in MOVER_SORTS:
                movers[f"{index}:{sort}"] = await deps.market.movers(index, sort)
        earnings: list[EarningsEvent] = []
        earnings_ok = True
        try:
            earnings = await deps.events.earnings_calendar(self.calendar_start, self.calendar_end)
        except EventsUnavailable as exc:
            earnings_ok = False
            self.note(f"earnings calendar unavailable: {exc}", partial=True)
        else:
            span = weekdays_between(self.calendar_start, self.calendar_end) + 1
            if not earnings and span >= EMPTY_CALENDAR_WEEKDAYS:
                earnings_ok = False
                self.note(
                    f"earnings calendar returned nothing for {span} weekdays; treated as "
                    "unavailable",
                    partial=True,
                )
        names = list(dict.fromkeys(name for found in movers.values() for name in found))
        pinned = set(deps.settings.pinned_symbols)
        for reason, skip in (("picked", picked), ("held", held), ("pinned", pinned)):
            if count := sum(1 for n in names if n in skip):
                self.counts[f"excluded_{reason}"] = count
        candidates = build_candidates(
            watchlist=(),
            earnings_names=(),
            movers=names,
            pinned=picked | held | pinned,
            cap=jobs.intraday.max_candidates,
        )
        self.counts["candidates"] = len(candidates)
        return Snapshot(
            taken_at=self.now,
            day=today,
            context=context,
            spy_bars=spy_bars,
            movers=movers,
            earnings_ok=earnings_ok,
            earnings=earnings,
            news_ok=True,
            market_news=[],
            watchlist=[],
            candidates=[c.symbol for c in candidates],
            candidate_sources={c.symbol: list(c.sources) for c in candidates},
        )
