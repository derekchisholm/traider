from datetime import timedelta
from decimal import Decimal

import pytest

from tests.unit.helpers import T0, make_quote
from traider.broker.base import BrokerError, OrderRejected
from traider.broker.paper import PaperBroker
from traider.marketdata import MarketData
from traider.models import OrderRequest, OrderStatus, OrderType, Side
from traider.state.memory import MemoryStateStore
from traider.timeutil import ManualClock


def limit(side, qty, price) -> OrderRequest:
    return OrderRequest("SPY", side, qty, OrderType.LIMIT, Decimal(price))


def market_order(side, qty) -> OrderRequest:
    return OrderRequest("SPY", side, qty, OrderType.MARKET, None)


@pytest.fixture
def market():
    data = MarketData()
    data.on_quote(make_quote("SPY", "100.00", "100.02"))
    return data


@pytest.fixture
def broker(market):
    return PaperBroker(market, ManualClock(T0), starting_cash=Decimal(1000))


def requote(market, bid, ask, seconds=1):
    market.on_quote(make_quote("SPY", bid, ask, at=T0 + timedelta(seconds=seconds)))


async def test_new_account_is_all_cash(broker):
    account = await broker.get_account()
    assert (account.equity, account.cash_available, dict(account.positions)) == (
        Decimal(1000),
        Decimal(1000),
        {},
    )


async def test_marketable_limit_buy_fills_at_the_ask_not_the_limit(broker):
    order_id = await broker.place(limit(Side.BUY, 2, "100.10"))
    order = await broker.get_order(order_id)
    assert (order.status, order.filled_quantity, order.avg_fill_price) == (
        OrderStatus.FILLED,
        2,
        Decimal("100.02"),
    )
    account = await broker.get_account()
    assert account.cash_available == Decimal("799.96")
    assert account.position("SPY") == 2


async def test_limit_buy_below_the_ask_rests_unfilled(broker):
    order_id = await broker.place(limit(Side.BUY, 2, "99.90"))
    order = await broker.get_order(order_id)
    assert (order.status, order.filled_quantity) == (OrderStatus.WORKING, 0)
    assert (await broker.get_account()).position("SPY") == 0


async def test_resting_buy_fills_when_the_ask_comes_down_to_the_limit(broker, market):
    order_id = await broker.place(limit(Side.BUY, 2, "99.90"))
    requote(market, "99.85", "99.88")
    order = await broker.get_order(order_id)
    assert (order.status, order.avg_fill_price) == (OrderStatus.FILLED, Decimal("99.88"))


async def test_market_sell_fills_at_the_bid(broker, market):
    await broker.place(market_order(Side.BUY, 3))
    requote(market, "101.00", "101.02")
    order = await broker.get_order(await broker.place(market_order(Side.SELL, 3)))
    assert (order.status, order.avg_fill_price) == (OrderStatus.FILLED, Decimal("101.00"))
    account = await broker.get_account()
    assert dict(account.positions) == {}
    # Bought 3 at 100.02, sold 3 at 101.00.
    assert account.cash_available == Decimal("1002.94")


async def test_limit_sell_above_the_bid_rests_until_the_bid_reaches_it(broker, market):
    await broker.place(market_order(Side.BUY, 1))
    order_id = await broker.place(limit(Side.SELL, 1, "100.50"))
    assert (await broker.get_order(order_id)).status is OrderStatus.WORKING
    requote(market, "100.55", "100.57")
    assert (await broker.get_order(order_id)).avg_fill_price == Decimal("100.55")


async def test_buy_without_enough_cash_is_rejected(broker):
    with pytest.raises(OrderRejected, match="cash"):
        await broker.place(limit(Side.BUY, 11, "100.10"))


async def test_sell_of_more_than_held_is_rejected(broker):
    await broker.place(market_order(Side.BUY, 1))
    with pytest.raises(OrderRejected, match="held"):
        await broker.place(market_order(Side.SELL, 2))


