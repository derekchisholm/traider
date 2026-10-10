"""Research's market data: Schwab replies parsed defensively, through the fake Schwab server."""

from datetime import UTC, date, datetime, timedelta

import pytest

from tests.fakes.research import quote
from traider.research.market import (
    DailyBar,
    PutContract,
    SchwabMarketData,
    liquid_puts,
    parse_daily_bars,
    parse_market_quotes,
    parse_movers,
    parse_puts,
    put_summary,
)
from traider.schwab.client import SchwabError
from traider.schwab.parse import ParseError

TODAY = date(2026, 10, 9)

NVDA_QUOTE = {  # the shape Schwab sends for fields=quote,fundamental,reference
    "assetMainType": "EQUITY",
    "assetSubType": "COE",
    "quoteType": "NBBO",
    "realtime": True,
    "ssid": 1,
    "symbol": "NVDA",
    "fundamental": {
        "avg10DaysVolume": 41_000_000,
        "avg1YearVolume": 45_000_000,
        "divYield": 0.03,
        "peRatio": 55.1,
        "eps": 2.1,
    },
    "quote": {
        "52WeekHigh": 212.0,
        "52WeekLow": 98.5,
        "askPrice": 104.1,
        "bidPrice": 104.0,
        "closePrice": 100.0,
        "lastPrice": 104.05,
        "securityStatus": "Normal",
        "totalVolume": 1_200_000,
    },
    "reference": {"cusip": "67066G104", "exchange": "Q", "exchangeName": "NASDAQ"},
}


def chain_entry(symbol, strike, days, bid, ask, oi):
    return {
        "putCall": "PUT",
        "symbol": symbol,
        "strikePrice": strike,
        "daysToExpiration": days,
        "bid": bid,
        "ask": ask,
        "openInterest": oi,
    }


# --- parsing ----------------------------------------------------------------------------


def test_a_quote_with_fundamentals_is_read_in_full():
    batch = parse_market_quotes({"NVDA": NVDA_QUOTE})
    q = batch.quotes["NVDA"]
    assert batch.skipped == 0
    assert (q.asset_type, q.asset_sub_type, q.exchange) == ("EQUITY", "COE", "NASDAQ")
    assert (q.last, q.prev_close, q.halted) == (104.05, 100.0, False)
    assert (q.avg_volume, q.high_52w, q.low_52w, q.pe, q.div_yield) == (
        41_000_000,
        212.0,
        98.5,
        55.1,
        0.03,
    )
    assert q.gap_pct == pytest.approx(4.05)
    assert not q.is_etf and not q.is_otc


def test_unreadable_quotes_are_skipped_and_counted():
    raw = {
        "NVDA": NVDA_QUOTE,
        "BAD1": "not an object",
        "BAD2": {"assetMainType": "EQUITY"},  # no quote block
        "BAD3": {"quote": {"lastPrice": 1}},  # no asset type
        "errors": {"invalidSymbols": ["ZZZZ"]},
    }
    batch = parse_market_quotes(raw)
    assert set(batch.quotes) == {"NVDA"}
    assert batch.skipped == 3


def test_odd_numbers_become_unknown_not_guesses():
    entry = {
        **NVDA_QUOTE,
        "quote": {**NVDA_QUOTE["quote"], "lastPrice": "NaN", "closePrice": True},
        "fundamental": {"avg1YearVolume": 5},
    }
    q = parse_market_quotes({"NVDA": entry}).quotes["NVDA"]
    assert (q.last, q.prev_close, q.gap_pct) == (None, None, None)
    assert q.avg_volume == 5  # the one-year average when the ten-day one is missing


def test_a_status_other_than_normal_means_halted():
    entry = {**NVDA_QUOTE, "quote": {**NVDA_QUOTE["quote"], "securityStatus": "Halted"}}
    assert parse_market_quotes({"NVDA": entry}).quotes["NVDA"].halted


@pytest.mark.parametrize(
    ("asset_type", "sub_type", "etf"),
    [
        ("EQUITY", "COE", False),
        ("EQUITY", "ETF", True),
        ("EQUITY", "ETN", True),
        ("COLLECTIVE_INVESTMENT", None, True),
    ],
)
def test_etfs_and_etns_are_recognised(asset_type, sub_type, etf):
    assert quote("X", 10, 10, asset_type=asset_type, sub_type=sub_type).is_etf is etf


