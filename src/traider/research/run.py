"""The pre-market research run, start to finish.

    lock -> market open today? -> already done today? -> META running
      -> collect (Schwab + events) -> posture (code rules, then a model review; stricter wins)
      -> stand_aside? yes -> write the posture, no picks, ok
      -> screen (filters, stale history, features, pre_score, top K)
      -> deep-dives (tool loops, budgets, concurrency, deadline)
      -> rank + validate -> add the cost -> write picks + posture + META ok|partial -> alert
    any exception -> META failed, alert, exit 1 (no posture, so the bot stands aside)

Exit codes: 0 ok, partial, skipped, closed or disabled; 1 failed; 2 the lock is held.
A run is ``partial`` when planned work did not happen: a budget stopped calls, the
deadline passed, the events vendor failed, or a trail file could not be written. The bot
ignores partial runs by default.

The deadline is ``research_jobs.max_run_s`` from the start. Past it no new deep-dive
starts (checked on ``RunDeps.monotonic``), and a hard stop on the event loop's clock
cancels dives still running; a cancelled dive's reservation is charged in full.

Every text that reaches META or an alert (errors, notes) goes through ``scrub`` first:
vendor errors and model-written text can carry secrets or injected words.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from traider.alerts import Alerter
from traider.research.cost import CostMeter
from traider.research.dive import DiveContext, DiveResult, run_dive
from traider.research.events import (
    EarningsEvent,
    EventsData,
    EventsUnavailable,
    NewsItem,
    Profile,
)
from traider.research.llm import LLM
from traider.research.market import DailyBar, MarketData, MarketQuote, QuoteBatch
from traider.research.models import Pick, Posture, PostureLevel, RunMeta, RunStatus
from traider.research.posture import PostureDecision, decide_posture, posture_metrics
from traider.research.rank import RankInput, RankResult, Rejection, rank_and_validate
from traider.research.screen import (
    DROP_HISTORY,
    DROP_HISTORY_ERROR,
    MIN_BARS,
    ScreenRow,
    build_candidates,
    compute_features,
    drop_counts,
    earnings_candidates,
    earnings_near,
    history_filter,
    quote_filter,
    score_rows,
    top_k,
    with_news,
)
from traider.research.scrub import scrub
from traider.research.store import ResearchWriter
from traider.research.trail import Trail, trail_prefix
from traider.schwab.client import SchwabError
from traider.schwab.parse import ParseError
from traider.settings import Settings
from traider.timeutil import (
    Clock,
    SystemClock,
    previous_weekday,
    trading_date,
    weekdays_after,
    weekdays_between,
)

log = logging.getLogger(__name__)

KIND: Final = "premarket"
LOCK_NAME = "premarket"
LOCK_SPARE_S = 600  # the lock outlives the deadline by this much
HISTORY_DAYS = 260
NEWS_DAYS = 3
BARS_CONCURRENCY = 8
SECTOR_ETFS = ("XLK", "XLF", "XLV", "XLE", "XLY", "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC")
CONTEXT_SYMBOLS = ("$VIX", "SPY", "QQQ", "IWM", *SECTOR_ETFS)
MOVER_INDEXES = ("EQUITY_ALL", "NYSE", "NASDAQ")
MOVER_SORTS = ("PERCENT_CHANGE_UP", "PERCENT_CHANGE_DOWN", "VOLUME")
MAX_NOTES = 20
# A name whose last daily bar is more than this many weekdays before today is dropped:
# its features would describe an old market.
MAX_BAR_AGE_WEEKDAYS = 3
DROP_STALE_HISTORY = "stale_history"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_LOCKED = 2


class Snapshot(BaseModel):
    """What the run saw before deciding anything. Stored as ``snapshot.json``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    taken_at: datetime
    day: date
    context: dict[str, MarketQuote]
    spy_bars: list[DailyBar]
    movers: dict[str, list[str]]
    earnings_ok: bool
    earnings: list[EarningsEvent]
    news_ok: bool
    market_news: list[NewsItem]
    watchlist: list[str]
    candidates: list[str]
    candidate_sources: dict[str, list[str]]


