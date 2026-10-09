"""Moving-average crossover. A placeholder that exercises the plumbing.

Long while the fast average of one-minute closes is above the slow one, flat
while it is below. This is here so the bot does something observable on paper;
it is not a recommendation and has no demonstrated edge.

With ``require_pick`` set, it only enters a symbol while research has a live long
pick for it, and it sells a held symbol once that pick is gone or expired.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from traider.models import Bar, Target
from traider.research.models import Pick, PickSide
from traider.strategy.base import Strategy, StrategyContext

_DEFAULTS: dict[str, Any] = {"fast": 5, "slow": 20, "position_usd": 500, "require_pick": False}


def _is_live_long(pick: Pick | None, now: datetime) -> bool:
    return pick is not None and pick.side is PickSide.LONG and pick.expires_at > now


class SmaCross(Strategy):
    name = "sma_cross"

    def __init__(self, symbols: Sequence[str], params: Mapping[str, Any]) -> None:
        super().__init__(symbols, params)
        unknown = set(params) - set(_DEFAULTS)
        if unknown:
            raise ValueError(f"unknown sma_cross parameters: {sorted(unknown)}")
        merged = {**_DEFAULTS, **params}
        self.fast = int(merged["fast"])
        self.slow = int(merged["slow"])
        self.position_usd = Decimal(str(merged["position_usd"]))
        self.require_pick = bool(merged["require_pick"])
        if self.fast < 1 or self.slow <= self.fast:
            raise ValueError("sma_cross needs 1 <= fast < slow")
        if self.position_usd <= 0:
            raise ValueError("sma_cross position_usd must be positive")
        self.warmup_bars = self.slow
        # History is created on first bar for each symbol in the universe, so a symbol
        # added later by on_universe gets its own window.
        self._closes: dict[str, deque[Decimal]] = {}

    def on_universe(self, symbols: Sequence[str]) -> None:
        super().on_universe(symbols)
        # A symbol that leaves the universe forgets its history, so if it returns its
        # averages are not mixed with closes from before the gap.
        for symbol in list(self._closes):
            if symbol not in self.symbols:
                del self._closes[symbol]

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Target]:
        closes = self._closes.get(bar.symbol)
        if closes is None:
            if bar.symbol not in self.symbols:
                return ()
            closes = self._closes[bar.symbol] = deque(maxlen=self.slow)
        closes.append(bar.close)
        if len(closes) < self.slow:
            return ()
        recent = list(closes)
        fast = sum(recent[-self.fast :], Decimal(0)) / self.fast
        slow = sum(recent, Decimal(0)) / self.slow
        held = ctx.position(bar.symbol)
        if self.require_pick and not _is_live_long(ctx.pick(bar.symbol), ctx.now):
            return (Target(bar.symbol, 0, "no live long pick"),) if held > 0 else ()
        if fast > slow:
            # Hold what we have rather than resizing by a share every time the price moves.
            quantity = held if held > 0 else int(self.position_usd // bar.close)
            return (Target(bar.symbol, quantity, f"fast {fast:.2f} above slow {slow:.2f}"),)
        if fast < slow:
            return (Target(bar.symbol, 0, f"fast {fast:.2f} below slow {slow:.2f}"),)
        return ()