@pytest.mark.parametrize(
    ("exchange", "otc"),
    [("NASDAQ", False), ("NYSE", False), ("OTC Markets", True), ("Pink Sheet", True), (None, True)],
)
def test_otc_pink_sheets_and_unknown_exchanges_count_as_otc(exchange, otc):
    assert quote("X", 10, 10, exchange=exchange).is_otc is otc


def test_movers_are_symbols_in_order_without_repeats():
    raw = {"screeners": [{"symbol": "AMD"}, {"symbol": "NVDA"}, {"symbol": "AMD"}, {"x": 1}]}
    assert parse_movers(raw) == ["AMD", "NVDA"]
    assert parse_movers({"screeners": []}) == []


def test_a_movers_reply_without_a_list_is_an_error():
    with pytest.raises(ParseError):
        parse_movers({"errors": ["nope"]})


def test_daily_candles_become_new_york_days():
    raw = {
        "candles": [
            # Schwab stamps daily candles at midnight Central: 05:00 UTC in summer.
            {
                "datetime": 1759986000000,
                "open": 1,
                "high": 2,
                "low": 0.5,
                "close": 1.5,
                "volume": 100,
            },
            {
                "datetime": 1760072400000,
                "open": 1.5,
                "high": 2,
                "low": 1,
                "close": 2,
                "volume": 200,
            },
            {"datetime": "bad", "open": 1, "high": 1, "low": 1, "close": 1},
        ]
    }
    bars = parse_daily_bars(raw, "NVDA")
    assert [(b.day, b.close, b.volume) for b in bars] == [
        (date(2025, 10, 9), 1.5, 100),
        (date(2025, 10, 10), 2.0, 200),
    ]


def test_puts_are_kept_only_near_the_money_and_7_to_45_days_out():
    raw = {
        "putExpDateMap": {
            "2026-10-23:14": {
                "100.0": [chain_entry("NVDA  261023P00100000", 100.0, 14, 2.0, 2.1, 500)],
                "90.0": [chain_entry("NVDA  261023P00090000", 90.0, 14, 0.5, 0.6, 900)],
            },
            "2026-10-12:3": {
                "100.0": [chain_entry("NVDA  261012P00100000", 100.0, 3, 1.0, 1.1, 900)]
            },
            "2026-12-18:70": {
                "100.0": [chain_entry("NVDA  261218P00100000", 100.0, 70, 5.0, 5.2, 900)]
            },
        }
    }
    (only,) = parse_puts(raw, 102.0)
    assert (only.strike, only.days, only.open_interest) == (100.0, 14, 500)
    assert parse_puts(raw, 0.0) == []


def test_put_liquidity_summary_and_filter():
    puts = [
        PutContract(symbol="A", strike=100, days=14, bid=2.0, ask=2.1, open_interest=500),
        PutContract(symbol="B", strike=99, days=14, bid=0.0, ask=0.5, open_interest=5000),
        PutContract(symbol="C", strike=101, days=21, bid=1.0, ask=1.5, open_interest=50),
    ]
    assert put_summary(puts) == {"count": 3, "best_spread_pct": 4.88, "max_open_interest": 5000}
    assert put_summary([]) == {"count": 0, "best_spread_pct": None, "max_open_interest": 0}
    assert [p.symbol for p in liquid_puts(puts, max_spread_pct=10, min_open_interest=100)] == ["A"]
    assert [p.symbol for p in liquid_puts(puts, max_spread_pct=50, min_open_interest=10)] == [
        "A",
        "C",
    ]


# --- the adapter, through the fake Schwab server -----------------------------------------


async def test_quotes_go_out_in_chunks_of_100_with_fundamentals(client, schwab):
    symbols = [f"S{i:03d}" for i in range(250)]
    for symbol in symbols[:3]:
        schwab.quotes[symbol] = {**NVDA_QUOTE, "symbol": symbol}
    batch = await SchwabMarketData(client).quotes(symbols)
    assert set(batch.quotes) == set(symbols[:3])
    requests = schwab.calls("GET", "/marketdata/v1/quotes")
    assert [len(r["query"]["symbols"].split(",")) for r in requests] == [100, 100, 50]
    assert {r["query"]["fields"] for r in requests} == {"quote,fundamental,reference"}


async def test_movers_through_the_adapter(client, schwab):
    schwab.movers["EQUITY_ALL"] = [{"symbol": "NVDA"}, {"symbol": "AMD"}]
    assert await SchwabMarketData(client).movers("EQUITY_ALL", "VOLUME") == ["NVDA", "AMD"]


