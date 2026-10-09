from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from tests.fakes.schwab_server import (
    ACCOUNT_HASH,
    SECOND_ACCOUNT_HASH,
    SECOND_ACCOUNT_NUMBER,
)
from traider.broker.base import AmbiguousOrder, BrokerError, BrokerUnavailable, OrderRejected
from traider.broker.schwab import SchwabBroker
from traider.models import OrderRequest, OrderStatus, OrderType, Side
from traider.schwab.hours import SchwabSessionProvider
from traider.timeutil import SystemClock

BUY3 = OrderRequest("SPY", Side.BUY, 3, OrderType.LIMIT, Decimal("100.07"))


@pytest.fixture
def broker(client, schwab):
    schwab.set_quote("SPY", 100.00, 100.02)
    return SchwabBroker(client, SystemClock())


def two_accounts(schwab):
    schwab.accounts[SECOND_ACCOUNT_HASH] = SECOND_ACCOUNT_NUMBER


# --- which account ---------------------------------------------------------------


async def test_the_only_linked_account_is_used_when_none_is_named(broker, schwab):
    await broker.place(BUY3)
    (order,) = schwab.orders.values()
    assert order.account_hash == ACCOUNT_HASH


async def test_account_is_chosen_by_its_last_four_digits(client, schwab):
    two_accounts(schwab)
    broker = SchwabBroker(client, SystemClock(), account_last4="4321")
    await broker.place(BUY3)
    (order,) = schwab.orders.values()
    assert order.account_hash == SECOND_ACCOUNT_HASH


async def test_account_is_chosen_by_hash(client, schwab):
    two_accounts(schwab)
    broker = SchwabBroker(client, SystemClock(), account_hash=SECOND_ACCOUNT_HASH)
    await broker.place(BUY3)
    (order,) = schwab.orders.values()
    assert order.account_hash == SECOND_ACCOUNT_HASH


async def test_several_accounts_and_no_choice_is_an_error_not_a_guess(client, schwab):
    two_accounts(schwab)
    broker = SchwabBroker(client, SystemClock())
    with pytest.raises(BrokerError, match="2 accounts"):
        await broker.get_account()
    assert schwab.orders == {}


async def test_unknown_last_four_lists_the_choices_without_full_numbers(client, schwab):
    two_accounts(schwab)
    broker = SchwabBroker(client, SystemClock(), account_last4="0000")
    with pytest.raises(BrokerError) as caught:
        await broker.get_account()
    message = str(caught.value)
    assert "5678" in message and "4321" in message
    assert "12345678" not in message and "87654321" not in message


async def test_hash_that_is_not_a_linked_account_is_an_error(client, schwab):
    broker = SchwabBroker(client, SystemClock(), account_hash="NOT-MINE")
    with pytest.raises(BrokerError, match="not one of the linked accounts"):
        await broker.get_account()


async def test_account_lookup_happens_once(broker, schwab):
    await broker.get_account()
    await broker.get_account()
    await broker.get_open_orders()
    assert len(schwab.calls("GET", "/accountNumbers")) == 1


async def test_failed_account_lookup_is_retried_next_time(broker, schwab):
    schwab.fail("GET", "/accountNumbers", 503, times=3)
    with pytest.raises(BrokerUnavailable):
        await broker.get_account()
    assert (await broker.get_account()).equity == Decimal("10000.0")


# --- account ------------------------------------------------------------------------


async def test_account_snapshot_reflects_cash_and_positions(broker, schwab):
    schwab.positions["SPY"] = (5.0, 99.5)
    account = await broker.get_account()
    assert account.cash_available == Decimal("10000.0")
    assert account.equity == Decimal("10500.0")  # 5 shares marked at the 100.00 bid
    assert account.position("SPY") == 5
    assert account.account_type == "MARGIN"


async def test_account_read_failure_is_unavailable(broker, schwab):
    await broker.get_account()
    schwab.fail("GET", f"/accounts/{ACCOUNT_HASH}", 503, times=3)
    with pytest.raises(BrokerUnavailable):
        await broker.get_account()


