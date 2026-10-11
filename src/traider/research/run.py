"""The pre-market research run, start to finish.

    lock -> market open today? -> already done today? -> META running
      -> collect (Schwab + events) -> posture (code rules, then a model review; stricter wins)
      -> stand_aside? yes -> write the posture, no picks, ok
      -> screen (filters, stale history, features, pre_score, top K)
      -> deep-dives (tool loops, budgets, concurrency, deadline)
      -> earnings check (a symbol-scoped calendar call per swing idea)
      -> rank + validate -> add the cost -> write picks + posture + META ok|partial -> alert
    any exception -> META failed, alert, exit 1 (no posture, so the bot stands aside)

Exit codes: 0 ok, partial, skipped, closed or disabled; 1 failed; 2 the lock is held.
A run is ``partial`` when planned work did not happen: a budget stopped calls, the
deadline passed, the events vendor failed (an empty earnings calendar over a week or
more counts as failed), the model review failed, model calls failed in
half or more of the deep-dives, or a trail file could not be written. The bot
ignores partial runs by default.

Time: the lock lives ``max_run_s + LOCK_SPARE_S`` from the start. The whole run is boxed
to end ``RUN_BOX_MARGIN_S`` before that; hitting the box fails the run (no posture, the
cost recorded, the lock released). The soft deadline is ``max_run_s``: past it before the
screen, the posture is written with no picks; past it during the dives, no new dive starts
(both checked on ``RunDeps.monotonic``) and a hard stop on the event loop's clock cancels
dives still running. A cancelled model call's reservation is charged in full. Ranking gets
whatever time is left in the box.

Every text that reaches META or an alert (errors, notes) goes through ``scrub`` first:
vendor errors and model-written text can carry secrets or injected words.

``RunBase`` holds what every run kind shares: the lock (``run_locked``), META, the trail,
the cost meter and the day's cost, alerts, failure handling, the time box and the exit
codes. ``_Run`` is the pre-market kind; the scorecard and the intraday runs are kinds in
their own modules.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import traceback
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar, Final

from pydantic import BaseModel, ConfigDict

from traider.alerts import Alerter
from traider.research.botstate import BotState
from traider.research.cost import CostMeter
from traider.research.dive import DiveContext, DiveResult, run_dive
from traider.research.events import (
    EarningsEvent,
    EventsData,
    EventsUnavailable,
    NewsItem,
    Profile,
)
from traider.research.job_settings import DiveSettings, ScreenSettings
from traider.research.llm import LLM
from traider.research.market import DailyBar, MarketData, MarketQuote, QuoteBatch
from traider.research.models import Pick, Posture, PostureLevel, RunKind, RunMeta, RunStatus
from traider.research.posture import (
    REASON_MAX_CHARS,
    PostureDecision,
    decide_posture,
    posture_metrics,
)
from traider.research.rank import (
    RankInput,
    RankResult,
    Rejection,
    blended_score,
    rank_and_validate,
    share_class,
)
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
RUN_BOX_MARGIN_S = 60  # the whole run ends this long before the lock expires
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
# A market-wide earnings calendar with no rows over at least this many weekdays is taken
# as a vendor problem, not a quiet week: it counts as unavailable.
EMPTY_CALENDAR_WEEKDAYS = 5

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
    # The bot's ledger and event log, read-only. None without a state table.
    state: BotState | None = None


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
    extra: Mapping[str, Any] = field(default_factory=dict)  # more JSON-ready data to print

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
            **self.extra,
        }


def new_run_id(now: datetime, kind: str = KIND) -> str:
    return f"{kind}-{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(2)}"


def _describe(exc: BaseException) -> str:
    """An exception as text that is safe for logs, META and alerts."""
    return scrub(f"{type(exc).__name__}: {exc}")


class RunDeadline(Exception):
    """The whole run did not finish inside its time box."""


def _dive_deadline(max_run_s: float) -> float:
    """The hard stop for running deep-dives, on the event loop's clock."""
    return asyncio.get_running_loop().time() + max_run_s


def _run_deadline(max_run_s: float) -> float:
    """The time box for the whole run, on the event loop's clock: it ends a minute before
    the lock expires, so the failure can be recorded while the lock is still held."""
    return asyncio.get_running_loop().time() + max_run_s + LOCK_SPARE_S - RUN_BOX_MARGIN_S


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
    return await run_locked(_Run(deps, now, run_id, dry_run=dry_run), force=force)