@dataclass
class RunDeps:
    store: ResearchWriter
    market: MarketData
    events: EventsData
    llm: LLM
    trail: Callable[[str], Trail]  # from the run's prefix
    alerts: Alerter
    settings: Settings  # read once, when the run starts
    clock: Clock = field(default_factory=SystemClock)
    monotonic: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class RunOutcome:
    status: str  # ok, partial, failed, skipped, closed, disabled, locked
    exit_code: int
    run_id: str
    meta: RunMeta | None = None
    posture: Posture | None = None
    picks: tuple[Pick, ...] = ()
    rejected: tuple[Rejection, ...] = ()
    detail: str = ""

    def report(self) -> dict[str, Any]:
        """For printing: what the run decided, as JSON-ready data."""
        return {
            "status": self.status,
            "run_id": self.run_id,
            "detail": self.detail,
            "posture": self.posture.model_dump(mode="json") if self.posture else None,
            "picks": [p.model_dump(mode="json") for p in self.picks],
            "rejected": {r.symbol: r.reason for r in self.rejected},
            "cost_usd": str(self.meta.cost_usd) if self.meta else "0",
            "notes": list(self.meta.notes) if self.meta else [],
            "counts": dict(self.meta.counts) if self.meta else {},
        }


def new_run_id(now: datetime) -> str:
    return f"{KIND}-{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(2)}"


def _describe(exc: BaseException) -> str:
    """An exception as text that is safe for logs, META and alerts."""
    return scrub(f"{type(exc).__name__}: {exc}")


def _loop_deadline(max_run_s: float) -> float:
    """The hard stop for running deep-dives, on the event loop's clock."""
    return asyncio.get_running_loop().time() + max_run_s


async def run_premarket(
    deps: RunDeps, now: datetime, *, dry_run: bool = False, force: bool = False
) -> RunOutcome:
    """One pre-market run. ``dry_run`` makes every call but writes nothing to the research
    table (no lock, META, picks or cost) and sends no alert. ``force`` ignores an earlier
    ok or partial run today; it never ignores the lock."""
    now = now.astimezone(UTC)
    run_id = new_run_id(now)
    jobs = deps.settings.research_jobs
    if not jobs.enabled:
        log.info("research jobs are switched off (research_jobs.enabled); nothing to do")
        return RunOutcome("disabled", EXIT_OK, run_id, detail="research_jobs.enabled is false")
    run = _Run(deps, now, run_id, dry_run=dry_run)
    if dry_run:
        return await run.execute(force=True)
    try:
        acquired = await deps.store.acquire_lock(
            LOCK_NAME, run_id, jobs.max_run_s + LOCK_SPARE_S, now
        )
    except Exception as exc:
        return await run.fail(exc)
    if not acquired:
        log.warning("another %s run holds the lock; exiting", KIND)
        return RunOutcome("locked", EXIT_LOCKED, run_id, detail="another run holds the lock")
    try:
        return await run.execute(force=force)
    finally:
        try:
            await deps.store.release_lock(LOCK_NAME, run_id)
        except Exception as exc:
            log.error("could not release the research lock (it expires by itself): %s",
                      _describe(exc))  # fmt: skip


