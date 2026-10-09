"""Market data into the bot: the stream when it works, REST polling when it does not.

* Quotes come from the WebSocket stream (sub-second). If the stream is down or
  has gone quiet, quotes are polled over REST every few seconds instead.
* One-minute bars come from the stream's chart service, or from price history
  when polling. ``MarketData`` drops duplicates, so both may deliver a minute.
* Option contracts are not on the stream. The ones the engine is watching are
  always quoted by polling. With options switched on, a separate task reloads
  each symbol's option chain once a minute for the strategy to choose from.
* Only regular-session bars reach the strategy, which matches the history it
  is warmed up and backtested on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta

import aiohttp

from traider.config import Config
from traider.marketdata import MarketData
from traider.models import Bar, Quote
from traider.schwab.client import SchwabClient, SchwabError
from traider.schwab.parse import parse_candles, parse_option_chain, parse_quotes
from traider.schwab.stream import SchwabStream
from traider.schwab.tokens import TokenManager
from traider.session import SessionTracker
from traider.settings import Settings
from traider.timeutil import Clock, trading_date

log = logging.getLogger(__name__)

_MINUTE = timedelta(minutes=1)


class Feed:
    STREAM_SILENCE_S = 5.0  # a symbol with no stream price for this long is polled instead
    BAR_DELAY_S = 5.0  # give Schwab a moment to publish a minute after it closes
    BAR_LOOKBACK = timedelta(minutes=5)
    WARMUP_LOOKBACK = timedelta(days=5)  # reaches the previous session across a weekend
    CHAIN_REFRESH_S = 60.0

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
        settings: Settings | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        chosen = settings if settings is not None else Settings.from_config(config)
        self._sleep = sleep
        self._symbols = chosen.pinned_symbols
        self._poll_interval_s = config.poll_interval_s
        self._client = client
        self._market = market
        self._clock = clock
        self._session = session
        self._stream = (
            SchwabStream(
                http,
                client,
                tokens,
                sink=self,
                clock=clock,
                symbols=chosen.pinned_symbols,
                sleep=sleep,
            )
            if config.feed == "stream"
            else None
        )
        self._chains_wanted = chosen.risk.allow_options
        self._chain_span = timedelta(days=chosen.option_chain_days)
        self._chain_strikes = chosen.option_chain_strikes
        self._chain_tried_at: dict[str, datetime] = {}
        self._bars_fetched_for: datetime | None = None
        self._pending_warmup: dict[str, None] = {}  # symbols to replay history for; ordered
        self._warmup_bars = 0  # set by run(); 0 means no warm-up is wanted
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

    # -------------------------------------------------------------- symbols

    def set_symbols(self, symbols: Sequence[str]) -> None:
        """Follow the engine's universe. New symbols are warmed up from history on the
        next poll; the stream resubscribes."""
        new = tuple(symbols)
        if set(new) == set(self._symbols):
            self._symbols = new  # only the order differs: nothing to warm up or resubscribe
            return
        for symbol in new:
            if symbol not in self._symbols:
                self._pending_warmup[symbol] = None
        for symbol in list(self._pending_warmup):
            if symbol not in new:
                del self._pending_warmup[symbol]  # dropped before it was warmed up
        self._symbols = new
        if self._stream is not None:
            self._stream.set_symbols(new)

    async def _warm_pending(self, now: datetime) -> None:
        if not self._pending_warmup or self._warmup_bars <= 0:
            self._pending_warmup.clear()
            return
        for symbol in list(self._pending_warmup):
            try:
                raw = await self._client.price_history(symbol, now - self.WARMUP_LOOKBACK, now)
            except SchwabError as exc:
                self._complain(now, "warm-up history for %s not available yet: %s", symbol, exc)
                raw = None
            if symbol not in self._symbols:
                self._pending_warmup.pop(symbol, None)  # dropped while we were waiting
                continue
            if raw is None:
                continue  # stays queued for the next poll
            closed = [bar for bar in parse_candles(raw, symbol) if bar.start + _MINUTE <= now]
            for bar in closed[-self._warmup_bars :]:
                self._market.on_bar(bar, warmup=True)
            self._pending_warmup.pop(symbol, None)

    # --------------------------------------------------------------- status

    def stream_healthy(self) -> bool:
        """True while the stream is delivering prices for every symbol. A socket that is
        connected, or that only sends heartbeats, is not enough: polling must cover
        whatever the stream is not."""
        stream = self._stream
        if stream is None or not stream.connected:
            return False
        now = self._clock.now()
        for symbol in self._symbols:
            quote = self._market.quote(symbol)
            if quote is None or (now - quote.received_at).total_seconds() > self.STREAM_SILENCE_S:
                return False
        return True

    # -------------------------------------------------------------- running

    async def run(
        self,
        *,
        warmup_bars: int,
        poll_interval_s: float | None = None,
        warmup_retry_s: float = 30.0,
    ) -> None:
        """Warm up, then feed the market until cancelled."""
        self._warmup_bars = warmup_bars
        while not await self.warmup(warmup_bars):
            await self._sleep(warmup_retry_s)
        interval = self._poll_interval_s if poll_interval_s is None else poll_interval_s
        tasks = [asyncio.create_task(self._stream.run())] if self._stream is not None else []
        if self._chains_wanted:
            tasks.append(asyncio.create_task(self._keep_chains_fresh(interval)))
        try:
            while True:
                await self._sleep(interval)  # the stream gets the first chance
                await self.poll_once()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

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
        """One REST refresh: options always, share quotes and bars unless the stream is
        doing that job. Warm-up of new symbols comes after the quotes, so a failing history
        call cannot delay them, and before the bar poll, so its replayed bars arrive as
        warm-up bars. It also runs when the market is closed or the stream is healthy."""
        now = self._clock.now()
        if not self._session.view(now).is_open:
            await self._warm_pending(now)
            return
        await self._poll_options(now)
        polling = not self.stream_healthy()
        if polling:
            await self._poll_quotes(now)
        await self._warm_pending(now)
        if polling:
            await self._poll_bars(now)

    async def _poll_quotes(self, now: datetime) -> None:
        if not self._symbols:
            return
        try:
            for quote in parse_quotes(await self._client.quotes(self._symbols), now).values():
                self._market.on_quote(quote)
        except SchwabError as exc:
            self._complain(now, "quote poll failed: %s", exc)

    async def _poll_bars(self, now: datetime) -> None:
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

    async def _poll_options(self, now: datetime) -> None:
        watched = self._market.watched()
        if watched:
            try:
                for quote in parse_quotes(await self._client.quotes(watched), now).values():
                    self._market.on_quote(quote)
            except SchwabError as exc:
                self._complain(now, "option quote poll failed: %s", exc)

    async def refresh_chains(self) -> None:
        """Reload the option chain of every symbol whose chain is a minute old. Kept apart
        from the quote polls: chains only help a strategy choose, and a slow or failing
        chain endpoint must not delay the quotes that orders depend on."""
        now = self._clock.now()
        if not self._chains_wanted or not self._session.view(now).is_open:
            return
        today = trading_date(now)
        for symbol in self._symbols:
            last = self._chain_tried_at.get(symbol)
            if last is not None and (now - last).total_seconds() < self.CHAIN_REFRESH_S:
                continue
            self._chain_tried_at[symbol] = now  # failures wait their turn too
            try:
                raw = await self._client.option_chain(
                    symbol, today, today + self._chain_span, strikes=self._chain_strikes
                )
                self._market.set_chain(symbol, parse_option_chain(raw))
            except SchwabError as exc:
                # A strategy must not choose from prices that are no longer being updated.
                self._market.set_chain(symbol, ())
                self._complain(now, "option chain for %s not available: %s", symbol, exc)

    async def _keep_chains_fresh(self, interval: float) -> None:
        while True:
            await self.refresh_chains()
            await self._sleep(interval)

    def _complain(self, now: datetime, message: str, *args: object) -> None:
        if self._complained_at is None or (now - self._complained_at).total_seconds() >= 60:
            self._complained_at = now
            log.warning(message, *args)
