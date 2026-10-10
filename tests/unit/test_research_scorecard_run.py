"""The scorecard run end to end, on fakes: a week of picks, their bars and the bot's event
log, then every way it can go wrong. Nothing here touches Schwab or AWS."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal

from tests.fakes.research import FakeEvents, FakeMarketData, ScriptedLLM
from tests.unit.test_research_run import Trails
from traider.alerts import LogAlerter
from traider.research.market import DailyBar
from traider.research.models import (
    OutcomeStatus,
    Pick,
    PickOutcome,
    RunMeta,
    RunStatus,
    ScoreSummary,
)
from traider.research.rank import close_of
from traider.research.run import RunDeps
from traider.research.scorecard_run import run_scorecard
from traider.research.store import MemoryResearchStore
from traider.schwab.client import SchwabUnavailable
from traider.settings import Settings
from traider.state.memory import MemoryStateStore
from traider.timeutil import ManualClock

NOW = datetime(2026, 10, 9, 20, 30, tzinfo=UTC)  # Friday 16:30 New York
TODAY = date(2026, 10, 9)
DAY = TODAY.isoformat()
MON, TUE, WED, THU = (date(2026, 10, d) for d in (5, 6, 7, 8))


def bar(day, o, h, low, c) -> DailyBar:
    return DailyBar(day=day, open=o, high=h, low=low, close=c, volume=1_000_000)


def week(start: float) -> list[DailyBar]:
    """Monday to Friday: up 2% on Monday, then drifting; Friday is today."""
    s = start / 100
    return [
        bar(MON, 100 * s, 103 * s, 99 * s, 102 * s),
        bar(TUE, 102 * s, 104 * s, 98 * s, 101 * s),
        bar(WED, 101 * s, 106 * s, 100 * s, 105 * s),
        bar(THU, 105 * s, 107 * s, 103 * s, 104 * s),
        bar(TODAY, 104 * s, 105 * s, 94 * s, 96 * s),
    ]


def pick(symbol, rank, run_id, *, side="long", horizon="intraday", expires=MON, score=80):
    return Pick(
        run_id=run_id,
        rank=rank,
        symbol=symbol,
        side=side,
        horizon=horizon,
        score=score,
        pre_score=70,
        thesis="t",
        invalidation=Decimal("95") if side == "long" else Decimal("120"),
        expires_at=close_of(expires),
        features={"llm_score": 85.0, "price_at_pick": 100.0},
    )


def run_meta(run_id, day, *, kind="premarket", status="ok") -> RunMeta:
    started = datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=12)
    return RunMeta(
        run_id=run_id,
        kind=kind,
        status=status,
        started_at=started,
        finished_at=started.replace(minute=10),
        trading_day=day,
    )


async def a_week(store: MemoryResearchStore) -> None:
    """Monday's pre-market run: NVDA long swing to Friday, AMD bearish intraday. Tuesday's
    partial run: TSLA. Wednesday's failed run: MSFT (never scored). Thursday's intraday
    run: PLTR."""
    await store.write_run(
        run_meta("premarket-a", MON),
        [
            pick("NVDA", 1, "premarket-a", horizon="swing", expires=TODAY, score=84),
            pick("AMD", 2, "premarket-a", side="bearish", score=72),
        ],
        None,
    )
    await store.write_run(
        run_meta("premarket-p", TUE, status="partial"),
        [pick("TSLA", 1, "premarket-p", expires=TUE)],
        None,
    )
    await store.write_run(
        run_meta("premarket-f", WED, status="failed"),
        [pick("MSFT", 1, "premarket-f", expires=WED)],
        None,
    )
    await store.write_run(
        run_meta("intraday-i", THU, kind="intraday"),
        [pick("PLTR", 1, "intraday-i", expires=THU, score=91)],
        None,
    )


def market() -> FakeMarketData:
    m = FakeMarketData()
    m.bars = {"NVDA": week(100), "AMD": week(50), "TSLA": week(200), "PLTR": week(20)}
    return m


async def bot_log() -> MemoryStateStore:
    """The bot bought NVDA shares on Monday and a put on AMD... on Tuesday, after AMD's
    intraday pick had expired."""
    state = MemoryStateStore("paper")
    buy = {"side": "BUY", "quantity": 1, "order_type": "LIMIT"}
    await state.log_event(
        "order_submitted", {**buy, "symbol": "NVDA"}, datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
    )
    await state.log_event(
        "order_submitted",
        {**buy, "symbol": "AMD   261016P00048000"},
        datetime(2026, 10, 6, 14, 0, tzinfo=UTC),
    )
    return state


async def deps(*, store=None, m=None, state=None, settings=None) -> RunDeps:
    if store is None:
        store = MemoryResearchStore()
        await a_week(store)
    return RunDeps(
        store=store,
        market=m or market(),
        events=FakeEvents(),
        llm=ScriptedLLM(),
        trail=Trails(),
        alerts=LogAlerter(),
        settings=settings or Settings(),
        clock=ManualClock(NOW),
        monotonic=lambda: 0.0,
        state=state if state is not None else await bot_log(),
    )


def stored(store, key) -> PickOutcome:
    return PickOutcome.model_validate_json(store.raw(key, "OUTCOME")["body"])


def stored_meta(store, run_id) -> RunMeta:
    return RunMeta.model_validate(json.loads(store.raw(f"RUN#{run_id}", "META")["body"]))


# --- the golden week -----------------------------------------------------------------------


async def test_a_week_of_picks_is_scored():
    d = await deps()
    outcome = await run_scorecard(d, NOW)
    assert (outcome.status, outcome.exit_code) == ("ok", 0)
    assert outcome.run_id.startswith("scorecard-20261009T203000Z-")

    nvda = stored(d.store, "PICK#premarket-a#001")
    assert (nvda.entry, nvda.ret_1d, nvda.ret_5d, nvda.ret_20d) == (100.0, 2.0, -4.0, None)
    assert (nvda.mfe_pct, nvda.mae_pct, nvda.hit_invalidation) == (7.0, -6.0, True)
    assert (nvda.expired_return, nvda.traded, nvda.status) == (-4.0, True, OutcomeStatus.PARTIAL)
    assert (nvda.llm_score, nvda.price_at_pick) == (85, 100.0)

    amd = stored(d.store, "PICK#premarket-a#002")
    assert (amd.ret_0d, amd.ret_1d, amd.expired_return) == (-2.0, -2.0, -2.0)
    assert amd.traded is False  # the put was bought after the intraday pick expired

    tsla = stored(d.store, "PICK#premarket-p#001")
    assert (tsla.run_status, tsla.entry, tsla.ret_0d) == (RunStatus.PARTIAL, 204.0, -0.9804)
    assert d.store.raw("PICK#premarket-f#001", "OUTCOME") is None  # a failed run's pick

    summary = ScoreSummary.model_validate_json(d.store.raw(f"SCORE#{DAY}", "SUMMARY")["body"])
    assert summary.picks == 4
    assert summary.by_kind == {"intraday": 1, "premarket": 3}
    assert summary.by_side == {"bearish": 1, "long": 3}
    # Matured today: Monday's 5-day returns (NVDA -4%, AMD bearish +4%); no pick was made
    # today, so no 1-day return matured.
    assert (summary.ret_1d.matured, summary.ret_5d.matured) == (0, 2)
    assert (summary.ret_5d.hits, summary.ret_5d.mean_pct) == (1, 0.0)

    meta = stored_meta(d.store, outcome.run_id)
    assert (meta.kind, meta.status, meta.models, meta.cost_usd) == (
        "scorecard",
        RunStatus.OK,
        (),
        Decimal(0),
    )
    assert meta.counts == {"picks": 4, "final_skipped": 0, "scored": 4, "outcome_partial": 4}
    ((event, subject, message),) = d.alerts.sent
    assert (event, subject) == ("research_scorecard", f"Scorecard {DAY}: ok")
    assert message == (
        "traider scorecard 2026-10-09: no picks matured 1d; 2 picks matured 5d, hit 1/2, "
        "mean +0.0%; 4 picks in the window"
    )
    assert ("LOCK#scorecard", "LOCK") not in d.store.keys
    result = d.trail.only().files["result.json"]
    assert result["status"] == "ok" and len(result["outcomes"]) == 4


async def test_the_bot_never_sees_the_scorecard():
    from traider.config import ResearchSettings
    from traider.research.source import ResearchSource

    d = await deps()
    await run_scorecard(d, NOW)
    source = ResearchSource(d.store, ResearchSettings)
    await source.refresh(NOW)
    assert "scorecard" not in {run.kind for run in (await d.store.day(DAY)).runs.values()}
    assert source.view.posture is None


async def test_final_outcomes_are_skipped_and_still_summarised():
    d = await deps()
    final = stored_outcome_final()
    await d.store.put_outcome(final)
    outcome = await run_scorecard(d, NOW)
    assert stored(d.store, final.key) == final  # not rewritten
    assert outcome.meta.counts["final_skipped"] == 1
    assert "NVDA" not in d.market.called("daily_bars")
    summary = ScoreSummary.model_validate_json(d.store.raw(f"SCORE#{DAY}", "SUMMARY")["body"])
    assert summary.picks == 4


def stored_outcome_final() -> PickOutcome:
    return PickOutcome(
        run_id="premarket-a",
        rank=1,
        symbol="NVDA",
        side="long",
        horizon="swing",
        score=84,
        pre_score=70,
        pick_day=MON,
        run_status="ok",
        status="final",
        updated_at=NOW,
    )


async def test_picks_older_than_the_lookback_are_left_alone():
    store = MemoryResearchStore()
    await a_week(store)
    old_day = date(2026, 8, 27)  # 31 weekdays before today
    await store.write_run(
        run_meta("premarket-o", old_day), [pick("OLD", 1, "premarket-o", expires=old_day)], None
    )
    edge_day = date(2026, 8, 28)  # 30 weekdays before today
    await store.write_run(
        run_meta("premarket-e", edge_day), [pick("EDGE", 1, "premarket-e", expires=edge_day)], None
    )
    d = await deps(
        store=store, settings=Settings(research_jobs={"scorecard": {"lookback_days": 30}})
    )
    await run_scorecard(d, NOW)
    assert store.raw("PICK#premarket-o#001", "OUTCOME") is None
    # Scored, and with no bars at 30 weekdays old it never will be priced: final.
    assert stored(store, "PICK#premarket-e#001").status is OutcomeStatus.FINAL


# --- failures ----------------------------------------------------------------------------


class SomeBarsFail(FakeMarketData):
    def __init__(self, failing):
        super().__init__()
        self.failing = set(failing)

    async def daily_bars(self, symbol, before, days):
        if symbol in self.failing:
            self.calls.append(("daily_bars", symbol))
            raise SchwabUnavailable("GET /marketdata/v1/pricehistory: HTTP 503")
        return await super().daily_bars(symbol, before, days)


async def test_a_symbol_without_bars_stays_pending_with_a_note():
    m = SomeBarsFail({"TSLA"})
    m.bars = market().bars
    d = await deps(m=m)
    outcome = await run_scorecard(d, NOW)
    assert outcome.status == "ok"
    assert stored(d.store, "PICK#premarket-p#001").status is OutcomeStatus.PENDING
    assert outcome.meta.counts["bars_failed"] == 1
    assert (
        "daily bars unreadable for 1 of 4 symbol(s); their picks stay pending" in outcome.meta.notes
    )


async def test_unreadable_bars_keep_the_last_outcome():
    m = SomeBarsFail({"NVDA"})
    m.bars = market().bars
    d = await deps(m=m)
    before = stored_outcome_final().model_copy(
        update={"status": OutcomeStatus.PARTIAL, "ret_1d": 2.0}
    )
    await d.store.put_outcome(before)
    await run_scorecard(d, NOW)
    assert stored(d.store, before.key) == before


async def test_half_the_symbols_without_bars_is_partial():
    m = SomeBarsFail({"NVDA", "AMD"})
    m.bars = market().bars
    d = await deps(m=m)
    outcome = await run_scorecard(d, NOW)
    assert outcome.status == "partial"
    assert stored_meta(d.store, outcome.run_id).status is RunStatus.PARTIAL
    ((_, subject, message),) = d.alerts.sent
    assert subject == f"Scorecard {DAY}: partial"
    assert "notes: daily bars unreadable for 2 of 4 symbol(s)" in message


async def test_an_unreadable_event_log_day_makes_traded_unknown():
    class Flaky(MemoryStateStore):
        async def events(self, day):
            if day == "2026-10-06":
                raise RuntimeError("throttled")
            return await super().events(day)

    state = Flaky("paper")
    state._data = (await bot_log())._data
    d = await deps(state=state)
    outcome = await run_scorecard(d, NOW)
    assert outcome.status == "ok"
    assert stored(d.store, "PICK#premarket-a#001").traded is True  # found on Monday
    assert stored(d.store, "PICK#premarket-p#001").traded is None  # Tuesday unreadable
    assert stored(d.store, "PICK#premarket-a#002").traded is False  # Monday only
    assert outcome.meta.counts["event_log_failures"] == 1


async def test_without_a_state_table_traded_is_unknown():
    d = await deps(state=MemoryStateStore())
    d.state = None
    outcome = await run_scorecard(d, NOW)
    assert stored(d.store, "PICK#premarket-a#001").traded is None
    assert "no state table: whether the bot traded a pick is unknown" in outcome.meta.notes


async def test_anything_else_fails_the_run():
    m = market()
    m.failures["daily_bars"] = RuntimeError("bug")
    d = await deps(m=m)
    outcome = await run_scorecard(d, NOW)
    assert (outcome.status, outcome.exit_code) == ("failed", 1)
    meta = stored_meta(d.store, outcome.run_id)
    assert (meta.status, meta.error) == (RunStatus.FAILED, "bars: RuntimeError: bug")
    assert d.store.raw(f"SCORE#{DAY}", "SUMMARY") is None
    ((event, _, message),) = d.alerts.sent
    assert event == "research_run_failed"
    assert message == f"traider research scorecard {DAY} failed: bars: RuntimeError: bug"
    assert ("LOCK#scorecard", "LOCK") not in d.store.keys


# --- when it runs ------------------------------------------------------------------------


async def test_a_closed_market_writes_nothing():
    m = market()
    m.open_today = False
    d = await deps(m=m)
    before = set(d.store.keys)
    outcome = await run_scorecard(d, NOW)
    assert outcome.status == "closed" and set(d.store.keys) == before


async def test_once_a_day_unless_forced():
    d = await deps()
    first = await run_scorecard(d, NOW)
    again = await run_scorecard(d, NOW)
    assert again.status == "skipped" and first.run_id in again.detail
    forced = await run_scorecard(d, NOW, force=True)
    assert forced.status == "ok"


async def test_switched_off_means_no_run():
    for jobs in ({"enabled": False}, {"scorecard": {"enabled": False}}):
        d = await deps(settings=Settings(research_jobs=jobs))
        before = set(d.store.keys)
        outcome = await run_scorecard(d, NOW)
        assert (outcome.status, outcome.exit_code) == ("disabled", 0)
        assert set(d.store.keys) == before


async def test_a_held_lock_exits_2():
    d = await deps()
    await d.store.acquire_lock("scorecard", "someone", 3600, NOW)
    outcome = await run_scorecard(d, NOW)
    assert (outcome.status, outcome.exit_code) == ("locked", 2)


async def test_the_premarket_lock_does_not_block_the_scorecard():
    d = await deps()
    await d.store.acquire_lock("premarket", "someone", 3600, NOW)
    assert (await run_scorecard(d, NOW)).status == "ok"


async def test_a_dry_run_reads_everything_and_writes_nothing():
    d = await deps()
    before = set(d.store.keys)
    outcome = await run_scorecard(d, NOW, dry_run=True)
    assert outcome.status == "ok" and set(d.store.keys) == before
    assert d.alerts.sent == []
    report = outcome.report()
    assert report["summary"]["picks"] == 4 and len(report["outcomes"]) == 4


def test_the_scorecard_never_imports_order_code():
    from pathlib import Path

    from traider.research import botstate, scorecard, scorecard_run

    for module in (scorecard, scorecard_run, botstate):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "place_order" not in source and "broker" not in source, module.__name__


# --- what is already known stays known ------------------------------------------------------


class Unreadable(MemoryStateStore):
    """The bot's event log, with some days that cannot be read."""

    def __init__(self, days, error="throttled"):
        super().__init__("paper")
        self.days = set(days)
        self.error = error

    async def events(self, day):
        if day in self.days:
            raise RuntimeError(self.error)
        return await super().events(day)


