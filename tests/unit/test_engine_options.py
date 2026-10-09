"""Options in the engine: long calls and puts, bought to open and sold to close."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tests.unit.engine_harness import Harness
from traider.models import OrderRequest, OrderType, Side, Target
from traider.options import OptionQuote
from traider.timeutil import trading_date

CALL = "SPY   261016C00500000"  # eight days out from the bench's clock
PUT = "SPY   261016P00500000"
TODAY_CALL = "SPY   261008C00500000"  # expires on the bench's own day
OTHER = "QQQ   261016C00400000"
TWO_PM = datetime(2026, 10, 8, 18, 0, tzinfo=UTC)  # 14:00 in New York


async def bench(tmp_path, *, options=True, risk=None, **kwargs) -> Harness:
    h = await Harness.create(tmp_path, risk={"allow_options": options, **(risk or {})}, **kwargs)
    for symbol in (CALL, PUT, TODAY_CALL, OTHER):
        h.price(symbol, "2.00", "2.10")
    return h


@pytest.fixture
async def h(tmp_path):
    return await bench(tmp_path)


async def ask_for(h: Harness, symbol: str, quantity: int, reason: str = "test") -> None:
    """The strategy asks for an option position while hearing a bar of the underlying."""
    h.strategy.bar_targets.append(Target(symbol, quantity, reason))
    h.bar("SPY")
    await h.engine.step()


async def hold(h: Harness, symbol: str, quantity: int = 1) -> None:
    """Put contracts in the account without the engine having bought them."""
    await h.broker.place(OrderRequest(symbol, Side.BUY, quantity, OrderType.LIMIT, Decimal("2.10")))
    h.broker.placed.clear()


# --- buying and selling ----------------------------------------------------------


async def test_an_option_target_is_bought_with_a_limit_at_the_ask(h):
    await ask_for(h, CALL, 2, "breakout")
    assert h.broker.placed == [
        OrderRequest(CALL, Side.BUY, 2, OrderType.LIMIT, Decimal("2.10"), "breakout")
    ]
    await h.settle()
    assert h.position(CALL) == 2


async def test_an_option_is_sold_with_a_limit_at_the_bid(h):
    await ask_for(h, CALL, 2)
    await h.settle()
    await ask_for(h, CALL, 0, "done")
    assert h.broker.placed[-1] == OrderRequest(
        CALL, Side.SELL, 2, OrderType.LIMIT, Decimal("2.00"), "done"
    )
    await h.settle()
    assert h.position(CALL) == 0


async def test_options_use_limit_orders_even_when_shares_use_market_orders(tmp_path):
    h = await bench(tmp_path, config={"order_type": "MARKET"})
    await ask_for(h, CALL, 1)
    assert h.broker.placed[-1].order_type is OrderType.LIMIT
    assert h.broker.placed[-1].limit_price == Decimal("2.10")


async def test_an_option_limit_is_the_quoted_price_with_no_offset(tmp_path):
    roomy = {"max_order_usd": 3000, "max_position_usd": 3000, "max_total_exposure_usd": 3000}
    h = await bench(tmp_path, risk=roomy)
    h.price(CALL, "20.00", "20.10")  # the 5 bps offset used for shares would make 20.11
    await ask_for(h, CALL, 1)
    assert h.broker.placed[-1].limit_price == Decimal("20.10")
    await h.settle()
    await ask_for(h, CALL, 0)
    assert h.broker.placed[-1].limit_price == Decimal("20.00")


async def test_a_contract_being_traded_is_put_on_the_watch_list(h):
    assert h.market.watched() == ()
    await ask_for(h, CALL, 1)
    assert h.market.watched() == (CALL,)


async def test_an_option_on_a_symbol_the_bot_does_not_trade_is_ignored(h):
    await ask_for(h, OTHER, 1)
    await h.settle()
    assert h.broker.placed == []
    assert h.market.watched() == ()
    assert await h.events("target") == []


async def test_a_target_that_is_not_a_share_or_an_option_symbol_is_ignored(h):
    await ask_for(h, "SPY 261016C500", 1)
    assert h.market.watched() == ()


async def test_no_option_is_bought_while_options_are_switched_off(tmp_path):
    h = await bench(tmp_path, options=False)
    await ask_for(h, CALL, 1)
    await h.settle()
    assert h.broker.placed == []
    (blocked,) = await h.events("order_blocked")
    assert "options_off" in blocked["data"]["codes"]


async def test_an_option_can_still_be_sold_while_options_are_switched_off(tmp_path):
    h = await bench(tmp_path, options=False)
    await hold(h, CALL)
    await h.run_for(31)
    await ask_for(h, CALL, 0)
    await h.settle()
    assert h.position(CALL) == 0


async def test_the_number_of_contracts_in_play_is_capped(h):
    for strike in range(400, 400 + h.engine.MAX_OPTION_SYMBOLS + 1):
        await ask_for(h, f"SPY   261016C00{strike}000", 0)
    assert len(h.market.watched()) == h.engine.MAX_OPTION_SYMBOLS


# --- every dollar figure counts a contract as a hundred shares --------------------


async def test_option_premium_counts_toward_total_exposure(tmp_path):
    h = await bench(tmp_path, risk={"max_total_exposure_usd": 1000})
    await ask_for(h, CALL, 4)  # 4 x 100 x 2.00 = 800 once held
    await h.settle()
    await h.target("SPY", 3)  # another 300 would make 1100
    await h.settle()
    assert h.position("SPY") == 0
    assert "max_total_exposure_usd" in (await h.events("order_blocked"))[-1]["data"]["codes"]


async def test_an_option_buy_on_its_way_counts_toward_total_exposure(tmp_path):
    h = await bench(tmp_path, risk={"max_total_exposure_usd": 1000})
    h.broker.hold_fills = True
    await ask_for(h, CALL, 4)  # 840 working
    await h.target("SPY", 3)
    assert h.broker.calls["place"] == 1
    assert "max_total_exposure_usd" in (await h.events("order_blocked"))[-1]["data"]["codes"]


async def test_an_option_buy_on_its_way_ties_up_its_full_premium(tmp_path):
    h = await bench(tmp_path, cash="500")
    h.broker.hold_fills = True
    await ask_for(h, CALL, 2)  # 420 of the 500 is spoken for
    await h.target("SPY", 1)  # 100 more is not there
    assert h.broker.calls["place"] == 1
    assert "cash" in (await h.events("order_blocked"))[-1]["data"]["codes"]


async def test_an_option_sale_counts_its_full_proceeds_as_unsettled(h):
    await ask_for(h, CALL, 1)
    await h.settle()
    await ask_for(h, CALL, 0)
    await h.settle()
    day = await h.store.get_day(trading_date(h.clock.now()).isoformat())
    assert day.sold_usd == Decimal("200.00")


# --- expiry ---------------------------------------------------------------------


async def test_an_option_is_sold_on_its_last_day_before_the_close(tmp_path):
    h = await bench(tmp_path, start=TWO_PM)
    await hold(h, TODAY_CALL)
    await h.run_for(40)
    assert h.broker.placed == []  # two hours to go: not yet
    h.clock.advance(3600)  # 15:00, an hour before the close
    await h.run_for(15)
    assert h.broker.placed == [
        OrderRequest(
            TODAY_CALL, Side.SELL, 1, OrderType.LIMIT, Decimal("2.00"), "option expires today"
        )
    ]
    assert h.position(TODAY_CALL) == 0
    assert f"option_expiring:{TODAY_CALL}" in h.alert_keys()


async def test_the_exit_window_before_expiry_can_be_changed(tmp_path):
    h = await bench(tmp_path, start=TWO_PM, risk={"option_expiry_exit_min": 150})
    await hold(h, TODAY_CALL)
    await h.run_for(40)
    assert h.position(TODAY_CALL) == 0


async def test_an_expiring_option_is_sold_even_if_the_strategy_wants_to_keep_it(tmp_path):
    h = await bench(tmp_path, start=TWO_PM)
    await hold(h, TODAY_CALL)
    await ask_for(h, TODAY_CALL, 1, "hold on")
    h.clock.advance(3600)
    await h.run_for(15)
    assert h.position(TODAY_CALL) == 0


async def test_an_option_with_days_left_is_not_sold_near_the_close(tmp_path):
    h = await bench(tmp_path, start=TWO_PM)
    await hold(h, CALL)
    h.clock.advance(3600)
    await h.run_for(40)
    assert h.broker.placed == []


async def test_expiring_options_are_left_alone_while_options_are_off(tmp_path):
    h = await bench(tmp_path, start=TWO_PM, options=False)
    await hold(h, TODAY_CALL)
    h.clock.advance(3600)
    await h.run_for(40)
    assert h.broker.placed == []
    assert h.market.watched() == ()


async def test_with_options_off_an_expiring_option_the_strategy_holds_is_not_sold(tmp_path):
    h = await bench(tmp_path, start=TWO_PM, options=False)
    await hold(h, TODAY_CALL)
    await ask_for(h, TODAY_CALL, 1, "hold on")
    h.clock.advance(3600)
    await h.run_for(40)
    assert h.broker.placed == []


async def test_an_expiring_option_on_another_symbol_is_left_alone(tmp_path):
    h = await bench(tmp_path, start=TWO_PM)
    expiring = "QQQ   261008C00400000"
    h.price(expiring, "2.00", "2.10")
    await hold(h, expiring)
    h.clock.advance(3600)
    await h.run_for(40)
    assert h.broker.placed == []
    assert h.market.watched() == ()


# --- bookkeeping -----------------------------------------------------------------


async def test_held_contracts_are_watched_without_the_strategy_naming_them(h):
    await hold(h, PUT)
    await h.run_for(31)  # the next account refresh
    assert h.market.watched() == (PUT,)
    h.bar("SPY")
    await h.engine.step()
    assert h.strategy.contexts[-1].position(PUT) == 1


async def test_a_resumed_option_order_is_watched_again(tmp_path):
    h = await bench(tmp_path)
    h.broker.hold_fills = True
    await ask_for(h, CALL, 1)
    h.market.unwatch(CALL)  # a new process starts with an empty watch list
    await Harness.create(tmp_path, restart_of=h, risk={"allow_options": True})
    assert h.market.watched() == (CALL,)


async def test_contracts_no_longer_held_drop_off_the_watch_list_the_next_day(h):
    await ask_for(h, CALL, 1)
    await h.settle()
    await ask_for(h, PUT, 1)
    await h.settle()
    await ask_for(h, PUT, 0)
    await h.settle()
    h.clock.advance(24 * 3600)
    await h.run_for(3)
    assert h.market.watched() == (CALL,)  # still held, so still quoted


async def test_the_strategy_can_read_the_option_chain(h):
    line = OptionQuote(CALL, Decimal("2.00"), Decimal("2.10"), Decimal("0.45"), 8)
    h.market.set_chain("SPY", [line])
    h.bar("SPY")
    await h.engine.step()
    ctx = h.strategy.contexts[-1]
    assert (ctx.chain("SPY"), ctx.chain("QQQ")) == ((line,), ())
