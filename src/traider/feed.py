"""Market data into the bot: the stream when it works, REST polling when it does not.

* Quotes come from the WebSocket stream (sub-second). If the stream is down or
  has gone quiet, quotes are polled over REST every few seconds instead.
* One-minute bars come from the stream's chart service, or from price history
  when polling. ``MarketData`` drops duplicates, so both may deliver a minute.
* Only regular-session bars reach the strategy, which matches the history it
  is warmed up and backtested on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import aiohttp

from traider.config import Config
from traider.marketdata import MarketData
from traider.models import Bar, Quote
from traider.schwab.client import SchwabClient, SchwabError
from traider.schwab.parse import parse_candles, parse_quotes
from traider.schwab.stream import SchwabStream
from traider.schwab.tokens import TokenManager
from traider.session import SessionTracker
from traider.timeutil import Clock

log = logging.getLogger(__name__)

_MINUTE = timedelta(minutes=1)


class Feed:
    STREAM_SILENCE_S = 30.0  # connected but nothing received for this long: not healthy
    BAR_DELAY_S = 5.0  # give Schwab a moment to publish a minute after it closes
    BAR_LOOKBACK = timedelta(minutes=5)
    WARMUP_LOOKBACK = timedelta(days=5)  # reaches the previous session across a weekend

    def __init__(
        self,
        *,
        config: Config,
        http: aiohttp.ClientSession,
        client: SchwabClient,
        tokens: TokenManager,
        market: MarketData,
        clock: Clock,
        session: SessionTracker,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sleep = sleep
        self._symbols = config.symbols
        self._poll_interval_s = config.poll_interval_s
        self._client = client
        self._market = market
        self._clock = clock
        self._session = session
        self._stream = (
            SchwabStream(
                http, client, tokens, sink=self, clock=clock, symbols=config.symbols, sleep=sleep
            )
            if config.feed == "stream"
            else None
        )
        self._bars_fetched_for: datetime | None = None
        self._complained_at: datetime | None = None

    # -------------------------------------------------- sink for the stream

    def on_quote(self, quote: Quote) -> None:
        self._market.on_quote(quote)

    def on_bar(self, bar: Bar) -> None:
        session = self._session.session
        if session is None or session.open is None or session.close is None:
            return
        if session.open <= bar.start < session.close:
            self._market.on_bar(bar)

    def on_alive(self, when: datetime) -> None:
        self._market.mark_alive(when)

    # --------------------------------------------------------------- status

    def stream_healthy(self) -> bool:
        stream = self._stream
        if stream is None or not stream.connected or stream.last_message_at is None:
            return False
        silence = (self._clock.now() - stream.last_message_at).total_seconds()
        return silence <= self.STREAM_SILENCE_S

    # -------------------------------------------------------------- running

    async def run(
        self,
        *,
        warmup_bars: int,
        poll_interval_s: float | None = None,
        warmup_retry_s: float = 30.0,
    ) -> None:
        """Warm up, then feed the market until cancelled."""
        while not await self.warmup(warmup_bars):
            await self._sleep(warmup_retry_s)
        interval = self._poll_interval_s if poll_interval_s is None else poll_interval_s
        stream_task = asyncio.create_task(self._stream.run()) if self._stream is not None else None
        try:
            while True:
                await self._sleep(interval)  # the stream gets the first chance
                await self.poll_once()
        finally:
            if stream_task is not None:
                stream_task.cancel()
                await asyncio.gather(stream_task, return_exceptions=True)

    async def warmup(self, bars_wanted: int) -> bool:
        """Replay recent history so the strategy does not start cold. False means try again."""
        if bars_wanted <= 0:
            return True
        now = self._clock.now()
        try:
            for symbol in self._symbols:
                raw = await self._client.price_history(symbol, now - self.WARMUP_LOOKBACK, now)
                closed = [bar for bar in parse_candles(raw, symbol) if bar.start + _MINUTE <= now]
                for bar in closed[-bars_wanted:]:
                    self._market.on_bar(bar, warmup=True)
        except SchwabError as exc:
            self._complain(now, "warm-up history not available yet: %s", exc)
            return False
        log.info("warm-up done for %d symbols", len(self._symbols))
        return True

    async def poll_once(self) -> None:
        """One REST refresh of quotes and bars, unless the stream is doing the job."""
        now = self._clock.now()
        if self.stream_healthy() or not self._session.view(now).is_open:
            return
        try:
            for quote in parse_quotes(await self._client.quotes(self._symbols), now).values():
                self._market.on_quote(quote)
        except SchwabError as exc:
            self._complain(now, "quote poll failed: %s", exc)

        minute = now.replace(second=0, microsecond=0)
        due = (now - minute).total_seconds() >= self.BAR_DELAY_S
        if not due or self._bars_fetched_for == minute:
            return
        try:
            for symbol in self._symbols:
                raw = await self._client.price_history(symbol, minute - self.BAR_LOOKBACK, now)
                for bar in parse_candles(raw, symbol):
                    if bar.start + _MINUTE <= now:
                        self.on_bar(bar)
        except SchwabError as exc:
            self._complain(now, "bar poll failed: %s", exc)
            return
        self._bars_fetched_for = minute

    def _complain(self, now: datetime, message: str, *args: object) -> None:
        if self._complained_at is None or (now - self._complained_at).total_seconds() >= 60:
            self._complained_at = now
            log.warning(message, *args)
