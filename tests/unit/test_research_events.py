"""Finnhub through a fake server: parsing, the rate limit, errors, and keeping the key secret."""

import json
import logging
from datetime import UTC, date, datetime

import aiohttp
import boto3
import pytest
from moto import mock_aws

from tests.fakes.finnhub_server import API_KEY
from traider.research.events import (
    EarningsEvent,
    EventsUnavailable,
    FinnhubEvents,
    finnhub_key_from_secret,
    parse_earnings,
    parse_news,
    parse_profile,
)

TODAY = date(2026, 10, 9)


@pytest.fixture
async def events(finnhub):
    async with aiohttp.ClientSession() as session:
        yield FinnhubEvents(session, API_KEY, base_url=finnhub.base_url, backoff_s=0.01)


async def test_earnings_calendar_reads_dates_hours_and_estimates(events, finnhub):
    finnhub.earnings = [
        {
            "date": "2026-10-08",
            "hour": "amc",
            "symbol": "AMD",
            "epsEstimate": 0.9,
            "epsActual": 1.1,
            "quarter": 3,
            "year": 2026,
        },
        {"date": "2026-10-09", "hour": "bmo", "symbol": "jpm", "epsEstimate": None},
        {"date": "2026-10-14", "hour": "dmh", "symbol": "NFLX"},
        {"date": "not a date", "hour": "bmo", "symbol": "BAD"},
        {"hour": "bmo", "symbol": "NODATE"},
        "garbage",
    ]
    found = await events.earnings_calendar(date(2026, 10, 8), date(2026, 10, 23))
    assert found == [
        EarningsEvent(
            symbol="AMD", day=date(2026, 10, 8), hour="amc", eps_estimate=0.9, eps_actual=1.1
        ),
        EarningsEvent(symbol="JPM", day=date(2026, 10, 9), hour="bmo"),
        EarningsEvent(symbol="NFLX", day=date(2026, 10, 14), hour="unknown"),
    ]
    (request,) = finnhub.requests
    assert request["query"] == {"from": "2026-10-08", "to": "2026-10-23"}


async def test_news_is_newest_first_with_short_summaries(events, finnhub):
    finnhub.company_news["NVDA"] = [
        {"datetime": 1760000000, "source": "Wire", "headline": "older", "summary": "s"},
        {"datetime": 1760090000, "source": "Wire", "headline": "newer", "summary": "x" * 900},
        {"datetime": "bad", "headline": "skipped"},
    ]
    found = await events.company_news("NVDA", date(2026, 10, 6), TODAY)
    assert [n.headline for n in found] == ["newer", "older"]
    assert len(found[0].summary) == 300
    assert found[0].at == datetime.fromtimestamp(1760090000, UTC)
    assert finnhub.requests[0]["query"] == {
        "symbol": "NVDA",
        "from": "2026-10-06",
        "to": "2026-10-09",
    }


async def test_market_news_is_the_general_category_limited(events, finnhub):
    finnhub.general_news = [
        {"datetime": 1760000000 + i, "headline": f"h{i}", "source": "s"} for i in range(40)
    ]
    found = await events.market_news(30)
    assert len(found) == 30 and found[0].headline == "h39"
    assert finnhub.requests[0]["query"] == {"category": "general"}


async def test_profile_gives_industry_and_market_cap(events, finnhub):
    finnhub.profiles["NVDA"] = {
        "name": "NVIDIA Corp",
        "finnhubIndustry": "Semiconductors",
        "marketCapitalization": 2_500_000.5,
        "ticker": "NVDA",
    }
    profile = await events.profile("NVDA")
    assert (profile.industry, profile.market_cap_m, profile.name) == (
        "Semiconductors",
        2_500_000.5,
        "NVIDIA Corp",
    )
    assert await events.profile("UNKNOWN") is None


async def test_the_key_goes_in_a_header_never_in_the_address(events, finnhub):
    await events.market_news(1)
    (request,) = finnhub.requests
    assert request["headers"]["X-Finnhub-Token"] == API_KEY
    assert "token" not in request["query"]
    assert API_KEY not in json.dumps(request["query"]) + request["path"]


