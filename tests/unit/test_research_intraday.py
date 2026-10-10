"""The intraday run end to end, on fakes: a morning posture, the bot's ledger, the movers
at 11:00, then every way it can go wrong. Nothing here touches Schwab, Finnhub, Bedrock
or AWS."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

import traider.research.run as run_module
from tests.fakes.research import (
    PLTR_LONG,
    ScriptedLLM,
    market_day,
    submit,
)
from tests.unit.test_research_run import StepClock, Trails
from tests.unit.test_research_run import deps as premarket_deps
from traider.alerts import LogAlerter
from traider.config import ResearchSettings
from traider.research.dive import INTRADAY_LINE
from traider.research.intraday import IntradayRun, latest_ok_posture, run_intraday
from traider.research.job_settings import DEFAULT_MODEL
from traider.research.models import Pick, Posture, PostureLevel, RunMeta
from traider.research.rank import close_of
from traider.research.run import (
    LOCK_SPARE_S,
    RUN_BOX_MARGIN_S,
    RunDeps,
    run_locked,
    run_premarket,
)
from traider.research.source import STRICTNESS, ResearchSource
from traider.research.store import MemoryResearchStore
from traider.settings import Settings
from traider.state.base import LedgerEntry
from traider.state.memory import MemoryStateStore
from traider.timeutil import ManualClock

NOW = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)  # Friday 11:00 New York
DAY = "2026-10-09"
MORNING = datetime(2026, 10, 9, 12, 10, tzinfo=UTC)
PLTR_SWING = PLTR_LONG | {"horizon": "swing", "swing_days": 3}


def morning_meta(run_id="premarket-m", status="ok", kind="premarket") -> RunMeta:
    return RunMeta(
        run_id=run_id,
        kind=kind,
        status=status,
        started_at=MORNING,
        finished_at=MORNING,
        trading_day=datetime(2026, 10, 9).date(),
    )


def morning_pick(symbol="NVDA", run_id="premarket-m") -> Pick:
    return Pick(
        run_id=run_id,
        rank=1,
        symbol=symbol,
        side="long",
        horizon="swing",
        score=84,
        pre_score=89,
        thesis="t",
        invalidation=Decimal(101),
        expires_at=close_of(datetime(2026, 10, 16).date()),
    )


async def morning(store, level="trade", status="ok", run_id="premarket-m") -> None:
    """The pre-market run: NVDA picked, and the day's posture."""
    posture = Posture(level=level, reasons=("code: x",), run_id=run_id, at=MORNING)
    await store.write_run(morning_meta(run_id, status), [morning_pick(run_id=run_id)], posture)


async def ledger(*symbols) -> MemoryStateStore:
    state = MemoryStateStore("paper")
    for symbol in symbols:
        await state.put_ledger(LedgerEntry(symbol, horizon="swing", side="long", opened_at=MORNING))
    return state


async def deps(
    *,
    store=None,
    llm=None,
    market=None,
    settings=None,
    state=None,
    monotonic=None,
    level="trade",
) -> RunDeps:
    """By default: the morning said trade and picked NVDA, the bot holds AMD, MSFT is
    pinned. Of the movers, PLTR alone survives the screen."""
    if store is None:
        store = MemoryResearchStore()
        await morning(store, level)
    day_market, events = market_day()
    return RunDeps(
        store=store,
        market=market or day_market,
        events=events,
        llm=llm or ScriptedLLM(dives={"PLTR": [submit(**PLTR_SWING)]}),
        trail=Trails(),
        alerts=LogAlerter(),
        settings=settings or Settings(pinned_symbols=("MSFT",)),
        clock=ManualClock(NOW),
        monotonic=monotonic or (lambda: 0.0),
        state=state if state is not None else await ledger("AMD"),
    )


async def bot_view(store, now=NOW, **settings):
    source = ResearchSource(store, lambda: ResearchSettings(**settings))
    await source.refresh(now)
    return source.view


# --- the golden mid-morning ----------------------------------------------------------------


