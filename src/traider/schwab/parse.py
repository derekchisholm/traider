"""Schwab JSON to the bot's own types.

Field names follow the sample responses in the schwabdev documentation and the
enums in schwab-py. Parsing is strict about the few fields the bot's safety
depends on and forgiving about everything else: a balance that is missing
becomes ``None`` (which blocks entries downstream), never a guessed zero.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from traider.models import AccountSnapshot, Bar, BrokerOrder, OrderStatus, Position, Quote, Side
from traider.session import Session

# Asset types that are plain shares. Schwab reports ETFs as COLLECTIVE_INVESTMENT.
_SHARE_TYPES = {"EQUITY", "COLLECTIVE_INVESTMENT", "ETF"}

_TERMINAL = {
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED,
    "REPLACED": OrderStatus.CANCELED,  # the old order is gone; a new id replaced it
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
}
_OPEN = {
    "AWAITING_PARENT_ORDER", "AWAITING_CONDITION", "AWAITING_STOP_CONDITION",
    "AWAITING_MANUAL_REVIEW", "ACCEPTED", "AWAITING_UR_OUT", "PENDING_ACTIVATION", "QUEUED",
    "WORKING", "PENDING_CANCEL", "PENDING_REPLACE", "NEW", "AWAITING_RELEASE_TIME",
    "PENDING_ACKNOWLEDGEMENT", "PENDING_RECALL",
}  # fmt: skip


class ParseError(ValueError):
    """Schwab sent something the bot cannot safely interpret."""


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _from_ms(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# ------------------------------------------------------------------------- account


def parse_account(raw: Any, as_of: datetime) -> AccountSnapshot:
    account = _mapping(raw).get("securitiesAccount")
    if not isinstance(account, Mapping):
        raise ParseError("account response has no securitiesAccount")
    current = _mapping(account.get("currentBalances"))
    aggregated = _mapping(_mapping(raw).get("aggregatedBalance"))

    equity = _decimal(current.get("liquidationValue"))
    if equity is None:
        equity = _decimal(aggregated.get("currentLiquidationValue"))
    if equity is None:
        equity = _decimal(aggregated.get("liquidationValue"))

    # Cash the bot may spend without borrowing. Cash accounts report
    # cashAvailableForTrading; margin accounts report cashBalance. Buying power and
    # availableFunds include margin, so they are deliberately not used.
    cash = _decimal(current.get("cashAvailableForTrading"))
    if cash is None:
        cash = _decimal(current.get("cashBalance"))

    positions: dict[str, Position] = {}
    for item in account.get("positions") or []:
        entry = _mapping(item)
        instrument = _mapping(entry.get("instrument"))
        symbol = instrument.get("symbol")
        if not isinstance(symbol, str) or instrument.get("assetType") not in _SHARE_TYPES:
            continue
        held = (_decimal(entry.get("longQuantity")) or Decimal(0)) - (
            _decimal(entry.get("shortQuantity")) or Decimal(0)
        )
        quantity = int(held)  # whole shares; any fraction is left alone
        if quantity == 0:
            continue
        positions[symbol] = Position(
            symbol, quantity, _decimal(entry.get("averagePrice")) or Decimal(0)
        )

    kind = account.get("type")
    return AccountSnapshot(
        equity=equity,
        cash_available=cash,
        positions=positions,
        as_of=as_of,
        account_type=kind if isinstance(kind, str) and kind else None,
    )


# -------------------------------------------------------------------------- orders


def parse_order(raw: Any) -> BrokerOrder:
    order = _mapping(raw)
    order_id = order.get("orderId")
    if order_id is None or isinstance(order_id, bool) or not isinstance(order_id, int | str):
        raise ParseError("order has no orderId")

    legs = order.get("orderLegCollection") or []
    leg = _mapping(legs[0]) if legs else {}
    symbol = _mapping(leg.get("instrument")).get("symbol")
    instruction = str(leg.get("instruction", ""))
    side = Side.SELL if instruction.startswith("SELL") else Side.BUY

    raw_status = str(order.get("status", ""))
    if raw_status in _TERMINAL:
        status = _TERMINAL[raw_status]
    elif raw_status in _OPEN:
        status = OrderStatus.WORKING
    else:
        status = OrderStatus.UNKNOWN

    quantity = _decimal(order.get("quantity")) or _decimal(leg.get("quantity")) or Decimal(0)
    filled = _decimal(order.get("filledQuantity")) or Decimal(0)
    return BrokerOrder(
        order_id=str(order_id),
        symbol=symbol if isinstance(symbol, str) else "",
        side=side,
        quantity=int(quantity),
        filled_quantity=int(filled),
        status=status,
        avg_fill_price=_average_fill_price(order),
        entered_at=_iso(order.get("enteredTime")),
        raw_status=raw_status,
    )


def _average_fill_price(order: Mapping[str, Any]) -> Decimal | None:
    """Quantity-weighted price over real executions. Cancel records carry a zero price
    and must not be averaged in."""
    shares = Decimal(0)
    value = Decimal(0)
    for item in order.get("orderActivityCollection") or []:
        activity = _mapping(item)
        if activity.get("executionType") != "FILL":
            continue
        for leg_item in activity.get("executionLegs") or []:
            leg = _mapping(leg_item)
            quantity, price = _decimal(leg.get("quantity")), _decimal(leg.get("price"))
            if quantity and price and quantity > 0 and price > 0:
                shares += quantity
                value += quantity * price
    return value / shares if shares > 0 else None


# -------------------------------------------------------------------------- quotes


def parse_quotes(raw: Any, received_at: datetime) -> dict[str, Quote]:
    quotes: dict[str, Quote] = {}
    for symbol, item in _mapping(raw).items():
        entry = _mapping(item)
        fields = _mapping(entry.get("quote"))
        bid, ask = _decimal(fields.get("bidPrice")), _decimal(fields.get("askPrice"))
        if bid is None or ask is None:
            continue
        last = _decimal(fields.get("lastPrice"))
        quotes[symbol] = Quote(
            symbol=symbol,
            bid=bid,
            ask=ask,
            last=last if last is not None else bid,
            ts=_from_ms(fields.get("quoteTime")) or received_at,
            received_at=received_at,
            delayed=entry.get("realtime") is False,
        )
    return quotes


# -------------------------------------------------------------------- market hours


def parse_market_hours(raw: Any, day: date) -> Session:
    """The regular equity session for ``day``. Raises rather than guessing."""
    equity = _mapping(raw).get("equity")
    if not isinstance(equity, Mapping) or not equity:
        raise ParseError("market-hours response has no equity section")
    # Schwab keys the entry "EQ" on trading days and "equity" on closed days.
    entry = _mapping(next(iter(equity.values())))
    reported = entry.get("date")
    if reported is not None and reported != day.isoformat():
        raise ParseError(f"asked for hours on {day.isoformat()} but got {reported}")
    is_open = entry.get("isOpen")
    if is_open is False:
        return Session(day, None, None)
    if is_open is not True:
        raise ParseError("market-hours response does not say whether the market is open")
    regular = _mapping(entry.get("sessionHours")).get("regularMarket") or []
    window = _mapping(regular[0]) if regular else {}
    start, end = _iso(window.get("start")), _iso(window.get("end"))
    if start is None or end is None or end <= start:
        raise ParseError("market-hours response has no usable regular session")
    return Session(day, start, end)


# ------------------------------------------------------------------------- candles


def parse_candles(raw: Any, symbol: str) -> list[Bar]:
    bars: list[Bar] = []
    for item in _mapping(raw).get("candles") or []:
        candle = _mapping(item)
        start = _from_ms(candle.get("datetime"))
        prices = [_decimal(candle.get(name)) for name in ("open", "high", "low", "close")]
        if start is None or any(price is None for price in prices):
            continue
        open_, high, low, close = (price for price in prices if price is not None)
        volume = _decimal(candle.get("volume")) or Decimal(0)
        bars.append(Bar(symbol, start, open_, high, low, close, int(volume)))
    bars.sort(key=lambda bar: bar.start)
    return bars
