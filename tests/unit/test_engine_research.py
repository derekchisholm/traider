"""The engine only enters what research picked, on the right side, on a day it may trade."""

from datetime import date, timedelta
from decimal import Decimal

from tests.unit.engine_harness import Harness
from traider.models import OrderRequest, OrderType, Side
from traider.research.models import Pick, Posture, RunMeta
from traider.research.store import MemoryResearchStore


def run_meta(h, run_id="r1", status="ok"):
    now = h.clock.now()
    return RunMeta(
        run_id=run_id,
        kind="premarket",
        status=status,
        started_at=now,
        finished_at=now,
        trading_day=date(2026, 10, 8),
    )


def make_pick(h, symbol, side="long", horizon="intraday", score=80, run_id="r1", rank=1):
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
            "invalidation": "1",
            "expires_at": (h.clock.now() + timedelta(hours=4)).isoformat(),
        }
    )


async def write(h, store, picks=(), level="trade", status="ok", run_id="r1") -> None:
    posture = Posture(level=level, run_id=run_id, at=h.clock.now()) if level else None
    made = [make_pick(h, *p, run_id=run_id, rank=i) for i, p in enumerate(picks, 1)]
    await store.write_run(run_meta(h, run_id, status=status), made, posture)


async def researched(tmp_path, picks=(), level="trade", status="ok", **kwargs) -> Harness:
    store = MemoryResearchStore()
    h = await Harness.create(tmp_path, symbols=(), research_store=store, begin=False, **kwargs)
    await write(h, store, picks, level, status)
    assert h.research is not None
    await h.research.refresh(h.clock.now())
    await h.engine.start()
    await h.engine.step()
    return h


async def blocked_codes(h) -> list[str]:
    return (await h.events("order_blocked"))[-1]["data"]["codes"]


async def test_a_live_pick_joins_the_universe_and_can_be_bought(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)])
    assert h.engine.universe == ("NVDA",)
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()
    assert h.position("NVDA") == 2


async def test_a_held_symbol_whose_pick_is_gone_cannot_be_added_to(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)])
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()
    # The pick expires; NVDA stays in the universe because it is held.
    h.clock.advance(4 * 3600 + 60)
    await h.settle(65)
    assert "NVDA" in h.engine.universe
    await h.target("NVDA", 4)
    await h.settle()
    assert h.position("NVDA") == 2
    assert "no_pick" in await blocked_codes(h)


async def test_pinned_symbols_need_no_pick_but_follow_the_posture(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)], symbols_extra=("SPY",))
    await h.target("SPY", 2)
    await h.settle()
    assert h.position("SPY") == 2
    h2 = await researched(tmp_path, picks=[], level="stand_aside", symbols_extra=("SPY",))
    await h2.target("SPY", 2)
    await h2.settle()
    assert h2.position("SPY") == 0
    assert "posture" in await blocked_codes(h2)


async def test_stand_aside_blocks_entries(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)], level="stand_aside")
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()
    assert h.position("NVDA") == 0
    assert "posture" in await blocked_codes(h)


async def test_no_posture_today_blocks_entries(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)], level=None)
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()
    assert h.position("NVDA") == 0
    assert "posture" in await blocked_codes(h)


async def test_picks_from_a_failed_run_are_not_traded(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)], status="failed")
    assert h.engine.universe == ()


async def test_shares_on_a_bearish_pick_are_refused(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA", "bearish")])
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()
    assert h.position("NVDA") == 0
    assert "pick_side" in await blocked_codes(h)


async def test_reduced_days_shrink_the_caps(tmp_path):
    h = await researched(
        tmp_path, picks=[("NVDA",)], level="reduced", research_settings={"reduced_factor": 0.5}
    )
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 8)  # ~800: fine at 1000, too big at 500
    await h.settle()
    assert h.position("NVDA") == 0
    assert "max_order_usd" in await blocked_codes(h)
    await h.target("NVDA", 4)  # ~400: under the halved cap
    await h.settle()
    assert h.position("NVDA") == 4


