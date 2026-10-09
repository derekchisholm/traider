"""The bot only manages what it opened, and intraday positions are flat by the close."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tests.unit.engine_harness import Harness
from tests.unit.test_engine_research import researched, write
from tests.unit.test_engine_universe import pin
from traider.broker.paper import _Holding
from traider.research.store import MemoryResearchStore
from traider.settings_store import MemorySettingsStore


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

    def broken(order, account, now):
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


# --- fixes after review -----------------------------------------------------------------


async def test_a_buy_still_working_keeps_its_ledger_entry(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)], config={"order_timeout_s": 120})
    h.broker.hold_fills = True
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    assert h.broker.calls["place"] == 1
    snapshots = h.broker.calls["get_account"]
    await h.run_for(35)  # an account refresh while the buy works and nothing is held yet
    assert h.broker.calls["get_account"] > snapshots
    assert h.position("NVDA") == 0
    assert "NVDA" in await h.store.ledger()
    h.broker.hold_fills = False
    await h.settle()
    assert h.position("NVDA") == 2
    assert not await h.events("unknown_holding")


async def _broken_ledger():
    raise RuntimeError("no table")


async def without_ledger(tmp_path, *, step=True, **kwargs) -> tuple[Harness, object]:
    """A bench whose position ledger cannot be read from start-up on."""
    store = MemoryResearchStore()
    h = await Harness.create(tmp_path, symbols=(), research_store=store, begin=False, **kwargs)
    await write(h, store, [("NVDA",)])
    assert h.research is not None
    await h.research.refresh(h.clock.now())
    working = h.store.ledger
    h.store.ledger = _broken_ledger
    await h.engine.start()
    if step:
        await h.engine.step()
    return h, working


def ledger_alerts(h, key="ledger_not_loaded"):
    return [a for a in h.alerts.sent if a[0] == key]


async def test_a_ledger_outage_is_recorded_and_alerted_once_per_outage(tmp_path):
    h, working = await without_ledger(tmp_path, step=False)
    halts = [e["data"] for e in await h.events("entries_halted")]
    assert halts == [{"reason": "position ledger not loaded"}]  # recorded by start()
    await h.run_for(100, step=5)
    assert ledger_alerts(h) == []  # not before LEDGER_ALERT_AFTER_S
    await h.run_for(300, step=5)
    [(_, subject, body)] = ledger_alerts(h)
    assert subject == "Position ledger not loaded"
    assert "No new positions are being opened" in body
    assert "not flattened automatically" in body and "by hand before the close" in body
    assert "Check the state table and the task role" in body
    assert len(await h.events("entries_halted")) == 1

    h.store.ledger = working  # it loads: the outage is over
    await h.run_for(10)

    # A new outage: the lease moves away and back, and the reload on winning it fails.
    h.store.ledger = _broken_ledger
    lease = h.store.acquire_lease

    async def refuse(*_args, **_kwargs):
        return False

    h.store.acquire_lease = refuse
    await h.run_for(15)
    assert not h.engine.is_leader
    h.store.acquire_lease = lease
    await h.run_for(15)
    assert h.engine.is_leader
    assert len(await h.events("entries_halted")) == 2
    await h.run_for(130, step=5)
    assert len(ledger_alerts(h)) == 2


async def test_a_ledger_outage_at_the_close_gets_a_louder_alert(tmp_path):
    h, _ = await without_ledger(tmp_path)
    h.clock.set(datetime(2026, 10, 8, 19, 30, tzinfo=UTC))  # 30 minutes to the close
    await h.run_for(5)
    assert ledger_alerts(h, "ledger_not_loaded_close") == []
    h.clock.set(datetime(2026, 10, 8, 19, 46, tzinfo=UTC))  # inside the 15-minute window
    await h.run_for(30)
    [(_, subject, body)] = ledger_alerts(h, "ledger_not_loaded_close")
    assert subject == "Position ledger not loaded at the close"
    assert "Sell intraday positions yourself" in body


async def test_the_ledger_follows_the_lease(tmp_path):
    a = await researched(tmp_path, picks=[("NVDA",)])
    b = await Harness.create(
        tmp_path,
        symbols=(),
        research_store=a.research._store,
        restart_of=a,
        instance="bot-2",
        begin=False,
    )
    b.price("NVDA", "100.00", "100.02")
    await b.engine.start()  # reads the ledger now, before A buys anything
    await b.engine.step()
    assert not b.engine.is_leader
    a.price("NVDA", "100.00", "100.02")
    await a.target("NVDA", 2)
    await a.settle()
    assert a.position("NVDA") == 2
    await b.run_for(65)  # A has stopped; its lease runs out and B takes over
    assert b.engine.is_leader
    assert not await b.events("unknown_holding")
    assert not [k for k in b.alert_keys() if k.startswith("unknown_holding")]
    b._bars = a._bars  # the shared market data drops bars it has already seen
    await b.target("NVDA", 0)
    await b.settle()
    assert b.position("NVDA") == 0


async def _lose_the_entry(h, symbol):
    """The entry goes missing after the order went out (to test that it is booked again)."""
    await h.store.delete_ledger(symbol)
    h.engine._ledger.pop(symbol)


async def test_an_adopted_buy_is_booked_again(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)])
    h.broker.drop_order_id = True  # accepted with no id: the engine looks the order up
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await _lose_the_entry(h, "NVDA")
    await h.settle(35)
    assert await h.events("order_adopted")
    assert h.position("NVDA") == 2
    assert "NVDA" in await h.store.ledger()
    assert not await h.events("unknown_holding")


async def test_an_unconfirmed_buy_that_filled_is_booked_again(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)])
    h.broker.place_error_after_accepting = RuntimeError("connection reset")
    h.broker.find_error = RuntimeError("lookup down")  # so the fill is only seen in the account
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await _lose_the_entry(h, "NVDA")
    await h.settle(35)
    assert not await h.events("order_adopted")
    assert h.position("NVDA") == 2
    assert "NVDA" in await h.store.ledger()
    assert not await h.events("unknown_holding")


# --- unpinning a held symbol with research on -------------------------------------------


async def _pinned_researched(tmp_path, settings_store, **kwargs) -> Harness:
    """Research on, SPY and QQQ pinned, settings read from ``settings_store``."""
    return await researched(
        tmp_path, picks=[], symbols_extra=("SPY", "QQQ"), settings_store=settings_store, **kwargs
    )


def unpinned_alerts(h) -> list[str]:
    return [k for k in h.alert_keys() if k.startswith("unpinned_")]


async def test_unpinning_a_booked_symbol_keeps_it_managed_across_a_restart(tmp_path):
    store = MemorySettingsStore()
    h = await _pinned_researched(tmp_path, store)
    h.price("QQQ", "50.00", "50.02")
    await h.target("QQQ", 2)
    await h.settle()
    assert h.position("QQQ") == 2
    assert "QQQ" in await h.store.ledger()
    await pin(h, store, ("SPY",))
    assert unpinned_alerts(h) == []
    assert not await h.events("unpinned_but_held")
    assert "QQQ" in h.engine.universe

    h2 = await Harness.create(
        tmp_path,
        symbols=(),
        research_store=h.research._store,
        settings_store=store,
        restart_of=h,
        begin=False,
    )
    h2.price("QQQ", "50.00", "50.02")
    await h2.engine.start()
    await h2.engine.step()
    await h2.run_for(35)
    assert h2.engine.settings.pinned_symbols == ("SPY",)
    assert "QQQ" in h2.engine.universe
    assert not await h2.events("unknown_holding")
    await h2.target("QQQ", 0)
    await h2.settle()
    assert h2.position("QQQ") == 0


async def test_a_pinned_holding_the_bot_never_booked_is_booked_and_stays_managed(tmp_path):
    store = MemorySettingsStore()
    h = await _pinned_researched(tmp_path, store)
    h.broker._holdings["QQQ"] = _Holding(5, Decimal(45))  # held before research was on
    h.price("QQQ", "50.00", "50.02")
    await h.tick(31)  # the next account snapshot books it
    entry = (await h.store.ledger())["QQQ"]
    assert (entry.horizon, entry.side, entry.pick_run_id, entry.pick_rank) == (
        "swing",
        "long",
        "",
        0,
    )
    await pin(h, store, ("SPY",))
    await h.tick(31)
    assert not await h.events("unknown_holding")
    assert unpinned_alerts(h) == []
    assert "QQQ" in h.engine.universe
    await h.target("QQQ", 0)
    await h.settle()
    assert h.position("QQQ") == 0
    await h.tick(31)
    assert "QQQ" not in await h.store.ledger()
    assert "QQQ" not in h.engine.universe


async def test_a_pinned_put_is_booked_bearish_and_a_call_long(tmp_path):
    put, call = "QQQ   261016P00400000", "QQQ   261016C00400000"
    h = await researched(tmp_path, picks=[], symbols_extra=("QQQ",), risk={"allow_options": True})
    h.broker._holdings[put] = _Holding(1, Decimal(2))
    h.broker._holdings[call] = _Holding(1, Decimal(2))
    await h.tick(31)
    ledger = await h.store.ledger()
    assert ledger[put].side == "bearish" and ledger[call].side == "long"
    assert ledger[put].horizon == ledger[call].horizon == "swing"


async def test_a_failed_booking_is_retried_and_unpinning_meanwhile_says_so(tmp_path):
    store = MemorySettingsStore()
    h = await _pinned_researched(tmp_path, store)
    h.broker._holdings["QQQ"] = _Holding(5, Decimal(45))
    h.price("QQQ", "50.00", "50.02")
    working = h.store.put_ledger

    async def refuse(entry):
        raise RuntimeError("throttled")

    h.store.put_ledger = refuse
    await h.tick(31)
    assert "QQQ" not in await h.store.ledger()
    await pin(h, store, ("SPY",))
    [(key, subject, body)] = [a for a in h.alerts.sent if a[0].startswith("unpinned_")]
    assert key == "unpinned_not_in_ledger:2"
    assert subject == "Unpinned symbols are not in the ledger"
    assert body.startswith(
        "QQQ are not in the bot's ledger; once unpinned, the bot leaves them alone, sells "
        "included. Pin them again or sell them yourself."
    )
    assert [e["data"] for e in await h.events("unpinned_but_held")] == [
        {"version": 2, "symbols": ["QQQ"]}
    ]

    # Pinned again, the next snapshot retries the booking and it goes through.
    h.store.put_ledger = working
    await pin(h, store, ("SPY", "QQQ"))
    await h.tick(31)
    assert "QQQ" in await h.store.ledger()


async def test_without_research_unpinning_still_warns_about_the_restart(tmp_path):
    store = MemorySettingsStore()
    h = await Harness.create(tmp_path, symbols=("SPY", "QQQ"), settings_store=store)
    await h.target("QQQ", 2)
    await h.settle()
    await pin(h, store, ("SPY",))
    assert [k for k in h.alert_keys() if k.startswith("unpinned_")] == ["unpinned_but_held:2"]
