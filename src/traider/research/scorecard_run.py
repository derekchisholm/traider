"""The scorecard run, every weekday after the close: how each recent pick actually did.

    lock(scorecard) -> market open today? -> already done today? -> META running
      -> picks from ok and partial runs over the last ``scorecard.lookback_days`` weekdays
      -> skip those already ``final`` -> daily bars per symbol -> the bot's event log
      -> score each pick -> write outcomes, the day's summary, then META -> alert

No model is called. A symbol whose bars cannot be read keeps its last outcome (or gets a
``pending`` one) and a note; half the symbols or more unreadable makes the run ``partial``.
An unreadable event-log day makes ``traded`` unknown (None) for the picks it covers, with a
note. A value an earlier scorecard knew is never overwritten with None: an unreadable log
day or a bar Schwab no longer returns keeps what was known. Anything else fails the run.
Nothing the bot reads depends on the scorecard.

Writes go outcomes, then the day's summary, then META last: META ``ok`` or ``partial``
means everything before it is in. A write that fails part-way fails the run (META
``failed``, no summary); the outcomes already written are each true as of this run, and
the next scorecard scores them again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar, Final

from traider.research.market import DailyBar
from traider.research.models import (
    OutcomeStatus,
    Pick,
    PickOutcome,
    RunKind,
    RunMeta,
    RunStatus,
    outcome_key,
)
from traider.research.run import (
    BARS_CONCURRENCY,
    EXIT_OK,
    RunBase,
    RunDeps,
    RunOutcome,
    _describe,
    new_run_id,
    run_locked,
)
from traider.research.scorecard import (
    Scored,
    score_pick,
    summarize,
    summary_text,
    traded_from_logs,
)
from traider.research.scrub import scrub
from traider.schwab.client import SchwabError
from traider.schwab.parse import ParseError
from traider.timeutil import previous_weekday, trading_date, weekdays_between, weekdays_from

log = logging.getLogger(__name__)

KIND: Final = "scorecard"
EVENT: Final = "research_scorecard"
BARS_SPARE_DAYS = 5  # history asked for beyond the oldest pick day, for holidays
MESSAGE_LIMIT = 8000  # the alert: the summary line plus at most MAX_NOTES scrubbed notes
# Every field an outcome may leave unknown. A known value there is never replaced by None.
KEEP_KNOWN: Final = tuple(
    name for name, info in PickOutcome.model_fields.items() if info.default is None
)
STATUS_ORDER: Final = (OutcomeStatus.PENDING, OutcomeStatus.PARTIAL, OutcomeStatus.FINAL)


@dataclass(frozen=True, slots=True)
class Entry:
    day: date
    pick: Pick
    run: RunMeta

    @property
    def key(self) -> str:
        return outcome_key(self.pick.run_id, self.pick.rank)

    @property
    def live_from(self) -> datetime:
        """When the bot could first see the pick: its run's META is written last."""
        return self.run.finished_at or self.run.started_at


async def run_scorecard(
    deps: RunDeps, now: datetime, *, dry_run: bool = False, force: bool = False
) -> RunOutcome:
    """One scorecard run. ``dry_run`` reads everything but writes nothing to the research
    table and sends no alert; ``force`` ignores an earlier ok or partial scorecard today."""
    now = now.astimezone(UTC)
    run_id = new_run_id(now, KIND)
    jobs = deps.settings.research_jobs
    if not jobs.enabled or not jobs.scorecard.enabled:
        log.info("the scorecard is switched off; nothing to do")
        detail = "research_jobs.enabled or research_jobs.scorecard.enabled is false"
        return RunOutcome("disabled", EXIT_OK, run_id, detail=detail)
    return await run_locked(ScorecardRun(deps, now, run_id, dry_run=dry_run), force=force)


