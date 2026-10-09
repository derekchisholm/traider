"""Where settings versions live, and the bot's live view of them.

Every change is a new, immutable, numbered version; the highest number is current.
A write names the version it was based on and is refused if anything was written
since (``SettingsConflict``), so two editors cannot overwrite each other unseen.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from botocore.exceptions import ClientError

from traider.settings import Settings, settings_diff

PK = "SETTINGS"


def _sk(version: int) -> str:
    return f"V#{version:09d}"


@dataclass(frozen=True, slots=True)
class SettingsVersion:
    version: int
    settings: Settings
    author: str
    at: datetime
    note: str = ""
    diff: dict[str, list[Any]] = field(default_factory=dict)


class SettingsConflict(Exception):
    """Another version was written since the one this write was based on."""


class SettingsInvalid(Exception):
    """A stored version does not describe valid settings."""

    def __init__(self, version: int, problem: str) -> None:
        super().__init__(f"settings version {version} is invalid: {problem}")
        self.version = version


class SettingsStore(Protocol):
    async def latest(self) -> SettingsVersion | None: ...

    async def write(
        self, settings: Settings, *, expected_version: int, author: str, note: str, now: datetime
    ) -> SettingsVersion: ...

    async def history(self, limit: int = 20) -> list[SettingsVersion]: ...


def _parse(item: dict[str, Any]) -> SettingsVersion:
    version = int(item["version"])
    try:
        settings = Settings.model_validate(json.loads(item["body"]))
    except Exception as exc:
        # A strategy constructor may raise anything (KeyError, TypeError, ...) on bad
        # params. A stored version must never crash the reader, so every failure counts.
        first_line = (str(exc).splitlines() or [""])[0]
        raise SettingsInvalid(version, first_line or type(exc).__name__) from None
    return SettingsVersion(
        version=version,
        settings=settings,
        author=str(item.get("author", "")),
        at=datetime.fromisoformat(str(item["at"])),
        note=str(item.get("note", "")),
        diff=json.loads(item.get("diff") or "{}"),
    )


def _item(
    settings: Settings,
    version: int,
    *,
    author: str,
    note: str,
    now: datetime,
    diff: dict[str, Any],
) -> dict[str, Any]:
    return {
        "pk": PK,
        "sk": _sk(version),
        "version": version,
        "author": author,
        "at": now.isoformat(),
        "note": note,
        "body": json.dumps(settings.model_dump(mode="json")),
        "diff": json.dumps(diff),
    }


class MemorySettingsStore:
    def __init__(self) -> None:
        self._items: dict[int, dict[str, Any]] = {}

    def put_raw(
        self, version: int, body: dict[str, Any], *, author: str = "test", at: str = ""
    ) -> None:
        """Store a body without checking it, the way a buggy writer might."""
        self._items[version] = {
            "version": version,
            "author": author,
            "at": at or "2026-10-09T13:00:00+00:00",
            "note": "",
            "body": json.dumps(body),
            "diff": "{}",
        }

    async def latest(self) -> SettingsVersion | None:
        if not self._items:
            return None
        return _parse(self._items[max(self._items)])

    async def write(
        self, settings: Settings, *, expected_version: int, author: str, note: str, now: datetime
    ) -> SettingsVersion:
        version = expected_version + 1
        if version in self._items or (self._items and max(self._items) > expected_version):
            raise SettingsConflict(f"version {version} already exists")
        previous = self._items.get(expected_version)
        diff = settings_diff(_parse(previous).settings, settings) if previous else {}
        self._items[version] = _item(
            settings, version, author=author, note=note, now=now, diff=diff
        )
        return _parse(self._items[version])

    async def history(self, limit: int = 20) -> list[SettingsVersion]:
        out: list[SettingsVersion] = []
        for version in sorted(self._items, reverse=True):
            try:
                out.append(_parse(self._items[version]))
            except SettingsInvalid:
                continue
            if len(out) >= limit:
                break
        return out


class DynamoSettingsStore:
    def __init__(self, table: Any) -> None:
        self._table = table

    async def _call[T](self, fn: Callable[..., T], **kwargs: Any) -> T:
        return await asyncio.to_thread(fn, **kwargs)

    async def _newest(self, limit: int) -> list[dict[str, Any]]:
        response = await self._call(
            self._table.query,
            KeyConditionExpression="pk = :pk AND begins_with(sk, :v)",
            ExpressionAttributeValues={":pk": PK, ":v": "V#"},
            ScanIndexForward=False,
            Limit=limit,
            ConsistentRead=True,
        )
        items: list[dict[str, Any]] = response.get("Items", [])
        return items

    async def latest(self) -> SettingsVersion | None:
        items = await self._newest(1)
        return _parse(items[0]) if items else None

    async def write(
        self, settings: Settings, *, expected_version: int, author: str, note: str, now: datetime
    ) -> SettingsVersion:
        version = expected_version + 1
        diff: dict[str, Any] = {}
        if expected_version > 0:
            response = await self._call(
                self._table.get_item,
                Key={"pk": PK, "sk": _sk(expected_version)},
                ConsistentRead=True,
            )
            previous = response.get("Item")
            if previous is not None:
                try:
                    diff = settings_diff(_parse(previous).settings, settings)
                except SettingsInvalid:
                    diff = {}
        item = _item(settings, version, author=author, note=note, now=now, diff=diff)
        try:
            await self._call(
                self._table.put_item,
                Item=item,
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise SettingsConflict(f"version {version} already exists") from None
            raise
        latest = await self.latest()
        if latest is None or latest.version != version:
            # Someone wrote a later version between our put and this read. Ours stands in
            # history, but it is not current: tell the caller.
            raise SettingsConflict(f"version {version} was superseded at once")
        return latest

    async def history(self, limit: int = 20) -> list[SettingsVersion]:
        out: list[SettingsVersion] = []
        for item in await self._newest(limit + 10):
            try:
                out.append(_parse(item))
            except SettingsInvalid:
                continue
            if len(out) >= limit:
                break
        return out