async def test_a_mid_morning_run_adds_only_new_names_as_intraday_picks():
    d = await deps()
    outcome = await run_intraday(d, NOW)
    assert (outcome.status, outcome.exit_code) == ("ok", 0)
    assert outcome.run_id.startswith("intraday-20261009T150000Z-")
    (pltr,) = outcome.picks
    assert (pltr.symbol, pltr.side.value, pltr.horizon.value) == ("PLTR", "long", "intraday")
    assert pltr.expires_at == datetime(2026, 10, 9, 20, 0, tzinfo=UTC)  # today's close
    assert outcome.posture.level is PostureLevel.TRADE
    assert outcome.posture.reasons == (
        "intraday: at least trade, from premarket-m",
        "code: no rule matched",
    )
    counts = outcome.meta.counts
    assert (counts["excluded_picked"], counts["excluded_held"], counts["excluded_pinned"]) == (
        1,
        1,
        1,
    )
    assert counts["coerced_to_intraday"] == 1
    assert "PLTR: a swing idea was made intraday" in outcome.meta.notes
    snapshot = d.trail.only().files["snapshot.json"]
    assert {"NVDA", "AMD", "MSFT"}.isdisjoint(snapshot["candidates"])
    assert (outcome.meta.kind, outcome.meta.models) == ("intraday", (DEFAULT_MODEL,))
    (request,) = d.llm.requests
    assert request["model"] == DEFAULT_MODEL
    intro = request["messages"][0]["content"]
    assert "during the session" in intro and INTRADAY_LINE in intro
    ((event, subject, message),) = d.alerts.sent
    assert (event, subject) == ("research_run_ok", f"Research intraday {DAY}: ok")
    assert message.startswith(f"traider research intraday {DAY}: posture trade")
    assert ("LOCK#intraday", "LOCK") not in d.store.keys
    view = await bot_view(d.store)
    assert sorted(view.picks) == ["NVDA", "PLTR"]
    assert view.posture.run_id == outcome.run_id  # the newest posture


async def test_it_uses_the_intraday_model_and_budget():
    haiku = "anthropic.claude-haiku-5"
    settings = Settings(
        pinned_symbols=("MSFT",),
        research_jobs={
            "dive": {"intraday_model": haiku},
            "budget": {
                "intraday_run_usd": "0.001",
                "prices": {
                    haiku: {"in_per_mtok": "1", "out_per_mtok": "5"},
                    DEFAULT_MODEL: {"in_per_mtok": "2", "out_per_mtok": "10"},
                },
            },
        },
    )
    d = await deps(settings=settings)
    outcome = await run_intraday(d, NOW)
    assert outcome.meta.models == (haiku,)
    assert d.llm.requests == []  # the budget refused the dive before any call
    assert outcome.status == "partial"
    assert "budget reached: 1 deep-dive(s) stopped" in outcome.meta.notes


async def test_the_dives_call_the_intraday_model_and_the_trail_says_so():
    haiku = "anthropic.claude-haiku-5"
    settings = Settings(
        pinned_symbols=("MSFT",),
        research_jobs={
            "dive": {"intraday_model": haiku},
            "budget": {
                "prices": {
                    haiku: {"in_per_mtok": "1", "out_per_mtok": "5"},
                    DEFAULT_MODEL: {"in_per_mtok": "2", "out_per_mtok": "10"},
                },
            },
        },
    )
    d = await deps(settings=settings)
    outcome = await run_intraday(d, NOW)
    assert outcome.status == "ok"
    assert [r["model"] for r in d.llm.requests] == [haiku]
    assert d.trail.only().files["dives/PLTR.json"]["model"] == haiku


async def test_it_dives_into_intraday_deep_dive_count_names_only():
    store = MemoryResearchStore()
    posture = Posture(level="trade", reasons=("code: x",), run_id="premarket-m", at=MORNING)
    await store.write_run(morning_meta(), [], posture)  # nothing picked this morning
    settings = Settings(research_jobs={"intraday": {"deep_dive_count": 2}})
    d = await deps(store=store, settings=settings, state=await ledger())
    outcome = await run_intraday(d, NOW)
    assert outcome.meta.counts["screened"] == 4  # NVDA, AMD, MSFT and PLTR
    assert outcome.meta.counts["dived"] == 2


