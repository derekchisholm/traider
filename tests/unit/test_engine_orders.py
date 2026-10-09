"""Order lifecycle: working orders, timeouts, partial fills, and never sending a duplicate."""

from decimal import Decimal

import pytest

from tests.unit.engine_harness import Harness
from traider.broker.base import AmbiguousOrder, BrokerUnavailable, OrderRejected
from traider.models import OrderRequest, OrderType, Side


@pytest.fixture
async def h(tmp_path):
    return await Harness.create(tmp_path)


# --- working orders ------------------------------------------------------------


async def test_no_second_order_while_one_is_working(h):
    h.broker.hold_fills = True
    await h.target("SPY", 3)
    await h.run_for(10)
    assert h.broker.calls["place"] == 1


async def test_unfilled_order_is_cancelled_after_the_timeout(h):
    h.broker.hold_fills = True
    await h.target("SPY", 3)
    await h.run_for(19)
    assert h.broker.cancelled == []
    await h.run_for(3)
    assert len(h.broker.cancelled) == 1


async def test_after_a_cancel_the_order_is_repriced_and_sent_again(h):
    h.broker.hold_fills = True
    await h.target("SPY", 3)
    await h.run_for(22)  # times out and is cancelled
    h.price("SPY", "101.00", "101.02")
    await h.run_for(10)
    assert h.broker.calls["place"] == 2
    assert h.broker.placed[-1].limit_price == Decimal("101.07")


async def test_cancel_is_requested_once_not_on_every_poll(h):
    h.broker.hold_fills = True
    h.broker.ignore_cancels = True  # the broker is slow to act on the cancel
    await h.target("SPY", 3)
    await h.run_for(40)
    assert h.broker.cancelled == ["P1"]


async def test_a_failed_cancel_is_tried_again(h):
    h.broker.hold_fills = True
    h.broker.cancel_error = BrokerUnavailable("503")
    await h.target("SPY", 3)
    await h.run_for(25)
    assert h.broker.cancelled == []
    h.broker.cancel_error = None
    await h.run_for(3)
    assert len(h.broker.cancelled) == 1


async def test_partial_fill_then_cancel_leaves_only_the_remainder_to_buy(h):
    h.broker.hold_fills = True
    await h.target("SPY", 3)
    h.broker.fill_partially_then_cancel("P1", 1)
    h.broker.hold_fills = False
    await h.settle(10)
    assert [o.quantity for o in h.broker.placed] == [3, 2]
    assert h.position() == 3


async def test_order_status_errors_do_not_lose_track_of_the_order(h):
    h.broker.hold_fills = True
    await h.target("SPY", 3)
    h.broker.order_error = BrokerUnavailable("timeout")
    await h.run_for(5)
    h.broker.order_error = None
    h.broker.hold_fills = False
    await h.settle()
    assert h.broker.calls["place"] == 1
    assert h.position() == 3


# --- the broker's view lags or disagrees -----------------------------------------


async def test_no_rebuy_while_the_brokers_position_lags_behind_the_fill(h):
    stale = await h.broker.get_account()  # position 0
    await h.target("SPY", 3)  # fills at once inside the paper broker
    h.broker.frozen_account = stale  # ...but the account endpoint still shows 0
    await h.run_for(60)
    assert h.broker.calls["place"] == 1
    h.broker.frozen_account = None
    await h.run_for(30)
    assert h.broker.calls["place"] == 1
    assert h.position() == 3


async def test_symbol_is_frozen_when_the_fill_never_shows_up_in_the_account(h):
    stale = await h.broker.get_account()
    await h.target("SPY", 3)
    h.broker.frozen_account = stale
    await h.run_for(400)
    # The bot must not "fix" the mismatch by buying again.
    assert h.broker.calls["place"] == 1
    assert "symbol_frozen:SPY" in h.alert_keys()
    (frozen,) = await h.events("symbol_frozen")
    assert frozen["data"]["symbol"] == "SPY"


async def test_a_frozen_symbol_stays_frozen_even_if_the_target_changes(h):
    stale = await h.broker.get_account()
    await h.target("SPY", 3)
    h.broker.frozen_account = stale
    await h.run_for(400)
    await h.target("SPY", 6)
    await h.run_for(60)
    assert h.broker.calls["place"] == 1


async def test_freezing_one_symbol_does_not_stop_the_others(tmp_path):
    h = await Harness.create(tmp_path, symbols=("SPY", "QQQ"))
    stale = await h.broker.get_account()
    await h.target("SPY", 3)
    h.broker.frozen_account = stale
    await h.run_for(400)
    h.broker.frozen_account = None
    await h.target("QQQ", 2)
    await h.settle()
    assert h.position("QQQ") == 2