async def run_locked(run: RunBase, *, force: bool) -> RunOutcome:
    """Run under the kind's lock, then each of ``also_locks``, in that order. Every lock
    lives ``max_run_s + LOCK_SPARE_S`` from the start and is released on every path (it
    expires by itself if the release fails). Any lock held elsewhere exits 2 and touches
    nothing but the locks this run took, which it gives back. A dry run takes no lock and
    runs with ``force``."""
    if run.dry_run:
        return await run.execute(force=True)
    store = run.deps.store
    taken: list[str] = []
    try:
        for name in (run.lock_name, *run.also_locks):
            try:
                acquired = await store.acquire_lock(
                    name, run.run_id, run.max_run_s + LOCK_SPARE_S, run.now
                )
            except Exception as exc:
                return await run.fail(exc)
            if not acquired:
                if name == run.lock_name:
                    log.warning("another %s run holds the lock; exiting", run.kind)
                    detail = "another run holds the lock"
                else:
                    log.warning("a %s run holds its lock; this %s run exits", name, run.kind)
                    detail = f"a {name} run holds its lock"
                return RunOutcome("locked", EXIT_LOCKED, run.run_id, detail=detail)
            taken.append(name)
        return await run.execute(force=force)
    finally:
        for name in reversed(taken):
            try:
                await store.release_lock(name, run.run_id)
            except Exception as exc:
                log.error("could not release the research lock (it expires by itself): %s",
                          _describe(exc))  # fmt: skip