# --- it never rescues a day ---------------------------------------------------------------


@pytest.mark.parametrize("status", ["partial", "failed", "running"])
async def test_without_an_ok_posture_today_it_writes_nothing(status):
    store = MemoryResearchStore()
    await morning(store, status=status)
    d = await deps(store=store)
    before = set(store.keys)
    outcome = await run_intraday(d, NOW)
    assert (outcome.status, outcome.exit_code, outcome.detail) == (
        "skipped",
        0,
        "no ok posture today",
    )
    assert set(store.keys) == before
    assert d.llm.requests == [] and d.alerts.sent == []
    assert d.market.called("quotes") == []


async def test_an_empty_morning_is_skipped_too():
    d = await deps(store=MemoryResearchStore())
    assert (await run_intraday(d, NOW)).status == "skipped"


async def test_an_unreadable_posture_item_means_no_posture():
    store = MemoryResearchStore()
    await morning(store)
    store.put_raw(f"DAY#{DAY}", "POSTURE#2026-10-09T13:00:00+00:00", "{not json")
    d = await deps(store=store)
    assert (await run_intraday(d, NOW)).status == "skipped"


async def test_the_latest_ok_posture_is_the_one_it_starts_from():
    store = MemoryResearchStore()
    await morning(store, "trade")
    later = Posture(
        level="reduced",
        run_id="intraday-a",
        at=MORNING.replace(hour=14),
    )
    await store.write_run(morning_meta("intraday-a", kind="intraday"), [], later)
    newest_but_partial = Posture(level="trade", run_id="intraday-b", at=MORNING.replace(hour=15))
    await store.write_run(
        morning_meta("intraday-b", status="partial", kind="intraday"), [], newest_but_partial
    )
    found = latest_ok_posture(await store.day(DAY))
    assert (found.run_id, found.level) == ("intraday-a", PostureLevel.REDUCED)


async def test_force_never_skips_the_morning_posture_check():
    d = await deps(store=MemoryResearchStore())
    assert (await run_intraday(d, NOW, force=True)).status == "skipped"


# --- when it runs ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "status"),
    [
        (datetime(2026, 10, 9, 13, 0, tzinfo=UTC), "closed"),  # 09:00, before the open
        (datetime(2026, 10, 9, 20, 0, tzinfo=UTC), "closed"),  # 16:00, the close
        (datetime(2026, 10, 9, 19, 4, tzinfo=UTC), "ok"),  # 15:04, inside the start grace
        (datetime(2026, 10, 9, 19, 6, tzinfo=UTC), "skipped"),  # 15:06, after last_start
    ],
)
async def test_it_runs_only_in_the_session_and_until_last_start(now, status):
    d = await deps()
    d.clock = ManualClock(now)
    assert (await run_intraday(d, now)).status == status


async def test_force_starts_after_last_start():
    late = datetime(2026, 10, 9, 19, 30, tzinfo=UTC)
    d = await deps()
    assert (await run_intraday(d, late, force=True)).status == "ok"


async def test_a_holiday_is_closed():
    d = await deps()
    d.market.open_today = False
    assert (await run_intraday(d, NOW)).status == "closed"


async def test_switched_off_means_no_run():
    for jobs in ({"enabled": False}, {"intraday": {"enabled": False}}):
        d = await deps(settings=Settings(research_jobs=jobs))
        assert (await run_intraday(d, NOW)).status == "disabled"


