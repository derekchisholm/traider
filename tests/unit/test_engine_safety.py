"""Kill switch, leadership, loss limit, session rules and failure tolerance."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.unit.engine_harness import Harness
from tests.unit.helpers import T0
from traider.broker.base import BrokerUnavailable
from traider.models import Side, Target
from traider.session import Session


@pytest.fixture
async def h(tmp_path):
    return await Harness.create(tmp_path)


async def bought(h, quantity=3):
    await h.target("SPY", quantity)
    await h.settle()
    assert h.position() == quantity


# --- control switch ---------------------------------------------------------------


async def test_halt_stops_new_orders(h):
    await h.set_control("halt")
    await h.target("SPY", 3)
    await h.run_for(30)
    assert h.broker.calls["place"] == 0


async def test_halt_cancels_working_orders_straight_away(h):
    h.broker.hold_fills = True
    await h.target("SPY", 3)
    await h.set_control("halt")
    assert h.broker.cancelled == ["P1"]


async def test_halt_leaves_existing_positions_alone(h):
    await bought(h)
    await h.set_control("halt")
    await h.target("SPY", 0)
    await h.run_for(30)
    assert h.position() == 3


async def test_close_only_lets_exits_through(h):
    await bought(h)
    await h.set_control("close_only")
    await h.target("SPY", 0)
    await h.settle()
    assert h.position() == 0


async def test_close_only_blocks_entries(h):
    await h.set_control("close_only")
    await h.target("SPY", 3)
    await h.run_for(30)
    assert h.broker.calls["place"] == 0


async def test_trading_resumes_when_control_returns_to_the_deployed_mode(h):
    await h.set_control("halt")
    await h.target("SPY", 3)
    await h.set_control("paper")
    await h.settle()
    assert h.position() == 3


async def test_live_deploy_does_nothing_until_control_says_live(tmp_path):
    h = await Harness.create(tmp_path, trading_mode="live", control="paper")
    await h.target("SPY", 3)
    await h.run_for(30)
    assert h.broker.calls["place"] == 0
    await h.set_control("live")
    await h.settle()
    assert h.position() == 3


async def test_paper_deploy_cannot_be_armed_by_setting_control_to_live(tmp_path):
    h = await Harness.create(tmp_path, trading_mode="paper", control="live")
    await h.target("SPY", 3)
    await h.run_for(30)
    assert h.broker.calls["place"] == 0


async def test_unreadable_control_halts_trading_after_a_minute(h):
    h.control_source.error = RuntimeError("ssm unreachable")
    await h.run_for(70)
    await h.target("SPY", 3)
    await h.run_for(30)
    assert h.broker.calls["place"] == 0


async def test_control_change_is_picked_up_within_seconds(h):
    h.control_source.value = "halt"
    await h.run_for(11)
    await h.target("SPY", 3)
    await h.run_for(5)
    assert h.broker.calls["place"] == 0


# --- only one instance trades -------------------------------------------------------


async def test_second_instance_on_the_same_state_does_not_trade(tmp_path):
    leader = await Harness.create(tmp_path, instance="bot-1")
    follower = await Harness.create(tmp_path, backing=leader.backing, instance="bot-2")
    await follower.target("SPY", 3)
    await follower.run_for(20)
    assert follower.broker.calls["place"] == 0
    assert follower.engine.is_leader is False
    assert leader.engine.is_leader is True


async def test_second_instance_takes_over_when_the_first_goes_away(tmp_path):
    leader = await Harness.create(tmp_path, instance="bot-1")
    follower = await Harness.create(tmp_path, backing=leader.backing, instance="bot-2")
    await follower.target("SPY", 3)
    # The leader stops renewing; its lease runs out after 30 seconds.
    await follower.run_for(45)
    assert follower.engine.is_leader is True
    await follower.settle()
    assert follower.position() == 3


async def test_running_instance_keeps_its_lease(tmp_path):
    leader = await Harness.create(tmp_path, instance="bot-1")
    await leader.run_for(120)
    follower = await Harness.create(
        tmp_path, backing=leader.backing, instance="bot-2", start=leader.clock.now()
    )
    assert follower.engine.is_leader is False


async def test_shutdown_releases_the_lease_for_the_next_instance(tmp_path):
    leader = await Harness.create(tmp_path, instance="bot-1")
    await leader.engine.shutdown()
    follower = await Harness.create(tmp_path, backing=leader.backing, instance="bot-2")
    assert follower.engine.is_leader is True


async def test_a_standby_leaves_the_leaders_working_order_alone(tmp_path):
    quick = {"order_timeout_s": 10}
    leader = await Harness.create(tmp_path, instance="bot-1", config=quick)
    leader.broker.hold_fills = True
    await leader.target("SPY", 3)
    standby = await Harness.create(tmp_path, restart_of=leader, instance="bot-2", config=quick)
    await standby.run_for(15)  # past the order timeout, inside the leader's lease
    assert standby.engine.is_leader is False
    assert standby.broker.cancelled == []
    await standby.engine.shutdown()
    assert standby.broker.cancelled == []
    assert await leader.store.open_orders() != []  # and the leader's record is still there


async def test_a_standby_that_takes_over_picks_up_the_orders_left_behind(tmp_path):
    quick = {"order_timeout_s": 10}
    leader = await Harness.create(tmp_path, instance="bot-1", config=quick)
    leader.broker.hold_fills = True
    await leader.target("SPY", 3)
    standby = await Harness.create(tmp_path, restart_of=leader, instance="bot-2", config=quick)
    await standby.run_for(50)  # the leader is gone; its lease runs out
    assert standby.engine.is_leader is True
    assert standby.broker.cancelled == ["P1"]  # the stale order is managed, not duplicated
    assert standby.broker.calls["place"] == 1


async def test_no_order_goes_out_once_the_lease_has_run_out_mid_step(h):
    slow = h.broker.get_open_orders

    async def stalled():
        h.clock.advance(31)  # the broker call hangs for longer than the lease lasts
        return await slow()

    h.broker.get_open_orders = stalled
    await h.target("SPY", 3)
    assert h.broker.calls["place"] == 0
    h.broker.get_open_orders = slow
    await h.settle(20)  # lease renewed: now it may trade
    assert h.position() == 3


async def test_no_trading_when_the_lease_cannot_be_checked(h, monkeypatch):
    async def broken(*_args, **_kwargs):
        raise RuntimeError("dynamodb unreachable")

    monkeypatch.setattr(h.store, "acquire_lease", broken)
    await h.run_for(15)
    await h.target("SPY", 3)
    await h.run_for(20)
    assert h.broker.calls["place"] == 0


# --- daily loss limit ---------------------------------------------------------------------


async def test_loss_beyond_the_daily_limit_halts_entries_for_the_day(h):
    await bought(h, 5)
    h.price("SPY", "79.00", "79.02")  # 5 shares down about 21 each: over the 100 limit
    await h.run_for(40)
    day = await h.store.get_day("2026-10-08")
    assert day.halted_reason is not None
    assert "day_halted" in h.alert_keys()

    await h.target("SPY", 7)
    await h.run_for(30)
    assert h.position() == 5


async def test_loss_halt_still_allows_selling(h):
    await bought(h, 5)
    h.price("SPY", "79.00", "79.02")
    await h.run_for(40)
    await h.target("SPY", 0)
    await h.settle()
    assert h.position() == 0


async def test_small_loss_does_not_halt(h):
    await bought(h, 5)
    h.price("SPY", "95.00", "95.02")  # down about 25
    await h.run_for(40)
    assert (await h.store.get_day("2026-10-08")).halted_reason is None


async def test_loss_is_measured_from_the_first_equity_seen_that_day_even_after_a_restart(tmp_path):
    first = await Harness.create(tmp_path)
    await bought(first, 5)
    first.price("SPY", "90.00", "90.02")  # down about 50, under the limit
    await first.run_for(40)

    second = await Harness.create(tmp_path, backing=first.backing, start=first.clock.now())
    second.price("SPY", "79.00", "79.02")  # now down about 105 from the morning
    await second.run_for(40)
    assert (await second.store.get_day("2026-10-08")).halted_reason is not None


async def test_loss_halt_is_remembered_across_a_restart(tmp_path):
    first = await Harness.create(tmp_path)
    await bought(first, 5)
    first.price("SPY", "79.00", "79.02")
    await first.run_for(40)

    second = await Harness.create(tmp_path, backing=first.backing, start=first.clock.now())
    second.price("SPY", "100.00", "100.02")  # price recovered; the halt stays
    await second.target("SPY", 8)
    await second.run_for(30)
    assert second.position() == 5


async def test_loss_halt_does_not_carry_into_the_next_day(tmp_path):
    first = await Harness.create(tmp_path)
    await bought(first, 5)
    first.price("SPY", "79.00", "79.02")
    await first.run_for(40)

    next_morning = T0 + timedelta(days=1)
    second = await Harness.create(tmp_path, backing=first.backing, start=next_morning)
    await second.target("SPY", 6)
    await second.settle()
    assert second.position() == 6


async def test_entries_wait_when_the_broker_does_not_report_equity(h):
    h.broker.frozen_account = replace(await h.broker.get_account(), equity=None)
    await h.run_for(35)
    await h.target("SPY", 3)
    await h.run_for(30)
    assert h.broker.calls["place"] == 0


# --- daily order cap, cooldown, login expiry ----------------------------------------------


async def test_daily_order_cap_stops_entries_but_not_the_exit(tmp_path):
    h = await Harness.create(tmp_path, risk={"max_orders_per_day": 3})
    for quantity in (1, 0, 1):
        await h.target("SPY", quantity)
        await h.settle()
    assert h.broker.calls["place"] == 3
    await h.target("SPY", 2)
    await h.run_for(30)
    assert h.broker.calls["place"] == 3  # entry refused
    await h.target("SPY", 0)
    await h.settle()
    assert h.position() == 0  # exit still allowed


async def test_cooldown_spaces_out_entries(tmp_path):
    h = await Harness.create(tmp_path, risk={"order_cooldown_s": 30})
    await h.target("SPY", 1)
    await h.settle(5)
    await h.target("SPY", 2)
    await h.run_for(10)
    assert h.broker.calls["place"] == 1
    await h.run_for(30)
    assert h.position() == 2


async def test_no_entries_when_the_schwab_login_is_about_to_expire(h):
    h.auth_seconds_left = 1800
    await h.target("SPY", 3)
    await h.run_for(30)
    assert h.broker.calls["place"] == 0


async def test_blocked_orders_are_logged_with_the_reason_but_not_on_every_step(h):
    h.auth_seconds_left = 1800
    await h.target("SPY", 3)
    await h.run_for(30)
    (event,) = await h.events("order_blocked")
    assert event["data"]["symbol"] == "SPY"
    assert "token_expiring" in event["data"]["codes"]


async def test_a_blocked_order_does_not_poll_the_broker_on_every_step(h):
    await h.tick(1)
    h.auth_seconds_left = 1800
    h.broker.calls.clear()
    await h.target("SPY", 3)
    await h.run_for(60)
    # Only the routine account refresh, not a pre-trade check per step.
    assert h.broker.calls["get_open_orders"] == 0
    assert h.broker.calls["get_account"] <= 3


async def test_total_exposure_cap_counts_buys_that_are_still_working(tmp_path):
    caps = {"max_order_usd": 1000, "max_position_usd": 1000, "max_total_exposure_usd": 1000}
    h = await Harness.create(tmp_path, symbols=("SPY", "QQQ"), risk=caps)
    h.broker.hold_fills = True
    h.strategy.bar_targets += [Target("SPY", 6), Target("QQQ", 6)]  # about 600 each
    h.bar("SPY")
    await h.engine.step()
    await h.run_for(10)
    # Both would fit alone; together they pass the 1000 cap, so only the first goes out.
    assert [o.symbol for o in h.broker.placed] == ["SPY"]
    (blocked,) = await h.events("order_blocked")
    assert blocked["data"]["symbol"] == "QQQ"
    assert "max_total_exposure_usd" in blocked["data"]["codes"]


# --- settled cash -----------------------------------------------------------------------------
#
# Quotes are 100.00 x 100.02: buys fill at 100.02, sells at 100.00. What a sale brings in
# settles the next business day, so by default the bot does not spend it again the same day.


async def sold_everything(h):
    await h.target("SPY", 0)
    await h.settle()
    assert h.position() == 0


async def sold_today(h) -> Decimal:
    return (await h.store.get_day("2026-10-08")).sold_usd


async def test_what_a_sale_brings_in_is_recorded_for_the_day(h):
    await bought(h, 3)
    assert await sold_today(h) == 0
    await sold_everything(h)
    assert await sold_today(h) == Decimal("300.00")


async def test_a_fraction_of_a_cent_is_rounded_up_so_proceeds_are_never_understated(h):
    await bought(h, 3)
    h.price("SPY", "100.004", "100.02")
    await sold_everything(h)  # 3 at 100.004 is 300.012
    assert await sold_today(h) == Decimal("300.02")


async def test_money_from_todays_sale_is_not_spent_again_today(tmp_path):
    h = await Harness.create(tmp_path, cash="500")
    await bought(h, 4)  # leaves 99.92
    await sold_everything(h)  # back to 499.92, but 400.00 of it has not settled
    await h.target("SPY", 4)
    await h.run_for(30)
    assert h.position() == 0
    (blocked,) = await h.events("order_blocked")
    assert blocked["data"]["codes"] == ["unsettled_cash"]


async def test_cash_that_was_never_spent_can_still_buy_after_a_sale(tmp_path):
    h = await Harness.create(tmp_path, cash="1000")
    await bought(h, 4)
    await sold_everything(h)  # 999.92 on hand, 599.92 of it settled
    await h.target("SPY", 4)
    await h.settle()
    assert h.position() == 4


async def test_several_sales_in_a_day_add_up(tmp_path):
    h = await Harness.create(tmp_path, cash="1000")
    for _ in range(2):
        await bought(h, 2)
        await sold_everything(h)
    assert await sold_today(h) == Decimal("400.00")


async def test_a_partly_filled_sale_counts_only_the_shares_sold(h):
    await bought(h, 5)
    h.broker.hold_fills = True
    await h.target("SPY", 0)
    (resting,) = await h.broker.get_open_orders()
    h.broker.fill_partially_then_cancel(resting.order_id, 2)
    await h.settle()
    assert h.position() == 3
    assert await sold_today(h) == Decimal("200.00")


async def test_buying_adds_nothing_to_the_days_sales(h):
    await bought(h, 3)
    await h.target("SPY", 5)
    await h.settle()
    assert await sold_today(h) == 0


async def test_todays_sales_are_remembered_across_a_restart(tmp_path):
    first = await Harness.create(tmp_path, cash="500")
    await bought(first, 4)
    await sold_everything(first)

    second = await Harness.create(tmp_path, restart_of=first)
    await second.target("SPY", 4)
    await second.run_for(30)
    assert second.position() == 0


async def test_yesterdays_sales_have_settled_by_the_next_session(tmp_path):
    first = await Harness.create(tmp_path, cash="500")
    await bought(first, 4)
    await sold_everything(first)

    first.clock.advance(24 * 3600)  # Friday, same time
    second = await Harness.create(tmp_path, restart_of=first)
    await second.target("SPY", 4)
    await second.settle()
    assert second.position() == 4


async def test_fridays_sales_are_still_unsettled_on_a_monday_when_banks_are_shut(tmp_path):
    friday = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)
    first = await Harness.create(tmp_path, cash="500", start=friday)
    await bought(first, 4)
    await sold_everything(first)

    first.clock.advance(3 * 24 * 3600)  # Monday 12 October 2026, Columbus Day
    monday = await Harness.create(tmp_path, restart_of=first)
    await monday.target("SPY", 4)
    await monday.run_for(30)
    assert monday.position() == 0
    assert (await monday.events("order_blocked"))[-1]["data"]["codes"] == ["unsettled_cash"]

    first.clock.advance(24 * 3600)  # Tuesday: settled
    tuesday = await Harness.create(tmp_path, restart_of=monday)
    await tuesday.target("SPY", 4)
    await tuesday.settle()
    assert tuesday.position() == 4


async def test_a_sale_that_could_not_be_saved_is_saved_once_the_store_is_back(
    tmp_path, monkeypatch
):
    h = await Harness.create(tmp_path, cash="350")
    await bought(h, 3)
    working = h.store.add_sold

    async def broken(day, amount):
        raise RuntimeError("state store down")

    monkeypatch.setattr(h.store, "add_sold", broken)
    await sold_everything(h)
    assert await sold_today(h) == 0
    monkeypatch.setattr(h.store, "add_sold", working)
    await h.run_for(15)
    assert await sold_today(h) == Decimal("300.00")
    await h.run_for(30)
    assert await sold_today(h) == Decimal("300.00")  # and only once


async def test_a_halt_that_could_not_be_saved_is_saved_once_the_store_is_back(h, monkeypatch):
    await bought(h, 5)
    working = h.store.halt_day

    async def broken(day, reason):
        raise RuntimeError("state store down")

    monkeypatch.setattr(h.store, "halt_day", broken)
    h.price("SPY", "79.00", "79.02")
    await h.run_for(40)
    assert (await h.store.get_day("2026-10-08")).halted_reason is None
    monkeypatch.setattr(h.store, "halt_day", working)
    await h.run_for(15)
    assert (await h.store.get_day("2026-10-08")).halted_reason is not None


async def test_yesterdays_unfilled_wish_is_not_acted_on_before_the_strategy_speaks(tmp_path):
    h = await Harness.create(tmp_path)
    h.auth_seconds_left = 1800  # something blocks the buy all day
    await h.target("SPY", 3)
    await h.run_for(30)
    h.auth_seconds_left = None
    h.clock.advance(24 * 3600)  # Friday, same time: the block is gone
    await h.run_for(30)
    assert h.broker.calls["place"] == 0
    await h.target("SPY", 3)  # the strategy says so again, today
    await h.settle()
    assert h.position() == 3


async def test_a_margin_account_can_turn_the_settled_cash_rule_off(tmp_path):
    h = await Harness.create(tmp_path, cash="500", risk={"settled_cash_only": False})
    await bought(h, 4)
    await sold_everything(h)
    await h.target("SPY", 4)
    await h.settle()
    assert h.position() == 4


async def test_a_sale_with_no_reported_fill_price_is_counted_at_its_limit_price(h):
    await bought(h, 3)
    h.broker.hide_fill_prices = True
    await sold_everything(h)
    # The sell limit sits just under the 100.00 bid, so this slightly understates 300.00.
    assert await sold_today(h) == Decimal("299.85")


async def test_a_sale_the_bot_never_saw_confirmed_still_counts(h):
    await bought(h, 3)
    # The order goes through, but the reply is lost and the order cannot be looked up.
    h.broker.place_error_after_accepting = TimeoutError("no reply")
    h.broker.find_error = BrokerUnavailable("order search is down")
    await h.target("SPY", 0)
    await h.settle()
    assert h.position() == 0
    assert await sold_today(h) == Decimal("299.85")  # 3 at the limit price it was sent with


async def test_two_buys_in_one_step_cannot_both_spend_the_same_cash(tmp_path):
    h = await Harness.create(tmp_path, symbols=("SPY", "QQQ"), cash="1000")
    h.broker.hold_fills = True
    h.strategy.bar_targets += [Target("SPY", 6), Target("QQQ", 6)]  # about 600 each
    h.bar("SPY")
    await h.engine.step()
    await h.run_for(10)
    assert [o.symbol for o in h.broker.placed] == ["SPY"]
    (blocked,) = await h.events("order_blocked")
    assert (blocked["data"]["symbol"], blocked["data"]["codes"]) == ("QQQ", ["cash"])


async def test_a_buy_does_not_spend_cash_a_fill_is_about_to_take(tmp_path):
    h = await Harness.create(tmp_path, symbols=("SPY", "QQQ"), cash="1000")
    stale = await h.broker.get_account()
    await h.target("SPY", 6)
    h.broker.frozen_account = stale  # the fill happened; the account has not caught up
    await h.target("QQQ", 6)
    await h.run_for(20)
    assert h.broker.calls["place"] == 1  # the second buy was never even sent


async def test_a_sale_still_working_already_counts_as_unsettled(tmp_path):
    h = await Harness.create(
        tmp_path, symbols=("SPY", "QQQ"), cash="1000", config={"order_timeout_s": 600}
    )
    await bought(h, 9)  # leaves 99.82
    h.broker.hold_fills = True
    await h.target("SPY", 0)
    (resting,) = await h.broker.get_open_orders()
    h.broker.fill_part(resting.order_id, 5)  # 500 comes in; the sell keeps working
    await h.target("QQQ", 4)  # about 400: only possible with the sale's money
    await h.run_for(40)
    assert [o.symbol for o in h.broker.placed if o.side is Side.BUY] == ["SPY"]
    assert "unsettled_cash" in (await h.events("order_blocked"))[-1]["data"]["codes"]


async def test_a_buy_the_bot_never_saw_confirmed_is_not_mistaken_for_a_sale(h):
    await bought(h, 3)
    await sold_everything(h)
    h.broker.place_error_after_accepting = TimeoutError("no reply")
    h.broker.find_error = BrokerUnavailable("order search is down")
    await h.target("SPY", 2)
    await h.settle()
    assert h.position() == 2
    assert await sold_today(h) == Decimal("300.00")  # the earlier sale, nothing more or less


async def test_a_sale_that_cannot_be_valued_stops_buying_for_the_day(tmp_path):
    h = await Harness.create(tmp_path, config={"order_type": "MARKET"})
    await bought(h, 3)
    h.broker.hide_fill_prices = True  # a market order has no limit price to fall back on
    await sold_everything(h)
    assert "day_halted" in h.alert_keys()

    await h.target("SPY", 3)
    await h.run_for(30)
    assert h.position() == 0


async def test_a_second_reason_to_stop_buying_does_not_replace_the_first(tmp_path):
    h = await Harness.create(tmp_path, config={"order_type": "MARKET"})
    await bought(h, 5)
    h.price("SPY", "79.00", "79.02")  # over the daily loss limit
    await h.run_for(40)
    h.broker.hide_fill_prices = True
    await sold_everything(h)  # and now a sale that cannot be valued
    (halt,) = await h.events("entries_halted")
    assert "equity down" in halt["data"]["reason"]
    assert h.alert_keys().count("day_halted") == 1


async def test_sales_still_count_today_when_they_cannot_be_saved(tmp_path, monkeypatch):
    async def broken(day, amount):
        raise RuntimeError("state store down")

    h = await Harness.create(tmp_path, cash="350")
    await bought(h, 3)
    monkeypatch.setattr(h.store, "add_sold", broken)
    await sold_everything(h)  # 349.94 on hand, 300.00 of it from the sale just now
    await h.target("SPY", 3)
    await h.run_for(40)
    assert h.position() == 0
    assert await sold_today(h) == 0  # nothing reached the store; the engine remembered anyway


# --- session rules ----------------------------------------------------------------------------


async def test_nothing_is_sent_outside_the_regular_session(tmp_path):
    before_open = datetime(2026, 10, 8, 13, 0, tzinfo=UTC)  # 09:00 New York
    h = await Harness.create(tmp_path, start=before_open)
    await h.target("SPY", 3)
    await h.run_for(60)
    assert h.broker.calls["place"] == 0


async def test_a_target_set_before_the_open_is_acted_on_after_it(tmp_path):
    before_open = datetime(2026, 10, 8, 13, 25, tzinfo=UTC)
    h = await Harness.create(tmp_path, start=before_open)
    await h.target("SPY", 3)
    await h.run_for(8 * 60, step=5)  # through the open and past the first-minute delay
    assert h.position() == 3


async def test_unknown_market_hours_mean_no_trading(tmp_path):
    class Unknown:
        async def session_for(self, _day):
            return None

    h = await Harness.create(tmp_path, session_provider=Unknown())
    await h.target("SPY", 3)
    await h.run_for(30)
    assert h.broker.calls["place"] == 0


async def test_half_day_close_from_the_calendar_is_respected(tmp_path):
    class HalfDay:
        async def session_for(self, day):
            return Session(
                day,
                datetime(2026, 10, 8, 13, 30, tzinfo=UTC),
                datetime(2026, 10, 8, 17, 0, tzinfo=UTC),
            )  # closes 13:00 New York

    after_early_close = datetime(2026, 10, 8, 17, 30, tzinfo=UTC)
    h = await Harness.create(tmp_path, session_provider=HalfDay(), start=after_early_close)
    await h.target("SPY", 3)
    await h.run_for(30)
    assert h.broker.calls["place"] == 0


async def test_flatten_before_close_sells_everything_and_blocks_re_entry(tmp_path):
    near_close = datetime(2026, 10, 8, 19, 40, tzinfo=UTC)  # 15:40 New York
    h = await Harness.create(tmp_path, start=near_close, config={"flatten_before_close_min": 10})
    await bought(h)
    await h.run_for(11 * 60, step=5)  # into the last ten minutes
    assert h.position() == 0
    await h.target("SPY", 3)
    await h.run_for(60)
    assert h.position() == 0
    assert h.broker.placed[-1].side is Side.SELL


async def test_positions_are_kept_overnight_unless_flatten_is_configured(tmp_path):
    near_close = datetime(2026, 10, 8, 19, 40, tzinfo=UTC)
    h = await Harness.create(tmp_path, start=near_close)
    await bought(h)
    await h.run_for(25 * 60, step=5)  # through the close
    assert h.position() == 3


# --- things break ----------------------------------------------------------------------------


async def test_engine_survives_the_broker_being_unreachable(h):
    h.broker.account_error = BrokerUnavailable("503")
    await h.run_for(60)
    h.broker.account_error = None
    await h.target("SPY", 3)
    await h.run_for(40)
    assert h.position() == 3


async def test_engine_does_not_trade_on_an_old_account_snapshot(h):
    await h.tick(1)
    h.broker.account_error = BrokerUnavailable("503")
    await h.run_for(200)  # the cached snapshot is now minutes old
    await h.target("SPY", 3)
    await h.run_for(10)
    assert h.broker.calls["place"] == 0


async def test_strategy_error_stops_entries_but_keeps_the_bot_running(h):
    await bought(h)
    h.strategy.fail_with = RuntimeError("division by zero")
    h.bar("SPY")
    await h.tick(1)
    h.strategy.fail_with = None
    assert "strategy_error" in h.alert_keys()

    await h.target("SPY", 6)
    await h.run_for(30)
    assert h.position() == 3  # no new entries after a strategy fault
    await h.target("SPY", 0)
    await h.settle()
    assert h.position() == 0  # exits still work


async def test_no_order_if_the_daily_counter_cannot_be_updated(h, monkeypatch):
    async def broken(_day):
        raise RuntimeError("dynamodb unreachable")

    monkeypatch.setattr(h.store, "incr_orders", broken)
    await h.target("SPY", 3)
    await h.run_for(20)
    assert h.broker.calls["place"] == 0


async def test_audit_log_failure_does_not_stop_an_exit(h, monkeypatch):
    await bought(h)

    async def broken(*_args, **_kwargs):
        raise RuntimeError("dynamodb unreachable")

    monkeypatch.setattr(h.store, "log_event", broken)
    await h.target("SPY", 0)
    await h.settle()
    assert h.position() == 0


async def test_orders_stop_when_quotes_stop_arriving(h):
    h.clock.advance(60)  # a minute passes with no market data at all
    h.strategy.bar_targets.append(Target("SPY", 3))
    h.bar("SPY")
    for _ in range(10):
        await h.tick(5, requote=False)
    assert h.broker.calls["place"] == 0


# --- shutdown -----------------------------------------------------------------------------------


async def test_shutdown_cancels_working_orders(h):
    h.broker.hold_fills = True
    await h.target("SPY", 3)
    await h.engine.shutdown()
    assert h.broker.cancelled == ["P1"]


async def test_shutdown_survives_a_broker_error(h):
    h.broker.hold_fills = True
    await h.target("SPY", 3)
    h.broker.cancel_error = BrokerUnavailable("503")
    await h.engine.shutdown()


async def test_starting_cash_is_recorded_as_the_days_opening_equity(h):
    await h.run_for(35)
    assert (await h.store.get_day("2026-10-08")).start_equity == Decimal(10000)