class RunBase:
    """What every run kind shares: META, the trail, the cost meter and the day's cost,
    alerts, failure handling and the time box. A kind sets ``kind``, ``lock_name`` and
    ``title``, may change ``time_limit`` and ``models``, and implements ``_execute``."""

    kind: ClassVar[RunKind]
    lock_name: ClassVar[str]
    # Other kinds' locks this kind also holds for its whole run, so it never overlaps them.
    also_locks: ClassVar[tuple[str, ...]] = ()
    title: ClassVar[str]  # how each alert's subject starts

    def __init__(self, deps: RunDeps, now: datetime, run_id: str, *, dry_run: bool) -> None:
        self.deps = deps
        self.now = now
        self.today = trading_date(now)
        self.run_id = run_id
        self.dry_run = dry_run
        self.jobs = deps.settings.research_jobs
        self.started = deps.monotonic()
        self.max_run_s = self.time_limit()
        # Both from the same start as the lock's lifetime (the lock is taken right after).
        self.hard_stop = _dive_deadline(self.max_run_s)
        self.box = _run_deadline(self.max_run_s)
        self.stage = "start"
        self.notes: list[str] = []
        self.partial = False
        self.budget_noted = False
        self.counts: dict[str, int] = {}
        self.meter: CostMeter | None = None
        self.trail: Trail | None = None
        self.trail_failures = 0
        self.cost_added = False  # set only once add_day_cost has returned
        self.cost_task: asyncio.Task[Decimal] | None = None
        # Set once write_run has returned: META, picks and posture are in the table and a
        # later failure must not overwrite them.
        self.written: RunOutcome | None = None

    def time_limit(self) -> float:
        """The soft deadline, ``max_run_s``. The lock and the time box follow from it."""
        return self.jobs.max_run_s

    def models(self) -> tuple[str, ...]:
        """The models this kind may call, for META."""
        return ()

    # ------------------------------------------------------------------ helpers

    def note(self, text: str, *, partial: bool = False) -> None:
        clean = scrub(text)  # at most 300 characters, as RunMeta.notes requires
        log.warning("research %s: %s", self.run_id, clean)
        if len(self.notes) < MAX_NOTES:
            self.notes.append(clean)
        self.partial = self.partial or partial

    def meta(self, status: RunStatus, *, error: str = "", finished: bool = True) -> RunMeta:
        meter = self.meter
        return RunMeta(
            run_id=self.run_id,
            kind=self.kind,
            status=status,
            started_at=self.now,
            finished_at=self.deps.clock.now() if finished else None,
            trading_day=self.today,
            models=self.models(),
            cost_usd=meter.spent_usd if meter else Decimal(0),
            # The key prefix only: a bucket name carries the account id.
            s3_prefix=trail_prefix(self.today, self.run_id) if self.trail else "",
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
        box = asyncio.timeout_at(self.box)
        try:
            async with box:
                return await self._execute(force=force)
        except TimeoutError as exc:
            if not box.expired():
                return await self.fail(exc)
            limit = self.max_run_s + LOCK_SPARE_S - RUN_BOX_MARGIN_S
            return await self.fail(RunDeadline(f"the run did not finish within {limit:.0f}s"))
        except Exception as exc:
            return await self.fail(exc)

    async def _execute(self, *, force: bool) -> RunOutcome:
        raise NotImplementedError

    async def done_today(self) -> RunMeta | None:
        """The latest ok or partial run of this kind today, for skip-if-done."""
        self.stage = "skip_check"
        done = [
            m
            for m in await self.deps.store.runs_for_day(self.today.isoformat(), self.kind)
            if m.status in (RunStatus.OK, RunStatus.PARTIAL)
        ]
        return done[-1] if done else None

    async def begin(self, run_usd: Decimal) -> None:
        """Start the cost meter (held to ``run_usd`` and to what is left of the day's
        budget) and the trail, and write the running META (not on a dry run)."""
        self.stage = "start"
        budget = self.jobs.budget
        spent_today = await self.deps.store.day_cost(self.today.isoformat())
        self.meter = CostMeter(
            budget.prices, run_usd=run_usd, day_remaining_usd=budget.day_usd - spent_today
        )
        self.trail = self.deps.trail(trail_prefix(self.today, self.run_id))
        if not self.dry_run:
            await self.deps.store.put_meta(self.meta(RunStatus.RUNNING, finished=False))

    # ------------------------------------------------------------------- finish

    def _budget_notes(self) -> None:
        """Any budget stop makes the run partial, however it showed up."""
        assert self.meter is not None
        if self.meter.overrun:
            self.note("budget: a model call cost more than its reservation", partial=True)
        if self.meter.exhausted and not self.budget_noted:
            self.note("budget reached: the cost meter stopped further calls", partial=True)
            self.budget_noted = True

    async def commit(
        self,
        outcome: RunOutcome,
        write: Callable[[], Awaitable[None]],
        message: str,
        *,
        event: str | None = None,
        send: bool = True,
    ) -> RunOutcome:
        """Record a finished run: the day's cost first, then ``write`` (which writes META
        last), then the alert unless ``send`` is false. A dry run does none of it."""
        assert self.meter is not None
        assert outcome.meta is not None
        if not self.dry_run:
            # The cost first: a write that fails afterwards must not hide what was spent.
            # Shielded, so the time box cannot cut the add off halfway; fail() waits for
            # one still under way. Never under-counted: an add that raised is tried again.
            self.cost_task = asyncio.ensure_future(
                self.deps.store.add_day_cost(self.today.isoformat(), self.meter.spent_usd)
            )
            await asyncio.shield(self.cost_task)
            self.cost_added = True
            await write()
            self.written = outcome
            if send:
                await self.alert(outcome.meta.status, message, event=event)
        return outcome

    async def fail(self, exc: BaseException) -> RunOutcome:
        leaves = _leaves(exc)
        cause = leaves[0]
        error = scrub(f"{self.stage}: {type(cause).__name__}: {cause}")
        # Every failure is logged, scrubbed; the first goes to META. Frames only: the
        # exception's own text may carry vendor words.
        for leaf in leaves:
            frames = "".join(traceback.format_tb(leaf.__traceback__))
            log.error("research run %s failed in %s: %s\n%s", self.run_id, self.stage,
                      _describe(leaf), frames)  # fmt: skip
        if self.written is not None:
            # The result is in the table: META, picks and posture stand as written.
            written = self.written
            log.error("research run %s failed after its result was written as %s; "
                      "it stands", self.run_id, written.status)  # fmt: skip
            await self.alert(
                RunStatus.FAILED,
                f"traider research {self.kind} {self.today.isoformat()}: the run was written as "
                f"{written.status}, then failed: {error}. The written result stands.",
            )
            return replace(written, detail=error)
        meta = self.meta(RunStatus.FAILED, error=error)
        if not self.dry_run:
            await self._settle_cost()
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
                f"traider research {self.kind} {self.today.isoformat()} failed: {error}",
            )
        return RunOutcome("failed", EXIT_FAILED, self.run_id, meta=meta, detail=error)

    async def _settle_cost(self) -> None:
        """Wait for a cost add the time box interrupted, at most until half the margin
        before the lock expires is gone. If it finished, the cost is in; if it raised or
        is still going, fail() adds it (again): over-counting only makes budgets stricter."""
        task = self.cost_task
        if task is None or self.cost_added:
            return
        if not task.done():
            wait = self.box + RUN_BOX_MARGIN_S / 2 - asyncio.get_running_loop().time()
            await asyncio.wait({task}, timeout=max(wait, 0.0))  # never cancels the add
        if not task.done():
            log.error("the day's cost add did not finish in time; adding it again")
            return
        if task.cancelled():
            return
        if (exc := task.exception()) is not None:
            log.error("could not add the run's cost to the day: %s", _describe(exc))
            return
        self.cost_added = True

    async def alert(self, status: RunStatus, message: str, *, event: str | None = None) -> None:
        subject = f"{self.title} {self.today.isoformat()}: {status.value}"
        try:
            await self.deps.alerts.send(event or f"research_run_{status.value}", subject, message)
        except Exception as exc:
            log.error("could not send the research alert: %s", _describe(exc))