class _Run:
    def __init__(self, deps: RunDeps, now: datetime, run_id: str, *, dry_run: bool) -> None:
        self.deps = deps
        self.now = now
        self.today = trading_date(now)
        self.run_id = run_id
        self.dry_run = dry_run
        self.jobs = deps.settings.research_jobs
        self.started = deps.monotonic()
        self.hard_stop = _loop_deadline(self.jobs.max_run_s)
        self.stage = "start"
        self.notes: list[str] = []
        self.partial = False
        self.budget_noted = False
        self.counts: dict[str, int] = {}
        self.meter: CostMeter | None = None
        self.trail: Trail | None = None
        self.trail_failures = 0
        self.cost_added = False

    # ------------------------------------------------------------------ helpers

    def note(self, text: str, *, partial: bool = False) -> None:
        clean = scrub(text)  # at most 300 characters, as RunMeta.notes requires
        log.warning("research %s: %s", self.run_id, clean)
        if len(self.notes) < MAX_NOTES:
            self.notes.append(clean)
        self.partial = self.partial or partial

    def meta(self, status: RunStatus, *, error: str = "", finished: bool = True) -> RunMeta:
        meter = self.meter
        dive = self.jobs.dive
        return RunMeta(
            run_id=self.run_id,
            kind=KIND,
            status=status,
            started_at=self.now,
            finished_at=self.deps.clock.now() if finished else None,
            trading_day=self.today,
            models=tuple(dict.fromkeys((dive.posture_model, dive.model))),
            cost_usd=meter.spent_usd if meter else Decimal(0),
            s3_prefix=self.trail.location if self.trail else "",
            error=error,
            tokens_in=meter.tokens_in if meter else 0,
            tokens_out=meter.tokens_out if meter else 0,
            notes=tuple(self.notes),
            counts=dict(self.counts),
        )

    async def put_trail(self, name: str, data: Any) -> None:
        """A failed trail write does not stop the run, but the audit trail is incomplete,
        so the run is partial. The first failure is noted; all are counted."""
        assert self.trail is not None
        try:
            await self.trail.put(name, data)
        except Exception as exc:
            self.trail_failures += 1
            self.counts["trail_failures"] = self.trail_failures
            if self.trail_failures == 1:
                self.note(f"trail write failed ({name}): {_describe(exc)}", partial=True)
            else:
                log.warning("trail write failed (%s): %s", name, _describe(exc))

    # --------------------------------------------------------------------- flow

    async def execute(self, *, force: bool) -> RunOutcome:
        try:
            return await self._execute(force=force)
        except Exception as exc:
            return await self.fail(exc)

    async def _execute(self, *, force: bool) -> RunOutcome:
        deps, day = self.deps, self.today.isoformat()
        self.stage = "market_hours"
        session = await deps.market.market_session(self.today)
        if session.open is None or session.close is None:
            log.info("no regular session on %s; nothing to research", day)
            return RunOutcome("closed", EXIT_OK, self.run_id, detail=f"market closed on {day}")
        if not force:
            self.stage = "skip_check"
            done = [
                m
                for m in await deps.store.runs_for_day(day, KIND)
                if m.status in (RunStatus.OK, RunStatus.PARTIAL)
            ]
            if done:
                log.info("%s already has a %s run (%s); skipping", day, KIND, done[-1].run_id)
                return RunOutcome(
                    "skipped", EXIT_OK, self.run_id, detail=f"already done by {done[-1].run_id}"
                )
        self.stage = "start"
        budget = self.jobs.budget
        spent_today = await deps.store.day_cost(day)
        self.meter = CostMeter(
            budget.prices, run_usd=budget.run_usd, day_remaining_usd=budget.day_usd - spent_today
        )
        self.trail = deps.trail(trail_prefix(self.today, self.run_id))
        if not self.dry_run:
            await deps.store.put_meta(self.meta(RunStatus.RUNNING, finished=False))

        self.stage = "collect"
        snapshot = await self.collect()
        await self.put_trail("snapshot.json", snapshot)

        self.stage = "posture"
        decision = await self.decide(snapshot)
        posture = Posture(
            level=decision.level,
            reasons=decision.reasons,
            run_id=self.run_id,
            at=self.now,
            metrics=decision.metrics.as_dict(),
        )
        await self.put_trail("posture.json", {"posture": posture, "notes": decision.notes})
        if decision.level is PostureLevel.STAND_ASIDE:
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
                assessment=r.assessment,
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
        assert session.close is not None
        ranked = await rank_and_validate(
            inputs,
            market=deps.market,
            run_id=self.run_id,
            today=self.today,
            close=session.close,
            earnings_ok=snapshot.earnings_ok,
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

    # ------------------------------------------------------------------ collect

    async def collect(self) -> Snapshot:
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
            earnings = await deps.events.earnings_calendar(
                previous_weekday(today),
                weekdays_after(today, jobs.collect.earnings_lookahead_days),
            )
        except EventsUnavailable as exc:
            earnings_ok = False
            self.note(f"earnings calendar unavailable, no swing picks: {exc}", partial=True)
        market_news: list[NewsItem] = []
        news_ok = True
        if jobs.collect.market_news_count:
            try:
                market_news = await deps.events.market_news(jobs.collect.market_news_count)
            except EventsUnavailable as exc:
                news_ok = False
                self.note(f"market news unavailable: {exc}", partial=True)
        mover_names = list(dict.fromkeys(name for names in movers.values() for name in names))
        candidates = build_candidates(
            watchlist=jobs.watchlist,
            earnings_names=earnings_candidates(earnings, today),
            movers=mover_names,
            pinned=deps.settings.pinned_symbols,
            cap=jobs.collect.max_candidates,
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
            news_ok=news_ok,
            market_news=market_news,
            watchlist=list(jobs.watchlist),
            candidates=[c.symbol for c in candidates],
            candidate_sources={c.symbol: list(c.sources) for c in candidates},
        )

    # ------------------------------------------------------------------ posture

    async def decide(self, snapshot: Snapshot) -> PostureDecision:
        assert self.meter is not None
        decision = await decide_posture(
            self.deps.llm,
            self.meter,
            model=self.jobs.dive.posture_model,
            max_tokens=self.jobs.dive.max_tokens,
            metrics=posture_metrics(snapshot.context, snapshot.spy_bars),
            today=self.today,
            settings=self.jobs.posture,
            sector_gaps=_sector_gaps(snapshot.context),
            headlines=snapshot.market_news,
        )
        for text in decision.notes:
            self.note(text, partial=decision.budget_hit)
        self.budget_noted = self.budget_noted or decision.budget_hit
        return decision

    # ------------------------------------------------------------------- screen

    async def screen(
        self, snapshot: Snapshot
    ) -> tuple[
        list[ScreenRow], dict[str, MarketQuote], dict[str, list[DailyBar]], dict[str, Profile]
    ]:
        deps, jobs, today = self.deps, self.jobs, self.today
        settings = jobs.screen
        symbols = snapshot.candidates
        batch = await deps.market.quotes(symbols) if symbols else QuoteBatch({})
        if batch.skipped:
            self.counts["unreadable_quotes"] = batch.skipped
        dropped: dict[str, str] = {}
        passing: list[str] = []
        for symbol in symbols:
            reason = quote_filter(symbol, batch.quotes.get(symbol), settings)
            if reason:
                dropped[symbol] = reason
            else:
                passing.append(symbol)
        histories = await self._histories(passing)
        rows: list[ScreenRow] = []
        bars: dict[str, list[DailyBar]] = {}
        for symbol in passing:
            history = histories[symbol]
            q = batch.quotes[symbol]
            if history is None:
                dropped[symbol] = DROP_HISTORY_ERROR
                continue
            if len(history) < MIN_BARS:
                dropped[symbol] = DROP_HISTORY
                continue
            if weekdays_between(history[-1].day, today) > MAX_BAR_AGE_WEEKDAYS:
                dropped[symbol] = DROP_STALE_HISTORY
                continue
            reason = history_filter(q, history, settings)
            if reason:
                dropped[symbol] = reason
                continue
            events = _events_for(snapshot.earnings, symbol)
            features, atr = compute_features(
                q,
                history,
                today=today,
                events=events,
                earnings_ok=snapshot.earnings_ok,
                lookahead=jobs.collect.earnings_lookahead_days,
            )
            assert q.last is not None
            near = snapshot.earnings_ok and earnings_near(events, today)
            rows.append(ScreenRow(symbol, q.last, atr, features, near))
            bars[symbol] = history

        news_counts = await self._news_counts(
            top_k(score_rows(rows, settings.weights), 2 * settings.deep_dive_count)
        )
        scored = score_rows(
            [with_news(r, news_counts.get(r.symbol, 0)) for r in rows], settings.weights
        )
        top = top_k(scored, settings.deep_dive_count)
        profiles = await self._profiles(top)
        self.counts["screened"] = len(scored)
        for reason, count in drop_counts(dropped).items():
            self.counts[f"drop_{reason}"] = count
        scores = {row.symbol: row for row in scored}
        await self.put_trail(
            "screen.json",
            [
                {
                    "symbol": symbol,
                    "sources": snapshot.candidate_sources.get(symbol, []),
                    "dropped": dropped.get(symbol),
                    "features": scores[symbol].features.as_dict() if symbol in scores else None,
                    "pre_score": scores[symbol].pre_score if symbol in scores else None,
                    "deep_dive": any(r.symbol == symbol for r in top),
                }
                for symbol in symbols
            ],
        )
        return top, batch.quotes, bars, profiles

    async def _histories(self, symbols: Sequence[str]) -> dict[str, list[DailyBar] | None]:
        gate = asyncio.Semaphore(BARS_CONCURRENCY)

        async def one(symbol: str) -> list[DailyBar] | None:
            async with gate:
                try:
                    return await self.deps.market.daily_bars(symbol, self.today, HISTORY_DAYS)
                except (SchwabError, ParseError) as exc:
                    log.warning("no history for %s: %s", symbol, _describe(exc))
                    return None

        found = await asyncio.gather(*(one(s) for s in symbols))
        return dict(zip(symbols, found, strict=True))

    async def _news_counts(self, rows: Sequence[ScreenRow]) -> dict[str, int]:
        counts: dict[str, int] = {}
        start = self.today - timedelta(days=NEWS_DAYS)
        for row in rows:
            try:
                items = await self.deps.events.company_news(row.symbol, start, self.today)
            except EventsUnavailable as exc:
                self.note(f"company news unavailable, news counts incomplete: {exc}", partial=True)
                break
            counts[row.symbol] = len(items)
        return counts

    async def _profiles(self, rows: Sequence[ScreenRow]) -> dict[str, Profile]:
        found: dict[str, Profile] = {}
        for row in rows:
            try:
                profile = await self.deps.events.profile(row.symbol)
            except EventsUnavailable as exc:
                self.note(f"company profiles unavailable, sectors unknown: {exc}", partial=True)
                break
            if profile is not None:
                found[row.symbol] = profile
        return found

    # --------------------------------------------------------------------- dive

    async def dive(
        self,
        snapshot: Snapshot,
        *,
        decision: PostureDecision,
        top: Sequence[ScreenRow],
        quotes: Mapping[str, MarketQuote],
        bars: Mapping[str, list[DailyBar]],
        profiles: Mapping[str, Profile],
    ) -> list[DiveResult]:
        deps, jobs = self.deps, self.jobs
        assert self.meter is not None
        meter = self.meter
        gate = asyncio.Semaphore(jobs.dive.dive_concurrency)
        deadline = self.started + jobs.max_run_s
        context = {
            "posture": decision.level.value,
            "metrics": decision.metrics.as_dict(),
            "sector_etf_gaps_pct": {
                s: round(gap, 2) for s, gap in _sector_gaps(snapshot.context).items()
            },
        }

        async def one(row: ScreenRow) -> DiveResult | None:
            async with gate:
                if deps.monotonic() >= deadline:
                    return None
                ctx = DiveContext(
                    symbol=row.symbol,
                    today=self.today,
                    quote=quotes[row.symbol],
                    bars=tuple(bars[row.symbol]),
                    features=row.features.as_dict(),
                    earnings=_events_for(snapshot.earnings, row.symbol),
                    earnings_ok=snapshot.earnings_ok,
                    profile=profiles.get(row.symbol),
                    market_context=context,
                )
                # Cancelled by the hard stop, run_dive charges the call in flight and
                # re-raises; the task then ends cancelled.
                result = await run_dive(
                    ctx,
                    market=deps.market,
                    events=deps.events,
                    llm=deps.llm,
                    meter=meter,
                    settings=jobs.dive,
                )
                await self.put_trail(f"dives/{row.symbol}.json", result.trail(jobs.dive.model))
                return result

        tasks: list[asyncio.Task[DiveResult | None]] = []
        try:
            async with asyncio.timeout_at(self.hard_stop):
                async with asyncio.TaskGroup() as group:
                    tasks = [group.create_task(one(row)) for row in top]
        except TimeoutError:
            pass  # counted below: the cancelled tasks
        cut = sum(1 for task in tasks if task.cancelled())
        outcomes = [task.result() for task in tasks if not task.cancelled()]
        results = [r for r in outcomes if r is not None]
        self.counts["dived"] = len(results)
        not_started = len(outcomes) - len(results)
        if not_started:
            self.note(f"deadline passed: {not_started} deep-dive(s) not started", partial=True)
        if cut:
            self.note(f"deadline passed: {cut} deep-dive(s) cut short", partial=True)
        stopped = sum(1 for r in results if r.budget_hit)
        if stopped:
            self.note(f"budget reached: {stopped} deep-dive(s) stopped", partial=True)
            self.budget_noted = True
        if any(r.news_failed for r in results):
            self.note("company news unavailable during deep-dives", partial=True)
        return results

    # ------------------------------------------------------------------- finish

    def _budget_notes(self) -> None:
        """Any budget stop makes the run partial, however it showed up."""
        assert self.meter is not None
        if self.meter.overrun:
            self.note("budget: a model call cost more than its reservation", partial=True)
        if self.meter.exhausted and not self.budget_noted:
            self.note("budget reached: the cost meter stopped further calls", partial=True)
            self.budget_noted = True

    async def finish(
        self, posture: Posture, ranked: RankResult, assessments: Mapping[str, Any]
    ) -> RunOutcome:
        assert self.meter is not None
        self.counts["picks"] = len(ranked.picks)
        self._budget_notes()
        await self.put_trail(
            "result.json",
            {
                "status": (RunStatus.PARTIAL if self.partial else RunStatus.OK).value,
                "posture": posture,
                "assessments": assessments,
                "rejected": {r.symbol: r.reason for r in ranked.rejected},
                "picks": list(ranked.picks),
                "notes": self.notes,
                "cost_usd": str(self.meter.spent_usd),
                "counts": self.counts,
            },
        )
        # After result.json: a failed write there makes the run partial too.
        status = RunStatus.PARTIAL if self.partial else RunStatus.OK
        meta = self.meta(status)
        if not self.dry_run:
            # The cost first: a write that fails afterwards must not hide what was spent.
            await self.deps.store.add_day_cost(self.today.isoformat(), self.meter.spent_usd)
            self.cost_added = True
            await self.deps.store.write_run(meta, ranked.picks, posture)
            await self.alert(status, _summary(self.today, posture, ranked.picks, meta))
        return RunOutcome(
            status.value,
            EXIT_OK,
            self.run_id,
            meta=meta,
            posture=posture,
            picks=ranked.picks,
            rejected=ranked.rejected,
        )

    async def fail(self, exc: BaseException) -> RunOutcome:
        cause = exc.exceptions[0] if isinstance(exc, BaseExceptionGroup) and exc.exceptions else exc
        error = scrub(f"{self.stage}: {type(cause).__name__}: {cause}")
        # Frames only: the exception's own text may carry vendor words, so it is scrubbed.
        frames = "".join(traceback.format_tb(cause.__traceback__))
        log.error("research run %s failed: %s\n%s", self.run_id, error, frames)
        meta = self.meta(RunStatus.FAILED, error=error)
        if not self.dry_run:
            if self.meter is not None and not self.cost_added and self.meter.spent > 0:
                try:
                    await self.deps.store.add_day_cost(self.today.isoformat(), self.meter.spent_usd)
                except Exception as add_exc:
                    log.error("could not add the failed run's cost to the day: %s",
                              _describe(add_exc))  # fmt: skip
            try:
                await self.deps.store.put_meta(meta)
            except Exception as put_exc:
                log.error("could not record the failed run: %s", _describe(put_exc))
            await self.alert(
                RunStatus.FAILED,
                f"traider research {KIND} {self.today.isoformat()} failed: {error}",
            )
        return RunOutcome("failed", EXIT_FAILED, self.run_id, meta=meta, detail=error)

    async def alert(self, status: RunStatus, message: str) -> None:
        subject = f"Research {self.today.isoformat()}: {status.value}"
        try:
            await self.deps.alerts.send(f"research_run_{status.value}", subject, message)
        except Exception as exc:
            log.error("could not send the research alert: %s", _describe(exc))


def _sector_gaps(context: Mapping[str, MarketQuote]) -> dict[str, float]:
    return {
        s: gap
        for s in SECTOR_ETFS
        if (q := context.get(s)) is not None and (gap := q.gap_pct) is not None
    }


def _events_for(events: Sequence[EarningsEvent], symbol: str) -> tuple[EarningsEvent, ...]:
    return tuple(e for e in events if e.symbol == symbol)


def _summary(day: date, posture: Posture, picks: Sequence[Pick], meta: RunMeta) -> str:
    """The alert text. Notes are already scrubbed; posture reasons (model-written) are
    left out on purpose."""
    vix = posture.metrics.get("vix")
    level = posture.level.value + (f" (vix {vix:.1f})" if vix is not None else "")
    listed = ", ".join(
        f"{p.symbol} {'L' if p.side.value == 'long' else 'B'} {p.horizon.value} {p.score}"
        for p in picks
    )
    text = (
        f"traider research {KIND} {day.isoformat()}: posture {level}; {len(picks)} picks"
        f"{': ' + listed if listed else ''}; cost ${meta.cost_usd:.2f}"
    )
    if meta.status is RunStatus.PARTIAL and meta.notes:
        text += "; notes: " + "; ".join(meta.notes)
    return text
