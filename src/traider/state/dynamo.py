"""State in a single DynamoDB table (partition key ``pk``, sort key ``sk``).

Item layout, where ``ns`` is the trading mode so paper and live never mix::

    LEASE#ns        / bot            who may trade, and until when
    DAY#ns#<date>   / STATE          opening equity, order count, loss halt, sale proceeds
    OPEN#ns         / <order id>     orders this bot placed that are not finished
    LOG#ns#<date>   / <ms>#<id>      append-only audit log
    PAPER#ns        / ACCOUNT        the paper-trading account
    POS#ns          / <symbol>       positions this bot opened (the ledger)

Counters use atomic updates and the lease uses a conditional write, so two
instances cannot both believe they hold it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError

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


def _ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


class DynamoStateStore:
    def __init__(self, table: Any, namespace: str) -> None:
        self._table = table
        self._ns = namespace

    async def _call[T](self, fn: Callable[..., T], **kwargs: Any) -> T:
        return await asyncio.to_thread(fn, **kwargs)

    # -- lease ----------------------------------------------------------------

    @property
    def _lease_key(self) -> dict[str, str]:
        return {"pk": f"LEASE#{self._ns}", "sk": "bot"}

    async def acquire_lease(self, owner: str, ttl_s: float, now: datetime) -> bool:
        try:
            await self._call(
                self._table.put_item,
                Item={
                    **self._lease_key,
                    "owner": owner,
                    "expires_at": _ms(now) + int(ttl_s * 1000),
                },
                ConditionExpression="attribute_not_exists(pk) OR expires_at < :now OR #owner = :me",
                ExpressionAttributeNames={"#owner": "owner"},
                ExpressionAttributeValues={":now": _ms(now), ":me": owner},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    async def release_lease(self, owner: str) -> None:
        try:
            await self._call(
                self._table.delete_item,
                Key=self._lease_key,
                ConditionExpression="#owner = :me",
                ExpressionAttributeNames={"#owner": "owner"},
                ExpressionAttributeValues={":me": owner},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    # -- per-day counters -----------------------------------------------------

    def _day_key(self, day: str) -> dict[str, str]:
        return {"pk": f"DAY#{self._ns}#{day}", "sk": "STATE"}

    async def get_day(self, day: str) -> DayState:
        response = await self._call(
            self._table.get_item, Key=self._day_key(day), ConsistentRead=True
        )
        item = response.get("Item") or {}
        equity = item.get("start_equity")
        return DayState(
            day=day,
            start_equity=None if equity is None else Decimal(str(equity)),
            orders=int(item.get("orders", 0)),
            halted_reason=item.get("halted_reason"),
            sold_usd=Decimal(str(item.get("sold_usd", 0))),
        )

    async def init_start_equity(self, day: str, equity: Decimal) -> Decimal:
        response = await self._call(
            self._table.update_item,
            Key=self._day_key(day),
            UpdateExpression="SET start_equity = if_not_exists(start_equity, :equity)",
            ExpressionAttributeValues={":equity": str(equity)},
            ReturnValues="ALL_NEW",
        )
        return Decimal(str(response["Attributes"]["start_equity"]))

    async def incr_orders(self, day: str) -> int:
        response = await self._call(
            self._table.update_item,
            Key=self._day_key(day),
            UpdateExpression="ADD orders :one",
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        return int(response["Attributes"]["orders"])

    async def halt_day(self, day: str, reason: str) -> None:
        await self._call(
            self._table.update_item,
            Key=self._day_key(day),
            UpdateExpression="SET halted_reason = if_not_exists(halted_reason, :reason)",
            ExpressionAttributeValues={":reason": reason},
        )

    async def add_sold(self, day: str, amount: Decimal) -> Decimal:
        response = await self._call(
            self._table.update_item,
            Key=self._day_key(day),
            UpdateExpression="ADD sold_usd :amount",
            ExpressionAttributeValues={":amount": amount},
            ReturnValues="UPDATED_NEW",
        )
        return Decimal(str(response["Attributes"]["sold_usd"]))

    # -- open orders ----------------------------------------------------------

    async def save_order(self, record: OrderRecord) -> None:
        key = {"pk": f"OPEN#{self._ns}", "sk": record.order_id}
        if record.status.is_terminal:
            await self._call(self._table.delete_item, Key=key)
        else:
            await self._call(
                self._table.put_item, Item={**key, "body": json.dumps(order_to_dict(record))}
            )

    async def open_orders(self) -> list[OrderRecord]:
        items = await self._query(f"OPEN#{self._ns}")
        return [order_from_dict(json.loads(item["body"])) for item in items]

    # -- audit log ------------------------------------------------------------

    async def log_event(self, kind: str, data: Mapping[str, Any], now: datetime) -> None:
        day = trading_date(now).isoformat()
        await self._call(
            self._table.put_item,
            Item={
                "pk": f"LOG#{self._ns}#{day}",
                "sk": f"{_ms(now):013d}#{uuid.uuid4().hex[:8]}",
                "kind": kind,
                "at": now.isoformat(),
                "body": json.dumps(jsonable(data)),
            },
        )

    async def events(self, day: str) -> list[dict[str, Any]]:
        items = await self._query(f"LOG#{self._ns}#{day}")
        return [
            {"kind": item["kind"], "at": item["at"], "data": json.loads(item["body"])}
            for item in items
        ]

    # -- paper account --------------------------------------------------------

    @property
    def _paper_key(self) -> dict[str, str]:
        return {"pk": f"PAPER#{self._ns}", "sk": "ACCOUNT"}

    async def load_paper(self) -> dict[str, Any] | None:
        response = await self._call(self._table.get_item, Key=self._paper_key, ConsistentRead=True)
        item = response.get("Item")
        if not item:
            return None
        data: dict[str, Any] = json.loads(item["body"])
        return data

    async def save_paper(self, data: dict[str, Any]) -> None:
        await self._call(
            self._table.put_item, Item={**self._paper_key, "body": json.dumps(jsonable(data))}
        )

    # -- position ledger ------------------------------------------------------

    async def ledger(self) -> dict[str, LedgerEntry]:
        items = await self._query(f"POS#{self._ns}")
        entries = [ledger_from_dict(json.loads(item["body"])) for item in items]
        return {entry.symbol: entry for entry in entries}

    async def put_ledger(self, entry: LedgerEntry) -> None:
        await self._call(
            self._table.put_item,
            Item={
                "pk": f"POS#{self._ns}",
                "sk": entry.symbol,
                "body": json.dumps(ledger_to_dict(entry)),
            },
        )

    async def delete_ledger(self, symbol: str) -> None:
        await self._call(self._table.delete_item, Key={"pk": f"POS#{self._ns}", "sk": symbol})

    # -- helpers --------------------------------------------------------------

    async def _query(self, pk: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "pk = :pk",
            "ExpressionAttributeValues": {":pk": pk},
            "ConsistentRead": True,
        }
        while True:
            response = await self._call(self._table.query, **kwargs)
            items.extend(response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                return items
            kwargs["ExclusiveStartKey"] = last