async def test_a_refused_key_is_reported_without_the_key(finnhub):
    async with aiohttp.ClientSession() as session:
        wrong = FinnhubEvents(session, "wrongkey0123456789xyz", base_url=finnhub.base_url)
        with pytest.raises(EventsUnavailable, match="refused") as caught:
            await wrong.market_news(5)
    assert "wrongkey0123456789xyz" not in str(caught.value)
    assert "wrongkey" not in repr(wrong)


@pytest.mark.parametrize("status", [429, 500, "drop"])
async def test_a_temporary_failure_is_retried_once(events, finnhub, status):
    finnhub.fail("/news", status, times=1)
    assert await events.market_news(5) == []
    assert len(finnhub.requests) == 2


async def test_a_lasting_failure_is_events_unavailable(events, finnhub):
    finnhub.fail("/calendar/earnings", 503, times=5)
    with pytest.raises(EventsUnavailable, match="HTTP 503"):
        await events.earnings_calendar(TODAY, TODAY)
    assert len(finnhub.requests) == 2


async def test_a_bad_request_is_not_retried(events, finnhub):
    finnhub.fail("/stock/profile2", 422, times=5)
    with pytest.raises(EventsUnavailable, match="HTTP 422"):
        await events.profile("NVDA")
    assert len(finnhub.requests) == 1


async def test_an_unexpected_reply_shape_is_events_unavailable(events, finnhub):
    finnhub.general_news = {"not": "a list"}
    with pytest.raises(EventsUnavailable, match="unexpected"):
        await events.market_news(5)


@pytest.mark.parametrize(
    ("parse", "body"),
    [
        (parse_earnings, {"nope": 1}),
        (parse_earnings, []),
        (parse_news, {"not": "a list"}),
        (lambda raw: parse_profile(raw, "NVDA"), ["x"]),
    ],
)
def test_every_parser_refuses_a_reply_of_the_wrong_shape(parse, body):
    with pytest.raises(EventsUnavailable, match="unexpected"):
        parse(body)


async def test_calls_stay_under_the_free_tier_rate(finnhub):
    clock = {"now": 0.0}
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        clock["now"] += seconds

    async with aiohttp.ClientSession() as session:
        events = FinnhubEvents(
            session,
            API_KEY,
            base_url=finnhub.base_url,
            max_per_minute=2,
            monotonic=lambda: clock["now"],
            sleep=sleep,
        )
        for _ in range(3):
            await events.market_news(1)
    assert waits == [60.0]


def test_an_empty_key_is_refused_up_front():
    with pytest.raises(EventsUnavailable, match="no Finnhub API key"):
        FinnhubEvents(None, "  ")  # type: ignore[arg-type]


async def test_the_key_never_reaches_the_log(events, finnhub, caplog):
    caplog.set_level(logging.DEBUG)
    finnhub.fail("/news", 500, times=5)
    with pytest.raises(EventsUnavailable):
        await events.market_news(5)
    assert API_KEY not in caplog.text


# --- the key in Secrets Manager ---------------------------------------------------------


@pytest.fixture
def secrets():
    with mock_aws():
        yield boto3.client("secretsmanager")


def test_the_key_is_read_from_its_secret(secrets):
    arn = secrets.create_secret(Name="finnhub", SecretString='{"api_key": " abc123 "}')["ARN"]
    assert finnhub_key_from_secret(secrets, arn) == "abc123"


def test_an_empty_secret_says_to_store_the_key(secrets):
    arn = secrets.create_secret(Name="finnhub")["ARN"]
    with pytest.raises(EventsUnavailable, match="no value yet"):
        finnhub_key_from_secret(secrets, arn)


@pytest.mark.parametrize(
    "value", ["plain-key-not-json-0123456789", '{"key": "x"}', '{"api_key": ""}', "[1]"]
)
def test_a_malformed_secret_is_refused_without_echoing_it(secrets, value):
    arn = secrets.create_secret(Name="finnhub", SecretString=value)["ARN"]
    with pytest.raises(EventsUnavailable, match="api_key") as caught:
        finnhub_key_from_secret(secrets, arn)
    assert "plain-key-not-json" not in str(caught.value)


