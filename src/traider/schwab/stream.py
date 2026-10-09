"""Schwab's streaming market data over a WebSocket.

Protocol, as implemented by schwab-py and schwabdev:

1. ``GET /trader/v1/userPreference`` gives the socket URL and four identifiers.
2. Connect and send ``ADMIN/LOGIN`` with the access token.
3. Subscribe: ``LEVELONE_EQUITIES`` for quotes, ``CHART_EQUITY`` for one-minute bars.
4. Read messages: ``data`` (updates), ``notify`` (heartbeats), ``response`` (replies).

Level-one updates only carry the fields that changed, so the last known value
of each field is kept per symbol and merged. That memory is discarded on every
reconnect: a quote assembled from a stale bid and a fresh ask would be worse
than no quote.

The stream runs until cancelled and reconnects with backoff. Schwab allows one
stream per login, which is one more reason only one bot instance may run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import aiohttp

from traider.models import Bar, Quote
from traider.schwab.client import SchwabClient, SchwabError
from traider.schwab.tokens import AuthUnavailable, TokenManager
from traider.timeutil import Clock

log = logging.getLogger(__name__)

# LEVELONE_EQUITIES field numbers (schwab-py LevelOneEquityFields).
_BID, _ASK, _LAST, _QUOTE_TIME, _TRADE_TIME = "1", "2", "3", "34", "35"
_EARLIEST = datetime(2020, 1, 1, tzinfo=UTC)
LEVEL_ONE_FIELDS = "0,1,2,3,4,5,8,34,35"
# CHART_EQUITY: 0 key, 1 sequence, 2 open, 3 high, 4 low, 5 close, 6 volume, 7 time, 8 day.
CHART_FIELDS = "0,1,2,3,4,5,6,7,8"


class StreamSink(Protocol):
    def on_quote(self, quote: Quote) -> None: ...

    def on_bar(self, bar: Bar) -> None: ...

    def on_alive(self, when: datetime) -> None: ...


class StreamError(Exception):
    pass


class SchwabStream:
    REPLY_TIMEOUT_S = 15.0

    def __init__(
        self,
        session: aiohttp.ClientSession,
        client: SchwabClient,
        tokens: TokenManager,
        *,
        sink: StreamSink,
        clock: Clock,
        symbols: Sequence[str],
        backoff_initial_s: float = 1.0,
        backoff_max_s: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._session = session
        self._client = client
        self._tokens = tokens
        self._sink = sink
        self._clock = clock
        self._symbols = tuple(symbols)
        self._backoff_initial_s = backoff_initial_s
        self._backoff_max_s = backoff_max_s
        self._sleep = sleep
        self._connected = False
        self._last_message_at: datetime | None = None
        self._fields: dict[str, dict[str, Any]] = {}
        self._request_id = 0
        self._customer_id = ""
        self._correl_id = ""

    @property
    def connected(self) -> bool:
        """True once logged in and subscribed, until the connection is lost."""
        return self._connected

    @property
    def last_message_at(self) -> datetime | None:
        return self._last_message_at

    async def run(self) -> None:
        """Stay connected until cancelled."""
        backoff = self._backoff_initial_s
        while True:
            started = time.monotonic()
            try:
                await self._connect_and_read()
                log.warning("stream closed by the server")
            except asyncio.CancelledError:
                raise
            except (AuthUnavailable, SchwabError) as exc:
                log.info("stream cannot start: %s", exc)
            except Exception as exc:
                log.warning("stream error: %s: %s", type(exc).__name__, exc)
            finally:
                self._connected = False
                self._fields.clear()
            if time.monotonic() - started > 60:
                backoff = self._backoff_initial_s  # it was up for a while: start over
            await self._sleep(backoff * (1 + random.random() / 4))  # noqa: S311 - jitter
            backoff = min(backoff * 2, self._backoff_max_s)

    # ----------------------------------------------------------------- session

    async def _connect_and_read(self) -> None:
        preferences = await self._client.user_preference()
        try:
            info = preferences["streamerInfo"][0]
            url = str(info["streamerSocketUrl"])
            self._customer_id = str(info["schwabClientCustomerId"])
            self._correl_id = str(info["schwabClientCorrelId"])
            channel, function_id = info["schwabClientChannel"], info["schwabClientFunctionId"]
        except (KeyError, IndexError, TypeError):
            raise StreamError("userPreference has no streamer details") from None
        token = await self._tokens.access_token()
        keys = ",".join(self._symbols)

        async with self._session.ws_connect(url, heartbeat=20.0) as ws:
            try:
                await self._command(
                    ws,
                    "ADMIN",
                    "LOGIN",
                    {
                        "Authorization": token,
                        "SchwabClientChannel": channel,
                        "SchwabClientFunctionId": function_id,
                    },
                )
            except StreamError:
                await self._tokens.invalidate(token)  # the token may be the problem
                raise
            await self._command(
                ws, "LEVELONE_EQUITIES", "SUBS", {"keys": keys, "fields": LEVEL_ONE_FIELDS}
            )
            await self._command(ws, "CHART_EQUITY", "SUBS", {"keys": keys, "fields": CHART_FIELDS})
            self._connected = True
            log.info("stream connected, %d symbols", len(self._symbols))
            async for message in ws:
                if message.type is aiohttp.WSMsgType.TEXT:
                    self._handle_text(message.data)
                elif message.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break

    async def _command(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        service: str,
        command: str,
        parameters: Mapping[str, Any],
    ) -> None:
        """Send one request and wait for Schwab to acknowledge it."""
        request_id = str(self._request_id)
        self._request_id += 1
        await ws.send_str(
            json.dumps(
                {
                    "requests": [
                        {
                            "service": service,
                            "command": command,
                            "requestid": request_id,
                            "SchwabClientCustomerId": self._customer_id,
                            "SchwabClientCorrelId": self._correl_id,
                            "parameters": dict(parameters),
                        }
                    ]
                }
            )
        )
        deadline = time.monotonic() + self.REPLY_TIMEOUT_S
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StreamError(f"no reply to {service}/{command}")
            message = await ws.receive(timeout=remaining)
            if message.type is not aiohttp.WSMsgType.TEXT:
                raise StreamError(f"connection closed during {service}/{command}")
            payload = self._handle_text(message.data)
            for reply in _items(payload, "response"):
                if str(reply.get("requestid")) != request_id:
                    continue
                content = reply.get("content")
                code = content.get("code") if isinstance(content, Mapping) else None
                if code != 0:
                    detail = content.get("msg") if isinstance(content, Mapping) else content
                    raise StreamError(f"{service}/{command} refused: code {code}: {detail}")
                return

    # ---------------------------------------------------------------- messages

    def _handle_text(self, text: str) -> Mapping[str, Any]:
        """Process one frame. Returns the decoded payload ({} if it was not usable)."""
        try:
            payload = json.loads(text)
        except ValueError:
            log.debug("stream: ignoring a frame that is not JSON")
            return {}
        if not isinstance(payload, Mapping):
            return {}
        now = self._clock.now()
        self._last_message_at = now
        self._sink.on_alive(now)  # any frame, heartbeats included, proves the feed is up
        for entry in _items(payload, "data"):
            contents = entry.get("content")
            if not isinstance(contents, list):
                continue
            service = entry.get("service")
            for content in contents:
                if not isinstance(content, Mapping):
                    continue
                if service == "LEVELONE_EQUITIES":
                    self._on_level_one(content, entry.get("timestamp"), now)
                elif service == "CHART_EQUITY":
                    self._on_chart(content)
        return payload

    def _on_level_one(self, content: Mapping[str, Any], stamp: Any, now: datetime) -> None:
        symbol = content.get("key")
        if not isinstance(symbol, str):
            return
        known = self._fields.setdefault(symbol, {})
        known.update(content)
        bid, ask = _decimal(known.get(_BID)), _decimal(known.get(_ASK))
        if bid is None or ask is None:
            return
        last = _decimal(known.get(_LAST))
        times = [
            t for t in map(_market_time, (known.get(_QUOTE_TIME), known.get(_TRADE_TIME))) if t
        ]
        when = max(times) if times else (_market_time(stamp) or now)
        self._sink.on_quote(
            Quote(
                symbol=symbol,
                bid=bid,
                ask=ask,
                last=last if last is not None else bid,
                ts=when,
                received_at=now,
                delayed=known.get("delayed") is True,
            )
        )

    def _on_chart(self, content: Mapping[str, Any]) -> None:
        symbol = content.get("key")
        start = _from_ms(content.get("7"))
        prices = [_decimal(content.get(field)) for field in ("2", "3", "4", "5")]
        if not isinstance(symbol, str) or start is None or any(p is None for p in prices):
            return
        open_, high, low, close = (p for p in prices if p is not None)
        volume = _decimal(content.get("6")) or Decimal(0)
        self._sink.on_bar(Bar(symbol, start, open_, high, low, close, int(volume)))


def _items(payload: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _market_time(value: Any) -> datetime | None:
    """A millisecond timestamp, or None if the value cannot be one (a price, say)."""
    when = _from_ms(value)
    return when if when is not None and when >= _EARLIEST else None


def _from_ms(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
