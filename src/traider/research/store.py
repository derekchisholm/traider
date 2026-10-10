"""Where research lives: one table, written by the research jobs, read by the bot.

    RUN#<run_id>  / META                       how the run went (status, cost, ...)
    DAY#<date>    / PICK#<run_id>#<rank:03d>   one ranked pick
    DAY#<date>    / POSTURE#<iso time>         the day's posture as of that time
    COST#<date>   / TOTAL                      what the research jobs spent that day
    LOCK#<name>   / LOCK                       one research run of a kind at a time
    PICK#<run_id>#<rank:03d> / OUTCOME         how that pick did (the scorecard)
    SCORE#<date>  / SUMMARY                    the scorecard's summary for that day

META items carry ``gsi1pk = RUNDAY#<date>`` and ``gsi1sk = <kind>#<run_id>`` so the jobs
can find a day's runs. The bot never queries the index.

A run's META is written last, so a run that says ``ok`` has all its picks in place.
Anything that does not parse is skipped and counted, never trusted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from botocore.exceptions import ClientError

from traider.research.models import (
    Pick,
    PickOutcome,
    Posture,
    RunKind,
    RunMeta,
    ScoreSummary,
)
from traider.timeutil import weekdays_from

log = logging.getLogger(__name__)


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


class ResearchWriter(Protocol):
    """What a research job needs from the table. The bot uses ``ResearchStore`` only."""

    async def put_meta(self, meta: RunMeta) -> None: ...

    async def write_run(
        self, meta: RunMeta, picks: Sequence[Pick], posture: Posture | None
    ) -> None: ...

    async def runs_for_day(self, day: str, kind: RunKind) -> list[RunMeta]:
        """GSI reads are eventually consistent: the lock, not this, guards against double runs."""
        ...

    async def add_day_cost(self, day: str, usd: Decimal) -> Decimal: ...

    async def day_cost(self, day: str) -> Decimal: ...

    async def acquire_lock(self, name: str, owner: str, ttl_s: float, now: datetime) -> bool: ...

    async def release_lock(self, name: str, owner: str) -> None: ...

    async def day(self, day: str) -> DayResearch: ...

    async def picks_between(self, start: date, end: date) -> list[DayResearch]:
        """Each weekday's research from ``start`` to ``end``, both included, oldest first."""
        ...

    async def outcomes(self, keys: Sequence[str]) -> dict[str, PickOutcome]:
        """The stored outcomes for these ``PICK#...`` keys. Missing or unreadable: absent."""
        ...

    async def put_outcome(self, outcome: PickOutcome) -> None: ...

    async def put_score_summary(self, day: str, summary: ScoreSummary) -> None: ...


BATCH_GET_MAX = 100  # DynamoDB's limit per BatchGetItem request
BATCH_GET_ROUNDS = 5  # unprocessed keys are asked for again at most this often
BATCH_GET_BACKOFF_S = 0.1  # doubled before each new ask


def _outcome_item(outcome: PickOutcome) -> dict[str, Any]:
    return {
        "pk": outcome.key,
        "sk": "OUTCOME",
        "body": json.dumps(outcome.model_dump(mode="json")),
    }


def _summary_item(day: str, summary: ScoreSummary) -> dict[str, Any]:
    if summary.day.isoformat() != day:
        raise ValueError(f"a summary for {summary.day} cannot be stored under {day}")
    return {
        "pk": f"SCORE#{day}",
        "sk": "SUMMARY",
        "body": json.dumps(summary.model_dump(mode="json")),
    }


def _parse_outcomes(items: Sequence[Mapping[str, Any]]) -> dict[str, PickOutcome]:
    found: dict[str, PickOutcome] = {}
    for item in items:
        try:
            outcome = PickOutcome.model_validate(json.loads(str(item["body"])))
        except Exception:
            log.warning("skipping an unreadable pick outcome %s", item.get("pk"))
            continue
        if outcome.key == item.get("pk"):  # an outcome stored under another pick's key: skip
            found[outcome.key] = outcome
    return found


def _meta_item(meta: RunMeta) -> dict[str, Any]:
    return {
        "pk": f"RUN#{meta.run_id}",
        "sk": "META",
        "gsi1pk": f"RUNDAY#{meta.trading_day.isoformat()}",
        "gsi1sk": f"{meta.kind}#{meta.run_id}",
        "body": json.dumps(meta.model_dump(mode="json")),
    }


