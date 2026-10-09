"""The runtime control switch (kill switch) and what it permits.

Two things must agree before any order goes out:

* ``trading_mode``: set at deploy time (``paper`` or ``live``).
* the control value: changed at runtime, without a deploy
  (``halt``, ``close_only``, ``paper`` or ``live``).

Trading is enabled only when the control value names the deployed mode. A live
deploy therefore stays idle until someone also sets the control to ``live``,
and setting it to ``halt`` stops everything within seconds.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

log = logging.getLogger(__name__)


class ControlMode(StrEnum):
    HALT = "halt"
    CLOSE_ONLY = "close_only"
    PAPER = "paper"
    LIVE = "live"


def parse_control(raw: str | None) -> ControlMode:
    """Anything missing or unrecognised means halt."""
    if raw is None:
        return ControlMode.HALT
    try:
        return ControlMode(raw.strip().lower())
    except ValueError:
        return ControlMode.HALT


@dataclass(frozen=True, slots=True)
class Permissions:
    allow_entries: bool
    allow_exits: bool
    live: bool
    reason: str


def effective_permissions(trading_mode: str, control: ControlMode) -> Permissions:
    live = trading_mode == "live"
    if control is ControlMode.HALT:
        return Permissions(False, False, live, "control is halt")
    if control is ControlMode.CLOSE_ONLY:
        return Permissions(False, True, live, "control is close_only")
    if control.value == trading_mode:
        return Permissions(True, True, live, f"control and deploy agree on {trading_mode}")
    return Permissions(
        False,
        False,
        False,
        f"control is {control.value} but this deploy is {trading_mode}; "
        f"set control to {trading_mode} to trade",
    )


class ControlSource(Protocol):
    async def read(self) -> str | None: ...


class StaticControl:
    """A fixed value, for local runs, tests and backtests."""

    def __init__(self, value: str) -> None:
        self._value = value

    async def read(self) -> str | None:
        return self._value


class SsmControl:
    """Reads the control value from an SSM Parameter Store parameter."""

    def __init__(self, name: str, client: Any) -> None:
        self._name = name
        self._client = client

    async def read(self) -> str | None:
        response = await asyncio.to_thread(self._client.get_parameter, Name=self._name)
        value = response["Parameter"]["Value"]
        return str(value)


class ControlState:
    """Caches the last control value and fails closed when it goes stale."""

    def __init__(self, source: ControlSource, *, max_stale_s: float = 60.0) -> None:
        self._source = source
        self._max_stale_s = max_stale_s
        self._mode = ControlMode.HALT
        self._read_at: datetime | None = None
        self.last_error: str | None = None

    async def refresh(self, now: datetime) -> None:
        try:
            raw = await self._source.read()
        except Exception as exc:  # any read failure must not crash the bot
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("control read failed: %s", self.last_error)
            return
        mode = parse_control(raw)
        if mode is not self._mode or self._read_at is None:
            log.info("control is %s", mode.value)
        self._mode = mode
        self._read_at = now
        self.last_error = None

    def mode(self, now: datetime) -> ControlMode:
        if self._read_at is None:
            return ControlMode.HALT
        if (now - self._read_at).total_seconds() > self._max_stale_s:
            return ControlMode.HALT
        return self._mode