async def test_stale_research_stops_entries_alerts_once_and_keeps_exits(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)], research_settings={"max_stale_s": 60})
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()

    async def broken(day):
        raise RuntimeError("no table")

    h.research._store.day = broken
    await h.run_for(130, step=10)
    stale = await h.events("research_stale")
    assert [e["data"] for e in stale] == [{"detail": "RuntimeError: no table"}]
    [(_, subject, body)] = [a for a in h.alerts.sent if a[0] == "research_stale"]
    assert subject == "Research is stale"
    assert "No new positions" in body and "Exits still work" in body
    await h.run_for(130, step=10)
    assert len(await h.events("research_stale")) == 1  # once per outage
    await h.target("NVDA", 4)  # no entries
    await h.settle()
    assert h.position("NVDA") == 2
    await h.target("NVDA", 0)  # exits still work
    await h.settle()
    assert h.position("NVDA") == 0


async def test_readable_research_again_is_announced(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)], research_settings={"max_stale_s": 60})
    store = h.research._store
    working = store.day

    async def broken(day):
        raise RuntimeError("no table")

    store.day = broken
    await h.run_for(130, step=10)
    assert "research_stale" in h.alert_keys()
    store.day = working
    await h.run_for(65, step=5)
    assert [e["data"] for e in await h.events("research_restored")] == [{}]
    assert ("research_restored", "Research readable again") in [
        (key, subject) for key, subject, _ in h.alerts.sent
    ]
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()
    assert h.position("NVDA") == 2


async def test_the_strategy_sees_picks_and_posture(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)], level="reduced")
    h.price("NVDA", "100.00", "100.02")
    h.bar("NVDA")
    await h.engine.step()
    ctx = h.strategy.contexts[-1]
    assert "NVDA" in ctx.picks and ctx.posture.value == "reduced"


async def test_without_research_the_strategy_sees_no_picks_or_posture(tmp_path):
    h = await Harness.create(tmp_path)
    h.bar("SPY")
    await h.engine.step()
    ctx = h.strategy.contexts[-1]
    assert ctx.picks == {} and ctx.posture is None


async def test_research_is_read_on_the_first_step_then_every_poll_interval(tmp_path):
    store = MemoryResearchStore()
    # 45s keeps the poll off the 30s account snapshots, which also refresh the universe.
    h = await Harness.create(
        tmp_path,
        symbols=(),
        research_store=store,
        research_settings={"poll_s": 45},
        begin=False,
    )
    await write(h, store, [("NVDA",)])
    await h.engine.start()
    await h.engine.step()  # the engine reads research itself on its first step
    assert h.engine.universe == ("NVDA",)
    await write(h, store, [("NVDA",), ("AMD",)], run_id="r2")
    await h.run_for(40, step=5)
    assert h.engine.universe == ("NVDA",)  # not polled again yet
    await h.run_for(5, step=5)
    assert set(h.engine.universe) == {"NVDA", "AMD"}  # at once, not at the next snapshot


async def test_a_gate_that_cannot_be_built_stops_entries_but_not_exits(tmp_path, monkeypatch):
    h = await researched(tmp_path, picks=[("NVDA",)])
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()
    assert h.position("NVDA") == 2

    def broken(order, now):
        raise ValueError("unknown posture 'bogus'")

    monkeypatch.setattr(h.engine, "_gate", broken)
    await h.target("NVDA", 4)
    await h.settle()
    assert h.position("NVDA") == 2
    assert {"posture", "no_pick"} <= set(await blocked_codes(h))
    await h.target("NVDA", 0)
    await h.settle()
    assert h.position("NVDA") == 0


async def test_with_research_on_a_holding_outside_the_universe_is_not_reported(tmp_path):
    # Task 10's ledger reports holdings the bot did not open; this alert is for research off.
    h = await researched(tmp_path, picks=[("NVDA",)])
    h.price("QQQ", "50.00", "50.02")
    await h.broker.place(OrderRequest("QQQ", Side.BUY, 2, OrderType.LIMIT, Decimal("50.02")))
    await h.run_for(65)
    assert h.position("QQQ") == 2
    assert "QQQ" not in h.engine.universe
    assert not await h.events("unmanaged_holding")
    assert not [key for key in h.alert_keys() if key.startswith("unmanaged_holding")]
