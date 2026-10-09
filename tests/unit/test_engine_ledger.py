"""The bot only manages what it opened, and intraday positions are flat by the close."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tests.unit.engine_harness import Harness
from tests.unit.test_engine_research import researched
from traider.broker.paper import _Holding


async def test_a_buy_is_booked_in_the_ledger_before_it_is_sent(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA", "long", "intraday")])
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()
    ledger = await h.store.ledger()
    assert ledger["NVDA"].horizon == "intraday" and ledger["NVDA"].pick_run_id == "r1"


async def test_no_ledger_write_no_order(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)])

    async def refuse(entry):
        raise RuntimeError("throttled")

    h.store.put_ledger = refuse
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()
    assert h.position("NVDA") == 0
    assert h.broker.calls["place"] == 0


async def test_a_sold_out_position_leaves_the_ledger(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)])
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()
    await h.target("NVDA", 0)
    await h.settle(35)
    assert "NVDA" not in await h.store.ledger()


async def test_a_holding_the_bot_did_not_open_is_never_traded(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)])
    h.broker._holdings["NVDA"] = _Holding(7, Decimal(90))  # bought by hand
    h.price("NVDA", "100.00", "100.02")
    await h.tick(31)
    assert (await h.events("unknown_holding"))[-1]["data"] == {"symbol": "NVDA", "quantity": 7}
    assert "unknown_holding:NVDA" in h.alert_keys()
    await h.target("NVDA", 0)
    await h.settle()
    assert h.position("NVDA") == 7
    assert "foreign_holding" in (await h.events("order_blocked"))[-1]["data"]["codes"]


async def test_intraday_positions_are_sold_before_the_close_and_swing_ones_are_not(tmp_path):
    h = await researched(tmp_path, picks=[("DAY", "long", "intraday"), ("SWG", "long", "swing")])
    for s in ("DAY", "SWG"):
        h.price(s, "50.00", "50.02")
        await h.target(s, 2)
    await h.settle()
    assert h.position("DAY") == 2 and h.position("SWG") == 2
    close = datetime(2026, 10, 8, 20, 0, tzinfo=UTC)
    h.clock.set(close - timedelta(minutes=14))
    await h.settle()
    assert h.position("DAY") == 0
    assert h.position("SWG") == 2
    sold = [o for o in h.broker.placed if o.symbol == "DAY" and o.side.value == "SELL"]
    assert sold[-1].reason == "intraday position: flatten before close"


async def test_the_intraday_budget_caps_intraday_entries(tmp_path):
    h = await researched(
        tmp_path,
        picks=[("NVDA", "long", "intraday")],
        risk={"max_total_exposure_usd": 1000, "max_position_usd": 1000},
        research_settings={"intraday_share": 0.3},
    )
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 4)  # ~400 > 300 intraday budget
    await h.settle()
    assert h.position("NVDA") == 0
    assert "horizon_budget" in (await h.events("order_blocked"))[-1]["data"]["codes"]


async def test_without_research_there_is_no_ledger(tmp_path):
    h = await Harness.create(tmp_path)
    await h.target("SPY", 2)
    await h.settle()
    assert h.position("SPY") == 2
    assert await h.store.ledger() == {}


# --- decisions on top of the brief ----------------------------------------------------


async def test_a_foreign_holding_stays_untouched_when_the_gate_cannot_be_built(
    tmp_path, monkeypatch
):
    h = await researched(tmp_path, picks=[("NVDA",)])
    h.broker._holdings["NVDA"] = _Holding(7, Decimal(90))  # bought by hand
    h.price("NVDA", "100.00", "100.02")
    await h.tick(31)
    assert "unknown_holding:NVDA" in h.alert_keys()

    def broken(order, now):
        raise ValueError("unknown posture 'bogus'")

    monkeypatch.setattr(h.engine, "_gate", broken)
    await h.target("NVDA", 0)
    await h.settle()
    assert h.position("NVDA") == 7
    assert h.broker.calls["place"] == 0
    assert "foreign_holding" in (await h.events("order_blocked"))[-1]["data"]["codes"]


async def test_an_unreadable_ledger_stops_entries_but_not_exits_and_flags_nothing(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",), ("AMD",)])
    h.price("NVDA", "100.00", "100.02")
    h.price("AMD", "50.00", "50.02")
    await h.target("NVDA", 2)
    await h.settle()
    assert h.position("NVDA") == 2

    # A new process whose ledger read fails.
    h2 = await Harness.create(
        tmp_path, symbols=(), research_store=h.research._store, restart_of=h, begin=False
    )
    working = h2.store.ledger

    async def broken():
        raise RuntimeError("no table")

    h2.store.ledger = broken
    h2.price("NVDA", "100.00", "100.02")
    h2.price("AMD", "50.00", "50.02")
    await h2.engine.start()
    await h2.engine.step()
    await h2.run_for(35)
    # Nothing is foreign while the ledger is unknown: the bot's own NVDA is not flagged.
    assert not await h2.events("unknown_holding")
    assert not [k for k in h2.alert_keys() if k.startswith("unknown_holding")]
    # No entries...
    await h2.target("AMD", 2)
    await h2.settle()
    assert h2.position("AMD") == 0
    blocked = (await h2.events("order_blocked"))[-1]["data"]
    assert "entries_halted" in blocked["codes"]
    assert "position ledger not loaded" in blocked["detail"]
    # ...but exits work.
    await h2.target("NVDA", 0)
    await h2.settle()
    assert h2.position("NVDA") == 0

    # Once the ledger reads again, entries resume.
    h2.store.ledger = working
    await h2.settle(10)
    await h2.target("AMD", 2)
    await h2.settle()
    assert h2.position("AMD") == 2
    assert (await h2.store.ledger())["AMD"].pick_run_id == "r1"


async def test_after_a_restart_a_position_whose_pick_expired_is_still_managed(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA", "long", "swing")])
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()
    assert h.position("NVDA") == 2
    h.clock.advance(4 * 3600 + 60)  # the pick expires
    await h.settle(65)

    h2 = await Harness.create(
        tmp_path, symbols=(), research_store=h.research._store, restart_of=h, begin=False
    )
    h2.price("NVDA", "100.00", "100.02")
    await h2.engine.start()
    await h2.engine.step()
    await h2.run_for(35)
    assert "NVDA" in h2.engine.universe
    assert not await h2.events("unknown_holding")
    await h2.target("NVDA", 0)
    await h2.settle()
    assert h2.position("NVDA") == 0


async def test_a_failed_ledger_delete_is_retried_on_the_next_snapshot(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)])
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()
    working = h.store.delete_ledger

    async def refuse(symbol):
        raise RuntimeError("throttled")

    h.store.delete_ledger = refuse
    await h.target("NVDA", 0)
    await h.settle(35)
    assert h.position("NVDA") == 0
    assert "NVDA" in await h.store.ledger()
    h.store.delete_ledger = working
    await h.settle(35)
    assert "NVDA" not in await h.store.ledger()


async def test_no_intraday_entries_inside_the_closing_window(tmp_path):
    h = await researched(
        tmp_path,
        picks=[("NVDA", "long", "intraday")],
        research_settings={"intraday_flatten_min": 120},
    )
    h.clock.set(datetime(2026, 10, 8, 18, 30, tzinfo=UTC))  # 90 min to the close, pick live
    h.price("NVDA", "100.00", "100.02")
    await h.settle(35)
    await h.target("NVDA", 2)
    await h.settle()
    assert h.position("NVDA") == 0
    assert "intraday_closing" in (await h.events("order_blocked"))[-1]["data"]["codes"]