@pytest.mark.parametrize("held", ["intraday", "premarket"])
async def test_its_own_lock_or_the_premarket_one_held_means_exit_2(held):
    d = await deps()
    await d.store.acquire_lock(held, "someone", 3600, NOW)
    before = {k: d.store.raw(*k) for k in d.store.keys}
    outcome = await run_intraday(d, NOW)
    assert (outcome.status, outcome.exit_code) == ("locked", 2)
    # It touched nothing: the lock it did take (if any) is given back.
    assert {k: d.store.raw(*k) for k in d.store.keys} == before
    assert d.alerts.sent == [] and d.llm.requests == []


async def test_the_scorecard_lock_does_not_stop_it():
    d = await deps()
    await d.store.acquire_lock("scorecard", "someone", 3600, NOW)
    assert (await run_intraday(d, NOW)).status == "ok"


async def test_it_holds_both_locks_for_its_whole_run_and_gives_both_back():
    taken: list[tuple[str, float]] = []

    class Watching(MemoryResearchStore):
        async def acquire_lock(self, name, owner, ttl_s, now):
            taken.append((name, ttl_s))
            return await super().acquire_lock(name, owner, ttl_s, now)

        async def write_run(self, meta, picks, posture):
            if meta.kind == "intraday" and meta.status.value == "ok":
                # The final write: both locks are still held.
                assert {("LOCK#intraday", "LOCK"), ("LOCK#premarket", "LOCK")} <= self.keys
            await super().write_run(meta, picks, posture)

    store = Watching()
    await morning(store)
    d = await deps(store=store)
    loop = asyncio.get_running_loop()
    run = IntradayRun(d, NOW, "intraday-x", dry_run=False)
    box_left = run.box - loop.time()
    outcome = await run_locked(run, force=False)
    assert outcome.status == "ok"
    ttl = Settings().research_jobs.intraday.max_run_s + LOCK_SPARE_S
    assert taken == [("intraday", ttl), ("premarket", ttl)]
    assert box_left <= ttl - RUN_BOX_MARGIN_S  # the box ends before either lock expires
    assert not {k for k in store.keys if k[0].startswith("LOCK#")}


async def test_its_time_box_follows_intraday_max_run_s():
    d = await deps()
    loop = asyncio.get_running_loop()
    run = IntradayRun(d, NOW, "intraday-x", dry_run=False)
    limit = Settings().research_jobs.intraday.max_run_s
    assert run.max_run_s == limit == 600
    box = run.box - loop.time()
    edge = limit + LOCK_SPARE_S - RUN_BOX_MARGIN_S
    assert edge - 1 < box <= edge


async def test_past_the_deadline_before_the_screen_it_writes_the_posture_only():
    d = await deps(monotonic=StepClock([0.0], then=1e9))
    outcome = await run_intraday(d, NOW)
    assert (outcome.status, outcome.picks) == ("partial", ())
    assert "deadline passed before the screen: no deep-dives" in outcome.meta.notes
    assert d.llm.requests == []


async def test_a_run_past_its_box_fails(monkeypatch):
    class Hanging(ScriptedLLM):
        async def create(self, **request):
            await asyncio.Event().wait()

    monkeypatch.setattr(
        run_module, "_run_deadline", lambda max_run_s: asyncio.get_running_loop().time() + 0.3
    )
    d = await deps(llm=Hanging())
    outcome = await asyncio.wait_for(run_intraday(d, NOW), 5)
    assert outcome.status == "failed"
    assert outcome.meta.error == "dive: RunDeadline: the run did not finish within 1140s"
    assert ("LOCK#intraday", "LOCK") not in d.store.keys
    assert ("LOCK#premarket", "LOCK") not in d.store.keys


async def test_a_dry_run_writes_nothing():
    d = await deps()
    before = set(d.store.keys)
    outcome = await run_intraday(d, NOW, dry_run=True)
    assert outcome.status == "ok" and [p.symbol for p in outcome.picks] == ["PLTR"]
    assert set(d.store.keys) == before and d.alerts.sent == []


async def test_without_the_bots_ledger_it_fails_closed():
    d = await deps()
    d.state = None
    outcome = await run_intraday(d, NOW)
    assert (outcome.status, outcome.exit_code) == ("failed", 1)
    assert outcome.meta.error.startswith("held: NoBotState")
    assert outcome.picks == ()


