"""The strategy interface.

A strategy is deterministic: it turns market data into *targets* ("hold N shares
of X") and nothing else. It never sees the broker and never places orders. The
engine compares each target with the position actually held, sizes the order,
runs it through the risk checks and sends it. That split is what lets the same
strategy run unchanged in a backtest, on paper and live, and it makes a restart
harmless: the strategy rebuilds its state from recent bars and the engine only
trades the difference.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from traider.models import Bar, Quote, Target
from traider.options import OptionQuote
from traider.research.models import Pick, PostureLevel


@dataclass(frozen=True, slots=True)
class StrategyContext:
    now: datetime
    positions: Mapping[str, int]  # shares (or option contracts) currently held, by symbol
    chains: Mapping[str, Sequence[OptionQuote]] = field(default_factory=dict)
    # Live research picks by symbol, and today's posture. Empty and None when research is off.
    picks: Mapping[str, Pick] = field(default_factory=dict)
    posture: PostureLevel | None = None

    def position(self, symbol: str) -> int:
        return self.positions.get(symbol, 0)

    def chain(self, underlying: str) -> Sequence[OptionQuote]:
        """The option contracts on ``underlying`` to choose from, at most a minute old.
        Empty when options are off or the chain could not be loaded."""
        return self.chains.get(underlying, ())

    def pick(self, symbol: str) -> Pick | None:
        return self.picks.get(symbol)


class Strategy(ABC):
    #: Name used in configuration (``TRAIDER_STRATEGY``).
    name: ClassVar[str]
    #: How many one-minute bars of history to replay before trading starts.
    warmup_bars: int = 0

    def __init__(self, symbols: Sequence[str], params: Mapping[str, Any]) -> None:
        self.symbols = tuple(symbols)
        self.params = dict(params)

    def on_universe(self, symbols: Sequence[str]) -> None:
        """The symbols the bot now watches. Bars only arrive for these. Optional."""
        self.symbols = tuple(symbols)

    @abstractmethod
    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Target]:
        """Called once for every closed one-minute bar. Return new targets, or nothing."""

    def on_quote(self, quote: Quote, ctx: StrategyContext) -> Sequence[Target]:
        """Called on every quote update (sub-second on the stream). Optional.

        Use this for anything that must react inside a bar, such as a stop. Note
        that a backtest on one-minute bars only calls it once per bar.
        """
        return ()
