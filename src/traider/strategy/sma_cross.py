"""Moving-average crossover. A placeholder that exercises the plumbing.

Long while the fast average of one-minute closes is above the slow one, flat
while it is below. This is here so the bot does something observable on paper;
it is not a recommendation and has no demonstrated edge.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from traider.models import Bar, Target
from traider.strategy.base import Strategy, StrategyContext

_DEFAULTS: dict[str, Any] = {"fast": 5, "slow": 20, "position_usd": 500}


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
        if self.fast < 1 or self.slow <= self.fast:
            raise ValueError("sma_cross needs 1 <= fast < slow")
        if self.position_usd <= 0:
            raise ValueError("sma_cross position_usd must be positive")
        self.warmup_bars = self.slow
        self._closes: dict[str, deque[Decimal]] = {
            symbol: deque(maxlen=self.slow) for symbol in self.symbols
        }

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Target]:
        closes = self._closes.get(bar.symbol)
        if closes is None:
            return ()
        closes.append(bar.close)
        if len(closes) < self.slow:
            return ()
        recent = list(closes)
        fast = sum(recent[-self.fast :], Decimal(0)) / self.fast
        slow = sum(recent, Decimal(0)) / self.slow
        if fast > slow:
            held = ctx.position(bar.symbol)
            # Hold what we have rather than resizing by a share every time the price moves.
            quantity = held if held > 0 else int(self.position_usd // bar.close)
            return (Target(bar.symbol, quantity, f"fast {fast:.2f} above slow {slow:.2f}"),)
        if fast < slow:
            return (Target(bar.symbol, 0, f"fast {fast:.2f} below slow {slow:.2f}"),)
        return ()
