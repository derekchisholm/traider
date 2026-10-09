import asyncio
import contextlib
from datetime import UTC, datetime
from decimal import Decimal

import aiohttp

from traider.schwab.stream import SchwabStream
from traider.schwab.tokens import MemoryTokenStore, StaticCredentials, TokenManager
from traider.timeutil import SystemClock


class Recorder:
    def __init__(self):
        self.quotes = []
        self.bars = []
        self.alive = []

    def on_quote(self, quote):
        self.quotes.append(quote)

    def on_bar(self, bar):
        self.bars.append(bar)

    def on_alive(self, when):
        self.alive.append(when)


async def until(predicate, timeout=3.0):
    """Wait for something that happens on the event loop, without fixed sleeps."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


@contextlib.asynccontextmanager
async def running(schwab, client, tokens, symbols=("SPY", "QQQ"), connect=True):
    sink = Recorder()
    async with aiohttp.ClientSession() as session:
        stream = SchwabStream(
            session,
            client,
            tokens,
            sink=sink,
            clock=SystemClock(),
            symbols=symbols,
            backoff_initial_s=0.01,
            backoff_max_s=0.05,
        )
        task = asyncio.create_task(stream.run())
        try:
            if connect:
                await until(lambda: stream.connected)
            yield stream, sink
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def test_stream_logs_in_with_the_access_token_and_streamer_ids(schwab, client, signed_in):
    async with running(schwab, client, signed_in):
        login = schwab.stream_requests[0]
    assert (login["service"], login["command"]) == ("ADMIN", "LOGIN")
    assert login["SchwabClientCustomerId"] == "CUSTOMER-ID"
    assert login["SchwabClientCorrelId"] == "CORREL-ID"
    assert login["parameters"] == {
        "Authorization": await signed_in.access_token(),
        "SchwabClientChannel": "N9",
        "SchwabClientFunctionId": "APIAPP",
    }


async def test_stream_subscribes_to_quotes_and_minute_bars_for_every_symbol(
    schwab, client, signed_in
):
    async with running(schwab, client, signed_in):
        pass
    assert schwab.subscriptions == {
        "LEVELONE_EQUITIES": {"SPY", "QQQ"},
        "CHART_EQUITY": {"SPY", "QQQ"},
    }
    quotes = next(r for r in schwab.stream_requests if r["service"] == "LEVELONE_EQUITIES")
    assert quotes["parameters"]["keys"] == "SPY,QQQ"
    # symbol, bid, ask, last, bid size, ask size, volume, quote time, trade time
    assert quotes["parameters"]["fields"] == "0,1,2,3,4,5,8,34,35"


async def test_request_ids_are_distinct(schwab, client, signed_in):
    async with running(schwab, client, signed_in):
        pass
    ids = [r["requestid"] for r in schwab.stream_requests]
    assert len(ids) == len(set(ids)) == 3


async def test_a_quote_update_reaches_the_sink(schwab, client, signed_in):
    async with running(schwab, client, signed_in) as (_, sink):
        await schwab.push_quote("SPY", 512.30, 512.34, 512.32, at_ms=1760972400123)
        await until(lambda: sink.quotes)
    (quote,) = sink.quotes
    assert (quote.symbol, quote.bid, quote.ask, quote.last) == (
        "SPY",
        Decimal("512.3"),
        Decimal("512.34"),
        Decimal("512.32"),
    )
    assert quote.ts == datetime.fromtimestamp(1760972400.123, UTC)
    assert quote.delayed is False


async def test_partial_updates_are_merged_with_what_is_already_known(schwab, client, signed_in):
    async with running(schwab, client, signed_in) as (_, sink):
        await schwab.push_quote("SPY", 512.30, 512.34)
        await schwab.push_level_one("SPY", f2=512.40)  # only the ask moved
        await until(lambda: len(sink.quotes) == 2)
    assert (sink.quotes[1].bid, sink.quotes[1].ask) == (Decimal("512.3"), Decimal("512.4"))


async def test_symbols_do_not_share_merged_fields(schwab, client, signed_in):
    async with running(schwab, client, signed_in) as (_, sink):
        await schwab.push_quote("SPY", 512.30, 512.34)
        await schwab.push_level_one("QQQ", f2=440.10)  # no bid known for QQQ yet
        await schwab.push_heartbeat()
        await until(lambda: len(sink.alive) >= 3)
    assert [q.symbol for q in sink.quotes] == ["SPY"]


async def test_no_quote_is_emitted_until_both_sides_are_known(schwab, client, signed_in):
    async with running(schwab, client, signed_in) as (_, sink):
        await schwab.push_level_one("SPY", f3=512.32)
        await schwab.push_level_one("SPY", f1=512.30)
        await schwab.push_level_one("SPY", f2=512.34)
        await until(lambda: sink.quotes)
    assert len(sink.quotes) == 1


async def test_delayed_data_is_flagged(schwab, client, signed_in):
    async with running(schwab, client, signed_in) as (_, sink):
        await schwab.push_quote("SPY", 512.30, 512.34, delayed=True)
        await until(lambda: sink.quotes)
    assert sink.quotes[0].delayed is True


async def test_a_minute_bar_reaches_the_sink(schwab, client, signed_in):
    async with running(schwab, client, signed_in) as (_, sink):
        await schwab.push_bar("SPY", 512.1, 512.9, 511.8, 512.5, 120345, start_ms=1760972400000)
        await until(lambda: sink.bars)
    (bar,) = sink.bars
    assert (bar.symbol, bar.open, bar.high, bar.low, bar.close, bar.volume) == (
        "SPY",
        Decimal("512.1"),
        Decimal("512.9"),
        Decimal("511.8"),
        Decimal("512.5"),
        120345,
    )
    assert bar.start == datetime.fromtimestamp(1760972400, UTC)


async def test_heartbeats_show_the_feed_is_alive(schwab, client, signed_in):
    async with running(schwab, client, signed_in) as (stream, sink):
        before = len(sink.alive)
        await schwab.push_heartbeat()
        await until(lambda: len(sink.alive) > before)
        assert stream.last_message_at is not None
    assert sink.quotes == []


async def test_stream_reconnects_and_resubscribes_after_a_drop(schwab, client, signed_in):
    async with running(schwab, client, signed_in) as (stream, sink):
        await schwab.drop_streams()
        await until(lambda: schwab.stream_logins == 2 and stream.connected)
        await schwab.push_quote("SPY", 512.30, 512.34)
        await until(lambda: sink.quotes)
    assert schwab.subscriptions["LEVELONE_EQUITIES"] == {"SPY", "QQQ"}


async def test_fields_from_before_a_reconnect_are_not_reused(schwab, client, signed_in):
    async with running(schwab, client, signed_in) as (stream, sink):
        await schwab.push_quote("SPY", 512.30, 512.34)
        await until(lambda: len(sink.quotes) == 1)
        await schwab.drop_streams()
        await until(lambda: schwab.stream_logins == 2 and stream.connected)
        await schwab.push_level_one("SPY", f2=530.00)  # ask only; the old bid is stale
        await schwab.push_heartbeat()
        await asyncio.sleep(0.05)
    assert len(sink.quotes) == 1


async def test_connected_is_false_while_disconnected(schwab, client, signed_in):
    async with running(schwab, client, signed_in) as (stream, _):
        schwab.stream_login_code = 3  # refuse the next login
        await schwab.drop_streams()
        await until(lambda: not stream.connected)
        await until(lambda: schwab.stream_logins >= 3)
        assert stream.connected is False
        schwab.stream_login_code = 0
        await until(lambda: stream.connected)


async def test_failed_subscription_is_not_reported_as_connected(schwab, client, signed_in):
    schwab.stream_subs_code = 21
    async with running(schwab, client, signed_in, connect=False) as (stream, _):
        await until(lambda: schwab.stream_logins >= 2)
        assert stream.connected is False


async def test_a_reply_to_some_other_request_is_not_taken_as_the_answer(schwab, client, signed_in):
    schwab.stream_stray_replies = True  # each one reports a failure, under another request id
    async with running(schwab, client, signed_in) as (stream, _):
        assert stream.connected is True
        assert schwab.stream_logins == 1


def subscriptions_sent(schwab) -> int:
    return sum(1 for request in schwab.stream_requests if request["command"] == "SUBS")


async def test_stream_is_not_connected_until_its_subscriptions_are_acknowledged(
    schwab, client, signed_in
):
    schwab.stream_silent_subs = True
    async with running(schwab, client, signed_in, connect=False) as (stream, _):
        await until(lambda: subscriptions_sent(schwab) >= 1)
        # Logged in and waiting for the answer: no data can be flowing yet.
        assert stream.connected is False


async def test_an_unanswered_subscription_is_given_up_on_and_the_stream_reconnects(
    schwab, client, signed_in, monkeypatch
):
    monkeypatch.setattr(SchwabStream, "REPLY_TIMEOUT_S", 0.05)
    schwab.stream_silent_subs = True
    async with running(schwab, client, signed_in, connect=False) as (stream, _):
        await until(lambda: schwab.stream_logins >= 3)
        assert stream.connected is False
        schwab.stream_silent_subs = False
        await until(lambda: stream.connected)


async def test_stream_waits_quietly_when_there_is_no_login(schwab, client):
    logged_out = TokenManager(
        store=MemoryTokenStore(),
        credentials=StaticCredentials(None),
        clock=SystemClock(),
        token_url=schwab.token_url,
    )
    async with aiohttp.ClientSession() as session:
        from traider.schwab.client import SchwabClient

        lonely = SchwabClient(session, logged_out, base_url=schwab.base_url)
        async with running(schwab, lonely, logged_out, connect=False) as (stream, _):
            await asyncio.sleep(0.1)
            assert stream.connected is False
    assert schwab.stream_logins == 0


async def test_garbage_and_unknown_messages_are_ignored(schwab, client, signed_in):
    async with running(schwab, client, signed_in) as (stream, sink):
        await schwab.push_raw("not json at all")
        await schwab.push_raw('{"data": [{"service": "NASDAQ_BOOK", "content": [{"key": "SPY"}]}]}')
        await schwab.push_raw('{"data": [{"service": "LEVELONE_EQUITIES", "content": "oops"}]}')
        await schwab.push_raw('{"data": "nope", "notify": 7}')
        await schwab.push_quote("SPY", 512.30, 512.34)
        await until(lambda: sink.quotes)
        assert stream.connected is True
    assert schwab.stream_logins == 1


async def test_stopping_the_stream_closes_the_socket(schwab, client, signed_in):
    async with running(schwab, client, signed_in):
        assert len(schwab.sockets) == 1
    await until(lambda: schwab.sockets == [])


async def test_refused_login_is_retried_with_a_fresh_access_token(schwab, client, signed_in):
    schwab.stream_login_code = 3
    async with running(schwab, client, signed_in, connect=False) as (stream, _):
        await until(lambda: schwab.stream_logins >= 2)
        schwab.stream_login_code = 0
        await until(lambda: stream.connected)
    used = [
        r["parameters"]["Authorization"] for r in schwab.stream_requests if r["command"] == "LOGIN"
    ]
    assert used[0] != used[1]
