"""Where research lives: one table, written by the research jobs, read by the bot.

    RUN#<run_id>  / META                       how the run went (status, cost, ...)
    DAY#<date>    / PICK#<run_id>#<rank:03d>   one ranked pick
    DAY#<date>    / POSTURE#<iso time>         the day's posture as of that time

A run's META is written last, so a run that says ``ok`` has all its picks in place.
Anything that does not parse is skipped and counted, never trusted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from traider.research.models import Pick, Posture, RunMeta


@dataclass(frozen=True, slots=True)
class DayResearch:
    day: str
    picks: tuple[Pick, ...] = ()
    postures: tuple[Posture, ...] = ()
    runs: Mapping[str, RunMeta] = field(default_factory=dict)
    invalid: int = 0
    # Posture items that did not parse. Counted apart so the reader can refuse the day's posture.
    invalid_postures: int = 0


class ResearchStore(Protocol):
    async def write_run(
        self, meta: RunMeta, picks: Sequence[Pick], posture: Posture | None
    ) -> None: ...

    async def day(self, day: str) -> DayResearch: ...


def _check_run(meta: RunMeta, picks: Sequence[Pick], posture: Posture | None) -> None:
    """Refuse a run whose parts do not belong together, before anything is written."""
    for p in picks:
        if p.run_id != meta.run_id:
            raise ValueError(f"pick {p.symbol} belongs to run {p.run_id!r}, not {meta.run_id!r}")
    if posture is not None and posture.run_id != meta.run_id:
        raise ValueError(f"posture belongs to run {posture.run_id!r}, not {meta.run_id!r}")
    ranks = [p.rank for p in picks]
    if len(set(ranks)) != len(ranks):
        raise ValueError("duplicate pick ranks in one run")


def _items_for(
    meta: RunMeta, picks: Sequence[Pick], posture: Posture | None
) -> list[dict[str, Any]]:
    _check_run(meta, picks, posture)
    day = meta.trading_day.isoformat()
    items: list[dict[str, Any]] = [
        {
            "pk": f"DAY#{day}",
            "sk": f"PICK#{p.run_id}#{p.rank:03d}",
            "gsi1pk": f"SYM#{p.symbol}",
            "gsi1sk": f"{day}#{p.run_id}",
            "body": json.dumps(p.model_dump(mode="json")),
        }
        for p in picks
    ]
    if posture is not None:
        items.append(
            {
                "pk": f"DAY#{day}",
                "sk": f"POSTURE#{posture.at.isoformat()}",
                "body": json.dumps(posture.model_dump(mode="json")),
            }
        )
    items.append(
        {
            "pk": f"RUN#{meta.run_id}",
            "sk": "META",
            "body": json.dumps(meta.model_dump(mode="json")),
        }
    )  # last, on purpose
    return items


def _parse_day(
    day: str,
    items: Sequence[Mapping[str, Any]],
    metas: Mapping[str, Mapping[str, Any] | None],
) -> DayResearch:
    picks: list[Pick] = []
    postures: list[Posture] = []
    invalid = 0
    invalid_postures = 0
    for item in sorted(items, key=lambda i: str(i["sk"])):
        sk = str(item["sk"])
        try:
            body = json.loads(str(item["body"]))
            if sk.startswith("PICK#"):
                picks.append(Pick.model_validate(body))
            elif sk.startswith("POSTURE#"):
                postures.append(Posture.model_validate(body))
        except Exception:
            invalid += 1
            if sk.startswith("POSTURE#"):
                invalid_postures += 1
    runs: dict[str, RunMeta] = {}
    for run_id, meta_item in metas.items():
        if meta_item is None:
            continue
        try:
            runs[run_id] = RunMeta.model_validate(json.loads(str(meta_item["body"])))
        except Exception:
            invalid += 1
    return DayResearch(day, tuple(picks), tuple(postures), runs, invalid, invalid_postures)


def _run_ids(items: Sequence[Mapping[str, Any]]) -> set[str]:
    ids = set()
    for item in items:
        sk = str(item["sk"])
        if sk.startswith("PICK#"):
            # PICK#<run_id>#<rank>: the rank never contains '#', the run id might.
            ids.add(sk.removeprefix("PICK#").rsplit("#", 1)[0])
        elif sk.startswith("POSTURE#"):
            # A posture that does not parse names no run; the day read counts it as invalid.
            with contextlib.suppress(Exception):
                ids.add(str(json.loads(str(item["body"]))["run_id"]))
    return ids


class MemoryResearchStore:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}

    def put_raw(self, pk: str, sk: str, body: str) -> None:
        self._items[(pk, sk)] = {"pk": pk, "sk": sk, "body": body}

    async def write_run(
        self, meta: RunMeta, picks: Sequence[Pick], posture: Posture | None
    ) -> None:
        for item in _items_for(meta, picks, posture):
            self._items[(item["pk"], item["sk"])] = item

    async def day(self, day: str) -> DayResearch:
        items = [item for (pk, _), item in self._items.items() if pk == f"DAY#{day}"]
        metas = {run_id: self._items.get((f"RUN#{run_id}", "META")) for run_id in _run_ids(items)}
        return _parse_day(day, items, metas)


class DynamoResearchStore:
    def __init__(self, table: Any) -> None:
        self._table = table

    async def _call[T](self, fn: Callable[..., T], **kwargs: Any) -> T:
        return await asyncio.to_thread(fn, **kwargs)

    async def write_run(
        self, meta: RunMeta, picks: Sequence[Pick], posture: Posture | None
    ) -> None:
        for item in _items_for(meta, picks, posture):
            await self._call(self._table.put_item, Item=item)

    async def day(self, day: str) -> DayResearch:
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "pk = :pk",
            "ExpressionAttributeValues": {":pk": f"DAY#{day}"},
            "ConsistentRead": True,
        }
        while True:
            response = await self._call(self._table.query, **kwargs)
            items.extend(response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        metas: dict[str, Mapping[str, Any] | None] = {}
        for run_id in _run_ids(items):
            response = await self._call(
                self._table.get_item,
                Key={"pk": f"RUN#{run_id}", "sk": "META"},
                ConsistentRead=True,
            )
            metas[run_id] = response.get("Item")
        return _parse_day(day, items, metas)
