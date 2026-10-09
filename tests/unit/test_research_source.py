"""What the bot believes research says, and what happens when it cannot tell."""

from datetime import UTC, date, datetime, timedelta

from traider.config import ResearchSettings
from traider.research.models import Pick, Posture, PostureLevel, RunMeta
from traider.research.source import ResearchSource, ResearchView
from traider.research.store import MemoryResearchStore

NOW = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)  # Friday 11:00 New York
TODAY, YESTERDAY = "2026-10-09", "2026-10-08"


def meta(run_id, status="ok", day=TODAY) -> RunMeta:
    return RunMeta(
        run_id=run_id,
        kind="premarket",
        status=status,
        started_at=NOW,
        finished_at=NOW,
        trading_day=date.fromisoformat(day),
    )


def pick(symbol, run_id="r1", rank=1, score=80, horizon="intraday", hours=5, side="long"):
    return Pick.model_validate(
        {
            "run_id": run_id,
            "rank": rank,
            "symbol": symbol,
            "side": side,
            "horizon": horizon,
            "score": score,
            "pre_score": score,
            "thesis": "t",
            "invalidation": "10",
            "expires_at": (NOW + timedelta(hours=hours)).isoformat(),
        }
    )


def posture(level="trade", run_id="r1", minutes=0):
    return Posture(level=level, run_id=run_id, at=NOW + timedelta(minutes=minutes))


class Flaky(MemoryResearchStore):
    def __init__(self):
        super().__init__()
        self.error = None

    async def day(self, day):
        if self.error is not None:
            raise self.error
        return await super().day(day)


def source(store, **settings):
    return ResearchSource(store, lambda: ResearchSettings(**settings))


async def test_before_any_read_nothing_is_live_and_the_bot_stands_aside():
    src = source(MemoryResearchStore())
    assert src.view.picks == {}
    assert src.view.level is PostureLevel.STAND_ASIDE


async def test_live_picks_and_todays_posture():
    store = MemoryResearchStore()
    await store.write_run(meta("r1"), [pick("NVDA"), pick("AMD", rank=2)], posture("reduced"))
    src = source(store)
    assert await src.refresh(NOW) == []
    assert set(src.view.live_picks(NOW)) == {"NVDA", "AMD"}
    assert src.view.level is PostureLevel.REDUCED


async def test_no_posture_today_means_stand_aside():
    store = MemoryResearchStore()
    await store.write_run(meta("r1"), [pick("NVDA")], None)
    src = source(store)
    await src.refresh(NOW)
    assert src.view.level is PostureLevel.STAND_ASIDE


async def test_picks_from_failed_running_or_partial_runs_are_ignored():
    store = MemoryResearchStore()
    await store.write_run(meta("r1", "failed"), [pick("A")], posture(run_id="r1"))
    await store.write_run(meta("r2", "running"), [pick("B", run_id="r2")], None)
    await store.write_run(meta("r3", "partial"), [pick("C", run_id="r3")], None)
    src = source(store)
    await src.refresh(NOW)
    assert src.view.live_picks(NOW) == {}
    assert src.view.level is PostureLevel.STAND_ASIDE
    partial_ok = source(store, accept_partial_runs=True)
    await partial_ok.refresh(NOW)
    assert set(partial_ok.view.live_picks(NOW)) == {"C"}


async def test_picks_without_a_run_record_are_ignored():
    store = MemoryResearchStore()
    await store.write_run(meta("r1"), [pick("A")], None)
    store._items.pop(("RUN#r1", "META"))
    src = source(store)
    await src.refresh(NOW)
    assert src.view.live_picks(NOW) == {}


async def test_low_scores_and_expired_picks_are_not_live():
    store = MemoryResearchStore()
    await store.write_run(meta("r1"), [pick("LOW", score=40), pick("OLD", rank=2, hours=-1)], None)
    src = source(store, min_score=60)
    await src.refresh(NOW)
    assert src.view.live_picks(NOW) == {}


async def test_a_pick_expires_between_polls():
    store = MemoryResearchStore()
    await store.write_run(meta("r1"), [pick("NVDA", hours=1)], None)
    src = source(store)
    await src.refresh(NOW)
    assert src.view.pick("NVDA", NOW) is not None
    assert src.view.pick("NVDA", NOW + timedelta(hours=1)) is None


