"""Research's market data: Schwab replies parsed defensively, through the fake Schwab server."""

from datetime import UTC, date, datetime

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