async def test_an_unreadable_log_never_forgets_that_a_pick_was_traded():
    state = Unreadable({"2026-10-05"})  # Monday, the day the bot bought NVDA
    state._data = (await bot_log())._data
    d = await deps(state=state)
    before = stored_outcome_final().model_copy(
        update={"status": OutcomeStatus.PARTIAL, "traded": True, "ret_1d": 2.0}
    )
    await d.store.put_outcome(before)
    await run_scorecard(d, NOW)
    after = stored(d.store, before.key)
    assert after.traded is True  # known before, unknown now: kept
    assert (after.ret_5d, after.updated_at) == (-4.0, NOW)  # what is new is still written


async def test_bars_that_lost_the_pick_day_keep_the_known_prices():
    m = market()
    m.bars["NVDA"] = m.bars["NVDA"][1:]  # Schwab no longer returns Monday's bar
    d = await deps(m=m)
    before = stored_outcome_final().model_copy(
        update={
            "status": OutcomeStatus.PARTIAL,
            "entry": 100.0,
            "ret_1d": 2.0,
            "mfe_pct": 3.0,
            "traded": True,
        }
    )
    await d.store.put_outcome(before)
    outcome = await run_scorecard(d, NOW)
    after = stored(d.store, before.key)
    assert (after.entry, after.ret_1d, after.mfe_pct, after.traded) == (100.0, 2.0, 3.0, True)
    assert after.status is OutcomeStatus.PARTIAL  # not back to pending
    assert outcome.meta.counts["outcomes_kept_known"] == 1


