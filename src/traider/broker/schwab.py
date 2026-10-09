"""The live broker: real orders in a real Schwab account."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from traider.broker.base import AmbiguousOrder, BrokerError, BrokerUnavailable, OrderRejected
from traider.models import AccountSnapshot, BrokerOrder, OrderRequest, Side
from traider.schwab.client import SchwabClient, SchwabError, SchwabRejected, SchwabUnavailable
from traider.schwab.orders import build_equity_order
from traider.schwab.parse import ParseError, parse_account, parse_order
from traider.timeutil import Clock

log = logging.getLogger(__name__)


class SchwabBroker:
    #: How far back to look for open orders. The bot only places day orders, but an
    #: order somebody left open on one of its symbols should still block it.
    OPEN_ORDER_LOOKBACK = timedelta(days=3)
    #: Search a little past "now" so an order entered this instant is not missed to
    #: timestamp rounding or a small clock difference.
    SEARCH_AHEAD = timedelta(minutes=1)

    def __init__(
        self,
        client: SchwabClient,
        clock: Clock,
        *,
        account_hash: str | None = None,
        account_last4: str | None = None,
    ) -> None:
        self._client = client
        self._clock = clock
        self._wanted_hash = account_hash
        self._wanted_last4 = account_last4
        self._hash: str | None = None

    # ----------------------------------------------------------- account choice

    async def _account_hash(self) -> str:
        """Resolve which account to use, once. Schwab's API addresses accounts by a hash."""
        if self._hash is not None:
            return self._hash
        try:
            accounts = await self._client.account_numbers()
        except SchwabUnavailable as exc:
            raise BrokerUnavailable(str(exc)) from None
        except SchwabError as exc:
            raise BrokerError(str(exc)) from None
        masked = ", ".join(f"...{a.number[-4:]}" for a in accounts) or "none"
        if self._wanted_hash is not None:
            matches = [a for a in accounts if a.hash == self._wanted_hash]
            if not matches:
                raise BrokerError(
                    f"the configured account hash is not one of the linked accounts ({masked})"
                )
        elif self._wanted_last4 is not None:
            matches = [a for a in accounts if a.number.endswith(self._wanted_last4)]
            if len(matches) != 1:
                raise BrokerError(
                    f"{len(matches)} linked accounts end in {self._wanted_last4}; "
                    f"linked accounts: {masked}"
                )
        elif len(accounts) == 1:
            matches = accounts
        else:
            raise BrokerError(
                f"this login has {len(accounts)} accounts ({masked}); "
                "set account_last4 to choose one"
            )
        self._hash = matches[0].hash
        log.info("using Schwab account ...%s", matches[0].number[-4:])
        return self._hash

    async def describe_account(self) -> str:
        """The chosen account, masked, for display."""
        await self._account_hash()
        accounts = await self._client.account_numbers()
        chosen = next(a for a in accounts if a.hash == self._hash)
        return f"...{chosen.number[-4:]}"

    # --------------------------------------------------------------- interface

    async def get_account(self) -> AccountSnapshot:
        account_hash = await self._account_hash()
        try:
            raw = await self._client.account(account_hash)
            return parse_account(raw, self._clock.now())
        except SchwabUnavailable as exc:
            raise BrokerUnavailable(str(exc)) from None
        except (SchwabError, ParseError) as exc:
            raise BrokerError(str(exc)) from None

    async def get_open_orders(self) -> list[BrokerOrder]:
        now = self._clock.now()
        orders = await self._orders(now - self.OPEN_ORDER_LOOKBACK, now + self.SEARCH_AHEAD)
        return [order for order in orders if not order.status.is_terminal]

    async def place(self, request: OrderRequest) -> str | None:
        try:
            payload = build_equity_order(request)
        except ValueError as exc:
            raise OrderRejected(f"not sent: {exc}") from None
        account_hash = await self._account_hash()  # failures here mean nothing was sent
        try:
            return await self._client.place_order(account_hash, payload)
        except SchwabRejected as exc:
            raise OrderRejected(str(exc)) from None
        except SchwabError as exc:
            if not exc.sent:
                raise BrokerUnavailable(str(exc)) from None
            raise AmbiguousOrder(str(exc)) from None

    async def get_order(self, order_id: str) -> BrokerOrder:
        account_hash = await self._account_hash()
        try:
            return parse_order(await self._client.order(account_hash, order_id))
        except SchwabUnavailable as exc:
            raise BrokerUnavailable(str(exc)) from None
        except (SchwabError, ParseError) as exc:
            raise BrokerError(str(exc)) from None

    async def cancel(self, order_id: str) -> None:
        account_hash = await self._account_hash()
        try:
            await self._client.cancel_order(account_hash, order_id)
        except SchwabUnavailable as exc:
            raise BrokerUnavailable(str(exc)) from None
        except SchwabRejected as exc:
            # Schwab refuses to cancel an order that has already finished. That is fine.
            if (await self.get_order(order_id)).status.is_terminal:
                return
            raise BrokerError(str(exc)) from None
        except SchwabError as exc:
            raise BrokerError(str(exc)) from None

    async def find_order(
        self, symbol: str, side: Side, quantity: int, since: datetime
    ) -> BrokerOrder | None:
        now = self._clock.now()
        matches = [
            order
            for order in await self._orders(since, now + self.SEARCH_AHEAD)
            if order.symbol == symbol
            and order.side is side
            and order.quantity == quantity
            and (order.entered_at is None or order.entered_at >= since)
        ]
        if not matches:
            return None
        return max(matches, key=lambda order: order.entered_at or since)

    # ------------------------------------------------------------------ helpers

    async def _orders(self, start: datetime, end: datetime) -> list[BrokerOrder]:
        account_hash = await self._account_hash()
        try:
            raw = await self._client.orders(account_hash, start, end)
            return [parse_order(item) for item in raw]
        except SchwabUnavailable as exc:
            raise BrokerUnavailable(str(exc)) from None
        except (SchwabError, ParseError) as exc:
            raise BrokerError(str(exc)) from None