async def test_an_unreadable_ledger_fails_the_run():
    class Broken(MemoryStateStore):
        async def ledger(self):
            raise RuntimeError("throttled")

    d = await deps(state=Broken())
    assert (await run_intraday(d, NOW)).status == "failed"


# --- the posture only tightens -------------------------------------------------------------


async def test_a_calm_market_never_loosens_a_reduced_morning():
    d = await deps(level="reduced")
    outcome = await run_intraday(d, NOW)
    assert outcome.posture.level is PostureLevel.REDUCED
    assert outcome.posture.reasons[0] == "intraday: at least reduced, from premarket-m"


async def test_a_stand_aside_morning_stays_stand_aside_with_no_dives():
    d = await deps(level="stand_aside")
    outcome = await run_intraday(d, NOW)
    assert (outcome.posture.level, outcome.picks) == (PostureLevel.STAND_ASIDE, ())
    assert d.llm.requests == []
    assert d.alerts.sent == []  # nothing new to say


async def test_a_vix_spike_tightens_the_day_and_says_so():
    d = await deps()
    d.market.quote_map["$VIX"] = d.market.quote_map["$VIX"].model_copy(update={"last": 40.0})
    outcome = await run_intraday(d, NOW)
    assert (outcome.posture.level, outcome.picks) == (PostureLevel.STAND_ASIDE, ())
    assert d.llm.requests == []
    ((_, _, message),) = d.alerts.sent
    assert "posture stand_aside (vix 40.0); 0 picks" in message
    assert (await bot_view(d.store)).level is PostureLevel.STAND_ASIDE


async def test_missing_market_data_means_stand_aside():
    d = await deps()
    del d.market.quote_map["$VIX"]
    outcome = await run_intraday(d, NOW)
    assert outcome.posture.level is PostureLevel.STAND_ASIDE


async def test_no_new_picks_and_no_change_means_no_alert_but_a_posture_all_the_same():
    d = await deps(llm=ScriptedLLM(dives={"PLTR": [submit(**(PLTR_LONG | {"side": "pass"}))]}))
    outcome = await run_intraday(d, NOW)
    assert (outcome.status, outcome.picks) == ("ok", ())
    assert d.alerts.sent == []
    stored = [k for k in d.store.keys if k[1].startswith("POSTURE#2026-10-09T15")]
    assert stored  # written even though unchanged


def test_the_intraday_run_never_imports_order_code():
    from pathlib import Path

    from traider.research import intraday

    source = Path(intraday.__file__).read_text(encoding="utf-8")
    assert "place_order" not in source and "broker" not in source


async def test_rescue_check_reads_only_todays_runs():
    store = MemoryResearchStore()
    yesterday = Posture(level="trade", run_id="premarket-y", at=MORNING.replace(day=8))
    meta = morning_meta("premarket-y").model_copy(
        update={"trading_day": datetime(2026, 10, 8).date()}
    )
    await store.write_run(meta, [], yesterday)
    d = await deps(store=store)
    assert (await run_intraday(d, NOW)).status == "skipped"


# --- the alert text is scrubbed as a whole -------------------------------------------------


def _summary_of(meta_notes, picks=()):
    from traider.research.intraday import IntradayRun

    async def make():
        return IntradayRun(await deps(), NOW, "intraday-x", dry_run=False)

    run = asyncio.run(make())
    posture = Posture(level="trade", run_id="intraday-x", at=NOW, metrics={"vix": 18.0})
    meta = morning_meta("intraday-x", status="partial", kind="intraday").model_copy(
        update={"notes": tuple(meta_notes)}
    )
    return run.summary(posture, list(picks), meta)


def test_the_intraday_alert_text_is_scrubbed_as_a_whole():
    secret = "authorization: Bearer abcdefabcdefabcdefabcdefabcdef0123456789"
    message = _summary_of([f"vendor said {secret}"])
    assert message.startswith(f"traider research intraday {DAY}: posture trade (vix 18.0)")
    assert "abcdefabcdef" not in message and "Bearer abc" not in message