async def test_a_pick_scored_for_the_first_time_has_nothing_to_keep():
    d = await deps(state=Unreadable({"2026-10-05"}))
    await run_scorecard(d, NOW)
    assert stored(d.store, "PICK#premarket-a#001").traded is None


# --- writing ------------------------------------------------------------------------------


class Recording(MemoryResearchStore):
    def __init__(self, fail_on=None):
        super().__init__()
        self.puts = []
        self.fail_on = fail_on

    async def put_outcome(self, outcome):
        self.puts.append(("outcome", outcome.key))
        if self.fail_on == len(self.puts):
            raise RuntimeError("write throttled")
        await super().put_outcome(outcome)

    async def put_score_summary(self, day, summary):
        self.puts.append(("summary", day))
        await super().put_score_summary(day, summary)

    async def put_meta(self, meta):
        self.puts.append(("meta", meta.status.value))
        await super().put_meta(meta)


async def test_outcomes_then_the_summary_then_meta_last():
    store = Recording()
    await a_week(store)
    d = await deps(store=store)
    await run_scorecard(d, NOW)
    kinds = [kind for kind, _ in store.puts]
    assert kinds == ["meta", "outcome", "outcome", "outcome", "outcome", "summary", "meta"]
    assert store.puts[0] == ("meta", "running") and store.puts[-1] == ("meta", "ok")


