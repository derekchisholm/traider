"""Turning Schwab's JSON into the bot's types. Samples follow the shapes in the schwabdev docs."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from traider.models import OrderRequest, OrderStatus, OrderType, Side
from traider.schwab.orders import build_equity_order
from traider.schwab.parse import (
    ParseError,
    parse_account,
    parse_candles,
    parse_market_hours,
    parse_order,
    parse_order_tree,
    parse_quotes,
)

NOW = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)


def margin_account(**current) -> dict:
    balances = {
        "cashBalance": 2500.5,
        "liquidationValue": 10250.75,
        "buyingPower": 5001.0,
        "availableFunds": 5001.0,
        "equity": 10250.75,
    }
    balances.update(current)
    return {
        "securitiesAccount": {
            "type": "MARGIN",
            "accountNumber": "12345678",
            "roundTrips": 2,
            "isDayTrader": False,
            "initialBalances": {"cashAvailableForTrading": 9999.0, "accountValue": 10000.0},
            "currentBalances": balances,
            "positions": [
                {
                    "shortQuantity": 0.0,
                    "averagePrice": 512.3456,
                    "longQuantity": 15.0,
                    "instrument": {
                        "assetType": "COLLECTIVE_INVESTMENT",
                        "symbol": "SPY",
                        "type": "EXCHANGE_TRADED_FUND",
                    },
                    "marketValue": 7750.25,
                },
                {
                    "shortQuantity": 0.0,
                    "averagePrice": 190.0,
                    "longQuantity": 3.0,
                    "instrument": {"assetType": "EQUITY", "symbol": "AAPL"},
                    "marketValue": 570.0,
                },
            ],
        },
        "aggregatedBalance": {"currentLiquidationValue": 10250.75, "liquidationValue": 10250.75},
    }


# --- account -----------------------------------------------------------------------


def test_equity_is_the_current_liquidation_value():
    assert parse_account(margin_account(), NOW).equity == Decimal("10250.75")


def test_margin_account_cash_is_the_cash_balance_not_buying_power():
    assert parse_account(margin_account(), NOW).cash_available == Decimal("2500.5")


def test_cash_account_uses_cash_available_for_trading():
    raw = margin_account(cashAvailableForTrading=1800.25)
    raw["securitiesAccount"]["type"] = "CASH"
    assert parse_account(raw, NOW).cash_available == Decimal("1800.25")


def test_todays_opening_balances_are_not_mistaken_for_current_cash():
    raw = margin_account()
    del raw["securitiesAccount"]["currentBalances"]["cashBalance"]
    assert parse_account(raw, NOW).cash_available is None


def test_equity_falls_back_to_the_aggregated_balance():
    raw = margin_account()
    del raw["securitiesAccount"]["currentBalances"]["liquidationValue"]
    assert parse_account(raw, NOW).equity == Decimal("10250.75")


def test_equity_is_unknown_when_schwab_sends_none():
    raw = margin_account()
    del raw["securitiesAccount"]["currentBalances"]["liquidationValue"]
    del raw["aggregatedBalance"]
    assert parse_account(raw, NOW).equity is None


def test_account_type_is_read():
    raw = margin_account()
    assert parse_account(raw, NOW).account_type == "MARGIN"
    raw["securitiesAccount"]["type"] = "CASH"
    assert parse_account(raw, NOW).account_type == "CASH"


@pytest.mark.parametrize("value", [None, 7, ""])
def test_missing_or_odd_account_type_is_unknown(value):
    raw = margin_account()
    raw["securitiesAccount"]["type"] = value
    assert parse_account(raw, NOW).account_type is None


def test_positions_are_keyed_by_symbol_with_whole_share_quantities():
    account = parse_account(margin_account(), NOW)
    assert account.position("SPY") == 15
    assert account.positions["SPY"].avg_price == Decimal("512.3456")
    assert account.position("AAPL") == 3


def test_etfs_reported_as_collective_investments_count_as_positions():
    assert "SPY" in parse_account(margin_account(), NOW).positions


def test_short_positions_are_negative():
    raw = margin_account()
    raw["securitiesAccount"]["positions"][1].update(longQuantity=0.0, shortQuantity=4.0)
    assert parse_account(raw, NOW).position("AAPL") == -4


def test_fractional_shares_are_rounded_towards_zero():
    raw = margin_account()
    raw["securitiesAccount"]["positions"][0]["longQuantity"] = 15.75
    assert parse_account(raw, NOW).position("SPY") == 15


def test_option_positions_are_left_out():
    raw = margin_account()
    raw["securitiesAccount"]["positions"].append(
        {
            "shortQuantity": 0.0,
            "averagePrice": 2.5,
            "longQuantity": 1.0,
            "instrument": {"assetType": "OPTION", "symbol": "SPY   261016C00500000"},
        }
    )
    assert set(parse_account(raw, NOW).positions) == {"SPY", "AAPL"}


def test_account_without_positions_is_empty_not_an_error():
    raw = margin_account()
    del raw["securitiesAccount"]["positions"]
    assert dict(parse_account(raw, NOW).positions) == {}


def test_snapshot_is_stamped_with_the_time_it_was_taken():
    assert parse_account(margin_account(), NOW).as_of == NOW


@pytest.mark.parametrize("raw", [{}, [], {"securitiesAccount": "x"}, None])
def test_unrecognisable_account_payload_is_an_error(raw):
    with pytest.raises(ParseError):
        parse_account(raw, NOW)


# --- orders ----------------------------------------------------------------------------


def order(status="WORKING", filled=0.0, activities=None, **extra) -> dict:
    raw = {
        "session": "NORMAL",
        "duration": "DAY",
        "orderType": "LIMIT",
        "quantity": 3.0,
        "filledQuantity": filled,
        "remainingQuantity": 3.0 - filled,
        "price": 512.34,
        "orderLegCollection": [
            {
                "orderLegType": "EQUITY",
                "legId": 1,
                "instrument": {"assetType": "EQUITY", "symbol": "SPY"},
                "instruction": "BUY",
                "quantity": 3.0,
            }
        ],
        "orderStrategyType": "SINGLE",
        "orderId": 1001234567890,
        "status": status,
        "enteredTime": "2026-10-08T14:59:58+0000",
    }
    if activities is not None:
        raw["orderActivityCollection"] = activities
    raw.update(extra)
    return raw


def fill(quantity, price) -> dict:
    return {
        "activityType": "EXECUTION",
        "executionType": "FILL",
        "quantity": quantity,
        "executionLegs": [{"legId": 1, "quantity": quantity, "price": price}],
    }


def test_order_basics_are_read():
    parsed = parse_order(order())
    assert (parsed.order_id, parsed.symbol, parsed.side, parsed.quantity) == (
        "1001234567890",
        "SPY",
        Side.BUY,
        3,
    )
    assert parsed.entered_at == datetime(2026, 10, 8, 14, 59, 58, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw_status", "expected"),
    [
        ("FILLED", OrderStatus.FILLED),
        ("CANCELED", OrderStatus.CANCELED),
        ("REPLACED", OrderStatus.CANCELED),
        ("REJECTED", OrderStatus.REJECTED),
        ("EXPIRED", OrderStatus.EXPIRED),
        ("WORKING", OrderStatus.WORKING),
        ("ACCEPTED", OrderStatus.WORKING),
        ("QUEUED", OrderStatus.WORKING),
        ("PENDING_ACTIVATION", OrderStatus.WORKING),
        ("PENDING_CANCEL", OrderStatus.WORKING),
        ("AWAITING_PARENT_ORDER", OrderStatus.WORKING),
        ("NEW", OrderStatus.WORKING),
    ],
)
def test_schwab_statuses_map_to_the_bots(raw_status, expected):
    parsed = parse_order(order(raw_status))
    assert parsed.status is expected
    assert parsed.raw_status == raw_status


def test_a_status_the_bot_has_never_seen_is_not_treated_as_finished():
    parsed = parse_order(order("SOMETHING_NEW"))
    assert parsed.status is OrderStatus.UNKNOWN
    assert parsed.status.is_terminal is False


def test_fill_price_is_the_quantity_weighted_average_of_executions():
    parsed = parse_order(order("FILLED", 3.0, [fill(1.0, 512.30), fill(2.0, 512.36)]))
    assert parsed.filled_quantity == 3
    assert parsed.avg_fill_price == Decimal("512.34")


def test_cancel_activity_with_zero_price_does_not_distort_the_fill_price():
    cancel = {
        "activityType": "EXECUTION",
        "executionType": "CANCELED",
        "quantity": 2.0,
        "executionLegs": [{"legId": 1, "quantity": 2.0, "price": 0.0}],
    }
    parsed = parse_order(order("CANCELED", 1.0, [fill(1.0, 512.30), cancel]))
    assert (parsed.filled_quantity, parsed.avg_fill_price) == (1, Decimal("512.3"))


def test_unfilled_order_has_no_fill_price():
    assert parse_order(order()).avg_fill_price is None


def test_filled_order_without_execution_details_has_no_invented_price():
    parsed = parse_order(order("FILLED", 3.0))
    assert (parsed.filled_quantity, parsed.avg_fill_price) == (3, None)


@pytest.mark.parametrize(
    ("instruction", "side"),
    [("SELL", Side.SELL), ("SELL_SHORT", Side.SELL), ("BUY_TO_COVER", Side.BUY)],
)
def test_instructions_map_to_sides(instruction, side):
    raw = order()
    raw["orderLegCollection"][0]["instruction"] = instruction
    assert parse_order(raw).side is side


def test_order_without_an_id_is_an_error():
    raw = order()
    del raw["orderId"]
    with pytest.raises(ParseError):
        parse_order(raw)


def test_order_with_an_unreadable_entered_time_still_parses():
    assert parse_order(order(enteredTime="0000-00-00T00:00:00+0000")).entered_at is None


def test_order_with_no_legs_parses_with_an_empty_symbol_so_it_blocks_nothing():
    raw = order()
    raw["orderLegCollection"] = []
    assert parse_order(raw).symbol == ""


def test_legs_of_a_bracket_or_one_cancels_other_order_are_listed_too():
    stop = order(status="AWAITING_PARENT_ORDER", orderId=202)
    target = order(status="WORKING", orderId=203)
    parent = order(status="WORKING", orderId=201)
    parent["orderLegCollection"] = []  # an OCO parent has no legs of its own
    parent["orderStrategyType"] = "OCO"
    parent["childOrderStrategies"] = [
        stop,
        {**target, "childOrderStrategies": [order(orderId=204)]},
    ]
    listed = parse_order_tree(parent)
    assert [o.order_id for o in listed] == ["201", "202", "203", "204"]
    assert [o.symbol for o in listed][1:] == ["SPY", "SPY", "SPY"]


def test_a_child_order_without_an_id_is_still_listed():
    parent = order(orderId=301)
    child = order()
    del child["orderId"]
    parent["childOrderStrategies"] = [child]
    assert [o.order_id for o in parse_order_tree(parent)] == ["301", "301/1"]


# --- quotes ----------------------------------------------------------------------------------


def quote_payload(**quote) -> dict:
    fields = {
        "askPrice": 234.88,
        "bidPrice": 234.86,
        "lastPrice": 234.87,
        "quoteTime": 1760972400000,
        "tradeTime": 1760972399000,
    }
    fields.update(quote)
    return {
        "AAPL": {"assetMainType": "EQUITY", "realtime": True, "symbol": "AAPL", "quote": fields}
    }


def test_quote_prices_and_time_are_read():
    (parsed,) = parse_quotes(quote_payload(), NOW).values()
    assert (parsed.symbol, parsed.bid, parsed.ask, parsed.last) == (
        "AAPL",
        Decimal("234.86"),
        Decimal("234.88"),
        Decimal("234.87"),
    )
    assert parsed.ts == datetime.fromtimestamp(1760972400, UTC)
    assert parsed.received_at == NOW
    assert parsed.delayed is False


def test_non_realtime_quote_is_marked_delayed():
    payload = quote_payload()
    payload["AAPL"]["realtime"] = False
    assert parse_quotes(payload, NOW)["AAPL"].delayed is True


def test_quote_that_does_not_say_it_is_realtime_is_treated_as_delayed():
    payload = quote_payload()
    del payload["AAPL"]["realtime"]
    assert parse_quotes(payload, NOW)["AAPL"].delayed is True


@pytest.mark.parametrize(
    ("status", "halted"), [("Normal", False), (None, False), ("Halted", True), ("Closed", True)]
)
def test_a_security_that_is_not_trading_normally_is_flagged(status, halted):
    payload = quote_payload(securityStatus=status)
    if status is None:
        del payload["AAPL"]["quote"]["securityStatus"]
    assert parse_quotes(payload, NOW)["AAPL"].halted is halted


def test_quote_time_is_the_newer_of_the_last_quote_and_the_last_trade():
    payload = quote_payload(quoteTime=1760972400000, tradeTime=1760972460000)
    assert parse_quotes(payload, NOW)["AAPL"].ts == datetime.fromtimestamp(1760972460, UTC)


def test_quote_without_a_bid_or_ask_is_skipped():
    payload = quote_payload()
    del payload["AAPL"]["quote"]["bidPrice"]
    assert parse_quotes(payload, NOW) == {}


def test_quote_without_a_timestamp_uses_the_receive_time():
    payload = quote_payload()
    del payload["AAPL"]["quote"]["quoteTime"]
    del payload["AAPL"]["quote"]["tradeTime"]
    assert parse_quotes(payload, NOW)["AAPL"].ts == NOW


def test_error_entries_in_a_quote_response_are_ignored():
    payload = quote_payload()
    payload["errors"] = {"invalidSymbols": ["NOPE"]}
    assert set(parse_quotes(payload, NOW)) == {"AAPL"}


def test_prices_keep_their_exact_decimal_value():
    payload = quote_payload(bidPrice=0.1, askPrice=0.3)
    parsed = parse_quotes(payload, NOW)["AAPL"]
    assert (parsed.bid, parsed.ask) == (Decimal("0.1"), Decimal("0.3"))


# --- market hours --------------------------------------------------------------------------------


def hours(start="09:30:00", end="16:00:00") -> dict:
    return {
        "equity": {
            "EQ": {
                "date": "2026-10-08",
                "marketType": "EQUITY",
                "product": "EQ",
                "isOpen": True,
                "sessionHours": {
                    "preMarket": [
                        {"start": "2026-10-08T07:00:00-04:00", "end": "2026-10-08T09:30:00-04:00"}
                    ],
                    "regularMarket": [
                        {"start": f"2026-10-08T{start}-04:00", "end": f"2026-10-08T{end}-04:00"}
                    ],
                },
            }
        }
    }


def test_regular_session_hours_are_read():
    session = parse_market_hours(hours(), date(2026, 10, 8))
    assert session.open == datetime(2026, 10, 8, 13, 30, tzinfo=UTC)
    assert session.close == datetime(2026, 10, 8, 20, 0, tzinfo=UTC)


def test_an_early_close_is_taken_from_the_calendar():
    session = parse_market_hours(hours(end="13:00:00"), date(2026, 10, 8))
    assert session.close == datetime(2026, 10, 8, 17, 0, tzinfo=UTC)


@pytest.mark.parametrize("inner_key", ["equity", "EQ"])
def test_a_closed_day_has_no_session_whatever_schwab_calls_the_entry(inner_key):
    payload = {
        "equity": {inner_key: {"date": "2026-10-10", "marketType": "EQUITY", "isOpen": False}}
    }
    session = parse_market_hours(payload, date(2026, 10, 10))
    assert (session.open, session.close) == (None, None)


def test_open_day_without_regular_hours_is_an_error_not_a_guess():
    payload = hours()
    del payload["equity"]["EQ"]["sessionHours"]["regularMarket"]
    with pytest.raises(ParseError):
        parse_market_hours(payload, date(2026, 10, 8))


def test_hours_for_a_different_date_are_refused():
    with pytest.raises(ParseError, match="2026-10-08"):
        parse_market_hours(hours(), date(2026, 10, 9))


@pytest.mark.parametrize("payload", [{}, {"equity": {}}, {"option": {}}, []])
def test_hours_payload_without_equity_data_is_an_error(payload):
    with pytest.raises(ParseError):
        parse_market_hours(payload, date(2026, 10, 8))


# --- candles ------------------------------------------------------------------------


def test_candles_become_bars():
    payload = {
        "symbol": "SPY",
        "empty": False,
        "candles": [
            {
                "open": 512.1,
                "high": 512.9,
                "low": 511.8,
                "close": 512.5,
                "volume": 120345,
                "datetime": 1760972400000,
            }
        ],
    }
    (bar,) = parse_candles(payload, "SPY")
    assert (bar.symbol, bar.open, bar.high, bar.low, bar.close, bar.volume) == (
        "SPY",
        Decimal("512.1"),
        Decimal("512.9"),
        Decimal("511.8"),
        Decimal("512.5"),
        120345,
    )
    assert bar.start == datetime.fromtimestamp(1760972400, UTC)


def test_candles_come_back_oldest_first():
    payload = {
        "candles": [
            {"open": 2, "high": 2, "low": 2, "close": 2, "volume": 1, "datetime": 1760972460000},
            {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "datetime": 1760972400000},
        ]
    }
    assert [bar.close for bar in parse_candles(payload, "SPY")] == [Decimal(1), Decimal(2)]


def test_empty_history_is_no_bars():
    assert parse_candles({"candles": [], "symbol": "SPY", "empty": True}, "SPY") == []


def test_incomplete_candles_are_dropped():
    payload = {
        "candles": [{"open": 1, "high": 1, "low": 1, "volume": 1, "datetime": 1760972400000}]
    }
    assert parse_candles(payload, "SPY") == []


# --- order payloads the bot sends ---------------------------------------------------


def test_limit_buy_payload_matches_schwabs_schema():
    request = OrderRequest("SPY", Side.BUY, 3, OrderType.LIMIT, Decimal("512.3"))
    assert build_equity_order(request) == {
        "orderType": "LIMIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "price": "512.30",
        "orderLegCollection": [
            {
                "instruction": "BUY",
                "quantity": 3,
                "instrument": {"symbol": "SPY", "assetType": "EQUITY"},
            }
        ],
    }


def test_market_sell_payload_has_no_price():
    request = OrderRequest("SPY", Side.SELL, 2, OrderType.MARKET, None)
    payload = build_equity_order(request)
    assert payload["orderType"] == "MARKET"
    assert "price" not in payload
    assert payload["orderLegCollection"][0]["instruction"] == "SELL"


def test_limit_price_is_never_sent_with_more_than_two_decimals():
    request = OrderRequest("SPY", Side.BUY, 1, OrderType.LIMIT, Decimal("512.3456"))
    with pytest.raises(ValueError, match="cent"):
        build_equity_order(request)


@pytest.mark.parametrize(
    "request_",
    [
        OrderRequest("SPY", Side.BUY, 0, OrderType.MARKET, None),
        OrderRequest("SPY", Side.BUY, -1, OrderType.MARKET, None),
        OrderRequest("SPY", Side.BUY, 1, OrderType.LIMIT, None),
        OrderRequest("SPY", Side.BUY, 1, OrderType.LIMIT, Decimal(0)),
    ],
)
def test_nonsense_orders_are_refused_before_they_reach_schwab(request_):
    with pytest.raises(ValueError):
        build_equity_order(request_)