# --- placing ---------------------------------------------------------------------------


async def test_order_reaches_schwab_in_its_schema(broker, schwab):
    order_id = await broker.place(BUY3)
    assert schwab.orders[int(order_id)].body == {
        "orderType": "LIMIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "price": "100.07",
        "orderLegCollection": [
            {
                "instruction": "BUY",
                "quantity": 3,
                "instrument": {"symbol": "SPY", "assetType": "EQUITY"},
            }
        ],
    }


async def test_schwab_refusal_is_a_rejection_with_the_reason(broker, schwab):
    schwab.reject_orders_with = "Insufficient settled funds"
    with pytest.raises(OrderRejected, match="Insufficient settled funds"):
        await broker.place(BUY3)


async def test_server_error_on_placement_is_an_unknown_outcome(broker, schwab):
    schwab.fail("POST", "/orders", 502)
    with pytest.raises(AmbiguousOrder):
        await broker.place(BUY3)


async def test_lost_reply_on_placement_is_an_unknown_outcome(broker, schwab):
    schwab.fail("POST", "/orders", "drop_after")
    with pytest.raises(AmbiguousOrder):
        await broker.place(BUY3)
    assert len(schwab.orders) == 1


async def test_rate_limit_on_placement_means_nothing_was_placed(broker, schwab):
    schwab.fail("POST", "/orders", 429)
    with pytest.raises(BrokerUnavailable):
        await broker.place(BUY3)


async def test_lost_login_means_nothing_was_placed(broker, schwab):
    await broker.get_account()
    schwab.revoke_refresh_tokens()
    schwab.expire_access_tokens()
    with pytest.raises(BrokerUnavailable):
        await broker.place(BUY3)
    assert schwab.orders == {}


async def test_account_lookup_failure_means_nothing_was_placed(broker, schwab):
    schwab.fail("GET", "/accountNumbers", 503, times=3)
    with pytest.raises(BrokerUnavailable):
        await broker.place(BUY3)
    assert schwab.calls("POST", "/orders") == []


async def test_accepted_order_without_an_id_returns_none(broker, schwab):
    schwab.omit_location_header = True
    assert await broker.place(BUY3) is None


async def test_malformed_order_is_refused_locally(broker, schwab):
    bad = OrderRequest("SPY", Side.BUY, 3, OrderType.LIMIT, Decimal("100.071"))
    with pytest.raises(OrderRejected):
        await broker.place(bad)
    assert schwab.calls("POST", "/orders") == []


# --- following an order ---------------------------------------------------------------------


async def test_filled_order_is_reported_with_its_price(broker, schwab):
    order = await broker.get_order(await broker.place(BUY3))
    assert (order.status, order.filled_quantity, order.avg_fill_price) == (
        OrderStatus.FILLED,
        3,
        Decimal("100.07"),
    )


async def test_working_order_is_reported_as_working(broker, schwab):
    schwab.fill_on_place = False
    order = await broker.get_order(await broker.place(BUY3))
    assert (order.status, order.filled_quantity) == (OrderStatus.WORKING, 0)


async def test_open_orders_exclude_finished_ones(broker, schwab):
    await broker.place(BUY3)  # fills
    schwab.fill_on_place = False
    resting = await broker.place(BUY3)
    assert [o.order_id for o in await broker.get_open_orders()] == [resting]


async def test_open_orders_cover_the_last_few_days(broker, schwab):
    schwab.fill_on_place = False
    order_id = await broker.place(BUY3)
    schwab.orders[int(order_id)].entered = datetime.now(UTC) - timedelta(days=2)
    assert [o.order_id for o in await broker.get_open_orders()] == [order_id]


async def test_open_orders_include_one_schwab_stamped_slightly_ahead_of_our_clock(broker, schwab):
    schwab.fill_on_place = False
    order_id = await broker.place(BUY3)
    schwab.orders[int(order_id)].entered = datetime.now(UTC) + timedelta(seconds=5)
    assert [o.order_id for o in await broker.get_open_orders()] == [order_id]


