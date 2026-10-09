"""Listed equity options: contract identity and the numbers that follow from it.

The bot only ever buys options to open and sells them to close: long calls and
long puts, one leg at a time. The most that can be lost on a position is the
premium paid for it. A contract is identified by Schwab's symbol, which is the
underlying padded to six characters, the expiry as YYMMDD, C or P, and the
strike in thousandths: ``SPY   261016C00500000``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

SHARES_PER_CONTRACT = 100
_SYMBOL = re.compile(r"^([A-Z][A-Z.]{0,5}) {0,5}(\d{6})([CP])(\d{8})$")


@dataclass(frozen=True, slots=True)
class OptionContract:
    underlying: str
    expiry: date
    right: str  # "C" or "P"
    strike: Decimal

    @property
    def symbol(self) -> str:
        thousandths = int(self.strike * 1000)
        return f"{self.underlying:<6}{self.expiry:%y%m%d}{self.right}{thousandths:08d}"

    def days_to_expiry(self, today: date) -> int:
        return (self.expiry - today).days


@dataclass(frozen=True, slots=True)
class OptionQuote:
    """One line of an option chain, for a strategy to choose from."""

    symbol: str
    bid: Decimal
    ask: Decimal
    delta: Decimal | None
    days_to_expiry: int

    @property
    def contract(self) -> OptionContract:
        return parse_option_symbol(self.symbol)

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


def parse_option_symbol(symbol: str) -> OptionContract:
    match = _SYMBOL.match(symbol) if len(symbol) == 21 else None
    if match is None:
        raise ValueError(f"not an option symbol: {symbol!r}")
    try:
        expiry = datetime.strptime(match[2], "%y%m%d").date()
    except ValueError:
        raise ValueError(f"not an option symbol: {symbol!r}") from None
    return OptionContract(match[1], expiry, match[3], Decimal(match[4]) / 1000)


def is_option_symbol(symbol: str) -> bool:
    try:
        parse_option_symbol(symbol)
    except ValueError:
        return False
    return True


def contract_size(symbol: str) -> int:
    """Shares one unit of ``symbol`` stands for: 100 for an option, 1 for a share."""
    return SHARES_PER_CONTRACT if is_option_symbol(symbol) else 1