async def test_a_write_that_fails_part_way_is_a_failed_run_with_no_summary():
    store = Recording(fail_on=3)  # the second outcome (the first put is the running META)
    await a_week(store)
    d = await deps(store=store)
    outcome = await run_scorecard(d, NOW)
    assert (outcome.status, outcome.exit_code) == ("failed", 1)
    assert store.raw(f"SCORE#{DAY}", "SUMMARY") is None
    assert stored_meta(store, outcome.run_id).status is RunStatus.FAILED
    ((event, _, _),) = d.alerts.sent
    assert event == "research_run_failed"


async def test_an_event_log_error_reaches_meta_and_the_alert_scrubbed():
    secret = "Bearer eyJhbGciOiJIUzI1NiJ9.c2VjcmV0LXRva2VuLXZhbHVl"
    state = Unreadable({"2026-10-06"}, error=f"authorization: {secret}")
    state._data = (await bot_log())._data
    m = SomeBarsFail({"NVDA", "AMD"})  # partial, so the notes go in the alert
    m.bars = market().bars
    d = await deps(state=state, m=m)
    outcome = await run_scorecard(d, NOW)
    assert outcome.status == "partial"
    ((_, _, message),) = d.alerts.sent
    written = d.store.raw(f"RUN#{outcome.run_id}", "META")["body"]
    assert "event log was unreadable for 1 day(s)" in message
    for text in (message, written, *outcome.meta.notes):
        assert "eyJhbGciOiJIUzI1NiJ9" not in text and "c2VjcmV0" not in text