async def test_swing_picks_carry_over_from_earlier_days_and_intraday_ones_do_not():
    store = MemoryResearchStore()
    await store.write_run(
        meta("r0", day=YESTERDAY),
        [pick("SWING", run_id="r0", horizon="swing", hours=72), pick("DAY", run_id="r0", rank=2)],
        posture(run_id="r0", minutes=-1440),
    )
    src = source(store)
    await src.refresh(NOW)
    assert set(src.view.live_picks(NOW)) == {"SWING"}
    assert src.view.level is PostureLevel.STAND_ASIDE  # yesterday's posture does not count


async def test_the_best_pick_per_symbol_wins():
    store = MemoryResearchStore()
    await store.write_run(meta("r1"), [pick("NVDA", score=70)], None)
    await store.write_run(meta("r2"), [pick("NVDA", run_id="r2", score=90)], None)
    src = source(store)
    await src.refresh(NOW)
    assert src.view.pick("NVDA", NOW).run_id == "r2"


async def test_the_latest_qualifying_posture_wins():
    store = MemoryResearchStore()
    await store.write_run(meta("r1"), [], posture("stand_aside", minutes=-60))
    await store.write_run(meta("r2"), [], posture("trade", run_id="r2"))
    src = source(store)
    await src.refresh(NOW)
    assert src.view.level is PostureLevel.TRADE


async def test_a_failed_read_keeps_the_last_view_until_it_is_too_old():
    store = Flaky()
    await store.write_run(meta("r1"), [pick("NVDA")], posture())
    src = source(store, max_stale_s=600)
    await src.refresh(NOW)
    store.error = RuntimeError("throttled")
    assert await src.refresh(NOW + timedelta(seconds=599)) == []
    assert "NVDA" in src.view.picks
    updates = await src.refresh(NOW + timedelta(seconds=601))
    assert [u.kind for u in updates] == ["stale"]
    assert src.view.picks == {}
    assert src.view.stale and src.view.level is PostureLevel.STAND_ASIDE
    assert await src.refresh(NOW + timedelta(seconds=700)) == []  # once per outage
    store.error = None
    restored = await src.refresh(NOW + timedelta(seconds=760))
    assert [u.kind for u in restored] == ["restored"]
    assert not src.view.stale and "NVDA" in src.view.picks


async def test_never_read_successfully_goes_stale_from_the_first_attempt():
    store = Flaky()
    store.error = RuntimeError("no table")
    src = source(store, max_stale_s=600)
    assert await src.refresh(NOW) == []
    updates = await src.refresh(NOW + timedelta(seconds=601))
    assert [u.kind for u in updates] == ["stale"]


def test_a_stale_view_stands_aside_even_with_a_posture_in_hand():
    view = ResearchView(posture=posture("trade"), as_of=NOW, stale=True)
    assert view.level is PostureLevel.STAND_ASIDE


async def test_live_picks_drops_a_pick_that_expires_within_the_hour():
    store = MemoryResearchStore()
    await store.write_run(meta("r1"), [pick("NVDA", hours=1), pick("AMD", rank=2, hours=5)], None)
    src = source(store)
    await src.refresh(NOW)
    assert set(src.view.live_picks(NOW + timedelta(hours=1))) == {"AMD"}


async def test_an_expired_high_score_pick_does_not_displace_a_live_one():
    store = MemoryResearchStore()
    await store.write_run(meta("r1"), [pick("NVDA", score=90, hours=-1)], None)
    await store.write_run(meta("r2"), [pick("NVDA", run_id="r2", score=70)], None)
    src = source(store)
    await src.refresh(NOW)
    assert src.view.picks["NVDA"].run_id == "r2"


async def test_on_equal_score_the_newer_run_wins():
    store = MemoryResearchStore()
    await store.write_run(meta("r1"), [pick("NVDA", score=80)], None)
    await store.write_run(meta("r2"), [pick("NVDA", run_id="r2", score=80)], None)
    src = source(store)
    await src.refresh(NOW)
    assert src.view.picks["NVDA"].run_id == "r2"


async def test_on_equal_score_and_run_the_lower_rank_wins():
    store = MemoryResearchStore()
    await store.write_run(
        meta("r1"), [pick("NVDA", rank=2, score=80), pick("NVDA", rank=1, score=80)], None
    )
    src = source(store)
    await src.refresh(NOW)
    assert src.view.picks["NVDA"].rank == 1