# --- placement goes wrong ----------------------------------------------------------


async def test_broker_rejection_is_reported_and_not_hammered(h):
    h.broker.place_error = OrderRejected("insufficient settled funds")
    await h.target("SPY", 3)
    await h.run_for(30)
    assert h.broker.calls["place"] == 1
    assert "order_rejected:SPY" in h.alert_keys()
    (event,) = await h.events("order_rejected")
    assert "insufficient settled funds" in event["data"]["error"]


async def test_a_rejected_order_is_tried_again_later(h):
    h.broker.place_error = OrderRejected("try later")
    await h.target("SPY", 3)
    h.broker.place_error = None
    await h.run_for(90)
    assert h.position() == 3


async def test_unknown_outcome_that_actually_filled_is_not_sent_again(h):
    # The order reached the broker and filled, but the reply was lost.
    h.broker.place_error_after_accepting = AmbiguousOrder("read timeout")
    await h.target("SPY", 3)
    h.broker.place_error_after_accepting = None
    await h.run_for(300)
    assert h.broker.calls["place"] == 1
    assert h.position() == 3
    assert "order_unconfirmed:SPY" in h.alert_keys()


async def test_unknown_outcome_that_never_reached_the_broker_is_retried_after_a_wait(h):
    h.broker.place_error = AmbiguousOrder("connection reset")
    await h.target("SPY", 3)
    h.broker.place_error = None
    await h.run_for(30)
    assert h.broker.calls["place"] == 1  # still waiting to see what happened
    await h.run_for(60)
    assert h.broker.calls["place"] == 2
    assert h.position() == 3


async def test_unknown_outcome_with_the_order_still_working_is_found_and_managed(h):
    h.broker.hold_fills = True
    h.broker.place_error_after_accepting = AmbiguousOrder("read timeout")
    await h.target("SPY", 3)
    h.broker.place_error_after_accepting = None
    # The order sits open at the broker under an id the bot never received. The bot
    # looks it up by its details, then treats it like any other working order.
    await h.run_for(15)
    assert h.broker.calls["place"] == 1
    (adopted,) = await h.events("order_adopted")
    assert adopted["data"]["order_id"] == "P1"
    await h.run_for(8)  # past the order timeout
    assert h.broker.cancelled == ["P1"]
    assert h.broker.calls["place"] == 1  # nothing new while the first was still open


async def test_unknown_outcome_that_filled_is_not_duplicated_even_if_the_account_lags(h):
    stale = await h.broker.get_account()
    h.broker.place_error_after_accepting = AmbiguousOrder("read timeout")
    await h.target("SPY", 3)
    h.broker.place_error_after_accepting = None
    h.broker.frozen_account = stale  # the account endpoint keeps showing no position
    await h.run_for(400)
    assert h.broker.calls["place"] == 1


async def test_unknown_outcome_is_not_retried_until_the_broker_confirms_no_such_order(h):
    h.broker.place_error = AmbiguousOrder("connection reset")
    h.broker.find_error = BrokerUnavailable("503")
    await h.target("SPY", 3)
    h.broker.place_error = None
    await h.run_for(200)
    assert h.broker.calls["place"] == 1  # cannot rule the first order out yet
    h.broker.find_error = None
    await h.run_for(30)
    assert h.broker.calls["place"] == 2
    assert h.position() == 3


async def test_unexplained_position_change_after_a_lost_order_freezes_the_symbol(h):
    h.broker.place_error = AmbiguousOrder("connection reset")  # never reaches the broker
    await h.target("SPY", 3)
    h.broker.place_error = None
    # Meanwhile the position moves for some other reason: neither the old 0 nor the 3
    # the lost order would have produced.
    await h.broker.place(OrderRequest("SPY", Side.BUY, 1, OrderType.MARKET, None))
    await h.run_for(300)
    assert h.broker.calls["place"] == 2  # the lost attempt and the outside trade, nothing more
    assert "symbol_frozen:SPY" in h.alert_keys()


async def test_an_earlier_identical_order_is_not_mistaken_for_the_lost_one(h):
    await h.target("SPY", 3)  # P1 buys 3 and fills
    await h.settle(3)
    await h.target("SPY", 0)  # P2 sells 3
    await h.settle(3)
    h.broker.place_error = AmbiguousOrder("connection reset")  # third order never arrives
    await h.target("SPY", 3)
    h.broker.place_error = None
    await h.run_for(30)
    assert await h.events("order_adopted") == []
    assert h.position() == 0  # still waiting: P1 must not count as the new order
    await h.run_for(60)
    assert h.position() == 3


