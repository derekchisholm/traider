"""In-memory state, for tests, backtests and local paper runs. Nothing survives the process."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from traider.models import OrderRecord
from traider.state.base import (
    DayState,
    LedgerEntry,
    jsonable,
    ledger_from_dict,
    ledger_to_dict,
    order_from_dict,
    order_to_dict,
)
from traider.timeutil import trading_date


class MemoryStateStore:
    def __init__(self, namespace: str = "paper", backing: dict[str, Any] | None = None) -> None:
        # ``backing`` lets tests open a second store on the same data, like a restart would.
        root = backing if backing is not None else {}
        self._data: dict[str, Any] = root.setdefault(namespace, {})
        self._data.setdefault("days", {})
        self._data.setdefault("orders", {})
        self._data.setdefault("events", {})
        self._data.setdefault("ledger", {})

    async def acquire_lease(self, owner: str, ttl_s: float, now: datetime) -> bool:
        lease = self._data.get("lease")
        if lease and lease["owner"] != owner and lease["expires_at"] >= now:
            return False
        self._data["lease"] = {"owner": owner, "expires_at": now + timedelta(seconds=ttl_s)}
        return True

    async def release_lease(self, owner: str) -> None:
        lease = self._data.get("lease")
        if lease and lease["owner"] == owner:
            del self._data["lease"]

    def _day(self, day: str) -> dict[str, Any]:
        days: dict[str, dict[str, Any]] = self._data["days"]
        return days.setdefault(
            day,
            {"start_equity": None, "orders": 0, "halted_reason": None, "sold_usd": Decimal(0)},
        )

    async def get_day(self, day: str) -> DayState:
        return DayState(day, **self._day(day))

    async def init_start_equity(self, day: str, equity: Decimal) -> Decimal:
        entry = self._day(day)
        if entry["start_equity"] is None:
            entry["start_equity"] = equity
        stored: Decimal = entry["start_equity"]
        return stored

    async def incr_orders(self, day: str) -> int:
        entry = self._day(day)
        entry["orders"] += 1
        count: int = entry["orders"]
        return count

    async def halt_day(self, day: str, reason: str) -> None:
        entry = self._day(day)
        if entry["halted_reason"] is None:
            entry["halted_reason"] = reason

    async def add_sold(self, day: str, amount: Decimal) -> Decimal:
        entry = self._day(day)
        entry["sold_usd"] += amount
        total: Decimal = entry["sold_usd"]
        return total

    async def save_order(self, record: OrderRecord) -> None:
        orders: dict[str, dict[str, Any]] = self._data["orders"]
        if record.status.is_terminal:
            orders.pop(record.order_id, None)
        else:
            orders[record.order_id] = order_to_dict(record)

    async def open_orders(self) -> list[OrderRecord]:
        orders: dict[str, dict[str, Any]] = self._data["orders"]
        return [order_from_dict(item) for item in orders.values()]

    async def log_event(self, kind: str, data: Mapping[str, Any], now: datetime) -> None:
        events: dict[str, list[dict[str, Any]]] = self._data["events"]
        day = trading_date(now).isoformat()
        events.setdefault(day, []).append(
            {"kind": kind, "at": now.isoformat(), "data": jsonable(data)}
        )

    async def events(self, day: str) -> list[dict[str, Any]]:
        events: dict[str, list[dict[str, Any]]] = self._data["events"]
        return copy.deepcopy(events.get(day, []))

    async def load_paper(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._data.get("paper"))

    async def save_paper(self, data: dict[str, Any]) -> None:
        self._data["paper"] = copy.deepcopy(data)

    async def ledger(self) -> dict[str, LedgerEntry]:
        stored: dict[str, dict[str, Any]] = self._data["ledger"]
        return {symbol: ledger_from_dict(item) for symbol, item in stored.items()}

    async def put_ledger(self, entry: LedgerEntry) -> None:
        self._data["ledger"][entry.symbol] = ledger_to_dict(entry)

    async def delete_ledger(self, symbol: str) -> None:
        self._data["ledger"].pop(symbol, None)
