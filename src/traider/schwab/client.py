"""A small async client for the parts of the Schwab Trader API the bot uses.

Endpoints and parameters follow schwab-py and schwabdev. Two rules matter more
than the rest:

* **An order is sent once.** A read can be retried freely; a ``POST`` that
  places an order never is. When its outcome is unknown the error says so
  (``sent=True``) and the caller has to find out what happened.
* **The token never leaves the request.** Redirects are refused and error
  messages are built from Schwab's own error text, not from the request.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import aiohttp

from traider.schwab.tokens import AuthUnavailable, TokenManager

API_BASE = "https://api.schwabapi.com"


class SchwabError(Exception):
    def __init__(self, message: str, *, status: int | None = None, sent: bool = True) -> None:
        super().__init__(message)
        self.status = status
        # False only when the request is known not to have been acted on.
        self.sent = sent


class SchwabRejected(SchwabError):
    """Schwab understood the request and refused it (HTTP 4xx)."""


class SchwabUnavailable(SchwabError):
    """No usable answer: no login, network trouble, a timeout, a 5xx or rate limiting."""


@dataclass(frozen=True, slots=True)
class AccountNumber:
    number: str
    hash: str


class RateLimiter:
    """At most ``max_calls`` in any ``per_s`` window. Schwab allows 120 requests a minute."""

    def __init__(
        self,
        max_calls: int,
        per_s: float = 60.0,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._max = max_calls
        self._per_s = per_s
        self._monotonic = monotonic
        self._sleep = sleep
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._monotonic()
                while self._stamps and self._stamps[0] <= now - self._per_s:
                    self._stamps.popleft()
                if len(self._stamps) < self._max:
                    self._stamps.append(now)
                    return
                await self._sleep(self._stamps[0] + self._per_s - now)


class SchwabClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        tokens: TokenManager,
        *,
        base_url: str = API_BASE,
        timeout_s: float = 10.0,
        max_per_minute: int = 100,
        retries: int = 2,
        backoff_s: float = 0.5,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._session = session
        self._tokens = tokens
        self._base = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._retries = retries
        self._backoff_s = backoff_s
        self._sleep = sleep
        self._limiter = RateLimiter(max_per_minute, monotonic=monotonic, sleep=sleep)

    # ---------------------------------------------------------------- accounts

    async def account_numbers(self) -> list[AccountNumber]:
        data = await self._get("/trader/v1/accounts/accountNumbers")
        if not isinstance(data, list):
            raise SchwabError("accountNumbers: unexpected response shape")
        try:
            return [AccountNumber(str(a["accountNumber"]), str(a["hashValue"])) for a in data]
        except (KeyError, TypeError):
            raise SchwabError("accountNumbers: unexpected response shape") from None

    async def account(self, account_hash: str) -> Any:
        return await self._get(f"/trader/v1/accounts/{account_hash}", {"fields": "positions"})

    async def user_preference(self) -> Any:
        return await self._get("/trader/v1/userPreference")

    # ------------------------------------------------------------------ orders

    async def orders(self, account_hash: str, start: datetime, end: datetime) -> list[Any]:
        data = await self._get(
            f"/trader/v1/accounts/{account_hash}/orders",
            {"fromEnteredTime": _schwab_time(start), "toEnteredTime": _schwab_time(end)},
        )
        if data is None:
            return []
        if not isinstance(data, list):
            raise SchwabError("orders: unexpected response shape")
        return data

    async def order(self, account_hash: str, order_id: str) -> Any:
        return await self._get(f"/trader/v1/accounts/{account_hash}/orders/{order_id}")

    async def place_order(self, account_hash: str, order: Mapping[str, Any]) -> str | None:
        """Send an order, once. Returns the order id, or None if Schwab accepted it
        without telling us the id (it does that for orders that fill instantly)."""
        _, headers, _ = await self._request(
            "POST", f"/trader/v1/accounts/{account_hash}/orders", json_body=order, idempotent=False
        )
        location = {name.lower(): value for name, value in headers.items()}.get("location", "")
        order_id = location.rstrip("/").rsplit("/", 1)[-1]
        return order_id if order_id.isdigit() else None

    async def cancel_order(self, account_hash: str, order_id: str) -> None:
        await self._request(
            "DELETE", f"/trader/v1/accounts/{account_hash}/orders/{order_id}", idempotent=True
        )

    # ------------------------------------------------------------- market data

    async def quotes(self, symbols: Sequence[str]) -> Any:
        return await self._get(
            "/marketdata/v1/quotes",
            {"symbols": ",".join(symbols), "fields": "quote", "indicative": "false"},
        )

    async def price_history(self, symbol: str, start: datetime, end: datetime) -> Any:
        """One-minute candles for the regular session between two times."""
        return await self._get(
            "/marketdata/v1/pricehistory",
            {
                "symbol": symbol,
                "periodType": "day",
                "frequencyType": "minute",
                "frequency": "1",
                "startDate": str(int(start.timestamp() * 1000)),
                "endDate": str(int(end.timestamp() * 1000)),
                "needExtendedHoursData": "false",
                "needPreviousClose": "false",
            },
        )

    async def market_hours(self, day: date) -> Any:
        return await self._get(
            "/marketdata/v1/markets", {"markets": "equity", "date": day.isoformat()}
        )

    # ---------------------------------------------------------------- plumbing

    async def _get(self, path: str, params: Mapping[str, str] | None = None) -> Any:
        _, _, text = await self._request("GET", path, params=params, idempotent=True)
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except ValueError:
            raise SchwabError(f"GET {_safe(path)}: reply was not JSON") from None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        idempotent: bool,
    ) -> tuple[int, Mapping[str, str], str]:
        what = f"{method} {_safe(path)}"
        attempts = self._retries + 1 if idempotent else 1
        attempt = 0
        reauthorised = False
        failure: SchwabUnavailable | None = None
        while attempt < attempts:
            attempt += 1
            try:
                token = await self._tokens.access_token()
            except AuthUnavailable as exc:
                raise SchwabUnavailable(f"{what}: no Schwab login ({exc})", sent=False) from None
            await self._limiter.acquire()
            try:
                async with self._session.request(
                    method,
                    self._base + path,
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    timeout=self._timeout,
                    allow_redirects=False,
                ) as response:
                    status = response.status
                    headers = dict(response.headers)
                    text = await response.text()
            except aiohttp.ClientConnectorError as exc:
                # The connection was never made, so nothing reached Schwab.
                failure = SchwabUnavailable(
                    f"{what}: cannot connect ({type(exc).__name__})", sent=False
                )
            except (aiohttp.ClientError, TimeoutError) as exc:
                # Sent, or possibly sent, with no usable reply.
                failure = SchwabUnavailable(f"{what}: {type(exc).__name__}", sent=True)
            else:
                if status == 401:
                    # Refused for its token. Reads get a new token and one more try. An
                    # order is never sent twice from here: the caller is told it was not
                    # sent and decides again with fresh information.
                    await self._tokens.invalidate(token)
                    if idempotent and not reauthorised:
                        reauthorised = True
                        attempt -= 1
                        continue
                    raise SchwabUnavailable(
                        f"{what}: HTTP 401: {_reason(text)}", status=401, sent=False
                    )
                if status == 429:
                    failure = SchwabUnavailable(
                        f"{what}: rate limited (HTTP 429)", status=429, sent=False
                    )
                elif status >= 500:
                    failure = SchwabUnavailable(f"{what}: HTTP {status}", status=status, sent=True)
                elif status >= 400:
                    raise SchwabRejected(f"{what}: HTTP {status}: {_reason(text)}", status=status)
                elif status >= 300:
                    raise SchwabError(f"{what}: unexpected redirect (HTTP {status})", status=status)
                else:
                    return status, headers, text
            if attempt < attempts:
                await self._sleep(self._backoff_s * 2 ** (attempt - 1))
        assert failure is not None
        raise failure


def _schwab_time(when: datetime) -> str:
    """Schwab's order-search timestamps: yyyy-MM-dd'T'HH:mm:ss.SSSZ in UTC."""
    utc = when.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _safe(path: str) -> str:
    """Shorten account hashes in paths that end up in logs and error messages."""
    parts = path.split("/")
    return "/".join(p if len(p) < 20 else p[:6] + "..." for p in parts)


def _reason(text: str) -> str:
    """Schwab's own explanation from an error body, kept short."""
    try:
        data = json.loads(text)
    except ValueError:
        return text.strip()[:200]
    if isinstance(data, dict):
        if isinstance(data.get("message"), str):
            return str(data["message"])[:300]
        errors = data.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            first = errors[0]
            parts = [str(first[k]) for k in ("title", "detail", "message") if first.get(k)]
            if parts:
                return ": ".join(parts)[:300]
        if isinstance(data.get("error"), str):
            return str(data["error"])[:300]
    return str(data)[:200]