async def test_daily_bars_stop_before_the_given_day_and_keep_the_last_n(client, schwab):
    def candle(day: date, close: float) -> dict:
        stamp = datetime(day.year, day.month, day.day, 5, 0, tzinfo=UTC)
        return {
            "datetime": int(stamp.timestamp() * 1000),
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 10,
        }

    schwab.candles["NVDA"] = [
        candle(date(2026, 10, 6), 1.0),
        candle(date(2026, 10, 7), 2.0),
        candle(date(2026, 10, 8), 3.0),
    ]
    bars = await SchwabMarketData(client).daily_bars("NVDA", TODAY, 2)
    assert [(b.day, b.close) for b in bars] == [(date(2026, 10, 7), 2.0), (date(2026, 10, 8), 3.0)]
    (request,) = schwab.calls("GET", "/pricehistory")
    assert request["query"]["frequencyType"] == "daily"


async def test_puts_through_the_adapter_ask_for_puts_7_to_45_days_out(client, schwab):
    schwab.add_option("NVDA  261023P00100000", 2.0, 2.1, days=14)
    puts = await SchwabMarketData(client).puts("NVDA", 101.0, TODAY)
    assert [p.symbol for p in puts] == ["NVDA  261023P00100000"]
    (request,) = schwab.calls("GET", "/chains")
    assert request["query"]["contractType"] == "PUT"
    assert (request["query"]["fromDate"], request["query"]["toDate"]) == (
        "2026-10-16",
        "2026-11-23",
    )


async def test_market_session_through_the_adapter(client, schwab):
    session = await SchwabMarketData(client).market_session(TODAY)
    assert session.open is not None and session.close is not None
    schwab.market_open = False
    assert (await SchwabMarketData(client).market_session(TODAY)).open is None


async def test_a_failed_read_raises(client, schwab):
    schwab.fail("GET", "/movers/", 500, times=5)
    with pytest.raises(SchwabError):
        await SchwabMarketData(client).movers("NYSE", "VOLUME")


def test_daily_bar_is_plain_data():
    bar = DailyBar(day=TODAY, open=1, high=2, low=0.5, close=1.5, volume=10)
    assert bar.model_dump(mode="json")["day"] == "2026-10-09"


# --- fail closed: stricter parsing -----------------------------------------------------------


@pytest.mark.parametrize("raw", [None, [], "nope", 5])
def test_a_quotes_reply_that_is_not_an_object_is_an_error(raw):
    with pytest.raises(ParseError):
        parse_market_quotes(raw)


def _with_quote(**changes):
    return {"NVDA": {**NVDA_QUOTE, "quote": {**NVDA_QUOTE["quote"], **changes}}}


@pytest.mark.parametrize("bad", [-1.0, 0, 0.0, float("inf"), float("-inf"), float("nan")])
def test_prices_must_be_finite_and_above_zero(bad):
    q = parse_market_quotes(_with_quote(lastPrice=bad, closePrice=bad)).quotes["NVDA"]
    assert (q.last, q.prev_close, q.gap_pct) == (None, None, None)


@pytest.mark.parametrize("bad", [-1.0, float("inf")])
def test_volume_and_52_week_levels_must_be_finite_and_not_negative(bad):
    entry = {
        **NVDA_QUOTE,
        "quote": {**NVDA_QUOTE["quote"], "52WeekHigh": bad, "52WeekLow": bad},
        "fundamental": {"avg10DaysVolume": bad, "avg1YearVolume": bad},
    }
    q = parse_market_quotes({"NVDA": entry}).quotes["NVDA"]
    assert (q.avg_volume, q.high_52w, q.low_52w) == (None, None, None)


def test_zero_volume_and_zero_levels_are_allowed():
    entry = {
        **NVDA_QUOTE,
        "quote": {**NVDA_QUOTE["quote"], "52WeekLow": 0},
        "fundamental": {"avg10DaysVolume": 0},
    }
    q = parse_market_quotes({"NVDA": entry}).quotes["NVDA"]
    assert (q.avg_volume, q.low_52w) == (0, 0)


def test_gap_needs_two_positive_prices():
    assert quote("X", 0.0, 10.0).gap_pct is None
    assert quote("X", 10.0, 0.0).gap_pct is None
    assert quote("X", -1.0, 10.0).gap_pct is None
    assert quote("X", 11.0, 10.0).gap_pct == pytest.approx(10.0)


def test_only_an_explicit_normal_status_is_tradable():
    normal = parse_market_quotes(_with_quote(securityStatus="Normal")).quotes["NVDA"]
    assert not normal.halted
    missing_quote = {k: v for k, v in NVDA_QUOTE["quote"].items() if k != "securityStatus"}
    for status in ({**NVDA_QUOTE, "quote": missing_quote}, _with_quote(securityStatus=7)["NVDA"]):
        assert parse_market_quotes({"NVDA": status}).quotes["NVDA"].halted


