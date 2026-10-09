import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import aiohttp
import pytest

from tests.unit.helpers import make_bar
from traider.config import Config, RiskLimits
from traider.feed import Feed
from traider.marketdata import MarketData
from traider.session import SessionTracker, StaticSessionProvider
from traider.timeutil import ManualClock

# Thursday 2026-10-08, 11:00:20 in New York: mid-session, 20 seconds into a minute.
NOW = datetime(2026, 10, 8, 15, 0, 20, tzinfo=UTC)
MINUTE = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)


def candle(start: datetime, close: float) -> dict:
    return {
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 100,
        "datetime": int(start.timestamp() * 1000),
    }


async def until(predicate, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


@contextlib.asynccontextmanager
async def make_feed(
    schwab, client, signed_in, *, feed="poll", now=NOW, symbols=("SPY",), options=False, **extra
):
    clock = ManualClock(now)
    market = MarketData()
    session = SessionTracker(StaticSessionProvider())
    await session.refresh(clock.now())
    config = Config(symbols=symbols, feed=feed, risk=RiskLimits(allow_options=options), **extra)
    async with aiohttp.ClientSession() as http:
        built = Feed(
            config=config,
            http=http,
            client=client,
            tokens=signed_in,
            market=market,
            clock=clock,
            session=session,
        )
        built.clock = clock
        built.market = market
        yield built


async def schwab_connected(schwab) -> None:
    await until(lambda: sum(r["command"] == "SUBS" for r in schwab.stream_requests) >= 2)
    await asyncio.sleep(0.02)


# --- polling ------------------------------------------------------------------------


async def test_poll_puts_fresh_quotes_into_the_market(schwab, client, signed_in):
    schwab.set_quote("SPY", 512.30, 512.34)
    async with make_feed(schwab, client, signed_in) as feed:
        await feed.poll_once()
        quote = feed.market.quote("SPY")
    assert (quote.bid, quote.ask) == (Decimal("512.3"), Decimal("512.34"))
    assert quote.received_at == NOW
    assert feed.market.feed_alive_at == NOW


async def test_poll_flags_delayed_quotes(schwab, client, signed_in):
    schwab.set_quote("SPY", 512.30, 512.34, realtime=False)
    async with make_feed(schwab, client, signed_in) as feed:
        await feed.poll_once()
        assert feed.market.quote("SPY").delayed is True


async def test_poll_delivers_closed_minute_bars(schwab, client, signed_in):
    schwab.candles["SPY"] = [
        candle(MINUTE - timedelta(minutes=2), 512.0),
        candle(MINUTE - timedelta(minutes=1), 512.5),
    ]
    async with make_feed(schwab, client, signed_in) as feed:
        await feed.poll_once()
        closes = [bar.close for bar, _ in feed.market.drain_bars()]
    assert closes == [Decimal("512.0"), Decimal("512.5")]


async def test_the_minute_still_in_progress_is_not_delivered(schwab, client, signed_in):
    schwab.candles["SPY"] = [
        candle(MINUTE - timedelta(minutes=1), 512.5),
        candle(MINUTE, 513.0),
    ]  # 11:00 bar, only 20s old
    async with make_feed(schwab, client, signed_in) as feed:
        await feed.poll_once()
        closes = [bar.close for bar, _ in feed.market.drain_bars()]
    assert closes == [Decimal("512.5")]


async def test_bars_are_fetched_once_per_minute_not_on_every_poll(schwab, client, signed_in):
    async with make_feed(schwab, client, signed_in) as feed:
        await feed.poll_once()
        feed.clock.advance(5)
        await feed.poll_once()
        assert len(schwab.calls("GET", "/pricehistory")) == 1
        feed.clock.advance(60)
        await feed.poll_once()
        assert len(schwab.calls("GET", "/pricehistory")) == 2


async def test_the_same_bar_is_not_delivered_twice(schwab, client, signed_in):
    schwab.candles["SPY"] = [candle(MINUTE - timedelta(minutes=1), 512.5)]
    async with make_feed(schwab, client, signed_in) as feed:
        await feed.poll_once()
        feed.clock.advance(60)
        await feed.poll_once()
        assert len(feed.market.drain_bars()) == 1


async def test_bars_wait_a_few_seconds_after_the_minute_turns(schwab, client, signed_in):
    just_after = MINUTE + timedelta(seconds=1)
    async with make_feed(schwab, client, signed_in, now=just_after) as feed:
        await feed.poll_once()
        assert schwab.calls("GET", "/pricehistory") == []
        feed.clock.advance(5)
        await feed.poll_once()
        assert len(schwab.calls("GET", "/pricehistory")) == 1


async def test_poll_survives_schwab_errors(schwab, client, signed_in):
    schwab.fail("GET", "/marketdata/v1/quotes", 503, times=3)
    async with make_feed(schwab, client, signed_in) as feed:
        await feed.poll_once()
        assert feed.market.feed_alive_at is None


async def test_a_failed_bar_fetch_is_tried_again_on_the_next_poll(schwab, client, signed_in):
    schwab.candles["SPY"] = [candle(MINUTE - timedelta(minutes=1), 512.5)]
    schwab.fail("GET", "/pricehistory", 503, times=3)
    async with make_feed(schwab, client, signed_in) as feed:
        await feed.poll_once()
        assert feed.market.drain_bars() == []
        feed.clock.advance(5)
        await feed.poll_once()
        assert len(feed.market.drain_bars()) == 1


async def test_nothing_is_polled_while_the_market_is_closed(schwab, client, signed_in):
    evening = datetime(2026, 10, 8, 23, 0, 20, tzinfo=UTC)
    async with make_feed(schwab, client, signed_in, now=evening) as feed:
        await feed.poll_once()
    assert schwab.calls("GET", "/marketdata") == []


# --- bars outside the regular session -----------------------------------------------------


async def test_premarket_bars_never_reach_the_strategy(schwab, client, signed_in):
    async with make_feed(schwab, client, signed_in) as feed:
        feed.on_bar(make_bar("SPY", "500", start=datetime(2026, 10, 8, 12, 0, tzinfo=UTC)))
        feed.on_bar(make_bar("SPY", "512", start=datetime(2026, 10, 8, 14, 0, tzinfo=UTC)))
        closes = [bar.close for bar, _ in feed.market.drain_bars()]
    assert closes == [Decimal(512)]


async def test_quotes_are_passed_on_even_outside_the_session(schwab, client, signed_in):
    from tests.unit.helpers import make_quote

    evening = datetime(2026, 10, 8, 23, 0, 20, tzinfo=UTC)
    async with make_feed(schwab, client, signed_in, now=evening) as feed:
        feed.on_quote(make_quote("SPY", at=evening))
        assert feed.market.quote("SPY") is not None


# --- warm-up ---------------------------------------------------------------------------------


async def test_warmup_replays_the_most_recent_closed_bars_oldest_first(schwab, client, signed_in):
    schwab.candles["SPY"] = [
        candle(MINUTE - timedelta(minutes=i), 500 + i) for i in range(10, 0, -1)
    ]
    schwab.candles["SPY"].append(candle(MINUTE, 999))  # in progress: must be left out
    async with make_feed(schwab, client, signed_in) as feed:
        assert await feed.warmup(3) is True
        bars = feed.market.drain_bars()
    assert [bar.close for bar, _ in bars] == [Decimal(503), Decimal(502), Decimal(501)]
    assert all(warm for _, warm in bars)


async def test_warmup_covers_every_symbol(schwab, client, signed_in):
    for symbol in ("SPY", "QQQ"):
        schwab.candles[symbol] = [candle(MINUTE - timedelta(minutes=1), 500)]
    async with make_feed(schwab, client, signed_in, symbols=("SPY", "QQQ")) as feed:
        await feed.warmup(5)
        assert {bar.symbol for bar, _ in feed.market.drain_bars()} == {"SPY", "QQQ"}


async def test_warmup_reaches_back_over_previous_days(schwab, client, signed_in):
    async with make_feed(schwab, client, signed_in) as feed:
        await feed.warmup(5)
    (request,) = schwab.calls("GET", "/pricehistory")
    start = datetime.fromtimestamp(int(request["query"]["startDate"]) / 1000, UTC)
    assert NOW - start >= timedelta(days=4)  # covers a weekend


async def test_warmup_reports_failure_so_it_can_be_retried(schwab, client, signed_in):
    schwab.fail("GET", "/pricehistory", 503, times=3)
    async with make_feed(schwab, client, signed_in) as feed:
        assert await feed.warmup(5) is False


async def test_warmup_with_nothing_to_load_is_a_success(schwab, client, signed_in):
    async with make_feed(schwab, client, signed_in) as feed:
        assert await feed.warmup(0) is True
    assert schwab.calls("GET", "/pricehistory") == []


# --- stream first, polling as the fallback -----------------------------------------------------


@contextlib.asynccontextmanager
async def running(feed):
    task = asyncio.create_task(feed.run(warmup_bars=0))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_stream_quotes_flow_into_the_market(schwab, client, signed_in):
    async with make_feed(schwab, client, signed_in, feed="stream") as feed, running(feed):
        await schwab_connected(schwab)
        await schwab.push_quote("SPY", 512.30, 512.34)
        await until(lambda: feed.market.quote("SPY") is not None)
    assert schwab.calls("GET", "/marketdata/v1/quotes") == []


async def test_no_polling_while_the_stream_is_healthy(schwab, client, signed_in):
    async with make_feed(schwab, client, signed_in, feed="stream") as feed, running(feed):
        await schwab_connected(schwab)
        await schwab.push_quote("SPY", 512.30, 512.34)
        await until(feed.stream_healthy)
        await feed.poll_once()
    assert schwab.calls("GET", "/marketdata/v1/quotes") == []


async def test_polling_takes_over_when_the_stream_is_down(schwab, client, signed_in):
    schwab.set_quote("SPY", 512.30, 512.34)
    schwab.stream_login_code = 3  # the stream cannot log in
    async with make_feed(schwab, client, signed_in, feed="stream") as feed, running(feed):
        await until(lambda: schwab.stream_logins >= 1)
        await feed.poll_once()
        assert feed.market.quote("SPY") is not None


async def test_polling_takes_over_when_the_stream_goes_silent(schwab, client, signed_in):
    schwab.set_quote("SPY", 512.30, 512.34)
    async with make_feed(schwab, client, signed_in, feed="stream") as feed, running(feed):
        await schwab_connected(schwab)
        await schwab.push_quote("SPY", 512.10, 512.14)
        await until(feed.stream_healthy)
        feed.clock.advance(40)  # connected, but nothing has arrived for 40 seconds
        assert feed.stream_healthy() is False
        await feed.poll_once()
        assert feed.market.quote("SPY") is not None


async def test_heartbeats_alone_do_not_count_as_a_working_stream(schwab, client, signed_in):
    schwab.now = NOW.timestamp
    schwab.set_quote("SPY", 512.30, 512.34)
    async with make_feed(schwab, client, signed_in, feed="stream") as feed, running(feed):
        await schwab_connected(schwab)
        await schwab.push_quote("SPY", 512.10, 512.14)
        await until(feed.stream_healthy)
        feed.clock.advance(40)
        await schwab.push_heartbeat()  # the socket is alive, but no prices are coming
        await asyncio.sleep(0.05)
        assert feed.stream_healthy() is False
        await feed.poll_once()
        assert feed.market.quote("SPY").bid == Decimal("512.3")


async def test_polling_covers_a_symbol_the_stream_is_not_delivering(schwab, client, signed_in):
    schwab.now = NOW.timestamp
    schwab.set_quote("SPY", 512.30, 512.34)
    schwab.set_quote("QQQ", 440.10, 440.15)
    symbols = ("SPY", "QQQ")
    async with (
        make_feed(schwab, client, signed_in, feed="stream", symbols=symbols) as feed,
        running(feed),
    ):
        await schwab_connected(schwab)
        await schwab.push_quote("SPY", 512.10, 512.14)
        await until(lambda: feed.market.quote("SPY") is not None)
        assert feed.stream_healthy() is False  # nothing for QQQ yet
        await feed.poll_once()
        assert feed.market.quote("QQQ").bid == Decimal("440.1")


async def test_run_warms_up_before_any_live_data(schwab, client, signed_in):
    schwab.candles["SPY"] = [candle(MINUTE - timedelta(minutes=1), 512.5)]
    schwab.set_quote("SPY", 512.30, 512.34)
    async with make_feed(schwab, client, signed_in) as feed:
        task = asyncio.create_task(feed.run(warmup_bars=5, poll_interval_s=0.01))
        await until(lambda: feed.market.quote("SPY") is not None)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        bars = feed.market.drain_bars()
    assert [warm for _, warm in bars] == [True]


async def test_run_keeps_retrying_warmup_until_schwab_answers(schwab, client, signed_in):
    schwab.candles["SPY"] = [candle(MINUTE - timedelta(minutes=1), 512.5)]
    schwab.fail("GET", "/pricehistory", 503, times=3)
    async with make_feed(schwab, client, signed_in) as feed:
        task = asyncio.create_task(
            feed.run(warmup_bars=5, poll_interval_s=0.01, warmup_retry_s=0.01)
        )
        seen = []
        await until(lambda: seen.extend(feed.market.drain_bars()) or len(seen) == 1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert len(schwab.calls("GET", "/pricehistory")) > 3  # the first attempt failed three times


@pytest.mark.parametrize("mode", ["poll", "stream"])
async def test_run_stops_cleanly_when_cancelled(schwab, client, signed_in, mode):
    async with make_feed(schwab, client, signed_in, feed=mode) as feed:
        task = asyncio.create_task(feed.run(warmup_bars=0, poll_interval_s=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await until(lambda: schwab.sockets == [])


# --- options ------------------------------------------------------------------------

CALL = "SPY   261016C00500000"
PUT = "SPY   261016P00500000"


async def test_watched_option_contracts_are_quoted_by_polling(schwab, client, signed_in):
    schwab.add_option(CALL, 2.00, 2.10)
    async with make_feed(schwab, client, signed_in) as feed:
        feed.market.watch(CALL)
        await feed.poll_once()
        quote = feed.market.quote(CALL)
    assert (quote.bid, quote.ask, quote.delayed) == (Decimal("2.0"), Decimal("2.1"), False)


async def test_options_are_polled_even_while_the_stream_is_healthy(schwab, client, signed_in):
    schwab.add_option(CALL, 2.00, 2.10)
    async with make_feed(schwab, client, signed_in, feed="stream") as feed, running(feed):
        await schwab_connected(schwab)
        await schwab.push_quote("SPY", 512.30, 512.34)
        await until(feed.stream_healthy)
        feed.market.watch(CALL)
        await feed.poll_once()
        assert feed.market.quote(CALL) is not None
    asked = [call["query"]["symbols"] for call in schwab.calls("GET", "/marketdata/v1/quotes")]
    assert asked == [CALL]  # the stream covers SPY itself


async def test_no_option_quotes_are_asked_for_when_nothing_is_watched(schwab, client, signed_in):
    async with make_feed(schwab, client, signed_in, options=True) as feed:
        await feed.poll_once()
    asked = [call["query"]["symbols"] for call in schwab.calls("GET", "/marketdata/v1/quotes")]
    assert asked == ["SPY"]


async def test_a_failed_option_poll_does_not_stop_the_share_poll(schwab, client, signed_in):
    schwab.set_quote("SPY", 512.30, 512.34)
    schwab.fail("GET", "/marketdata/v1/quotes", 500, times=3)  # every try of the first call
    async with make_feed(schwab, client, signed_in) as feed:
        feed.market.watch(CALL)
        await feed.poll_once()
        assert feed.market.quote("SPY") is not None


async def test_option_chains_are_loaded_when_options_are_on(schwab, client, signed_in):
    schwab.add_option(CALL, 2.00, 2.10, delta=0.45, days=8)
    schwab.add_option(PUT, 1.50, 1.60, delta=-0.40, days=8)
    async with make_feed(
        schwab, client, signed_in, options=True, option_chain_days=30, option_chain_strikes=12
    ) as feed:
        await feed.poll_once()
        chain = feed.market.chain("SPY")
    assert [(line.symbol, line.delta, line.days_to_expiry) for line in chain] == [
        (CALL, Decimal("0.45"), 8),
        (PUT, Decimal("-0.4"), 8),
    ]
    (call,) = schwab.calls("GET", "/marketdata/v1/chains")
    assert (call["query"]["fromDate"], call["query"]["toDate"], call["query"]["strikeCount"]) == (
        "2026-10-08",
        "2026-11-07",
        "12",
    )


async def test_no_chains_are_loaded_while_options_are_off(schwab, client, signed_in):
    schwab.add_option(CALL, 2.00, 2.10)
    async with make_feed(schwab, client, signed_in) as feed:
        await feed.poll_once()
        assert feed.market.chain("SPY") == ()
    assert schwab.calls("GET", "/marketdata/v1/chains") == []


async def test_chains_are_refreshed_once_a_minute_not_on_every_poll(schwab, client, signed_in):
    schwab.add_option(CALL, 2.00, 2.10)
    async with make_feed(schwab, client, signed_in, options=True) as feed:
        await feed.poll_once()
        feed.clock.advance(30)
        await feed.poll_once()
        assert len(schwab.calls("GET", "/marketdata/v1/chains")) == 1
        feed.clock.advance(30)
        await feed.poll_once()
        assert len(schwab.calls("GET", "/marketdata/v1/chains")) == 2


async def test_a_chain_that_cannot_be_refreshed_is_dropped_not_kept_stale(
    schwab, client, signed_in
):
    schwab.add_option(CALL, 2.00, 2.10)
    async with make_feed(schwab, client, signed_in, options=True) as feed:
        await feed.poll_once()
        assert feed.market.chain("SPY") != ()
        feed.clock.advance(60)
        schwab.fail("GET", "/marketdata/v1/chains", 500, times=4)
        await feed.poll_once()
        assert feed.market.chain("SPY") == ()
        feed.clock.advance(5)  # and it is tried again on the next poll, not a minute later
        await feed.poll_once()
        assert feed.market.chain("SPY") != ()


async def test_no_option_data_is_polled_while_the_market_is_closed(schwab, client, signed_in):
    schwab.add_option(CALL, 2.00, 2.10)
    night = datetime(2026, 10, 8, 2, 0, tzinfo=UTC)
    async with make_feed(schwab, client, signed_in, options=True, now=night) as feed:
        feed.market.watch(CALL)
        await feed.poll_once()
    assert schwab.calls("GET", "/marketdata/v1") == []
