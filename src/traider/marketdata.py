"""The bot's current view of the market: latest quotes, closed bars, feed liveness.

Feeds push into this object from callbacks; the engine drains it. Updates are
plain synchronous method calls with no awaits, so on a single event loop they
cannot interleave with the engine's reads.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from traider.models import Bar, Quote
from traider.options import OptionQuote


class MarketData:
    def __init__(self) -> None:
        self._quotes: dict[str, Quote] = {}
        self._dirty: dict[str, None] = {}  # insertion-ordered set of symbols with news
        self._bars: list[tuple[Bar, bool]] = []
        self._last_bar_start: dict[str, datetime] = {}
        self._alive_at: datetime | None = None
        self._waker: Callable[[], None] | None = None
        self._watched: dict[str, None] = {}  # option contracts the engine wants quotes for
        self._chains: dict[str, tuple[OptionQuote, ...]] = {}

    def set_waker(self, waker: Callable[[], None]) -> None:
        """Called whenever something new arrives, so the engine can wake immediately."""
        self._waker = waker

    @property
    def feed_alive_at(self) -> datetime | None:
        return self._alive_at

    def mark_alive(self, when: datetime) -> None:
        if self._alive_at is None or when > self._alive_at:
            self._alive_at = when

    def on_quote(self, quote: Quote) -> None:
        self._quotes[quote.symbol] = quote
        self._dirty[quote.symbol] = None
        self.mark_alive(quote.received_at)
        self._wake()

    def on_bar(self, bar: Bar, *, warmup: bool = False) -> None:
        """Queue a closed bar. Bars at or before the last one seen for the symbol are dropped,
        which makes it safe for two sources (stream and REST) to deliver the same minute."""
        last = self._last_bar_start.get(bar.symbol)
        if last is not None and bar.start <= last:
            return
        self._last_bar_start[bar.symbol] = bar.start
        self._bars.append((bar, warmup))
        self._wake()

    def quote(self, symbol: str) -> Quote | None:
        return self._quotes.get(symbol)

    # -- options: the engine says which contracts it cares about, the feed polls them

    def watch(self, symbol: str) -> None:
        self._watched[symbol] = None

    def unwatch(self, symbol: str) -> None:
        self._watched.pop(symbol, None)
        self._quotes.pop(symbol, None)
        self._dirty.pop(symbol, None)

    def watched(self) -> tuple[str, ...]:
        return tuple(self._watched)

    def set_chain(self, underlying: str, quotes: Sequence[OptionQuote]) -> None:
        """Replace the option chain for ``underlying``. An empty one means "not known"."""
        self._chains[underlying] = tuple(quotes)

    def chain(self, underlying: str) -> tuple[OptionQuote, ...]:
        return self._chains.get(underlying, ())

    def chains(self) -> Mapping[str, tuple[OptionQuote, ...]]:
        return dict(self._chains)

    def drain_bars(self) -> list[tuple[Bar, bool]]:
        bars, self._bars = self._bars, []
        return bars

    def drain_dirty(self) -> list[str]:
        symbols = list(self._dirty)
        self._dirty.clear()
        return symbols

    def _wake(self) -> None:
        if self._waker is not None:
            self._waker()