class _Run(RunBase):
    """The pre-market run. The intraday run reuses its stages, through the hooks below."""

    kind: ClassVar[RunKind] = KIND
    lock_name: ClassVar[str] = LOCK_NAME
    title: ClassVar[str] = "Research"
    intraday_dives: ClassVar[bool] = False  # tells the model it is an intraday idea

    def __init__(self, deps: RunDeps, now: datetime, run_id: str, *, dry_run: bool) -> None:
        super().__init__(deps, now, run_id, dry_run=dry_run)
        # The earnings calendar's window. Nothing is known about earnings after
        # ``calendar_end``, so no swing pick outlives its close.
        self.calendar_start = previous_weekday(self.today)
        self.calendar_end = weekdays_after(self.today, self.jobs.collect.earnings_lookahead_days)

    def models(self) -> tuple[str, ...]:
        dive = self.jobs.dive
        return tuple(dict.fromkeys((dive.posture_model, dive.model)))

    def screen_settings(self) -> ScreenSettings:
        return self.jobs.screen

    def dive_settings(self) -> DiveSettings:
        return self.jobs.dive

    def alert_wanted(self, status: RunStatus, posture: Posture, picks: Sequence[Pick]) -> bool:
        return True

    def summary(self, posture: Posture, picks: Sequence[Pick], meta: RunMeta) -> str:
        """The alert text, from code-made values and scrubbed notes only."""
        return _summary(self.kind, self.today, posture, picks, meta)

    # --------------------------------------------------------------------- flow

    async def _execute(self, *, force: bool) -> RunOutcome:
        deps, day = self.deps, self.today.isoformat()
        self.stage = "market_hours"
        session = await deps.market.market_session(self.today)
        if session.open is None or session.close is None:
            log.info("no regular session on %s; nothing to research", day)
            return RunOutcome("closed", EXIT_OK, self.run_id, detail=f"market closed on {day}")
        if not force:
            done = await self.done_today()
            if done is not None:
                log.info("%s already has a %s run (%s); skipping", day, self.kind, done.run_id)
                return RunOutcome(
                    "skipped", EXIT_OK, self.run_id, detail=f"already done by {done.run_id}"
                )
        await self.begin(self.jobs.budget.run_usd)

        self.stage = "collect"
        snapshot = await self.collect()
        await self.put_trail("snapshot.json", snapshot)

        self.stage = "posture"
        decision = await self.decide(snapshot)
        posture = Posture(
            level=decision.level,
            # Model-written reasons can echo injected news: scrubbed like every other text.
            reasons=tuple(scrub(r, REASON_MAX_CHARS) for r in decision.reasons),
            run_id=self.run_id,
            at=self.now,
            metrics=decision.metrics.as_dict(),
        )
        await self.put_trail(
            "posture.json", {"posture": posture, "notes": [scrub(n) for n in decision.notes]}
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

        by_symbol = {row.symbol: row for row in top}
        self.stage = "earnings_check"
        earnings, confirmed = await self.confirm_earnings(snapshot, results, by_symbol)

        self.stage = "rank"
        inputs = [
            RankInput(
                symbol=r.symbol,
                assessment=r.assessment,
                pre_score=by_symbol[r.symbol].pre_score,
                features=by_symbol[r.symbol].features.as_dict(),
                atr=by_symbol[r.symbol].atr,
                sector=p.industry if (p := profiles.get(r.symbol)) else None,
                earnings=earnings.get(r.symbol, ()),
                earnings_confirmed=r.symbol in confirmed,
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
            earnings = await deps.events.earnings_calendar(self.calendar_start, self.calendar_end)
        except EventsUnavailable as exc:
            earnings_ok = False
            self.note(f"earnings calendar unavailable, no swing picks: {exc}", partial=True)
        else:
            span = weekdays_between(self.calendar_start, self.calendar_end) + 1
            if not earnings and span >= EMPTY_CALENDAR_WEEKDAYS:
                # No company reporting for a week or more is not believable: an empty
                # reply must not read as "no earnings ahead".
                earnings_ok = False
                self.note(
                    f"earnings calendar returned nothing for {span} weekdays; treated as "
                    "unavailable, no swing picks",
                    partial=True,
                )
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
        spy_bars = snapshot.spy_bars
        if spy_bars and _stale(spy_bars, self.today):
            # Old bars would describe an old market: the SPY metrics count as missing, so
            # the code posture stands aside with "missing data" as its reason.
            self.note(f"SPY daily history is stale (last bar {spy_bars[-1].day.isoformat()})")
            spy_bars = []
        decision = await decide_posture(
            self.deps.llm,
            self.meter,
            model=self.jobs.dive.posture_model,
            max_tokens=self.jobs.dive.max_tokens,
            metrics=posture_metrics(snapshot.context, spy_bars),
            today=self.today,
            settings=self.jobs.posture,
            sector_gaps=_sector_gaps(snapshot.context),
            headlines=snapshot.market_news,
        )
        # A failed review is partial: the posture is only the code's (made at least
        # reduced), not what the run planned.
        for text in decision.notes:
            self.note(text, partial=decision.budget_hit or decision.review_failed)
        self.budget_noted = self.budget_noted or decision.budget_hit
        return decision

    # ------------------------------------------------------------------- screen

    async def screen(
        self, snapshot: Snapshot
    ) -> tuple[
        list[ScreenRow], dict[str, MarketQuote], dict[str, list[DailyBar]], dict[str, Profile]
    ]:
        deps, jobs, today = self.deps, self.jobs, self.today
        settings = self.screen_settings()
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
            if _stale(history, today):
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

        if errors := sum(1 for r in dropped.values() if r == DROP_HISTORY_ERROR):
            # Half or more unreadable looks like an outage, not a few odd names.
            self.note(
                f"daily history unavailable for {errors} of {len(passing)} name(s)",
                partial=2 * errors >= len(passing),
            )

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

        # A TaskGroup: an unexpected error cancels the other reads, then fails the run.
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(one(s)) for s in symbols]
        return {s: task.result() for s, task in zip(symbols, tasks, strict=True)}

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
        deps = self.deps
        assert self.meter is not None
        meter = self.meter
        settings = self.dive_settings()
        gate = asyncio.Semaphore(settings.dive_concurrency)
        deadline = self.started + self.max_run_s
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
                    intraday=self.intraday_dives,
                )
                # Cancelled by the hard stop, run_dive charges the call in flight and
                # re-raises; the task then ends cancelled.
                result = await run_dive(
                    ctx,
                    market=deps.market,
                    events=deps.events,
                    llm=deps.llm,
                    meter=meter,
                    settings=settings,
                )
                await self.put_trail(f"dives/{row.symbol}.json", result.trail(settings.model))
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
        ended = Counter(r.outcome for r in results)
        for name, count in sorted(ended.items()):
            if name != "submitted":
                self.counts[f"dive_{name}"] = count
        timed_out = ended["timeout"]
        if failed := ended["llm_error"] + timed_out:
            # Half or more failing or timing out looks like the model being unreachable or
            # stuck, not one bad call.
            self.note(
                f"model calls failed in {failed} of {len(results)} deep-dive(s)"
                + (f" ({timed_out} timed out)" if timed_out else ""),
                partial=2 * failed >= len(results),
            )
        return results

    # --------------------------------------------------------- earnings check

    async def confirm_earnings(
        self,
        snapshot: Snapshot,
        results: Sequence[DiveResult],
        rows: Mapping[str, ScreenRow],
    ) -> tuple[dict[str, tuple[EarningsEvent, ...]], set[str]]:
        """Each name's earnings, and which swing ideas a symbol-scoped calendar call
        confirmed. The market-wide calendar can miss a name; a swing pick needs its own
        check. At most ``rank.max_picks`` calls, best ideas first, one at a time. A failed
        call refuses that name's swing pick only: noted, but not partial on its own."""
        earnings = {r.symbol: _events_for(snapshot.earnings, r.symbol) for r in results}
        confirmed: set[str] = set()
        if not snapshot.earnings_ok:
            return earnings, confirmed  # no swing picks at all
        settings = self.jobs.rank

        def order(r: DiveResult) -> tuple[int, int, str]:
            assert r.assessment is not None
            pre = rows[r.symbol].pre_score
            return (-blended_score(r.assessment.score, pre, settings.llm_weight), -pre, r.symbol)

        swing = sorted(
            (
                r
                for r in results
                if r.assessment is not None
                and r.assessment.side != "pass"
                and r.assessment.horizon == "swing"
                and not share_class(r.symbol)  # refused anyway
            ),
            key=order,
        )[: settings.max_picks]
        failed: list[str] = []
        first_error = ""
        for r in swing:
            try:
                found = await self.deps.events.earnings_calendar(
                    self.calendar_start, self.calendar_end, r.symbol
                )
            except EventsUnavailable as exc:
                failed.append(r.symbol)
                first_error = first_error or str(exc)
                continue
            merged = dict.fromkeys((*earnings[r.symbol], *_events_for(found, r.symbol)))
            earnings[r.symbol] = tuple(sorted(merged, key=lambda e: e.day))
            confirmed.add(r.symbol)
        if swing:
            self.counts["earnings_checks"] = len(swing)
        if failed:
            self.counts["earnings_check_failures"] = len(failed)
            self.note(
                f"earnings check failed for {len(failed)} name(s), no swing pick for them: "
                f"{first_error}"
            )
        return earnings, confirmed

    # ------------------------------------------------------------------- finish

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
        outcome = RunOutcome(
            status.value,
            EXIT_OK,
            self.run_id,
            meta=meta,
            posture=posture,
            picks=ranked.picks,
            rejected=ranked.rejected,
        )

        async def write() -> None:
            await self.deps.store.write_run(meta, ranked.picks, posture)

        return await self.commit(
            outcome,
            write,
            self.summary(posture, ranked.picks, meta),
            send=self.alert_wanted(status, posture, ranked.picks),
        )


def _sector_gaps(context: Mapping[str, MarketQuote]) -> dict[str, float]:
    return {
        s: gap
        for s in SECTOR_ETFS
        if (q := context.get(s)) is not None and (gap := q.gap_pct) is not None
    }


def _leaves(exc: BaseException) -> list[BaseException]:
    """Every exception inside ``exc``, groups opened (in order), or ``exc`` itself."""
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        return [leaf for sub in exc.exceptions for leaf in _leaves(sub)]
    return [exc]


def _stale(bars: Sequence[DailyBar], today: date) -> bool:
    return weekdays_between(bars[-1].day, today) > MAX_BAR_AGE_WEEKDAYS


def _events_for(events: Sequence[EarningsEvent], symbol: str) -> tuple[EarningsEvent, ...]:
    return tuple(e for e in events if e.symbol == symbol)


def _summary(kind: str, day: date, posture: Posture, picks: Sequence[Pick], meta: RunMeta) -> str:
    """The alert text. Notes are already scrubbed; posture reasons (model-written) are
    left out on purpose."""
    vix = posture.metrics.get("vix")
    level = posture.level.value + (f" (vix {vix:.1f})" if vix is not None else "")
    listed = ", ".join(
        f"{p.symbol} {'L' if p.side.value == 'long' else 'B'} {p.horizon.value} {p.score}"
        for p in picks
    )
    text = (
        f"traider research {kind} {day.isoformat()}: posture {level}; {len(picks)} picks"
        f"{': ' + listed if listed else ''}; cost ${meta.cost_usd:.2f}"
    )
    if meta.status is RunStatus.PARTIAL and meta.notes:
        text += "; notes: " + "; ".join(meta.notes)
    return text