async def test_order_without_a_quote_is_rejected(broker):
    with pytest.raises(OrderRejected, match="quote"):
        await broker.place(OrderRequest("QQQ", Side.BUY, 1, OrderType.MARKET, None))


async def test_average_price_is_weighted_across_buys(broker, market):
    await broker.place(market_order(Side.BUY, 1))  # 100.02
    requote(market, "102.00", "102.02")
    await broker.place(market_order(Side.BUY, 3))  # 102.02
    account = await broker.get_account()
    assert account.positions["SPY"].avg_price == Decimal("101.52")


async def test_partial_sell_keeps_the_average_price(broker):
    await broker.place(market_order(Side.BUY, 4))
    await broker.place(market_order(Side.SELL, 1))
    position = (await broker.get_account()).positions["SPY"]
    assert (position.quantity, position.avg_price) == (3, Decimal("100.02"))


async def test_cancelled_order_never_fills(broker, market):
    order_id = await broker.place(limit(Side.BUY, 2, "99.90"))
    await broker.cancel(order_id)
    requote(market, "99.00", "99.02")
    assert (await broker.get_order(order_id)).status is OrderStatus.CANCELED
    assert (await broker.get_account()).position("SPY") == 0


async def test_cancelling_a_filled_order_changes_nothing(broker):
    order_id = await broker.place(market_order(Side.BUY, 1))
    await broker.cancel(order_id)
    assert (await broker.get_order(order_id)).status is OrderStatus.FILLED


async def test_open_orders_lists_only_working_orders(broker):
    await broker.place(market_order(Side.BUY, 1))
    resting = await broker.place(limit(Side.BUY, 1, "99.00"))
    assert [o.order_id for o in await broker.get_open_orders()] == [resting]


async def test_equity_marks_positions_at_the_bid(broker, market):
    await broker.place(market_order(Side.BUY, 2))  # cash 799.96
    requote(market, "110.00", "110.02")
    assert (await broker.get_account()).equity == Decimal("1019.96")


async def test_unknown_order_id_is_an_error(broker):
    with pytest.raises(BrokerError):
        await broker.get_order("nope")


async def test_account_survives_a_restart(market):
    store = MemoryStateStore()
    first = PaperBroker(market, ManualClock(T0), starting_cash=Decimal(1000), store=store)
    await first.load()
    first_id = await first.place(market_order(Side.BUY, 2))

    restarted = PaperBroker(market, ManualClock(T0), starting_cash=Decimal(1000), store=store)
    await restarted.load()
    account = await restarted.get_account()
    assert (account.cash_available, account.position("SPY")) == (Decimal("799.96"), 2)
    # Order ids keep counting up so they never collide with earlier ones.
    assert await restarted.place(market_order(Side.BUY, 1)) != first_id


async def test_starting_cash_only_applies_to_a_brand_new_account(market):
    store = MemoryStateStore()
    first = PaperBroker(market, ManualClock(T0), starting_cash=Decimal(1000), store=store)
    await first.load()
    await first.place(market_order(Side.BUY, 2))
    again = PaperBroker(market, ManualClock(T0), starting_cash=Decimal(50000), store=store)
    await again.load()
    assert (await again.get_account()).cash_available == Decimal("799.96")


# --- looking an order up by its details (used when a placement reply is lost) ----


async def test_find_order_returns_a_matching_recent_order(broker):
    order_id = await broker.place(limit(Side.BUY, 2, "99.90"))
    found = await broker.find_order("SPY", Side.BUY, 2, T0 - timedelta(seconds=5))
    assert found is not None and found.order_id == order_id


async def test_find_order_reports_finished_orders_too(broker):
    order_id = await broker.place(market_order(Side.BUY, 2))
    found = await broker.find_order("SPY", Side.BUY, 2, T0 - timedelta(seconds=5))
    assert (found.order_id, found.status) == (order_id, OrderStatus.FILLED)