def test_the_intraday_alert_text_is_never_cut_short():
    from traider.research.run import MAX_NOTES

    notes = [f"note {i} " + "word " * 58 for i in range(MAX_NOTES)]
    picks = [
        Pick.model_validate(
            morning_pick(symbol=s).model_dump()
            | {"horizon": "intraday", "rank": i + 1, "expires_at": close_of(NOW.date())}
        )
        for i, s in enumerate(["ABCDE", "B", "C", "D", "E"] * 5)  # rank.max_picks is 25
    ]
    message = _summary_of([n[:300] for n in notes], picks)
    assert message.endswith(notes[-1][:300].rstrip())


# --- C2a fix 1: postures only tighten across partial runs ----------------------------------


@pytest.mark.parametrize("accept", [False, True])
async def test_a_partial_stand_aside_holds_for_the_rest_of_the_day(accept):
    store = MemoryResearchStore()
    await morning(store, "trade")
    aside = Posture(
        level="stand_aside", reasons=("code: x",), run_id="intraday-a", at=MORNING.replace(hour=14)
    )
    await store.write_run(morning_meta("intraday-a", status="partial", kind="intraday"), [], aside)
    assert (await bot_view(store, accept_partial_runs=accept)).level is PostureLevel.STAND_ASIDE
    d = await deps(store=store)  # calm metrics: the code rules alone would say trade
    outcome = await run_intraday(d, NOW)
    assert outcome.status == "ok"
    assert (outcome.posture.level, outcome.picks) == (PostureLevel.STAND_ASIDE, ())
    assert outcome.posture.reasons[0] == "intraday: at least stand_aside, from intraday-a"
    assert d.llm.requests == []
    assert (await bot_view(store, accept_partial_runs=accept)).level is PostureLevel.STAND_ASIDE


async def test_a_newer_looser_posture_of_a_partial_run_is_not_where_it_starts():
    store = MemoryResearchStore()
    await morning(store, "reduced")
    looser = Posture(level="trade", run_id="premarket-p", at=MORNING.replace(hour=13))
    await store.write_run(morning_meta("premarket-p", status="partial"), [], looser)
    outcome = await run_intraday(await deps(store=store), NOW)
    assert outcome.posture.level is PostureLevel.REDUCED
    assert outcome.posture.reasons[0] == "intraday: at least reduced, from premarket-m"


@pytest.mark.parametrize("day", [DAY, "2026-10-08"])
async def test_unreadable_picks_mean_the_posture_only(day):
    store = MemoryResearchStore()
    await morning(store)
    store.put_raw(f"DAY#{day}", "PICK#premarket-x#009", "{not json")
    d = await deps(store=store)
    outcome = await run_intraday(d, NOW)
    assert (outcome.status, outcome.picks) == ("partial", ())
    assert outcome.posture.level is PostureLevel.TRADE
    assert d.llm.requests == []
    assert "1 unreadable pick item(s) in the research table: no new picks" in outcome.meta.notes
    assert outcome.meta.counts["unreadable_picks"] == 1
    assert [k for k in store.keys if k[1].startswith("POSTURE#2026-10-09T15")]
    ((event, _, _),) = d.alerts.sent
    assert event == "research_run_partial"


async def test_a_live_pick_from_an_earlier_day_is_never_replaced():
    store = MemoryResearchStore()
    await morning(store)
    yesterday = morning_meta("premarket-y").model_copy(
        update={"trading_day": datetime(2026, 10, 8).date()}
    )
    await store.write_run(yesterday, [morning_pick("PLTR", run_id="premarket-y")], None)
    d = await deps(store=store)
    outcome = await run_intraday(d, NOW)
    assert (outcome.status, outcome.picks) == ("ok", ())
    assert outcome.meta.counts["excluded_live"] == 1
    assert "PLTR" not in d.trail.only().files["snapshot.json"]["candidates"]
    assert d.llm.requests == []