async def test_orders_in_other_accounts_are_not_listed(client, schwab):
    two_accounts(schwab)
    schwab.fill_on_place = False
    schwab.set_quote("SPY", 100.0, 100.02)
    await SchwabBroker(client, SystemClock(), account_last4="4321").place(BUY3)
    mine = SchwabBroker(client, SystemClock(), account_last4="5678")
    assert await mine.get_open_orders() == []


async def test_cancel_stops_a_working_order(broker, schwab):
    schwab.fill_on_place = False
    order_id = await broker.place(BUY3)
    await broker.cancel(order_id)
    assert (await broker.get_order(order_id)).status is OrderStatus.CANCELED


async def test_cancelling_an_order_that_already_finished_is_not_an_error(broker):
    order_id = await broker.place(BUY3)  # filled at once
    await broker.cancel(order_id)


async def test_cancel_refused_for_a_live_order_is_an_error(broker, schwab):
    schwab.fill_on_place = False
    order_id = await broker.place(BUY3)
    schwab.fail("DELETE", "/orders", 400, body={"message": "Order cannot be canceled"})
    with pytest.raises(BrokerError, match="cannot be canceled"):
        await broker.cancel(order_id)


# --- finding an order whose placement reply was lost ------------------------------------------


async def test_lost_order_is_found_by_its_details(broker, schwab):
    since = datetime.now(UTC) - timedelta(seconds=5)
    schwab.fail("POST", "/orders", "drop_after")
    with pytest.raises(AmbiguousOrder):
        await broker.place(BUY3)
    found = await broker.find_order("SPY", Side.BUY, 3, since)
    assert found is not None
    assert int(found.order_id) in schwab.orders


async def test_find_order_returns_nothing_when_the_order_never_arrived(broker):
    since = datetime.now(UTC) - timedelta(seconds=5)
    assert await broker.find_order("SPY", Side.BUY, 3, since) is None


@pytest.mark.parametrize(
    ("symbol", "side", "quantity"),
    [("QQQ", Side.BUY, 3), ("SPY", Side.SELL, 3), ("SPY", Side.BUY, 4)],
)
async def test_find_order_needs_symbol_side_and_quantity_to_match(broker, symbol, side, quantity):
    since = datetime.now(UTC) - timedelta(seconds=5)
    await broker.place(BUY3)
    assert await broker.find_order(symbol, side, quantity, since) is None


async def test_find_order_ignores_older_orders(broker, schwab):
    order_id = await broker.place(BUY3)
    schwab.orders[int(order_id)].entered = datetime.now(UTC) - timedelta(minutes=10)
    since = datetime.now(UTC) - timedelta(seconds=30)
    assert await broker.find_order("SPY", Side.BUY, 3, since) is None


async def test_find_order_returns_the_newest_match(broker, schwab):
    since = datetime.now(UTC) - timedelta(minutes=5)
    first = await broker.place(BUY3)
    schwab.orders[int(first)].entered = datetime.now(UTC) - timedelta(minutes=1)
    second = await broker.place(BUY3)
    assert (await broker.find_order("SPY", Side.BUY, 3, since)).order_id == second


# --- market hours ---------------------------------------------------------------------------------


async def test_session_hours_come_from_schwabs_calendar(client, schwab):
    schwab.session_end = "13:00:00"  # the half day after Thanksgiving; New York is on UTC-5
    session = await SchwabSessionProvider(client).session_for(date(2026, 11, 27))
    assert session.open == datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
    assert session.close == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


async def test_holiday_has_no_session(client, schwab):
    schwab.market_open = False
    session = await SchwabSessionProvider(client).session_for(date(2026, 12, 25))
    assert (session.open, session.close) == (None, None)


async def test_calendar_failure_is_raised_so_the_market_is_treated_as_closed(client, schwab):
    schwab.fail("GET", "/markets", 503, times=3)
    with pytest.raises(Exception, match="503"):
        await SchwabSessionProvider(client).session_for(date(2026, 10, 8))
