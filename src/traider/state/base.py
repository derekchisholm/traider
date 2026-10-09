"""Durable state the bot needs to stay safe across restarts.

* a lease, so only one instance trades at a time
* per-day counters: opening equity, order count, loss halt, sale proceeds
* the bot's own open orders, so a restart resumes managing them
* an append-only audit log of what it decided and did
* the paper-trading account
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from traider.models import OrderRecord, OrderStatus, OrderType, Side


@dataclass(frozen=True, slots=True)
class DayState:
    day: str  # New York date, YYYY-MM-DD
    start_equity: Decimal | None = None
    orders: int = 0
    halted_reason: str | None = None
    sold_usd: Decimal = Decimal(0)  # proceeds of the bot's sales today; they settle tomorrow


class StateStore(Protocol):
    async def acquire_lease(self, owner: str, ttl_s: float, now: datetime) -> bool: ...

    async def release_lease(self, owner: str) -> None: ...

    async def get_day(self, day: str) -> DayState: ...

    async def init_start_equity(self, day: str, equity: Decimal) -> Decimal: ...

    async def incr_orders(self, day: str) -> int: ...

    async def halt_day(self, day: str, reason: str) -> None: ...

    async def add_sold(self, day: str, amount: Decimal) -> Decimal: ...

    async def save_order(self, record: OrderRecord) -> None: ...

    async def open_orders(self) -> list[OrderRecord]: ...

    async def log_event(self, kind: str, data: Mapping[str, Any], now: datetime) -> None: ...

    async def events(self, day: str) -> list[dict[str, Any]]: ...

    async def load_paper(self) -> dict[str, Any] | None: ...

    async def save_paper(self, data: dict[str, Any]) -> None: ...


def jsonable(data: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise to plain JSON types (Decimal and datetime become strings)."""
    result: dict[str, Any] = json.loads(json.dumps(data, default=str))
    return result


def order_to_dict(record: OrderRecord) -> dict[str, Any]:
    return {
        "order_id": record.order_id,
        "symbol": record.symbol,
        "side": record.side.value,
        "quantity": record.quantity,
        "order_type": record.order_type.value,
        "limit_price": None if record.limit_price is None else str(record.limit_price),
        "submitted_at": record.submitted_at.isoformat(),
        "position_before": record.position_before,
        "reason": record.reason,
        "filled_quantity": record.filled_quantity,
        "avg_fill_price": None if record.avg_fill_price is None else str(record.avg_fill_price),
        "status": record.status.value,
        "cancel_requested": record.cancel_requested,
        "extra": dict(record.extra),
    }


def order_from_dict(data: Mapping[str, Any]) -> OrderRecord:
    limit, fill = data.get("limit_price"), data.get("avg_fill_price")
    return OrderRecord(
        order_id=str(data["order_id"]),
        symbol=str(data["symbol"]),
        side=Side(data["side"]),
        quantity=int(data["quantity"]),
        order_type=OrderType(data["order_type"]),
        limit_price=None if limit is None else Decimal(str(limit)),
        submitted_at=datetime.fromisoformat(str(data["submitted_at"])),
        position_before=int(data["position_before"]),
        reason=str(data.get("reason", "")),
        filled_quantity=int(data.get("filled_quantity", 0)),
        avg_fill_price=None if fill is None else Decimal(str(fill)),
        status=OrderStatus(data.get("status", OrderStatus.WORKING.value)),
        cancel_requested=bool(data.get("cancel_requested", False)),
        extra={str(k): str(v) for k, v in dict(data.get("extra") or {}).items()},
    )
