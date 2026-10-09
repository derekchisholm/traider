"""The engine turns strategy targets into orders, once, and only when allowed."""

from decimal import Decimal

import pytest

from tests.unit.engine_harness import Harness
from traider.models import OrderRequest, OrderType, Side, Target


@pytest.fixture
async def h(tmp_path):
    return await Harness.create(tmp_path)


async def test_a_long_target_is_bought(h):
    await h.target("SPY", 3)
    await h.settle()
    assert h.position() == 3


async def test_the_same_target_is_not_bought_twice(h):
    await h.target("SPY", 3)
    await h.run_for(120)
    assert h.broker.calls["place"] == 1
    assert h.position() == 3


async def test_a_flat_target_sells_the_position(h):
    await h.target("SPY", 3)
    await h.settle()
    await h.target("SPY", 0)
    await h.settle()
    assert h.position() == 0
    assert [o.side for o in h.broker.placed] == [Side.BUY, Side.SELL]


async def test_raising_the_target_buys_only_the_difference(h):
    await h.target("SPY", 2)
    await h.settle()
    await h.target("SPY", 5)
    await h.settle()
    assert [o.quantity for o in h.broker.placed] == [2, 3]


async def test_lowering_the_target_sells_only_the_difference(h):
    await h.target("SPY", 5)
    await h.settle()
    await h.target("SPY", 2)
    await h.settle()
    assert [(o.side, o.quantity) for o in h.broker.placed] == [(Side.BUY, 5), (Side.SELL, 3)]


async def test_no_target_means_hands_off_even_with_a_position(tmp_path):
    h = await Harness.create(tmp_path, begin=False)
    # A position the strategy has said nothing about.
    await h.broker.place(OrderRequest("SPY", Side.BUY, 4, OrderType.MARKET, None))
    h.broker.calls.clear()
    await h.engine.start()
    await h.run_for(60)
    assert h.broker.calls["place"] == 0
    assert h.position() == 4


async def test_buy_is_a_limit_just_above_the_ask(h):
    await h.target("SPY", 3)
    (order,) = h.broker.placed
    # ask 100.02 plus 5 bps is 100.07001, rounded down to the cent.
    assert (order.order_type, order.limit_price) == (OrderType.LIMIT, Decimal("100.07"))


async def test_sell_is_a_limit_just_below_the_bid(h):
    await h.target("SPY", 3)
    await h.settle()
    await h.target("SPY", 0)
    sell = h.broker.placed[-1]
    # bid 100.00 less 5 bps is 99.95.
    assert (sell.order_type, sell.limit_price) == (OrderType.LIMIT, Decimal("99.95"))


async def test_market_orders_when_configured(tmp_path):
    h = await Harness.create(tmp_path, config={"order_type": "MARKET"})
    await h.target("SPY", 3)
    (order,) = h.broker.placed
    assert (order.order_type, order.limit_price) == (OrderType.MARKET, None)


async def test_negative_targets_are_treated_as_flat_because_the_bot_never_shorts(h):
    await h.target("SPY", 3)
    await h.settle()
    await h.target("SPY", -5)
    assert h.engine.target("SPY").quantity == 0
    await h.settle()
    assert h.position() == 0
    assert h.broker.placed[-1].quantity == 3


async def test_targets_for_symbols_outside_the_configured_list_are_ignored(h):
    await h.target("QQQ", 3)
    await h.run_for(30)
    assert h.broker.calls["place"] == 0


async def test_symbols_are_handled_independently(tmp_path):
    h = await Harness.create(tmp_path, symbols=("SPY", "QQQ"))
    await h.target("SPY", 2)
    await h.target("QQQ", 4)
    await h.settle()
    assert (h.position("SPY"), h.position("QQQ")) == (2, 4)


async def test_quote_hook_can_change_the_target_between_bars(h):
    await h.target("SPY", 3)
    await h.settle()
    h.strategy.quote_targets.append(Target("SPY", 0, "stop"))
    await h.tick(1)  # the tick publishes a fresh quote, which calls on_quote
    await h.settle()
    assert h.position() == 0


async def test_strategy_sees_every_bar_and_the_current_position(h):
    await h.target("SPY", 3)
    await h.settle()
    h.bar("SPY")
    await h.tick(1)
    assert len(h.strategy.bars_seen) == 2
    assert h.strategy.contexts[-1].position("SPY") == 3


async def test_warmup_bars_reach_the_strategy_and_set_targets(h):
    h.strategy.bar_targets.append(Target("SPY", 2, "already trending"))
    h.bar("SPY", warmup=True)
    await h.tick(1)
    await h.settle()
    assert h.position() == 2


async def test_target_changes_are_written_to_the_audit_log(h):
    await h.target("SPY", 3, "fast above slow")
    (event,) = await h.events("target")
    assert event["data"] == {"symbol": "SPY", "quantity": 3, "reason": "fast above slow"}


async def test_repeating_the_same_target_is_not_logged_again(h):
    await h.target("SPY", 3)
    await h.target("SPY", 3)
    assert len(await h.events("target")) == 1


async def test_orders_and_fills_are_written_to_the_audit_log(h):
    await h.target("SPY", 3)
    await h.settle()
    (submitted,) = await h.events("order_submitted")
    (done,) = await h.events("order_done")
    assert submitted["data"]["symbol"] == "SPY"
    assert submitted["data"]["side"] == "BUY"
    assert submitted["data"]["quantity"] == 3
    assert done["data"]["status"] == "FILLED"
    assert done["data"]["filled_quantity"] == 3
    assert done["data"]["avg_fill_price"] == "100.02"


async def test_each_order_counts_towards_the_daily_total(h):
    await h.target("SPY", 3)
    await h.settle()
    await h.target("SPY", 0)
    await h.settle()
    assert (await h.store.get_day("2026-10-08")).orders == 2


async def test_heartbeat_file_is_kept_current(h):
    await h.tick(5)
    with open(h.config.heartbeat_file) as f:
        assert float(f.read()) == pytest.approx(h.clock.now().timestamp(), abs=0.01)
