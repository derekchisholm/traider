"""The research trail: what a run saw and decided, kept for reports and audits.

Per run, under ``runs/<day>/<run_id>/``: ``snapshot.json``, ``posture.json``,
``screen.json``, ``dives/<symbol>.json`` and ``result.json``. In S3 when the stack has a
trail bucket (private, encrypted by the bucket, expiring after 400 days); in a local
directory otherwise and for dry runs. A failed write fails the run.

A write never leaves the trail: names may not be empty, absolute, or hold ``.``, ``..``
or empty parts, and a local write also refuses to follow a symlink out of the directory.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel


class Trail(Protocol):
    @property
    def location(self) -> str:
        """Where this run's files are, for ``RunMeta.s3_prefix``."""
        ...

    async def put(self, name: str, data: Any) -> None: ...


def trail_prefix(day: date, run_id: str) -> str:
    return f"runs/{day.isoformat()}/{run_id}/"


def _key(key: Any) -> Any:
    """JSON object keys must be text; dates, Decimals and enums are common keys."""
    if isinstance(key, Enum):
        return str(key.value)
    if isinstance(key, date | datetime):
        return key.isoformat()
    if isinstance(key, Decimal):
        return str(key)
    return key


def _keyed(value: Any) -> Any:
    if isinstance(value, dict):
        return {_key(k): _keyed(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_keyed(v) for v in value]
    return value


def _default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _keyed(dataclasses.asdict(value))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, set | frozenset):
        try:
            return sorted(value)  # a set has no order; sorted output is reproducible
        except TypeError:
            return list(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"cannot store a {type(value).__name__} in the trail")


def to_json(data: Any) -> str:
    # allow_nan=False: NaN and Infinity are not JSON, so a non-finite float fails the write.
    return json.dumps(_keyed(data), default=_default, indent=1, allow_nan=False)


def _check_name(name: str) -> None:
    # Split on "/": an empty name, an absolute name ("/x") and "a//b" all give an empty part.
    parts = name.split("/")
    if "\\" in name or "\x00" in name or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"not a trail file name: {name!r}")


class S3Trail:
    def __init__(self, client: Any, bucket: str, prefix: str) -> None:
        self._client = client
        self._bucket = bucket
        self._prefix = prefix

    @property
    def location(self) -> str:
        return f"s3://{self._bucket}/{self._prefix}"

    async def put(self, name: str, data: Any) -> None:
        _check_name(name)
        body = to_json(data).encode()
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=self._prefix + name,
            Body=body,
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )


class LocalTrail:
    def __init__(self, root: Path, prefix: str) -> None:
        self._root = root
        self._dir = root / prefix

    @property
    def location(self) -> str:
        return str(self._dir)

    async def put(self, name: str, data: Any) -> None:
        _check_name(name)
        path = self._dir / name
        text = to_json(data)

        def write() -> None:
            # Resolve symlinks, then insist the file lands inside the trail directory
            # (which itself must be inside the root).
            real_dir = self._dir.resolve()
            if not real_dir.is_relative_to(self._root.resolve()):
                raise ValueError(f"trail directory is outside the trail root: {self._dir}")
            if not path.resolve().is_relative_to(real_dir):
                raise ValueError(f"not a trail file name: {name!r}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

        await asyncio.to_thread(write)


class MemoryTrail:
    """Keeps every file as parsed JSON, which also proves it would serialise."""

    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix
        self.files: dict[str, Any] = {}

    @property
    def location(self) -> str:
        return f"memory://{self.prefix}"

    async def put(self, name: str, data: Any) -> None:
        _check_name(name)
        self.files[name] = json.loads(to_json(data))
