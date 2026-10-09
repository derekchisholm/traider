"""A simulated broker for paper trading and backtests.

Fills use the current quote: buys at the ask, sells at the bid. A limit order
that is not marketable rests and is re-checked whenever its status is read.
There is no partial-fill, queue-position or market-impact modelling, so paper
results are optimistic compared with live trading.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from traider.broker.base import BrokerError, OrderRejected
from traider.marketdata import MarketData
from traider.models import (
    AccountSnapshot,
    BrokerOrder,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
)
from traider.state.base import StateStore
from traider.timeutil import Clock


@dataclass(slots=True)
class _Holding:
    quantity: int
    avg_price: Decimal


class PaperBroker:
    def __init__(
        self,
        market: MarketData,
        clock: Clock,
        *,
        starting_cash: Decimal,
        store: StateStore | None = None,
    ) -> None:
        self._market = market
        self._clock = clock
        self._store = store
        self._cash = starting_cash
        self._holdings: dict[str, _Holding] = {}
        self._orders: dict[str, tuple[OrderRequest, BrokerOrder]] = {}
        self._next_id = 1

    async def load(self) -> None:
        """Restore a previously saved account. Resting orders are not restored."""
        if self._store is None:
            return
        saved = await self._store.load_paper()
        if not saved:
            return
        self._cash = Decimal(saved["cash"])
        self._next_id = int(saved["next_id"])
        self._holdings = {
            symbol: _Holding(int(item["quantity"]), Decimal(item["avg_price"]))
            for symbol, item in saved["positions"].items()
        }

    async def _save(self) -> None:
        if self._store is None:
            return
        data: dict[str, Any] = {
            "cash": str(self._cash),
            "next_id": self._next_id,
            "positions": {
                symbol: {"quantity": h.quantity, "avg_price": str(h.avg_price)}
                for symbol, h in self._holdings.items()
            },
        }
        await self._store.save_paper(data)

    # -- Broker interface -----------------------------------------------------

    async def get_account(self) -> AccountSnapshot:
        equity = self._cash
        positions: dict[str, Position] = {}
        for symbol, holding in self._holdings.items():
            quote = self._market.quote(symbol)
            mark = quote.bid if quote is not None and quote.bid > 0 else holding.avg_price
            equity += mark * holding.quantity
            positions[symbol] = Position(symbol, holding.quantity, holding.avg_price)
        return AccountSnapshot(
            equity=equity,
            cash_available=self._cash,
            positions=positions,
            as_of=self._clock.now(),
        )

    async def get_open_orders(self) -> list[BrokerOrder]:
        for order_id in list(self._orders):
            await self._evaluate(order_id)
        return [o for _, o in self._orders.values() if not o.status.is_terminal]

    async def place(self, request: OrderRequest) -> str | None:
        quote = self._market.quote(request.symbol)
        if quote is None or quote.bid <= 0 or quote.ask <= 0:
            raise OrderRejected(f"no usable quote for {request.symbol}")
        if request.quantity <= 0:
            raise OrderRejected("quantity must be positive")
        if request.order_type is OrderType.LIMIT and request.limit_price is None:
            raise OrderRejected("limit order without a price")
        if request.side is Side.BUY:
            worst = request.limit_price if request.limit_price is not None else quote.ask
            if worst * request.quantity > self._cash:
                raise OrderRejected(
                    f"not enough cash: need {worst * request.quantity:.2f}, have {self._cash:.2f}"
                )
        else:
            held = self._holdings.get(request.symbol)
            if held is None or request.quantity > held.quantity:
                raise OrderRejected(
                    f"sell {request.quantity} but only {held.quantity if held else 0} held"
                )
        order_id = f"P{self._next_id}"
        self._next_id += 1
        order = BrokerOrder(
            order_id=order_id,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            filled_quantity=0,
            status=OrderStatus.WORKING,
            entered_at=self._clock.now(),
            raw_status="WORKING",
        )
        self._orders[order_id] = (request, order)
        await self._save()  # persist the id counter even if nothing fills yet
        await self._evaluate(order_id)
        return order_id

    def _lost_in_a_restart(self, order_id: str) -> bool:
        """Resting orders are not saved. An id this broker once issued but no longer
        knows belonged to an order that was resting when the process stopped."""
        number = order_id.removeprefix("P")
        return order_id.startswith("P") and number.isdigit() and int(number) < self._next_id

    async def get_order(self, order_id: str) -> BrokerOrder:
        if order_id not in self._orders:
            if self._lost_in_a_restart(order_id):
                return BrokerOrder(
                    order_id, "", Side.BUY, 0, 0, OrderStatus.CANCELED, raw_status="CANCELED"
                )
            raise BrokerError(f"unknown paper order {order_id}")
        await self._evaluate(order_id)
        return self._orders[order_id][1]

    async def cancel(self, order_id: str) -> None:
        if order_id not in self._orders:
            if self._lost_in_a_restart(order_id):
                return
            raise BrokerError(f"unknown paper order {order_id}")
        request, order = self._orders[order_id]
        if not order.status.is_terminal:
            self._orders[order_id] = (
                request,
                replace(order, status=OrderStatus.CANCELED, raw_status="CANCELED"),
            )

    async def find_order(
        self, symbol: str, side: Side, quantity: int, since: datetime
    ) -> BrokerOrder | None:
        for order_id in reversed(list(self._orders)):
            order = await self.get_order(order_id)
            if (
                order.symbol == symbol
                and order.side is side
                and order.quantity == quantity
                and order.entered_at is not None
                and order.entered_at >= since
            ):
                return order
        return None

    def filled_orders(self) -> list[BrokerOrder]:
        """Every order that has filled, oldest first. Used to report backtest trades."""
        return [o for _, o in self._orders.values() if o.status is OrderStatus.FILLED]

    # -- fill simulation ------------------------------------------------------

    async def _evaluate(self, order_id: str) -> None:
        request, order = self._orders[order_id]
        if order.status.is_terminal:
            return
        quote = self._market.quote(request.symbol)
        if quote is None or quote.bid <= 0 or quote.ask <= 0:
            return
        price = quote.ask if request.side is Side.BUY else quote.bid
        if request.order_type is OrderType.LIMIT:
            limit = request.limit_price
            if limit is None:
                return
            marketable = price <= limit if request.side is Side.BUY else price >= limit
            if not marketable:
                return
        if not self._apply_fill(request, price):
            self._orders[order_id] = (
                request,
                replace(order, status=OrderStatus.REJECTED, raw_status="REJECTED"),
            )
            return
        self._orders[order_id] = (
            request,
            replace(
                order,
                status=OrderStatus.FILLED,
                raw_status="FILLED",
                filled_quantity=request.quantity,
                avg_fill_price=price,
            ),
        )
        await self._save()

    def _apply_fill(self, request: OrderRequest, price: Decimal) -> bool:
        """Move cash and shares. Returns False if the account can no longer cover it."""
        value = price * request.quantity
        held = self._holdings.get(request.symbol)
        if request.side is Side.BUY:
            if value > self._cash:
                return False
            self._cash -= value
            if held is None:
                self._holdings[request.symbol] = _Holding(request.quantity, price)
            else:
                total = held.quantity + request.quantity
                held.avg_price = (held.avg_price * held.quantity + value) / total
                held.quantity = total
            return True
        if held is None or request.quantity > held.quantity:
            return False
        self._cash += value
        held.quantity -= request.quantity
        if held.quantity == 0:
            del self._holdings[request.symbol]
        return True
