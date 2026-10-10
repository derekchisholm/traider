"""Events data for research: earnings calendar, company and market news, company profile.

Behind the ``EventsData`` protocol so the vendor can change. ``FinnhubEvents`` uses
Finnhub's free tier. Its key travels in a header, never in a URL, and never appears in a
log line, an error or an alert. Every failure is an ``EventsUnavailable``: the run goes on
without that data and is marked partial.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol

import aiohttp
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict

from traider.schwab.client import RateLimiter

FINNHUB_BASE = "https://finnhub.io/api/v1"
FINNHUB_MAX_PER_MINUTE = 55  # the free tier allows about 60
SUMMARY_MAX_CHARS = 300

EarningsHour = Literal["bmo", "amc", "unknown"]


class EventsUnavailable(Exception):
    """The events vendor could not answer. The message never contains the key."""


class EarningsEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    day: date
    hour: EarningsHour = "unknown"  # bmo: before the open, amc: after the close
    eps_estimate: float | None = None
    eps_actual: float | None = None


class NewsItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    at: datetime
    source: str = ""
    headline: str = ""
    summary: str = ""


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    name: str | None = None
    industry: str | None = None
    market_cap_m: float | None = None  # millions of dollars


class EventsData(Protocol):
    async def earnings_calendar(
        self, start: date, end: date, symbol: str | None = None
    ) -> list[EarningsEvent]:
        """Earnings dates in ``[start, end]``: every company's, or only ``symbol``'s."""
        ...

    async def company_news(self, symbol: str, start: date, end: date) -> list[NewsItem]: ...

    async def market_news(self, limit: int) -> list[NewsItem]: ...

    async def profile(self, symbol: str) -> Profile | None: ...


# ------------------------------------------------------------------------ parsing


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: Any, limit: int | None = None) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text[:limit] if limit is not None else text


def _positive(value: Any) -> float | None:
    number = _float(value)
    return number if number is not None and number > 0 else None


def _hour(value: Any) -> EarningsHour:
    hour = value.strip().lower() if isinstance(value, str) else ""
    return "bmo" if hour == "bmo" else "amc" if hour == "amc" else "unknown"


def parse_earnings(raw: Any) -> list[EarningsEvent]:
    rows = raw.get("earningsCalendar") if isinstance(raw, Mapping) else None
    if not isinstance(rows, list):
        raise EventsUnavailable("finnhub earnings calendar: unexpected reply")
    events: list[EarningsEvent] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        symbol, day = row.get("symbol"), row.get("date")
        if not isinstance(symbol, str) or not symbol.strip() or not isinstance(day, str):
            continue
        try:
            parsed = date.fromisoformat(day)
        except ValueError:
            continue
        events.append(
            EarningsEvent(
                symbol=symbol.strip().upper(),
                day=parsed,
                hour=_hour(row.get("hour")),
                eps_estimate=_float(row.get("epsEstimate")),
                eps_actual=_float(row.get("epsActual")),
            )
        )
    return sorted(events, key=lambda e: (e.day, e.symbol))


def parse_news(raw: Any) -> list[NewsItem]:
    if not isinstance(raw, list):
        raise EventsUnavailable("finnhub news: unexpected reply")
    items: list[NewsItem] = []
    for row in raw:
        if not isinstance(row, Mapping):
            continue
        stamp = row.get("datetime")
        if not isinstance(stamp, int | float) or isinstance(stamp, bool) or not stamp > 0:
            continue
        try:
            at = datetime.fromtimestamp(stamp, UTC)
        except (OverflowError, OSError, ValueError):
            continue
        headline = _text(row.get("headline"), 300)
        if not headline:
            continue  # an item with no headline says nothing
        items.append(
            NewsItem(
                at=at,
                source=_text(row.get("source"), 100),
                headline=headline,
                summary=_text(row.get("summary"), SUMMARY_MAX_CHARS),
            )
        )
    return sorted(items, key=lambda n: n.at, reverse=True)


def parse_profile(raw: Any, symbol: str) -> Profile | None:
    if not isinstance(raw, Mapping):
        raise EventsUnavailable("finnhub profile: unexpected reply")
    if not raw:
        return None  # Finnhub answers {} for a symbol it does not cover
    name = _text(raw.get("name"), 200) or None
    industry = _text(raw.get("finnhubIndustry"), 100) or None
    market_cap_m = _positive(raw.get("marketCapitalization"))
    if name is None and industry is None and market_cap_m is None:
        return None  # nothing usable in it
    return Profile(symbol=symbol, name=name, industry=industry, market_cap_m=market_cap_m)


# ------------------------------------------------------------------------- client


class FinnhubEvents:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        *,
        base_url: str = FINNHUB_BASE,
        timeout_s: float = 10.0,
        max_per_minute: int = FINNHUB_MAX_PER_MINUTE,
        retries: int = 1,
        backoff_s: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        key = api_key.strip()
        if not key:
            raise EventsUnavailable("no Finnhub API key")
        if not (key.isascii() and key.isprintable() and " " not in key):
            # Never echoed: a bad key must not reach an error, a log or an alert.
            raise EventsUnavailable("the Finnhub API key has invalid characters")
        self._session = session
        self._key = key
        self._base = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._retries = retries
        self._backoff_s = backoff_s
        self._sleep = sleep
        self._limiter = RateLimiter(max_per_minute, monotonic=monotonic, sleep=sleep)

    def __repr__(self) -> str:  # the key stays out of logs and tracebacks
        return f"FinnhubEvents(base_url={self._base!r})"

    async def earnings_calendar(
        self, start: date, end: date, symbol: str | None = None
    ) -> list[EarningsEvent]:
        params = {"from": start.isoformat(), "to": end.isoformat()}
        if symbol is not None:
            params["symbol"] = symbol
        return parse_earnings(await self._get("/calendar/earnings", params))

    async def company_news(self, symbol: str, start: date, end: date) -> list[NewsItem]:
        raw = await self._get(
            "/company-news",
            {"symbol": symbol, "from": start.isoformat(), "to": end.isoformat()},
        )
        return parse_news(raw)

    async def market_news(self, limit: int) -> list[NewsItem]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        return parse_news(await self._get("/news", {"category": "general"}))[:limit]

    async def profile(self, symbol: str) -> Profile | None:
        return parse_profile(await self._get("/stock/profile2", {"symbol": symbol}), symbol)

    async def _get(self, path: str, params: Mapping[str, str]) -> Any:
        failure = EventsUnavailable(f"finnhub {path}: no attempt made")
        for attempt in range(self._retries + 1):
            if attempt:
                await self._sleep(self._backoff_s * 2 ** (attempt - 1))
            await self._limiter.acquire()
            try:
                async with self._session.get(
                    self._base + path,
                    params=params,
                    headers={"X-Finnhub-Token": self._key, "Accept": "application/json"},
                    timeout=self._timeout,
                    allow_redirects=False,
                ) as response:
                    status = response.status
                    body = await response.read()
            except (aiohttp.ClientError, TimeoutError) as exc:
                failure = EventsUnavailable(f"finnhub {path}: {type(exc).__name__}")
                continue
            if status == 200:
                try:
                    return json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, LookupError, ValueError):
                    pass  # raised below, outside this block, so nothing is chained
                raise EventsUnavailable(f"finnhub {path}: reply was not JSON")
            if status in (401, 403):
                raise EventsUnavailable(f"finnhub {path}: the API key was refused (HTTP {status})")
            failure = EventsUnavailable(f"finnhub {path}: HTTP {status}")
            if status != 429 and status < 500:
                raise failure
        raise failure


def finnhub_key_from_secret(client: Any, secret_id: str) -> str:
    """The key from a secret holding ``{"api_key": "..."}``. Errors never echo the value."""
    code: str | None = None
    try:
        response = client.get_secret_value(SecretId=secret_id)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
    except BotoCoreError as exc:
        code = type(exc).__name__
    # Raised outside the except blocks so no cause or context is chained.
    if code == "ResourceNotFoundException":
        raise EventsUnavailable(
            "the Finnhub secret has no value yet: store the key (see docs/runbook.md)"
        )
    if code is not None:
        raise EventsUnavailable(f"cannot read the Finnhub secret: {code}")
    malformed = False
    try:
        key = json.loads(response["SecretString"])["api_key"].strip()
        if not key:
            raise ValueError("empty")
    except (KeyError, TypeError, ValueError, AttributeError):
        malformed = True  # the JSONDecodeError holds the secret text, so it is not chained
    if malformed:
        raise EventsUnavailable('the Finnhub secret must be JSON like {"api_key": "..."}')
    return str(key)
