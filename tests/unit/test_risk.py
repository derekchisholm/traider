"""Every guardrail, one behaviour per test.

``ctx()`` builds a context in which a small buy is fine. Each test changes one
thing and checks the specific rejection, so a guardrail that silently stops
working fails its own test.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from traider.config import RiskLimits
from traider.control import ControlMode, effective_permissions
from traider.models import AccountSnapshot, OrderRequest, OrderType, Position, Quote, Side
from traider.risk import RiskContext, RiskManager, SessionView

NOW = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)
LIMITS = RiskLimits()  # 500 / 1000 / 2000 USD, 20 orders, 20 bps spread, $5 min price


def quote(
    bid="100.00", ask="100.02", *, age_s=1.0, delayed=False, halted=False, market_age_s=None
) -> Quote:
    seen = NOW - timedelta(seconds=age_s)
    made = seen if market_age_s is None else NOW - timedelta(seconds=market_age_s)
    return Quote(
        "SPY",
        Decimal(bid),
        Decimal(ask),
        Decimal(bid),
        ts=made,
        received_at=seen,
        delayed=delayed,
        halted=halted,
    )


def account(*, cash="5000", positions=None, equity="5000") -> AccountSnapshot:
    return AccountSnapshot(
        equity=Decimal(equity),
        cash_available=None if cash is None else Decimal(cash),
        positions=positions or {},
        as_of=NOW,
    )


def buy(qty=2, price="100.05", order_type=OrderType.LIMIT) -> OrderRequest:
    limit = None if price is None else Decimal(price)
    return OrderRequest("SPY", Side.BUY, qty, order_type, limit)


def sell(qty=2, price="99.95", order_type=OrderType.LIMIT) -> OrderRequest:
    limit = None if price is None else Decimal(price)
    return OrderRequest("SPY", Side.SELL, qty, order_type, limit)


def ctx(**overrides) -> RiskContext:
    base = RiskContext(
        now=NOW,
        order=buy(),
        position=0,
        quote=quote(),
        feed_alive_at=NOW - timedelta(seconds=1),
        account=account(),
        exposure_usd=Decimal(0),
        orders_today=0,
        entries_halted=None,
        permissions=effective_permissions("paper", ControlMode.PAPER),
        session=SessionView(is_open=True, minutes_since_open=60, minutes_to_close=120),
        token_seconds_left=5 * 86400,
        seconds_since_last_order=None,
        unsettled_usd=Decimal(0),
        committed_usd=Decimal(0),
    )
    return replace(base, **overrides)


def holding(qty=5, avg="100") -> dict:
    return {
        "position": qty,
        "account": account(positions={"SPY": Position("SPY", qty, Decimal(avg))}),
    }


def check(context: RiskContext, limits: RiskLimits = LIMITS):
    return RiskManager(limits).check(context)


# --- the happy paths ------------------------------------------------------


def test_small_buy_in_good_conditions_is_allowed():
    decision = check(ctx())
    assert decision.allowed
    assert decision.codes == set()
    assert decision.reducing is False


def test_sell_of_a_held_position_is_allowed_and_flagged_reducing():
    decision = check(ctx(order=sell(5), **holding(5)))
    assert decision.allowed
    assert decision.reducing is True


# --- control switch --------------------------------------------------------


@pytest.mark.parametrize("order", [buy(), sell(2)])
def test_halt_rejects_every_order(order):
    context = ctx(
        order=order, permissions=effective_permissions("paper", ControlMode.HALT), **holding(5)
    )
    assert "control" in check(context).codes


def test_close_only_rejects_buys():
    context = ctx(permissions=effective_permissions("live", ControlMode.CLOSE_ONLY))
    assert "control" in check(context).codes


def test_close_only_allows_sells():
    context = ctx(
        order=sell(5),
        permissions=effective_permissions("live", ControlMode.CLOSE_ONLY),
        **holding(5),
    )
    assert check(context).allowed


def test_mismatched_control_and_deploy_rejects_everything():
    context = ctx(
        order=sell(5), permissions=effective_permissions("live", ControlMode.PAPER), **holding(5)
    )
    assert "control" in check(context).codes


# --- session ----------------------------------------------------------------


@pytest.mark.parametrize("order", [buy(), sell(2)])
def test_nothing_trades_when_the_market_is_closed(order):
    closed = SessionView(is_open=False, minutes_since_open=None, minutes_to_close=None)
    assert "session_closed" in check(ctx(order=order, session=closed, **holding(5))).codes


def test_no_entries_in_the_first_minute_after_the_open():
    early = SessionView(is_open=True, minutes_since_open=0.5, minutes_to_close=389.5)
    assert "entry_window" in check(ctx(session=early)).codes


def test_no_entries_in_the_last_minutes_before_the_close():
    late = SessionView(is_open=True, minutes_since_open=387, minutes_to_close=3)
    assert "entry_window" in check(ctx(session=late)).codes


def test_entries_wait_when_the_time_since_the_open_is_unknown():
    odd = SessionView(is_open=True, minutes_since_open=None, minutes_to_close=120)
    assert "entry_window" in check(ctx(session=odd)).codes


def test_entries_wait_when_the_time_to_the_close_is_unknown():
    odd = SessionView(is_open=True, minutes_since_open=60, minutes_to_close=None)
    assert "entry_window" in check(ctx(session=odd)).codes


def test_exits_are_allowed_right_up_to_the_close():
    late = SessionView(is_open=True, minutes_since_open=389.5, minutes_to_close=0.5)
    assert check(ctx(order=sell(5), session=late, **holding(5))).allowed


# --- market data ------------------------------------------------------------


@pytest.mark.parametrize("order", [buy(), sell(2)])
def test_no_quote_no_order(order):
    assert "no_quote" in check(ctx(order=order, quote=None, **holding(5))).codes


@pytest.mark.parametrize("order", [buy(), sell(2)])
def test_delayed_quotes_are_not_tradeable(order):
    assert "quote_delayed" in check(ctx(order=order, quote=quote(delayed=True), **holding(5))).codes


@pytest.mark.parametrize("order", [buy(), sell(2)])
def test_stale_quote_is_rejected(order):
    assert "quote_stale" in check(ctx(order=order, quote=quote(age_s=16), **holding(5))).codes


@pytest.mark.parametrize("order", [buy(), sell(2)])
def test_a_halted_security_is_not_traded(order):
    context = ctx(order=order, quote=quote(halted=True), **holding(5))
    assert "halted" in check(context).codes


@pytest.mark.parametrize("order", [buy(), sell(2)])
def test_a_quote_just_received_but_made_long_ago_is_stale(order):
    # Fetched a second ago, but the market last updated it five minutes ago.
    context = ctx(order=order, quote=quote(market_age_s=300), **holding(5))
    assert "quote_stale" in check(context).codes


def test_a_quote_made_within_the_allowed_lag_is_accepted():
    assert check(ctx(quote=quote(market_age_s=120))).allowed


def test_a_quote_stamped_slightly_ahead_of_our_clock_is_accepted():
    assert check(ctx(quote=quote(market_age_s=-3))).allowed


def test_quote_at_the_age_limit_is_still_accepted():
    assert check(ctx(quote=quote(age_s=15))).allowed


@pytest.mark.parametrize("alive_at", [None, NOW - timedelta(seconds=31)])
def test_silent_feed_blocks_orders_even_with_a_quote_on_hand(alive_at):
    assert "feed_silent" in check(ctx(feed_alive_at=alive_at)).codes


@pytest.mark.parametrize(("bid", "ask"), [("0", "100"), ("100", "0"), ("100.10", "100.00")])
def test_zero_or_crossed_quotes_are_rejected(bid, ask):
    context = ctx(
        order=sell(2, price=None, order_type=OrderType.MARKET), quote=quote(bid, ask), **holding(5)
    )
    assert "bad_quote" in check(context).codes


def test_a_locked_quote_with_bid_equal_to_ask_is_usable():
    assert check(ctx(quote=quote("100.00", "100.00"))).allowed


def test_wide_spread_blocks_entries():
    # 100.00 x 100.30 is about 30 bps wide; the limit is 20.
    assert "spread" in check(ctx(order=buy(2, "100.30"), quote=quote("100.00", "100.30"))).codes


def test_wide_spread_does_not_trap_an_exit():
    context = ctx(order=sell(5, "99.95"), quote=quote("100.00", "100.30"), **holding(5))
    assert check(context).allowed


def test_low_priced_stocks_are_not_bought():
    context = ctx(order=buy(10, "4.01"), quote=quote("4.00", "4.005"))
    assert "min_price" in check(context).codes


# --- order shape -------------------------------------------------------------


@pytest.mark.parametrize("qty", [0, -3])
def test_non_positive_quantity_is_rejected(qty):
    assert "quantity" in check(ctx(order=buy(qty))).codes


def test_limit_order_needs_a_price():
    assert "limit_price" in check(ctx(order=buy(2, None))).codes


def test_buy_limit_far_above_the_ask_is_a_fat_finger():
    # 102.00 is about 2% over the 100.02 ask; the tolerance is 1%.
    assert "limit_price" in check(ctx(order=buy(2, "102.00"))).codes


def test_sell_limit_far_below_the_bid_is_a_fat_finger():
    assert "limit_price" in check(ctx(order=sell(5, "98.00"), **holding(5))).codes


def test_selling_more_than_held_is_rejected_because_the_bot_never_shorts():
    assert "short" in check(ctx(order=sell(6), **holding(5))).codes


def test_selling_with_no_position_is_rejected():
    assert "short" in check(ctx(order=sell(1))).codes


# --- size caps -----------------------------------------------------------------


def test_share_count_cap_applies_to_buys():
    limits = RiskLimits(max_shares_per_order=3)
    assert "max_shares" in check(ctx(order=buy(4)), limits).codes


def test_share_count_cap_does_not_block_closing_a_large_position():
    limits = RiskLimits(max_shares_per_order=3)
    assert check(ctx(order=sell(9), **holding(9)), limits).allowed


def test_order_value_cap():
    # 6 shares at 100.05 is 600.30, over the 500 cap.
    assert "max_order_usd" in check(ctx(order=buy(6))).codes


def test_order_value_exactly_at_the_cap_is_allowed():
    context = ctx(order=buy(5, "100.00"), quote=quote("99.98", "100.00"))
    assert check(context).allowed


def test_market_buy_is_valued_at_the_ask():
    # 5 shares: 500.10 at the ask, over the cap; 500.00 at the bid would have passed.
    context = ctx(order=buy(5, None, OrderType.MARKET))
    assert "max_order_usd" in check(context).codes


def test_position_value_cap_counts_what_is_already_held():
    # Holding 8 (about 800) and buying 3 more (about 300) would pass 1000.
    context = ctx(order=buy(3), exposure_usd=Decimal("800"), **holding(8))
    assert "max_position_usd" in check(context).codes


def test_total_exposure_cap_counts_other_symbols():
    context = ctx(order=buy(3), exposure_usd=Decimal("1800"))
    assert "max_total_exposure_usd" in check(context).codes


# --- account -------------------------------------------------------------------


@pytest.mark.parametrize("order", [buy(), sell(2)])
def test_no_account_snapshot_no_order(order):
    assert "no_account" in check(ctx(order=order, account=None)).codes


def test_buy_must_be_covered_by_available_cash():
    assert "cash" in check(ctx(account=account(cash="150"))).codes


def test_unknown_cash_blocks_buys_by_default():
    assert "cash" in check(ctx(account=account(cash=None))).codes


def test_cash_check_can_be_turned_off():
    limits = RiskLimits(require_cash=False)
    assert check(ctx(account=account(cash=None)), limits).allowed


def test_cash_already_promised_to_other_buys_cannot_be_spent_twice():
    # 300 on hand, 200 of it promised to a buy still on its way; this one needs 200.10.
    context = ctx(account=account(cash="300"), committed_usd=Decimal("200"))
    assert check(context).codes == {"cash"}


def test_cash_left_after_other_buys_can_be_spent():
    assert check(ctx(account=account(cash="500"), committed_usd=Decimal("200"))).allowed


def test_promised_cash_and_unsettled_cash_both_count_against_a_buy():
    context = ctx(
        account=account(cash="500"), committed_usd=Decimal("150"), unsettled_usd=Decimal("150")
    )
    assert check(context).codes == {"unsettled_cash"}  # 200 free, 200.10 needed


def test_promised_cash_never_blocks_an_exit():
    assert check(ctx(order=sell(5), committed_usd=Decimal("99999"), **holding(5))).allowed


# --- settled cash ----------------------------------------------------------------
#
# A sale's proceeds settle the next business day. In a cash account, buying with
# them and selling again before then is a good-faith violation. The default buy in
# these tests costs 200.10.


def test_todays_sale_proceeds_cannot_fund_a_buy():
    context = ctx(account=account(cash="300"), unsettled_usd=Decimal("200"))
    decision = check(context)
    assert decision.codes == {"unsettled_cash"}
    assert "100.00" in decision.summary()  # what has settled
    assert "200.00" in decision.summary()  # what has not


def test_settled_cash_still_funds_a_buy_after_a_sale():
    assert check(ctx(account=account(cash="500"), unsettled_usd=Decimal("200"))).allowed


def test_buy_exactly_covered_by_settled_cash_is_allowed():
    assert check(ctx(account=account(cash="400.10"), unsettled_usd=Decimal("200"))).allowed


def test_a_cent_short_of_settled_cash_is_rejected():
    context = ctx(account=account(cash="400.09"), unsettled_usd=Decimal("200"))
    assert check(context).codes == {"unsettled_cash"}


def test_settled_cash_rule_can_be_turned_off_for_a_margin_account():
    limits = RiskLimits(settled_cash_only=False)
    context = ctx(account=account(cash="300"), unsettled_usd=Decimal("200"))
    assert check(context, limits).allowed


def test_settled_cash_rule_is_part_of_the_cash_requirement():
    limits = RiskLimits(require_cash=False)
    context = ctx(account=account(cash="0"), unsettled_usd=Decimal("200"))
    assert check(context, limits).allowed


def test_unsettled_proceeds_never_block_an_exit():
    context = ctx(order=sell(5), unsettled_usd=Decimal("99999"), **holding(5))
    assert check(context).allowed


def test_a_buy_beyond_all_cash_is_reported_as_cash_not_as_unsettled():
    context = ctx(account=account(cash="100"), unsettled_usd=Decimal("50"))
    assert check(context).codes == {"cash"}


# --- activity caps ---------------------------------------------------------------


def test_daily_order_cap_blocks_further_entries():
    assert "max_orders_per_day" in check(ctx(orders_today=20)).codes


def test_daily_order_cap_never_blocks_an_exit():
    assert check(ctx(order=sell(5), orders_today=500, **holding(5))).allowed


def test_an_entry_halt_blocks_entries():
    assert "entries_halted" in check(ctx(entries_halted="down 120 on the day")).codes


def test_an_entry_halt_still_allows_exits():
    assert check(ctx(order=sell(5), entries_halted="down 120", **holding(5))).allowed


def test_cooldown_blocks_a_quick_second_entry():
    assert "cooldown" in check(ctx(seconds_since_last_order=10)).codes


def test_cooldown_does_not_delay_an_exit():
    assert check(ctx(order=sell(5), seconds_since_last_order=1, **holding(5))).allowed


def test_no_entries_when_the_schwab_login_is_about_to_expire():
    assert "token_expiring" in check(ctx(token_seconds_left=3600)).codes


def test_expiring_login_does_not_block_exits():
    assert check(ctx(order=sell(5), token_seconds_left=60, **holding(5))).allowed


def test_token_check_is_skipped_when_there_is_no_token_to_track():
    assert check(ctx(token_seconds_left=None)).allowed


# --- reporting ----------------------------------------------------------------------


def test_every_failed_check_is_reported_not_just_the_first():
    context = ctx(order=buy(6), orders_today=20, entries_halted="down 120")
    assert {"max_order_usd", "max_orders_per_day", "entries_halted"} <= check(context).codes


def test_rejections_carry_a_human_readable_detail():
    decision = check(ctx(order=buy(6)))
    (rejection,) = decision.rejections
    assert rejection.code == "max_order_usd"
    assert "600.30" in rejection.detail and "500" in rejection.detail