def _parse_metas(items: Sequence[Mapping[str, Any]]) -> list[RunMeta]:
    metas: list[RunMeta] = []
    for item in items:
        try:
            metas.append(RunMeta.model_validate(json.loads(str(item["body"]))))
        except Exception:
            log.warning("skipping an unreadable run META %s", item.get("pk"))
    return sorted(metas, key=lambda m: m.run_id)


def _check_cost(usd: Decimal) -> None:
    if not usd.is_finite() or usd < 0:
        raise ValueError(f"a day's cost can only grow, not by {usd}")


def _check_lock(name: str, owner: str, ttl_s: float, now: datetime) -> None:
    if not name or not owner:
        raise ValueError("a lock needs a name and an owner")
    if ttl_s <= 0:
        raise ValueError("a lock needs a positive lifetime")
    if now.tzinfo is None:
        raise ValueError("now must carry a timezone")


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
    items.append(_meta_item(meta))  # last, on purpose
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

    def raw(self, pk: str, sk: str) -> dict[str, Any] | None:
        """One stored item, for tests."""
        return self._items.get((pk, sk))

    @property
    def keys(self) -> set[tuple[str, str]]:
        return set(self._items)

    async def put_meta(self, meta: RunMeta) -> None:
        item = _meta_item(meta)
        self._items[(item["pk"], item["sk"])] = item

    async def runs_for_day(self, day: str, kind: RunKind) -> list[RunMeta]:
        prefix = f"{kind}#"
        return _parse_metas(
            [
                item
                for item in self._items.values()
                if item.get("gsi1pk") == f"RUNDAY#{day}"
                and str(item.get("gsi1sk", "")).startswith(prefix)
            ]
        )

    async def add_day_cost(self, day: str, usd: Decimal) -> Decimal:
        _check_cost(usd)
        item = self._items.setdefault((f"COST#{day}", "TOTAL"), {"usd": Decimal(0)})
        item["usd"] += usd
        return Decimal(item["usd"])

    async def day_cost(self, day: str) -> Decimal:
        item = self._items.get((f"COST#{day}", "TOTAL"))
        return Decimal(0) if item is None else Decimal(item["usd"])

    async def acquire_lock(self, name: str, owner: str, ttl_s: float, now: datetime) -> bool:
        _check_lock(name, owner, ttl_s, now)
        key = (f"LOCK#{name}", "LOCK")
        held = self._items.get(key)
        if held is not None and held["expires_at"] > int(now.timestamp()):
            return False
        self._items[key] = {"owner": owner, "expires_at": int(now.timestamp() + ttl_s)}
        return True

    async def release_lock(self, name: str, owner: str) -> None:
        key = (f"LOCK#{name}", "LOCK")
        held = self._items.get(key)
        if held is not None and held["owner"] == owner:
            del self._items[key]

    async def write_run(
        self, meta: RunMeta, picks: Sequence[Pick], posture: Posture | None
    ) -> None:
        for item in _items_for(meta, picks, posture):
            self._items[(item["pk"], item["sk"])] = item

    async def day(self, day: str) -> DayResearch:
        items = [item for (pk, _), item in self._items.items() if pk == f"DAY#{day}"]
        metas = {run_id: self._items.get((f"RUN#{run_id}", "META")) for run_id in _run_ids(items)}
        return _parse_day(day, items, metas)

    async def picks_between(self, start: date, end: date) -> list[DayResearch]:
        return [await self.day(d.isoformat()) for d in weekdays_from(start, end)]

    async def outcomes(self, keys: Sequence[str]) -> dict[str, PickOutcome]:
        items = [item for key in dict.fromkeys(keys) if (item := self._items.get((key, "OUTCOME")))]
        return _parse_outcomes(items)

    async def put_outcome(self, outcome: PickOutcome) -> None:
        item = _outcome_item(outcome)
        self._items[(item["pk"], item["sk"])] = item

    async def put_score_summary(self, day: str, summary: ScoreSummary) -> None:
        item = _summary_item(day, summary)
        self._items[(item["pk"], item["sk"])] = item


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

    async def put_meta(self, meta: RunMeta) -> None:
        await self._call(self._table.put_item, Item=_meta_item(meta))

    async def runs_for_day(self, day: str, kind: RunKind) -> list[RunMeta]:
        """GSI reads are eventually consistent: the lock, not this, guards against double runs."""
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "IndexName": "gsi1",
            "KeyConditionExpression": "gsi1pk = :pk AND begins_with(gsi1sk, :kind)",
            "ExpressionAttributeValues": {":pk": f"RUNDAY#{day}", ":kind": f"{kind}#"},
        }
        while True:
            response = await self._call(self._table.query, **kwargs)
            items.extend(response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        return _parse_metas(items)

    async def add_day_cost(self, day: str, usd: Decimal) -> Decimal:
        _check_cost(usd)
        response = await self._call(
            self._table.update_item,
            Key={"pk": f"COST#{day}", "sk": "TOTAL"},
            UpdateExpression="ADD usd :usd",
            ExpressionAttributeValues={":usd": usd},
            ReturnValues="UPDATED_NEW",
        )
        return Decimal(response["Attributes"]["usd"])

    async def day_cost(self, day: str) -> Decimal:
        response = await self._call(
            self._table.get_item,
            Key={"pk": f"COST#{day}", "sk": "TOTAL"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return Decimal(0) if item is None else Decimal(item["usd"])

    async def acquire_lock(self, name: str, owner: str, ttl_s: float, now: datetime) -> bool:
        """A conditional put: free when there is no lock or the one there has expired."""
        _check_lock(name, owner, ttl_s, now)
        try:
            await self._call(
                self._table.put_item,
                Item={
                    "pk": f"LOCK#{name}",
                    "sk": "LOCK",
                    "owner": owner,
                    "expires_at": int(now.timestamp() + ttl_s),
                },
                ConditionExpression="attribute_not_exists(pk) OR #expires <= :now",
                ExpressionAttributeNames={"#expires": "expires_at"},
                ExpressionAttributeValues={":now": int(now.timestamp())},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise
        return True

    async def release_lock(self, name: str, owner: str) -> None:
        """Delete the lock if it is still ours. Someone else's is left alone."""
        try:
            await self._call(
                self._table.delete_item,
                Key={"pk": f"LOCK#{name}", "sk": "LOCK"},
                ConditionExpression="#owner = :owner",
                ExpressionAttributeNames={"#owner": "owner"},
                ExpressionAttributeValues={":owner": owner},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            log.warning("research lock %s was no longer ours to release", name)

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

    async def picks_between(self, start: date, end: date) -> list[DayResearch]:
        return [await self.day(d.isoformat()) for d in weekdays_from(start, end)]

    async def outcomes(self, keys: Sequence[str]) -> dict[str, PickOutcome]:
        """BatchGetItem, 100 keys a request, consistent reads. The table's client takes and
        returns plain values. Keys DynamoDB leaves unprocessed are asked for again after a
        growing pause; any still missing after that are absent, so the scorecard scores
        those picks again (an overwrite, never a loss)."""
        unique = list(dict.fromkeys(keys))
        items: list[dict[str, Any]] = []
        client = self._table.meta.client
        name = self._table.name
        for start in range(0, len(unique), BATCH_GET_MAX):
            wanted: list[dict[str, Any]] = [
                {"pk": key, "sk": "OUTCOME"} for key in unique[start : start + BATCH_GET_MAX]
            ]
            for attempt in range(BATCH_GET_ROUNDS):
                if attempt:
                    await asyncio.sleep(BATCH_GET_BACKOFF_S * 2 ** (attempt - 1))
                response = await self._call(
                    client.batch_get_item,
                    RequestItems={name: {"Keys": wanted, "ConsistentRead": True}},
                )
                items.extend(response.get("Responses", {}).get(name, []))
                wanted = response.get("UnprocessedKeys", {}).get(name, {}).get("Keys", [])
                if not wanted:
                    break
            else:
                log.warning(
                    "%d pick outcome(s) stayed unprocessed; scoring them again", len(wanted)
                )
        return _parse_outcomes(items)

    async def put_outcome(self, outcome: PickOutcome) -> None:
        await self._call(self._table.put_item, Item=_outcome_item(outcome))

    async def put_score_summary(self, day: str, summary: ScoreSummary) -> None:
        await self._call(self._table.put_item, Item=_summary_item(day, summary))