def test_the_ten_day_average_volume_wins_over_the_one_year_average():
    entry = {**NVDA_QUOTE, "fundamental": {"avg10DaysVolume": 7, "avg1YearVolume": 9}}
    assert parse_market_quotes({"NVDA": entry}).quotes["NVDA"].avg_volume == 7


def test_movers_that_are_not_valid_symbols_are_dropped():
    raw = {
        "screeners": [
            {"symbol": "AMD"},
            {"symbol": "lower"},
            {"symbol": "WAYTOOLONGSYMBOL"},
            {"symbol": "A B"},
            {"symbol": ""},
            {"symbol": 5},
            {"symbol": "BRK.B"},
        ]
    }
    assert parse_movers(raw) == ["AMD", "BRK.B"]


def candle_at(day: date, hour: int = 5, **changes):
    stamp = datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC)
    return {
        "datetime": int(stamp.timestamp() * 1000),
        "open": 10.0,
        "high": 12.0,
        "low": 9.0,
        "close": 11.0,
        "volume": 100,
        **changes,
    }


def _without(candle, name):
    return {k: v for k, v in candle.items() if k != name}


GOOD_DAY = date(2026, 10, 8)
BAD_CANDLES = {
    "nan open": candle_at(GOOD_DAY, open=float("nan")),
    "zero low": candle_at(GOOD_DAY, low=0),
    "negative close": candle_at(GOOD_DAY, close=-1.0),
    "infinite high": candle_at(GOOD_DAY, high=float("inf")),
    "text price": candle_at(GOOD_DAY, open="abc"),
    "high below low": candle_at(GOOD_DAY, high=8.0, low=9.0, open=8.5, close=8.5),
    "open above high": candle_at(GOOD_DAY, open=12.5),
    "open below low": candle_at(GOOD_DAY, open=8.5),
    "close above high": candle_at(GOOD_DAY, close=12.5),
    "close below low": candle_at(GOOD_DAY, close=8.5),
    "no volume": _without(candle_at(GOOD_DAY), "volume"),
    "text volume": candle_at(GOOD_DAY, volume="100"),
    "bool volume": candle_at(GOOD_DAY, volume=True),
    "negative volume": candle_at(GOOD_DAY, volume=-1),
    "no time": _without(candle_at(GOOD_DAY), "datetime"),
    "bool time": candle_at(GOOD_DAY, datetime=True),
    "not an object": "candle",
}


@pytest.mark.parametrize("candle", BAD_CANDLES.values(), ids=BAD_CANDLES.keys())
def test_a_candle_that_cannot_be_trusted_is_dropped_and_counted(candle, caplog):
    good = candle_at(date(2026, 10, 7))
    with caplog.at_level("WARNING", logger="traider.research.market"):
        bars = parse_daily_bars({"candles": [good, candle]}, "NVDA")
    assert [b.day for b in bars] == [date(2026, 10, 7)]
    (record,) = caplog.records
    assert "NVDA" in record.getMessage() and "dropped 1 " in record.getMessage()
    assert "10.0" not in record.getMessage()  # never the raw payload


def test_clean_candles_log_nothing(caplog):
    with caplog.at_level("WARNING", logger="traider.research.market"):
        parse_daily_bars({"candles": [candle_at(GOOD_DAY, volume=0)]}, "NVDA")
    assert not caplog.records


@pytest.mark.parametrize("raw", [None, [], {"errors": "x"}, {"candles": None}, {"candles": {}}])
def test_a_price_history_reply_without_a_candle_list_is_an_error(raw):
    with pytest.raises(ParseError):
        parse_daily_bars(raw, "NVDA")


def test_a_day_with_two_candles_keeps_the_later_one():
    early = candle_at(GOOD_DAY, hour=5, close=10.0)
    late = candle_at(GOOD_DAY, hour=15, close=11.0)
    for order in ([early, late], [late, early]):
        (bar,) = parse_daily_bars({"candles": order}, "NVDA")
        assert bar.close == 11.0


# --- puts: each malformed entry is skipped -----------------------------------------------------

GOOD_PUT = chain_entry("NVDA  261023P00100000", 100.0, 14, 2.0, 2.1, 500)


def _puts_raw(*entries, container=None):
    return {"putExpDateMap": {"2026-10-23:14": {"100.0": container or list(entries)}}}