async def test_an_expired_pick_from_an_earlier_day_does_not_exclude_its_name():
    store = MemoryResearchStore()
    await morning(store)
    yesterday = morning_meta("premarket-y").model_copy(
        update={"trading_day": datetime(2026, 10, 8).date()}
    )
    old = morning_pick("PLTR", run_id="premarket-y").model_copy(
        update={"expires_at": close_of(datetime(2026, 10, 8).date())}
    )
    await store.write_run(yesterday, [old], None)
    outcome = await run_intraday(await deps(store=store), NOW)
    assert [p.symbol for p in outcome.picks] == ["PLTR"]


# --- C2a final fix: premarket and intraday never overlap -----------------------------------

FORCED_AT = datetime(2026, 10, 9, 14, 55, tzinfo=UTC)  # a forced pre-market run, 10:55


class Racing(MemoryResearchStore):
    """Runs ``during`` once, just before the final write of the first ``kind`` run, while
    that run still holds its locks. Records the bot's posture after every write."""

    def __init__(self, kind):
        super().__init__()
        self.kind = kind
        self.during = None
        self.levels: list[tuple[PostureLevel, PostureLevel]] = []

    async def write_run(self, meta, picks, posture):
        if meta.kind == self.kind and meta.status.value in ("ok", "partial") and self.during:
            during, self.during = self.during, None
            await during()
        await super().write_run(meta, picks, posture)
        self.levels.append(
            (
                (await bot_view(self, accept_partial_runs=False)).level,
                (await bot_view(self, accept_partial_runs=True)).level,
            )
        )


def never_loosens(levels) -> bool:
    for accept in (0, 1):
        steps = [STRICTNESS[pair[accept]] for pair in levels]
        if steps != sorted(steps):
            return False
    return True


def forced_stand_aside(store) -> RunDeps:
    """A forced pre-market run that will say stand_aside (VIX at 40)."""
    d = premarket_deps(store=store)
    d.market.quote_map["$VIX"] = d.market.quote_map["$VIX"].model_copy(update={"last": 40.0})
    return d


async def test_a_premarket_run_started_during_an_intraday_run_exits_2():
    # The race: the morning said trade; intraday read that at 11:00; a forced pre-market
    # run then wrote stand_aside; intraday wrote trade after it and the bot loosened.
    store = Racing("intraday")
    await morning(store, "trade")
    outcomes = []

    async def premarket():
        outcomes.append(await run_premarket(forced_stand_aside(store), FORCED_AT, force=True))

    store.during = premarket
    outcome = await run_intraday(await deps(store=store), NOW)
    assert outcome.status == "ok"
    ((status, code),) = [(o.status, o.exit_code) for o in outcomes]
    assert (status, code) == ("locked", 2)
    assert not [k for k in store.keys if k[0].startswith("RUN#premarket-2026")]
    assert never_loosens(store.levels)
    assert not {k for k in store.keys if k[0].startswith("LOCK#")}


async def test_an_intraday_run_started_during_a_premarket_run_exits_2():
    store = Racing("premarket")
    await morning(store, "trade")
    outcomes = []

    async def intraday():
        outcomes.append(await run_intraday(await deps(store=store), NOW))

    store.during = intraday
    outcome = await run_premarket(forced_stand_aside(store), FORCED_AT, force=True)
    assert (outcome.status, outcome.posture.level) == ("ok", PostureLevel.STAND_ASIDE)
    ((status, code),) = [(o.status, o.exit_code) for o in outcomes]
    assert (status, code) == ("locked", 2)
    assert not [k for k in store.keys if k[0].startswith("RUN#intraday-")]
    assert never_loosens(store.levels)
    # The forced run's stand_aside is what the bot reads, not lost under a newer trade.
    assert store.levels[-1] == (PostureLevel.STAND_ASIDE, PostureLevel.STAND_ASIDE)
    assert not {k for k in store.keys if k[0].startswith("LOCK#")}
