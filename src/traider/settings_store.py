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
from typing import Any, Literal, Protocol

from botocore.exceptions import ClientError
from pydantic import ValidationError

from traider.settings import Settings, merge_live, restart_changes, settings_diff

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


class SettingsSuperseded(SettingsConflict):
    """The write succeeded, but a later version was written at the same moment, so it is
    that one, not this one, that is current."""

    def __init__(self, written: int, newest: int) -> None:
        super().__init__(
            f"version {written} was written, but version {newest} was written at the same time"
        )
        self.written = written
        self.newest = newest


class SettingsInvalid(Exception):
    """A stored version does not describe valid settings."""

    def __init__(self, version: int, problem: str) -> None:
        super().__init__(f"settings version {version} is invalid: {problem}")
        self.version = version


class SettingsStore(Protocol):
    async def latest(self) -> SettingsVersion | None: ...

    async def get(self, version: int) -> SettingsVersion | None: ...

    async def write(
        self, settings: Settings, *, expected_version: int, author: str, note: str, now: datetime
    ) -> SettingsVersion: ...

    async def history(self, limit: int = 20) -> list[SettingsVersion]: ...


def _version_of(item: dict[str, Any]) -> int:
    """The item's version number, from ``version`` or else its sort key; -1 if neither parses.

    Never raises, so a damaged item can still be reported as invalid under some number.
    """
    try:
        return int(item["version"])
    except (KeyError, TypeError, ValueError):
        pass
    try:
        return int(str(item.get("sk", "")).removeprefix("V#"))
    except ValueError:
        return -1


def _problem(exc: Exception) -> str:
    """One readable line: the first field error for a ValidationError, else the exception."""
    if isinstance(exc, ValidationError):
        error = exc.errors(include_url=False)[0]
        where = ".".join(str(part) for part in error["loc"])
        return f"{where}: {error['msg']}" if where else str(error["msg"])
    return f"{type(exc).__name__}: {exc}"


def _parse(item: dict[str, Any]) -> SettingsVersion:
    version = _version_of(item)
    try:
        diff = json.loads(item.get("diff") or "{}")
        if not isinstance(diff, dict):
            raise ValueError("diff is not an object")
        # A strategy constructor may raise anything (KeyError, TypeError, ...) on bad
        # params, and a damaged timestamp or diff can fail in several ways. A stored
        # version must never crash the reader, so every failure is reported as invalid.
        return SettingsVersion(
            version=version,
            settings=Settings.model_validate(json.loads(item["body"])),
            author=str(item.get("author", "")),
            at=datetime.fromisoformat(str(item["at"])),
            note=str(item.get("note", "")),
            diff=diff,
        )
    except Exception as exc:
        raise SettingsInvalid(version, _problem(exc)) from None


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


def _check_expected(expected_version: int, *, latest: int) -> None:
    """Refuse a write based on a version that is not a real one."""
    if expected_version < 0:
        raise ValueError(f"expected_version must be 0 or more, got {expected_version}")
    if expected_version > latest:
        raise SettingsConflict(
            f"expected_version {expected_version} is ahead of the latest version {latest}"
        )


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

    async def get(self, version: int) -> SettingsVersion | None:
        item = self._items.get(version)
        return _parse(item) if item is not None else None

    async def write(
        self, settings: Settings, *, expected_version: int, author: str, note: str, now: datetime
    ) -> SettingsVersion:
        _check_expected(expected_version, latest=max(self._items, default=0))
        version = expected_version + 1
        if version in self._items or (self._items and max(self._items) > expected_version):
            raise SettingsConflict(f"version {version} already exists")
        diff: dict[str, Any] = {}
        previous = self._items.get(expected_version)
        if previous is not None:
            try:
                diff = settings_diff(_parse(previous).settings, settings)
            except SettingsInvalid:
                diff = {}
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

    async def get(self, version: int) -> SettingsVersion | None:
        response = await self._call(
            self._table.get_item, Key={"pk": PK, "sk": _sk(version)}, ConsistentRead=True
        )
        item = response.get("Item")
        return _parse(item) if item is not None else None

    async def write(
        self, settings: Settings, *, expected_version: int, author: str, note: str, now: datetime
    ) -> SettingsVersion:
        newest = await self._newest(1)
        _check_expected(expected_version, latest=_version_of(newest[0]) if newest else 0)
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
        try:
            latest = await self.latest()
        except SettingsInvalid as exc:
            # A later version landed between our put and this read, and it is unreadable.
            raise SettingsSuperseded(version, exc.version) from None
        if latest is None:
            raise SettingsConflict(f"version {version} was written but cannot be read back")
        if latest.version != version:
            # Someone wrote a later version between our put and this read. Ours stands in
            # history, but it is not current: tell the caller.
            raise SettingsSuperseded(version, latest.version)
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


