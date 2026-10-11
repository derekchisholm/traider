"""The bot says so, once a day, when research is on and there is no usable posture a few
minutes after the open. It stands aside either way; this only makes it loud."""

from dataclasses import replace
from datetime import UTC, datetime

from tests.unit.engine_harness import Harness
from tests.unit.test_engine_research import write
from traider.research.models import Posture
from traider.research.source import ResearchView
from traider.research.store import MemoryResearchStore

OPEN = datetime(2026, 10, 8, 13, 30, tzinfo=UTC)  # Thursday 09:30 New York
BEFORE_OPEN = datetime(2026, 10, 8, 13, 0, tzinfo=UTC)
AFTER_CLOSE = datetime(2026, 10, 8, 20, 30, tzinfo=UTC)  # 16:30 New York
MESSAGE = (
    "No usable research posture for today; the bot is standing aside. Check the pre-market "
    "run (alerts, META, the DLQ)."
)


async def bench(tmp_path, *, start=OPEN, level=None, status="ok", store=None, **kwargs) -> Harness:
    """Research on. ``level=None``: no posture written today."""
    store = store if store is not None else MemoryResearchStore()
    h = await Harness.create(
        tmp_path, symbols=(), research_store=store, begin=False, start=start, **kwargs
    )
    if level is not None:
        await write(h, store, (), level, status)
    await h.engine.start()
    await h.engine.step()
    return h


def no_posture_alerts(h) -> list[tuple[str, str, str]]:
    return [a for a in h.alerts.sent if a[0] == "research_no_posture"]


async def test_it_fires_once_after_the_delay(tmp_path):
    h = await bench(tmp_path)
    await h.run_for(4 * 60)  # 09:34: not yet
    assert no_posture_alerts(h) == []
    await h.run_for(2 * 60)  # 09:36: research was read after 09:35
    assert no_posture_alerts(h) == [("research_no_posture", "No research posture today", MESSAGE)]
    (event,) = await h.events("research_no_posture")
    assert event["data"] == {"day": "2026-10-08"}
    await h.run_for(5 * 60)  # once a day
    assert len(no_posture_alerts(h)) == 1
    assert len(await h.events("research_no_posture")) == 1


async def test_it_waits_for_a_read_after_the_delay(tmp_path):
    # The research poll is every 60 s by default; the view read at 09:30 is too old.
    h = await bench(tmp_path, research_settings={"poll_s": 600})
    await h.run_for(6 * 60)
    assert no_posture_alerts(h) == []
    await h.run_for(5 * 60)  # 09:41: the 09:40 read counts
    assert len(no_posture_alerts(h)) == 1


async def test_the_delay_is_a_setting(tmp_path):
    h = await bench(tmp_path, research_settings={"posture_alert_after_open_min": 30})
    await h.run_for(10 * 60)
    assert no_posture_alerts(h) == []
    await h.run_for(22 * 60)
    assert len(no_posture_alerts(h)) == 1


async def test_not_before_the_open(tmp_path):
    h = await bench(tmp_path, start=BEFORE_OPEN)
    await h.run_for(29 * 60)  # 09:00 to 09:29: the session is not open yet
    assert no_posture_alerts(h) == []


async def test_not_when_research_chose_to_stand_aside(tmp_path):
    h = await bench(tmp_path, level="stand_aside")
    await h.run_for(10 * 60)
    assert no_posture_alerts(h) == []
    assert h.research.view.level.value == "stand_aside"


async def test_a_partial_runs_posture_counts_as_missing(tmp_path):
    h = await bench(tmp_path, level="trade", status="partial")
    await h.run_for(6 * 60)
    assert len(no_posture_alerts(h)) == 1


async def test_an_unreadable_posture_item_counts_as_missing(tmp_path):
    class Unreadable(MemoryResearchStore):
        async def day(self, day):
            return replace(await super().day(day), invalid_postures=1)

    h = await bench(tmp_path, level="trade", store=Unreadable())
    await h.run_for(6 * 60)
    assert len(no_posture_alerts(h)) == 1


async def test_not_when_research_is_stale(tmp_path):
    class Broken(MemoryResearchStore):
        async def day(self, day):
            raise RuntimeError("table unreachable")

    h = await bench(tmp_path, store=Broken(), research_settings={"max_stale_s": 60})
    await h.run_for(10 * 60)
    assert no_posture_alerts(h) == []
    assert "research_stale" in h.alert_keys()


async def test_not_when_a_good_view_goes_stale(tmp_path):
    class Flaky(MemoryResearchStore):
        broken = False

        async def day(self, day):
            if self.broken:
                raise RuntimeError("table unreachable")
            return await super().day(day)

    store = Flaky()
    h = await bench(tmp_path, level="trade", store=store, research_settings={"max_stale_s": 60})
    await h.run_for(6 * 60)  # 09:36: a good posture, read after the delay
    assert h.research.view.posture is not None
    store.broken = True
    await h.run_for(3 * 60)  # the view goes stale with no posture
    assert h.research.view.stale and h.research.view.posture is None
    assert "research_stale" in h.alert_keys()
    assert no_posture_alerts(h) == []
    assert await h.events("research_no_posture") == []


async def test_it_fires_again_on_the_next_trading_day(tmp_path):
    h = await bench(tmp_path)
    await h.run_for(6 * 60)  # Thursday 09:36
    assert len(no_posture_alerts(h)) == 1
    await h.run_for(24 * 3600, step=60)  # Friday 09:36, still no posture
    assert len(no_posture_alerts(h)) == 2
    (event,) = await h.events("research_no_posture")
    assert event["data"] == {"day": "2026-10-09"}


async def test_not_after_the_close(tmp_path):
    h = await bench(tmp_path, start=AFTER_CLOSE)
    await h.run_for(10 * 60)  # 16:30 to 16:40
    assert no_posture_alerts(h) == []


async def test_not_when_research_is_off(tmp_path):
    h = await Harness.create(tmp_path, start=OPEN)
    await h.run_for(10 * 60)
    assert no_posture_alerts(h) == []


async def test_a_good_posture_means_no_alert(tmp_path):
    h = await bench(tmp_path, level="trade")
    await h.run_for(10 * 60)
    assert no_posture_alerts(h) == []


def test_the_view_tells_a_missing_posture_from_a_chosen_stand_aside():
    now = OPEN
    assert ResearchView(as_of=now).posture_missing is True
    assert ResearchView().posture_missing is False  # never read
    assert ResearchView(as_of=now, stale=True).posture_missing is False
    chosen = Posture(level="stand_aside", run_id="r1", at=now)
    assert ResearchView(as_of=now, posture=chosen).posture_missing is False


async def test_only_the_lease_holder_sends_it(tmp_path):
    # Like trading, the alert is the leader's: a standby stays quiet, so one alert a day.
    store = MemoryResearchStore()
    h = await Harness.create(tmp_path, symbols=(), research_store=store, begin=False, start=OPEN)
    lease = h.store.acquire_lease

    async def refuse(*_args, **_kwargs):
        return False

    h.store.acquire_lease = refuse
    await h.engine.start()
    await h.run_for(10 * 60)  # 09:40: past the delay, research read, no posture
    assert not h.engine.is_leader
    assert h.research.view.posture_missing
    assert no_posture_alerts(h) == []
    assert await h.events("research_no_posture") == []
    h.store.acquire_lease = lease  # it wins the lease: now it says so, once
    await h.run_for(2 * 60)
    assert h.engine.is_leader
    assert len(no_posture_alerts(h)) == 1
    assert len(await h.events("research_no_posture")) == 1
