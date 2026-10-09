from datetime import UTC, date, datetime

import aiohttp
import pytest

from tests.fakes.schwab_server import ACCOUNT_HASH, ACCOUNT_NUMBER
from traider.schwab.client import (
    RateLimiter,
    SchwabClient,
    SchwabError,
    SchwabRejected,
    SchwabUnavailable,
)
from traider.schwab.tokens import MemoryTokenStore, StaticCredentials, TokenManager
from traider.timeutil import SystemClock

ORDER = {
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


# --- reads --------------------------------------------------------------------------


async def test_account_numbers_are_listed_with_their_hashes(client):
    (account,) = await client.account_numbers()
    assert (account.number, account.hash) == (ACCOUNT_NUMBER, ACCOUNT_HASH)


async def test_requests_carry_the_bearer_token(client, schwab):
    await client.account_numbers()
    (request,) = schwab.calls("GET", "/accountNumbers")
    token = request["headers"]["Authorization"].removeprefix("Bearer ")
    assert token in schwab.access_tokens


async def test_account_is_requested_with_positions(client, schwab):
    raw = await client.account(ACCOUNT_HASH)
    assert "securitiesAccount" in raw
    (request,) = schwab.calls("GET", f"/accounts/{ACCOUNT_HASH}")
    assert request["query"] == {"fields": "positions"}


async def test_order_search_uses_schwabs_timestamp_format(client, schwab):
    start = datetime(2026, 10, 8, 4, 0, tzinfo=UTC)
    end = datetime(2026, 10, 8, 20, 30, 15, 250000, tzinfo=UTC)
    assert await client.orders(ACCOUNT_HASH, start, end) == []
    (request,) = schwab.calls("GET", "/orders")
    assert request["query"] == {
        "fromEnteredTime": "2026-10-08T04:00:00.000Z",
        "toEnteredTime": "2026-10-08T20:30:15.250Z",
    }


async def test_quotes_are_requested_for_all_symbols_at_once(client, schwab):
    schwab.set_quote("SPY", 100.0, 100.02)
    schwab.set_quote("QQQ", 400.0, 400.05)
    raw = await client.quotes(["SPY", "QQQ"])
    assert set(raw) == {"SPY", "QQQ"}
    (request,) = schwab.calls("GET", "/marketdata/v1/quotes")
    assert request["query"] == {"symbols": "SPY,QQQ", "fields": "quote", "indicative": "false"}


async def test_minute_history_request(client, schwab):
    start = datetime(2026, 10, 8, 13, 30, tzinfo=UTC)
    end = datetime(2026, 10, 8, 14, 0, tzinfo=UTC)
    await client.price_history("SPY", start, end)
    (request,) = schwab.calls("GET", "/pricehistory")
    assert request["query"] == {
        "symbol": "SPY",
        "periodType": "day",
        "frequencyType": "minute",
        "frequency": "1",
        "startDate": str(int(start.timestamp() * 1000)),
        "endDate": str(int(end.timestamp() * 1000)),
        "needExtendedHoursData": "false",
        "needPreviousClose": "false",
    }


async def test_market_hours_request(client, schwab):
    raw = await client.market_hours(date(2026, 10, 8))
    assert raw["equity"]["EQ"]["isOpen"] is True
    (request,) = schwab.calls("GET", "/markets")
    assert request["query"] == {"markets": "equity", "date": "2026-10-08"}


async def test_streamer_details_come_from_user_preferences(client):
    raw = await client.user_preference()
    assert raw["streamerInfo"][0]["schwabClientChannel"] == "N9"


# --- placing and cancelling ---------------------------------------------------------------


async def test_placing_an_order_returns_its_id(client, schwab):
    order_id = await client.place_order(ACCOUNT_HASH, ORDER)
    assert int(order_id) in schwab.orders


async def test_order_payload_is_sent_as_json_unchanged(client, schwab):
    await client.place_order(ACCOUNT_HASH, ORDER)
    (request,) = schwab.calls("POST", "/orders")
    assert request["json"] == ORDER


async def test_order_accepted_without_a_location_header_returns_no_id(client, schwab):
    schwab.omit_location_header = True
    assert await client.place_order(ACCOUNT_HASH, ORDER) is None
    assert len(schwab.orders) == 1


async def test_rejected_order_carries_schwabs_reason(client, schwab):
    schwab.reject_orders_with = "Your limit price is significantly away from the market"
    with pytest.raises(SchwabRejected, match="significantly away") as caught:
        await client.place_order(ACCOUNT_HASH, ORDER)
    assert caught.value.status == 400
    assert len(schwab.calls("POST", "/orders")) == 1


async def test_order_is_never_resent_after_a_server_error(client, schwab):
    schwab.fail("POST", "/orders", 500, times=5)
    with pytest.raises(SchwabUnavailable) as caught:
        await client.place_order(ACCOUNT_HASH, ORDER)
    assert caught.value.sent is True
    assert len(schwab.calls("POST", "/orders")) == 1


async def test_order_is_never_resent_after_a_lost_reply(client, schwab):
    schwab.fail("POST", "/orders", "drop_after")
    with pytest.raises(SchwabUnavailable) as caught:
        await client.place_order(ACCOUNT_HASH, ORDER)
    assert caught.value.sent is True
    assert len(schwab.calls("POST", "/orders")) == 1
    assert len(schwab.orders) == 1  # it did go through; only the reply was lost


async def test_order_timeout_counts_as_possibly_sent(schwab, signed_in):
    schwab.fail("POST", "/orders", "delay", body=1.0)
    async with aiohttp.ClientSession() as session:
        slow = SchwabClient(session, signed_in, base_url=schwab.base_url, timeout_s=0.2)
        with pytest.raises(SchwabUnavailable) as caught:
            await slow.place_order(ACCOUNT_HASH, ORDER)
    assert caught.value.sent is True


async def test_order_that_could_not_connect_is_known_not_sent(signed_in, schwab):
    await signed_in.access_token()  # sign-in works; only the API host is down
    async with aiohttp.ClientSession() as session:
        dead = SchwabClient(session, signed_in, base_url="http://127.0.0.1:9")
        with pytest.raises(SchwabUnavailable) as caught:
            await dead.place_order(ACCOUNT_HASH, ORDER)
    assert caught.value.sent is False


async def test_order_without_a_login_is_known_not_sent(schwab):
    tokens = TokenManager(
        store=MemoryTokenStore(),
        credentials=StaticCredentials(None),
        clock=SystemClock(),
        token_url=schwab.token_url,
    )
    async with aiohttp.ClientSession() as session:
        logged_out = SchwabClient(session, tokens, base_url=schwab.base_url)
        with pytest.raises(SchwabUnavailable) as caught:
            await logged_out.place_order(ACCOUNT_HASH, ORDER)
    assert caught.value.sent is False
    assert schwab.calls("POST", "/orders") == []


async def test_rate_limited_order_is_known_not_placed(client, schwab):
    schwab.fail("POST", "/orders", 429)
    with pytest.raises(SchwabUnavailable) as caught:
        await client.place_order(ACCOUNT_HASH, ORDER)
    assert (caught.value.sent, caught.value.status) == (False, 429)
    assert len(schwab.calls("POST", "/orders")) == 1


async def test_order_status_is_fetched_by_id(client, schwab):
    order_id = await client.place_order(ACCOUNT_HASH, ORDER)
    raw = await client.order(ACCOUNT_HASH, order_id)
    assert (raw["orderId"], raw["status"]) == (int(order_id), "FILLED")


async def test_working_order_can_be_cancelled(client, schwab):
    schwab.fill_on_place = False
    order_id = await client.place_order(ACCOUNT_HASH, ORDER)
    await client.cancel_order(ACCOUNT_HASH, order_id)
    assert schwab.orders[int(order_id)].status == "CANCELED"


async def test_cancelling_a_finished_order_is_reported_as_a_rejection(client, schwab):
    order_id = await client.place_order(ACCOUNT_HASH, ORDER)
    with pytest.raises(SchwabRejected):
        await client.cancel_order(ACCOUNT_HASH, order_id)


# --- retries and failures -------------------------------------------------------------------


async def test_reads_are_retried_after_a_server_error(client, schwab):
    schwab.fail("GET", "/accountNumbers", 503, times=2)
    assert len(await client.account_numbers()) == 1
    assert len(schwab.calls("GET", "/accountNumbers")) == 3


async def test_reads_are_retried_after_being_rate_limited(client, schwab):
    schwab.fail("GET", "/accountNumbers", 429)
    assert len(await client.account_numbers()) == 1


async def test_reads_are_retried_after_a_dropped_connection(client, schwab):
    schwab.fail("GET", "/accountNumbers", "drop")
    assert len(await client.account_numbers()) == 1


async def test_reads_give_up_after_a_few_attempts(client, schwab):
    schwab.fail("GET", "/accountNumbers", 503, times=50)
    with pytest.raises(SchwabUnavailable) as caught:
        await client.account_numbers()
    assert caught.value.status == 503
    assert len(schwab.calls("GET", "/accountNumbers")) == 3


async def test_read_timeout_is_reported_as_unavailable(schwab, signed_in):
    schwab.fail("GET", "/accountNumbers", "delay", times=10, body=1.0)
    async with aiohttp.ClientSession() as session:
        slow = SchwabClient(
            session, signed_in, base_url=schwab.base_url, timeout_s=0.1, backoff_s=0.01
        )
        with pytest.raises(SchwabUnavailable):
            await slow.account_numbers()


async def test_bad_requests_are_not_retried(client, schwab):
    schwab.fail("GET", "/accountNumbers", 400, times=5)
    with pytest.raises(SchwabRejected):
        await client.account_numbers()
    assert len(schwab.calls("GET", "/accountNumbers")) == 1


async def test_expired_access_token_is_refreshed_and_the_request_repeated(client, schwab):
    await client.account_numbers()
    schwab.expire_access_tokens()  # Schwab voids the token early
    assert len(await client.account_numbers()) == 1
    assert len(schwab.calls("POST", "/v1/oauth/token")) == 2


async def test_order_after_an_expired_token_is_placed_exactly_once(client, schwab):
    await client.account_numbers()
    schwab.expire_access_tokens()
    await client.place_order(ACCOUNT_HASH, ORDER)
    assert len(schwab.orders) == 1


async def test_persistent_401_is_reported_instead_of_looping(client, schwab):
    schwab.fail("GET", "/accountNumbers", 401, times=10)
    with pytest.raises(SchwabUnavailable) as caught:
        await client.account_numbers()
    assert (caught.value.status, caught.value.sent) == (401, False)
    assert len(schwab.calls("GET", "/accountNumbers")) == 2


async def test_non_json_reply_is_an_error(client, schwab):
    schwab.fail("GET", "/accountNumbers", 200, body="<html>maintenance</html>")
    with pytest.raises(SchwabError):
        await client.account_numbers()


async def test_errors_do_not_leak_the_access_token(client, schwab, signed_in):
    token = await signed_in.access_token()
    schwab.fail("GET", "/accountNumbers", 400)
    with pytest.raises(SchwabRejected) as caught:
        await client.account_numbers()
    assert token not in str(caught.value)


# --- rate limiting --------------------------------------------------------------------------------


class FakeTime:
    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


async def test_limiter_lets_calls_through_under_the_limit():
    clock = FakeTime()
    limiter = RateLimiter(3, 60.0, monotonic=clock.monotonic, sleep=clock.sleep)
    for _ in range(3):
        await limiter.acquire()
    assert clock.slept == []


async def test_limiter_waits_for_the_window_to_free_up():
    clock = FakeTime()
    limiter = RateLimiter(3, 60.0, monotonic=clock.monotonic, sleep=clock.sleep)
    for _ in range(3):
        await limiter.acquire()
        clock.now += 10
    await limiter.acquire()  # 30s in: the first call leaves the window at 60s
    assert clock.slept == [pytest.approx(30.0)]


async def test_limiter_never_exceeds_the_limit_in_any_window():
    clock = FakeTime()
    limiter = RateLimiter(5, 60.0, monotonic=clock.monotonic, sleep=clock.sleep)
    stamps = []
    for _ in range(23):
        await limiter.acquire()
        stamps.append(clock.now)
    for i, start in enumerate(stamps):
        assert sum(1 for t in stamps[i:] if t < start + 60.0) <= 5


async def test_client_requests_go_through_the_limiter(schwab, signed_in):
    clock = FakeTime()
    async with aiohttp.ClientSession() as session:
        limited = SchwabClient(
            session,
            signed_in,
            base_url=schwab.base_url,
            max_per_minute=2,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        for _ in range(3):
            await limited.account_numbers()
    assert clock.slept == [pytest.approx(60.0)]