BAD_PUTS = {
    "no symbol": {**GOOD_PUT, "symbol": None},
    "text days": {**GOOD_PUT, "daysToExpiration": "14"},
    "bool days": {**GOOD_PUT, "daysToExpiration": True},
    "no strike": {**GOOD_PUT, "strikePrice": None},
    "zero strike": {**GOOD_PUT, "strikePrice": 0},
    "no bid": {**GOOD_PUT, "bid": None},
    "negative bid": {**GOOD_PUT, "bid": -1.0},
    "no ask": {**GOOD_PUT, "ask": "x"},
    "infinite ask": {**GOOD_PUT, "ask": float("inf")},
    "not an object": "put",
}


@pytest.mark.parametrize("bad", BAD_PUTS.values(), ids=BAD_PUTS.keys())
def test_a_malformed_put_is_skipped_and_the_good_one_kept(bad):
    other = chain_entry("NVDA  261023P00101000", 101.0, 14, 1.0, 1.1, 10)
    got = parse_puts(_puts_raw(bad, other), 100.0)
    assert [p.symbol for p in got] == ["NVDA  261023P00101000"]


@pytest.mark.parametrize("oi", [None, "500", True, -1.5])
def test_a_put_with_unreadable_open_interest_counts_as_zero(oi):
    (put,) = parse_puts(_puts_raw({**GOOD_PUT, "openInterest": oi}), 100.0)
    assert put.open_interest == 0


def test_a_zero_bid_put_is_kept_so_it_reads_as_illiquid():
    (put,) = parse_puts(_puts_raw({**GOOD_PUT, "bid": 0.0}), 100.0)
    assert put.bid == 0 and put.spread_pct is None


@pytest.mark.parametrize("entries", [5, None, 1.5, True])
def test_a_strike_whose_entries_are_not_a_list_is_skipped(entries):
    raw = {
        "putExpDateMap": {
            "2026-10-23:14": {"100.0": entries, "101.0": [GOOD_PUT]},
        }
    }
    assert [p.symbol for p in parse_puts(raw, 100.0)] == [GOOD_PUT["symbol"]]


def test_the_five_percent_band_edge():
    def keep(strike):
        entry = chain_entry("NVDA  261023P00100000", strike, 14, 2.0, 2.1, 5)
        return bool(parse_puts(_puts_raw(entry), 100.0))

    assert keep(95.0) and keep(95.1) and keep(104.9) and keep(105.0)
    assert not keep(94.9) and not keep(105.1)


@pytest.mark.parametrize(("days", "kept"), [(6, False), (7, True), (45, True), (46, False)])
def test_the_day_range_edges(days, kept):
    entry = chain_entry("NVDA  261023P00100000", 100.0, days, 2.0, 2.1, 5)
    assert bool(parse_puts(_puts_raw(entry), 100.0)) is kept


# --- the adapter keeps only the symbols it asked for -------------------------------------------


class StubClient:
    def __init__(self, reply):
        self.reply = reply
        self.requested: list[list[str]] = []

    async def quotes(self, symbols, *, fields):
        self.requested.append(list(symbols))
        return self.reply


async def test_quotes_the_adapter_did_not_ask_for_are_ignored():
    reply = {"NVDA": NVDA_QUOTE, "AMD": {**NVDA_QUOTE, "symbol": "AMD"}}
    batch = await SchwabMarketData(StubClient(reply)).quotes(["NVDA", "NVDA"])  # type: ignore[arg-type]
    assert set(batch.quotes) == {"NVDA"}


async def test_the_adapter_raises_when_a_quotes_reply_is_not_an_object():
    with pytest.raises(ParseError):
        await SchwabMarketData(StubClient(["nope"])).quotes(["NVDA"])  # type: ignore[arg-type]


async def test_daily_bars_look_back_far_enough_for_holidays(client, schwab):
    await SchwabMarketData(client).daily_bars("NVDA", TODAY, 50)
    (request,) = schwab.calls("GET", "/pricehistory")
    first = datetime(2026, 10, 9, tzinfo=UTC) - timedelta(days=50 * 7 // 5 + 30)
    assert int(request["query"]["startDate"]) // 1000 >= int(first.timestamp()) - 86400
    assert int(request["query"]["startDate"]) // 1000 <= int(first.timestamp()) + 86400


def test_a_boolean_is_not_a_day_count():
    raw = _puts_raw({**GOOD_PUT, "daysToExpiration": True})
    assert parse_puts(raw, 100.0, min_days=1) == []