async def test_each_outage_is_reported_once_and_each_recovery_once():
    store = Flaky()
    await store.write_run(meta("r1"), [pick("NVDA", hours=30)], posture())
    src = source(store, max_stale_s=600)
    assert await src.refresh(NOW) == []
    store.error = RuntimeError("throttled")
    assert [u.kind for u in await src.refresh(NOW + timedelta(seconds=601))] == ["stale"]
    store.error = None
    assert [u.kind for u in await src.refresh(NOW + timedelta(seconds=610))] == ["restored"]
    assert await src.refresh(NOW + timedelta(seconds=620)) == []  # plain success: no repeat
    store.error = RuntimeError("throttled again")
    assert [u.kind for u in await src.refresh(NOW + timedelta(seconds=1300))] == ["stale"]
    store.error = None
    assert [u.kind for u in await src.refresh(NOW + timedelta(seconds=1310))] == ["restored"]
    assert await src.refresh(NOW + timedelta(seconds=1320)) == []


async def test_the_outage_window_counts_from_the_last_success_not_the_first_attempt():
    store = Flaky()
    await store.write_run(meta("r1"), [pick("NVDA", hours=30)], posture())
    src = source(store, max_stale_s=600)
    store.error = RuntimeError("throttled")
    assert await src.refresh(NOW) == []  # first attempt fails, nothing read yet
    store.error = None
    assert await src.refresh(NOW + timedelta(seconds=500)) == []  # success
    store.error = RuntimeError("throttled")
    assert await src.refresh(NOW + timedelta(seconds=700)) == []  # 200s since success
    assert not src.view.stale and "NVDA" in src.view.picks


async def test_a_window_of_exactly_max_stale_s_is_still_fresh():
    store = Flaky()
    await store.write_run(meta("r1"), [pick("NVDA", hours=30)], posture())
    src = source(store, max_stale_s=600)
    await src.refresh(NOW)
    store.error = RuntimeError("throttled")
    assert await src.refresh(NOW + timedelta(seconds=600)) == []
    assert not src.view.stale and "NVDA" in src.view.picks


async def test_a_kept_view_from_an_earlier_trading_day_is_dropped_on_failure():
    store = Flaky()
    friday_late = datetime(2026, 10, 10, 3, 50, tzinfo=UTC)  # Friday 23:50 New York
    await store.write_run(meta("r1"), [pick("NVDA", hours=30)], posture())
    src = source(store, max_stale_s=3600)
    await src.refresh(friday_late)
    assert src.view.level is PostureLevel.TRADE and "NVDA" in src.view.picks
    store.error = RuntimeError("throttled")
    saturday_early = friday_late + timedelta(minutes=20)  # Saturday 00:10 New York, in the window
    assert await src.refresh(saturday_early) == []
    assert src.view.picks == {} and not src.view.stale
    assert src.view.level is PostureLevel.STAND_ASIDE


async def test_an_unreadable_newer_posture_makes_the_bot_stand_aside(caplog):
    store = MemoryResearchStore()
    await store.write_run(meta("r1"), [], posture("trade", minutes=-60))
    store.put_raw(f"DAY#{TODAY}", "POSTURE#2026-10-09T15:30:00+00:00", "not json")
    src = source(store)
    await src.refresh(NOW)
    assert src.view.level is PostureLevel.STAND_ASIDE
    assert "1 unreadable item" in caplog.text


async def test_a_view_that_cannot_be_built_goes_stale_like_a_failed_read(monkeypatch):
    store = MemoryResearchStore()
    await store.write_run(meta("r1"), [pick("NVDA", hours=30)], posture())
    src = source(store, max_stale_s=600)

    def broken(*args):
        raise ValueError("bad data")

    monkeypatch.setattr(src, "_build", broken)
    assert await src.refresh(NOW) == []
    assert await src.refresh(NOW + timedelta(seconds=300)) == []  # not a success
    updates = await src.refresh(NOW + timedelta(seconds=601))
    assert [(u.kind, u.detail) for u in updates] == [("stale", "ValueError: bad data")]
    assert src.view.stale and src.view.picks == {}
    assert await src.refresh(NOW + timedelta(seconds=700)) == []  # still failing: no "restored"
    assert src.view.stale


async def test_settings_that_cannot_be_read_count_towards_staleness():
    store = MemoryResearchStore()
    await store.write_run(meta("r1"), [pick("NVDA", hours=30)], posture())
    calls = []

    def settings():
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("settings gone")
        return ResearchSettings(max_stale_s=120)  # not the default 600: the last good one

    src = ResearchSource(store, settings)
    assert await src.refresh(NOW) == []
    assert "NVDA" in src.view.picks
    assert await src.refresh(NOW + timedelta(seconds=60)) == []  # kept, within the window
    assert "NVDA" in src.view.picks
    updates = await src.refresh(NOW + timedelta(seconds=121))
    assert [(u.kind, u.detail) for u in updates] == [("stale", "RuntimeError: settings gone")]
    assert src.view.stale and src.view.picks == {}