async def test_broker_unreachable_before_sending_is_simply_retried(h):
    h.broker.place_error = BrokerUnavailable("no access token")
    await h.target("SPY", 3)
    h.broker.place_error = None
    await h.run_for(30)
    assert h.position() == 3


async def test_order_accepted_without_an_id_is_not_sent_again(h):
    h.broker.drop_order_id = True
    await h.target("SPY", 3)
    await h.run_for(300)
    assert h.broker.calls["place"] == 1
    assert h.position() == 3


async def test_order_accepted_without_an_id_is_not_resent_even_if_the_account_lags(h):
    stale = await h.broker.get_account()
    h.broker.drop_order_id = True
    await h.target("SPY", 3)
    h.broker.frozen_account = stale
    await h.run_for(400)
    assert h.broker.calls["place"] == 1


async def test_an_unexpected_error_from_the_broker_is_treated_as_unknown_outcome(h):
    h.broker.place_error_after_accepting = RuntimeError("bug in client")
    await h.target("SPY", 3)
    h.broker.place_error_after_accepting = None
    await h.run_for(300)
    assert h.broker.calls["place"] == 1


# --- orders the bot did not place ----------------------------------------------------


async def test_someone_elses_open_order_on_the_symbol_blocks_new_orders(h):
    h.broker.add_foreign_order("SPY")
    await h.target("SPY", 3)
    await h.run_for(60)
    assert h.broker.calls["place"] == 0
    assert h.broker.cancelled == []


async def test_trading_resumes_once_the_foreign_order_is_gone(h):
    h.broker.add_foreign_order("SPY")
    await h.target("SPY", 3)
    await h.run_for(30)
    h.broker.foreign_orders.clear()
    await h.run_for(30)
    assert h.position() == 3


async def test_foreign_orders_on_other_symbols_do_not_block(h):
    h.broker.add_foreign_order("QQQ")
    await h.target("SPY", 3)
    await h.settle()
    assert h.position() == 3


async def test_a_foreign_order_that_lingers_raises_an_alert(h):
    h.broker.add_foreign_order("SPY")
    await h.target("SPY", 3)
    await h.run_for(400)
    assert "unknown_order:SPY" in h.alert_keys()


async def test_unknown_orders_are_cancelled_only_when_that_is_switched_on(tmp_path):
    h = await Harness.create(tmp_path, config={"cancel_unknown_orders": True})
    h.broker.add_foreign_order("SPY", order_id="F9")
    await h.target("SPY", 3)
    await h.run_for(60)
    assert h.broker.cancelled == ["F9"]
    await h.settle(30)
    assert h.position() == 3


# --- restart -----------------------------------------------------------------------------


async def test_restart_resumes_a_working_order_instead_of_sending_another(tmp_path):
    first = await Harness.create(tmp_path)
    first.broker.hold_fills = True
    await first.target("SPY", 3)

    second = await Harness.create(tmp_path, restart_of=first)
    await second.target("SPY", 3)
    await second.run_for(10)
    assert second.broker.calls["place"] == 1  # only the order from before the restart

    await second.run_for(15)  # past the order timeout: the resumed order is cancelled
    assert second.broker.cancelled == ["P1"]


async def test_position_is_read_from_the_broker_after_a_restart(tmp_path):
    first = await Harness.create(tmp_path)
    await first.target("SPY", 3)
    await first.settle()

    second = await Harness.create(tmp_path, backing=first.backing)
    await second.target("SPY", 3)
    await second.run_for(30)
    assert second.broker.calls["place"] == 0
    assert second.position() == 3


# --- pre-trade refresh ---------------------------------------------------------------------


async def test_order_is_sized_from_a_fresh_look_at_the_broker_not_a_cached_one(h):
    # Someone buys 3 SPY by hand; the engine's cached snapshot still says 0.
    await h.broker.place(OrderRequest("SPY", Side.BUY, 3, OrderType.MARKET, None))
    h.broker.placed.clear()
    h.broker.calls.clear()
    await h.target("SPY", 3)
    await h.run_for(10)
    assert h.broker.calls["place"] == 0


async def test_no_order_when_the_broker_cannot_be_read_before_trading(h):
    await h.tick(1)
    h.broker.account_error = BrokerUnavailable("503")
    await h.target("SPY", 3)
    await h.run_for(20)
    assert h.broker.calls["place"] == 0
    h.broker.account_error = None
    await h.run_for(40)
    assert h.position() == 3
