"""Plain data types shared by the strategy, risk, broker and engine layers.

Prices and cash are ``Decimal``. Share quantities are whole ``int`` shares.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

BPS = Decimal(10000)


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(StrEnum):
    """Broker order states collapsed to what the engine needs to know."""

    WORKING = "WORKING"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    # Anything we do not recognise. Treated as still open, which blocks the symbol.
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED}
)


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    ts: datetime  # exchange quote time when the feed gives one
    received_at: datetime  # when this process saw it
    delayed: bool = False
    halted: bool = False  # the broker says the security is not trading normally

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal | None:
        if self.bid <= 0 or self.ask <= 0:
            return None
        return (self.ask - self.bid) / self.mid * BPS


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    start: datetime  # bar open time, UTC
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    quantity: int  # signed: long is positive
    avg_price: Decimal


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    equity: Decimal | None
    cash_available: Decimal | None
    positions: Mapping[str, Position]
    as_of: datetime
    account_type: str | None = None  # "CASH" or "MARGIN" as the broker names it, if it does

    def position(self, symbol: str) -> int:
        held = self.positions.get(symbol)
        return held.quantity if held else 0


@dataclass(frozen=True, slots=True)
class Target:
    """What a strategy wants to hold in a symbol. The engine works out the order."""

    symbol: str
    quantity: int
    reason: str = ""


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType
    limit_price: Decimal | None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    order_id: str
    symbol: str
    side: Side
    quantity: int
    filled_quantity: int
    status: OrderStatus
    avg_fill_price: Decimal | None = None
    entered_at: datetime | None = None
    raw_status: str = ""


@dataclass(slots=True)
class OrderRecord:
    """The bot's own record of an order it placed. Persisted so a restart can resume."""

    order_id: str
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType
    limit_price: Decimal | None
    submitted_at: datetime
    position_before: int
    reason: str = ""
    filled_quantity: int = 0
    avg_fill_price: Decimal | None = None
    status: OrderStatus = OrderStatus.WORKING
    cancel_requested: bool = False
    extra: dict[str, str] = field(default_factory=dict)