def test_a_missing_secret_is_events_unavailable(secrets):
    with pytest.raises(EventsUnavailable, match="no value yet"):
        finnhub_key_from_secret(secrets, "does-not-exist")


# --- defensive parsing: skip what is malformed, never guess --------------------------------


@pytest.mark.parametrize("body", [None, "text", 3, [], [{"earningsCalendar": []}]])
def test_earnings_refuses_any_reply_that_is_not_an_object_with_a_list(body):
    with pytest.raises(EventsUnavailable, match="unexpected"):
        parse_earnings(body)


@pytest.mark.parametrize("body", [None, "text", 3, {}, {"a": 1}])
def test_news_refuses_a_reply_that_is_not_a_list(body):
    with pytest.raises(EventsUnavailable, match="unexpected"):
        parse_news(body)


@pytest.mark.parametrize("body", [None, "text", 3, []])
def test_profile_refuses_a_reply_that_is_not_an_object(body):
    with pytest.raises(EventsUnavailable, match="unexpected"):
        parse_profile(body, "NVDA")


def test_earnings_rows_that_are_malformed_are_skipped_not_repaired():
    rows = [
        {"date": "2026-10-09", "symbol": 7},
        {"date": "2026-10-09", "symbol": "  "},
        {"date": "2026-10-09", "symbol": ""},
        {"date": 20261009, "symbol": "AAA"},
        {"date": "2026-13-45", "symbol": "BBB"},
        {"date": "2026-10-09", "symbol": " ccc ", "hour": "BMO", "epsEstimate": "nan"},
        {"date": "2026-10-09", "symbol": "DDD", "epsEstimate": True, "epsActual": float("inf")},
    ]
    found = parse_earnings({"earningsCalendar": rows})
    assert found == [
        EarningsEvent(symbol="CCC", day=date(2026, 10, 9)),
        EarningsEvent(symbol="DDD", day=date(2026, 10, 9)),
    ]


def test_news_rows_that_are_malformed_are_skipped_not_repaired():
    good = {"datetime": 1760000000, "headline": "ok", "source": "s"}
    rows = [
        good,
        {"datetime": 0, "headline": "epoch"},
        {"datetime": -5, "headline": "before"},
        {"datetime": True, "headline": "bool"},
        {"datetime": float("nan"), "headline": "nan"},
        {"datetime": float("inf"), "headline": "inf"},
        {"datetime": 1e30, "headline": "huge"},
        {"datetime": 1760000001, "headline": "   "},
        {"datetime": 1760000002},
        {"datetime": 1760000003, "headline": 12},
        None,
        "x",
    ]
    assert [n.headline for n in parse_news(rows)] == ["ok"]


def test_a_profile_with_nothing_usable_in_it_is_none():
    assert (
        parse_profile({"name": 3, "finnhubIndustry": [], "marketCapitalization": "x"}, "X") is None
    )
    assert parse_profile({"ticker": "X"}, "X") is None


@pytest.mark.parametrize("cap", [0, -1, float("nan"), float("inf"), True, "big", None])
def test_a_market_cap_that_is_not_a_positive_number_is_dropped(cap):
    profile = parse_profile({"name": "Acme", "marketCapitalization": cap}, "ACME")
    assert profile is not None and profile.market_cap_m is None and profile.name == "Acme"


# --- the key stays secret ------------------------------------------------------------------


@pytest.mark.parametrize("key", ["bad\nkey0123456789", "bad key 0123456789", "clé0123456789abcd"])
def test_a_key_that_cannot_be_a_header_value_is_refused_without_echoing_it(key):
    with pytest.raises(EventsUnavailable, match="invalid") as caught:
        FinnhubEvents(None, key)  # type: ignore[arg-type]
    assert key.strip() not in str(caught.value) and "key0123456789" not in repr(caught.value)


async def test_the_key_is_in_no_error_and_no_repr(events, finnhub):
    for status in (401, 403, 422, 503, "drop"):
        finnhub.faults.clear()
        finnhub.fail("/news", status, times=5)
        with pytest.raises(EventsUnavailable) as caught:
            await events.market_news(5)
        assert API_KEY not in str(caught.value) + repr(caught.value) + repr(caught.value.args)
        assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert API_KEY not in repr(events) and API_KEY not in str(events)