@dataclass(frozen=True, slots=True)
class SettingsUpdate:
    kind: Literal["applied", "pending_restart", "rejected", "unreadable"]
    version: int | None
    detail: str
    diff: dict[str, list[Any]] = field(default_factory=dict)


class LiveSettings:
    """The settings a running bot uses, kept in step with the store.

    ``loaded`` is False until a version has been read successfully in this process;
    until then the engine allows no entries. After that, a read error or an invalid
    version leaves the last good settings in force.

    Caller contract: restart-only fields stay whatever ``current`` held when the process
    built its strategy, feed and engine, including the fallback after an unreadable
    start. ``refresh`` never changes them; it reports them as ``pending_restart``.
    """

    def __init__(self, store: SettingsStore, fallback: Settings) -> None:
        self._store = store
        self.current = fallback
        self.version: int | None = None
        self.loaded = False
        #: The newest stored settings, as written, while they differ from the running ones in
        #: a restart-only field; None otherwise. The engine reads it to say what a restart
        #: would change.
        self.pending: Settings | None = None
        self._rejected: set[int] = set()
        self.start_updates: list[SettingsUpdate] = []

    async def _bootstrap(self, now: datetime) -> SettingsVersion | None:
        """Write the fallback as version 1 of an empty store. Losing the race to another
        writer is fine: theirs is read instead."""
        try:
            return await self._store.write(
                self.current,
                expected_version=0,
                author="bootstrap",
                note="seeded from the environment",
                now=now,
            )
        except SettingsConflict:
            return await self._store.latest()

    async def start(self, now: datetime) -> list[SettingsUpdate]:
        """Load the settings once. What it returns is also kept in ``start_updates``, so
        the engine can report problems found before it existed."""
        self.start_updates = await self._start(now)
        return self.start_updates

    async def _start(self, now: datetime) -> list[SettingsUpdate]:
        try:
            latest = await self._store.latest()
            if latest is None:
                latest = await self._bootstrap(now)
        except SettingsInvalid as exc:
            self._rejected.add(exc.version)
            return [SettingsUpdate("rejected", exc.version, str(exc))]
        except Exception as exc:
            return [SettingsUpdate("unreadable", None, f"{type(exc).__name__}: {exc}")]
        if latest is None:
            return [SettingsUpdate("unreadable", None, "no settings version after bootstrap")]
        self.current, self.version, self.loaded = latest.settings, latest.version, True
        return []

    async def refresh(self, now: datetime) -> list[SettingsUpdate]:
        try:
            latest = await self._store.latest()
            if latest is None and not self.loaded:
                # The store could not be read at start-up and is empty now: seed it.
                latest = await self._bootstrap(now)
        except SettingsInvalid as exc:
            if exc.version in self._rejected:
                return []
            self._rejected.add(exc.version)
            return [SettingsUpdate("rejected", exc.version, str(exc))]
        except Exception as exc:
            return [SettingsUpdate("unreadable", self.version, f"{type(exc).__name__}: {exc}")]
        if latest is None:
            if not self.loaded:
                # Nothing readable and nothing to seed from: still no settings in force.
                return [SettingsUpdate("unreadable", None, "no settings version after bootstrap")]
            return []
        if latest.version == self.version:
            return []
        if latest.version in self._rejected:
            return []
        # Restart-only fields keep the values the running objects were built from, even
        # when the process loaded late: start() may have built them from the fallback.
        # The merge is a mix of two validated bodies, so validate the mix too: whatever
        # goes wrong, the version is rejected and the last good settings stay in force.
        try:
            merged = merge_live(self.current, latest.settings)
            merged = Settings.model_validate(merged.model_dump(mode="json"))
            restart = restart_changes(self.current, latest.settings)
            diff = settings_diff(self.current, merged)
        except Exception as exc:
            self._rejected.add(latest.version)
            detail = f"settings version {latest.version} cannot be applied: {_problem(exc)}"
            return [SettingsUpdate("rejected", latest.version, detail)]
        self.current, self.version, self.loaded = merged, latest.version, True
        self.pending = latest.settings if restart else None
        out = [SettingsUpdate("applied", latest.version, latest.author, diff)]
        if restart:
            out.append(SettingsUpdate("pending_restart", latest.version, ", ".join(restart)))
        return out
