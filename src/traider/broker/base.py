"""The broker interface the engine trades through, and the ways it can fail."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from traider.models import AccountSnapshot, BrokerOrder, OrderRequest, Side


class BrokerError(Exception):
    """Base class for broker failures."""


class OrderRejected(BrokerError):
    """The broker definitely refused the order. Nothing is working."""


class AmbiguousOrder(BrokerError):
    """The request failed in a way that leaves the outcome unknown.

    The order may or may not have been accepted. The caller must not resend it;
    it must look at positions and open orders to find out what happened.
    """


class BrokerUnavailable(BrokerError):
    """The broker could not be reached or the login is not usable right now."""


class Broker(Protocol):
    async def get_account(self) -> AccountSnapshot: ...

    async def get_open_orders(self) -> list[BrokerOrder]:
        """Every unfinished order in the account, whoever placed it."""
        ...

    async def place(self, request: OrderRequest) -> str | None:
        """Send an order. Returns its id, or None if it was accepted without one."""
        ...

    async def get_order(self, order_id: str) -> BrokerOrder: ...

    async def cancel(self, order_id: str) -> None: ...

    async def find_order(
        self, symbol: str, side: Side, quantity: int, since: datetime
    ) -> BrokerOrder | None:
        """The newest order with these details entered at or after ``since``, in any state.

        This is how the engine learns the fate of an order whose placement reply was lost.
        """
        ...