class ScorecardRun(RunBase):
    kind: ClassVar[RunKind] = KIND
    lock_name: ClassVar[str] = KIND
    title: ClassVar[str] = "Scorecard"

    async def _execute(self, *, force: bool) -> RunOutcome:
        deps, day = self.deps, self.today.isoformat()
        self.stage = "market_hours"
        session = await deps.market.market_session(self.today)
        if session.open is None or session.close is None:
            log.info("no regular session on %s; nothing to score", day)
            return RunOutcome("closed", EXIT_OK, self.run_id, detail=f"market closed on {day}")
        if not force:
            done = await self.done_today()
            if done is not None:
                log.info("%s already has a scorecard (%s); skipping", day, done.run_id)
                return RunOutcome(
                    "skipped", EXIT_OK, self.run_id, detail=f"already done by {done.run_id}"
                )
        await self.begin(Decimal(0))  # no model calls

        self.stage = "picks"
        entries = await self.entries()
        existing = await deps.store.outcomes([e.key for e in entries])
        todo = [e for e in entries if _not_final(existing.get(e.key))]
        self.counts["picks"] = len(entries)
        self.counts["final_skipped"] = len(entries) - len(todo)

        self.stage = "bars"
        bars = await self.bars(todo)
        self.stage = "event_log"
        logs = await self.event_logs(todo)

        self.stage = "score"
        fresh: list[Scored] = []
        kept: list[Scored] = []
        kept_known = 0
        for entry in todo:
            history = bars.get(entry.pick.symbol)
            before = existing.get(entry.key)
            if history is None and before is not None:
                kept.append(Scored(before))  # unreadable bars: the last outcome stands
                continue
            end = min(entry.pick.expires_at, self.now)
            traded = (
                traded_from_logs(logs, entry.pick.symbol, entry.live_from, end)
                if logs is not None
                else None
            )
            scored = score_pick(
                entry.pick,
                pick_day=entry.day,
                run_status=entry.run.status,
                bars=history or (),
                today=self.today,
                traded=traded,
                now=self.now,
            )
            if history is None:  # never "final" for want of bars
                pending = scored.outcome.model_copy(update={"status": OutcomeStatus.PENDING})
                scored = Scored(pending)
            merged = keep_known(scored.outcome, before)
            if merged is not scored.outcome:
                kept_known += 1
                scored = Scored(merged, scored.matured)
            fresh.append(scored)
        finals = [Scored(o) for e in entries if (o := existing.get(e.key)) and not _not_final(o)]
        summary = summarize(
            [*fresh, *kept, *finals],
            day=self.today,
            run_id=self.run_id,
            kinds={e.run.run_id: e.run.kind for e in entries},
            now=self.now,
        )
        self.counts["scored"] = len(fresh)
        if kept_known:
            self.counts["outcomes_kept_known"] = kept_known
        for kind in OutcomeStatus:
            if count := sum(1 for s in fresh if s.outcome.status is kind):
                self.counts[f"outcome_{kind.value}"] = count

        self.stage = "write"
        await self.put_trail(
            "result.json",
            {
                "status": (RunStatus.PARTIAL if self.partial else RunStatus.OK).value,
                "summary": summary,
                "outcomes": [s.outcome for s in fresh],
                "notes": self.notes,
                "counts": self.counts,
            },
        )
        status = RunStatus.PARTIAL if self.partial else RunStatus.OK
        meta = self.meta(status)
        outcome = RunOutcome(
            status.value,
            EXIT_OK,
            self.run_id,
            meta=meta,
            extra={
                "summary": summary.model_dump(mode="json"),
                "outcomes": [s.outcome.model_dump(mode="json") for s in fresh],
            },
        )

        async def write() -> None:
            for scored in fresh:
                await deps.store.put_outcome(scored.outcome)
            await deps.store.put_score_summary(day, summary)
            await deps.store.put_meta(meta)  # last

        message = summary_text(summary)
        if meta.notes and status is RunStatus.PARTIAL:
            message += "; notes: " + "; ".join(meta.notes)
        # Counts and scrubbed notes only; scrubbed again as a whole all the same.
        return await self.commit(outcome, write, scrub(message, MESSAGE_LIMIT), event=EVENT)

    async def entries(self) -> list[Entry]:
        """Every pick of an ok or partial run from ``lookback_days`` weekdays ago to today."""
        start = self.today
        for _ in range(self.jobs.scorecard.lookback_days):
            start = previous_weekday(start)
        days = await self.deps.store.picks_between(start, self.today)
        entries: list[Entry] = []
        unreadable = 0
        for found in days:
            unreadable += found.invalid
            for pick in found.picks:
                run = found.runs.get(pick.run_id)
                if run is not None and run.status in (RunStatus.OK, RunStatus.PARTIAL):
                    entries.append(Entry(date.fromisoformat(found.day), pick, run))
        if unreadable:
            self.counts["unreadable_items"] = unreadable
        return entries

    async def bars(self, entries: Sequence[Entry]) -> dict[str, list[DailyBar] | None]:
        """Daily bars per symbol, from the oldest pick day to today. None: unreadable."""
        if not entries:
            return {}
        symbols = sorted({e.pick.symbol for e in entries})
        oldest = min(e.day for e in entries)
        days = weekdays_between(oldest, self.today) + 1 + BARS_SPARE_DAYS
        after_today = self.today + timedelta(days=1)  # today's bar included, if Schwab has it
        gate = asyncio.Semaphore(BARS_CONCURRENCY)

        async def one(symbol: str) -> list[DailyBar] | None:
            async with gate:
                try:
                    return await self.deps.market.daily_bars(symbol, after_today, days)
                except (SchwabError, ParseError) as exc:
                    log.warning("no daily bars for %s: %s", symbol, _describe(exc))
                    return None

        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(one(s)) for s in symbols]
        found = {s: task.result() for s, task in zip(symbols, tasks, strict=True)}
        if failed := sum(1 for bars in found.values() if bars is None):
            self.counts["bars_failed"] = failed
            self.note(
                f"daily bars unreadable for {failed} of {len(symbols)} symbol(s); "
                "their picks stay pending",
                partial=2 * failed >= len(symbols),
            )
        return found

    async def event_logs(
        self, entries: Sequence[Entry]
    ) -> Mapping[date, Sequence[Mapping[str, Any]] | None] | None:
        """The bot's event log for every day a pick was live. None without a state table;
        a day that cannot be read is None (``traded`` is then unknown)."""
        state = self.deps.state
        if state is None:
            if entries:
                self.note("no state table: whether the bot traded a pick is unknown")
            return None
        days = sorted(
            {
                day
                for e in entries
                for day in weekdays_from(
                    trading_date(e.live_from), trading_date(min(e.pick.expires_at, self.now))
                )
            }
        )
        logs: dict[date, Sequence[Mapping[str, Any]] | None] = {}
        failed = 0
        first_error = ""
        for day in days:
            try:
                logs[day] = await state.events(day.isoformat())
            except Exception as exc:
                logs[day] = None
                failed += 1
                first_error = first_error or _describe(exc)
        if failed:
            self.counts["event_log_failures"] = failed
            self.note(
                f"the bot's event log was unreadable for {failed} day(s), so traded is "
                f"unknown there: {first_error}"
            )
        return logs


def _not_final(outcome: PickOutcome | None) -> bool:
    return outcome is None or outcome.status is not OutcomeStatus.FINAL


def keep_known(fresh: PickOutcome, before: PickOutcome | None) -> PickOutcome:
    """``fresh``, with every value it does not know taken from ``before`` where that knew
    it, and the further of the two statuses. ``fresh`` itself when nothing is taken."""
    if before is None:
        return fresh
    known = {
        name: value
        for name in KEEP_KNOWN
        if getattr(fresh, name) is None and (value := getattr(before, name)) is not None
    }
    if not known:
        return fresh
    status = max(fresh.status, before.status, key=STATUS_ORDER.index)
    return fresh.model_copy(update={**known, "status": status})