@pytest.mark.parametrize(
    ("symbol", "side", "quantity"),
    [("QQQ", Side.BUY, 2), ("SPY", Side.SELL, 2), ("SPY", Side.BUY, 3)],
)
async def test_find_order_ignores_orders_with_different_details(broker, symbol, side, quantity):
    await broker.place(market_order(Side.BUY, 2))
    assert await broker.find_order(symbol, side, quantity, T0 - timedelta(seconds=5)) is None


async def test_find_order_ignores_orders_entered_before_the_cutoff(broker):
    await broker.place(market_order(Side.BUY, 2))
    assert await broker.find_order("SPY", Side.BUY, 2, T0 + timedelta(seconds=1)) is None


async def test_find_order_prefers_the_most_recent_match(market):
    clock = ManualClock(T0)
    broker = PaperBroker(market, clock, starting_cash=Decimal(1000))
    await broker.place(market_order(Side.BUY, 2))
    clock.advance(30)
    second = await broker.place(market_order(Side.BUY, 2))
    found = await broker.find_order("SPY", Side.BUY, 2, T0 - timedelta(seconds=5))
    assert found.order_id == second


async def test_an_order_left_resting_before_a_restart_reads_as_cancelled(market):
    store = MemoryStateStore("paper")
    first = PaperBroker(market, ManualClock(T0), starting_cash=Decimal(1000), store=store)
    order_id = await first.place(limit(Side.BUY, 2, "90.00"))  # rests: far below the market
    restarted = PaperBroker(market, ManualClock(T0), starting_cash=Decimal(1000), store=store)
    await restarted.load()
    order = await restarted.get_order(order_id)
    assert (order.status, order.filled_quantity) == (OrderStatus.CANCELED, 0)
    await restarted.cancel(order_id)  # and cancelling it is not an error
    assert (await restarted.get_account()).cash_available == Decimal(1000)


# ------------------------------------------------------------------------- options

CALL = "SPY   261016C00500000"


def option_limit(side, qty, price) -> OrderRequest:
    return OrderRequest(CALL, side, qty, OrderType.LIMIT, Decimal(price))


@pytest.fixture
def option_market(market):
    market.on_quote(make_quote(CALL, "2.00", "2.10"))
    return market


async def test_an_option_contract_costs_a_hundred_times_its_price(option_market):
    broker = PaperBroker(option_market, ManualClock(T0), starting_cash=Decimal(1000))
    await broker.place(option_limit(Side.BUY, 2, "2.10"))
    account = await broker.get_account()
    assert account.cash_available == Decimal("580.00")
    assert account.position(CALL) == 2
    assert account.equity == Decimal("980.00")  # marked at the bid: 2 x 100 x 2.00


async def test_an_option_buy_needs_cash_for_the_whole_contract(option_market):
    broker = PaperBroker(option_market, ManualClock(T0), starting_cash=Decimal(200))
    with pytest.raises(OrderRejected, match=r"not enough cash: need 210\.00"):
        await broker.place(option_limit(Side.BUY, 1, "2.10"))


async def test_selling_an_option_brings_in_a_hundred_times_its_price(option_market):
    broker = PaperBroker(option_market, ManualClock(T0), starting_cash=Decimal(1000))
    await broker.place(option_limit(Side.BUY, 1, "2.10"))
    await broker.place(option_limit(Side.SELL, 1, "2.00"))
    account = await broker.get_account()
    assert (account.cash_available, account.position(CALL)) == (Decimal("990.00"), 0)


async def test_adding_to_an_option_keeps_the_average_price_per_share(option_market):
    broker = PaperBroker(option_market, ManualClock(T0), starting_cash=Decimal(1000))
    await broker.place(option_limit(Side.BUY, 1, "2.10"))
    option_market.on_quote(make_quote(CALL, "2.20", "2.30", at=T0 + timedelta(seconds=1)))
    await broker.place(option_limit(Side.BUY, 1, "2.30"))
    account = await broker.get_account()
    assert account.positions[CALL].avg_price == Decimal("2.20")
