"""Research written by hand, for paper testing before the research jobs exist.

``build_manual_run`` turns a small JSON document into a research run the bot reads like
any other. It builds and validates everything before returning, so a caller that writes
only what it returns never writes half a file.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from traider.research.models import (
    Horizon,
    Pick,
    Posture,
    PostureLevel,
    RunMeta,
    RunStatus,
)
from traider.timeutil import ET, trading_date

SWING_WEEKDAYS = 5

_TOP_KEYS = {"posture", "picks"}
_POSTURE_KEYS = {"level", "reasons"}
_PICK_KEYS = {
    "symbol",
    "side",
    "horizon",
    "score",
    "thesis",
    "invalidation",
    "expires_at",
    "pre_score",
    "earnings_date",
}


def _close(day: date) -> datetime:
    return datetime.combine(day, time(16, 0), tzinfo=ET).astimezone(UTC)


def _weekdays_after(day: date, n: int) -> date:
    for _ in range(n):
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
    return day


def _mapping(value: Any, what: str, allowed: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} must be an object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{what} has unknown field(s): {', '.join(unknown)}")
    return value


def _expiry(raw: Mapping[str, Any], horizon: Horizon, today: date) -> datetime:
    given = raw.get("expires_at")
    if given is not None:
        if not isinstance(given, str):
            raise ValueError("expires_at must be an ISO 8601 time with a timezone")
        return datetime.fromisoformat(given)  # the Pick refuses one without a timezone
    if horizon is Horizon.INTRADAY:
        return _close(today)
    return _close(_weekdays_after(today, SWING_WEEKDAYS))


def build_manual_run(
    data: Mapping[str, Any], now: datetime
) -> tuple[RunMeta, list[Pick], Posture | None]:
    if now.tzinfo is None:
        raise ValueError("now must carry a timezone")
    root = _mapping(data, "the seed file", _TOP_KEYS)
    now = now.astimezone(UTC)
    run_id = f"manual-{now:%Y%m%dT%H%M%SZ}"
    today = trading_date(now)

    raw_picks = root.get("picks", [])
    if not isinstance(raw_picks, list):
        raise ValueError("picks must be a list")
    picks: list[Pick] = []
    for index, item in enumerate(raw_picks, start=1):
        raw = _mapping(item, f"pick {index}", _PICK_KEYS)
        try:
            horizon = Horizon(raw.get("horizon", ""))
            fields = {k: v for k, v in raw.items() if k != "expires_at"}
            fields.setdefault("pre_score", raw.get("score"))
            picks.append(
                Pick.model_validate(
                    {
                        **fields,
                        "run_id": run_id,
                        "rank": index,
                        "expires_at": _expiry(raw, horizon, today),
                    }
                )
            )
        except ValueError as exc:
            raise ValueError(f"pick {index}: {exc}") from exc
    symbols = [p.symbol for p in picks]
    if len(set(symbols)) != len(symbols):
        raise ValueError("the same symbol is picked twice")

    posture: Posture | None = None
    if root.get("posture") is not None:
        raw_posture = _mapping(root["posture"], "posture", _POSTURE_KEYS)
        try:
            posture = Posture(
                level=PostureLevel(raw_posture.get("level", "")),
                reasons=raw_posture.get("reasons", ()),
                run_id=run_id,
                at=now,
            )
        except ValueError as exc:
            raise ValueError(f"posture: {exc}") from exc
    if not picks and posture is None:
        raise ValueError("nothing to seed: give a posture, picks, or both")

    meta = RunMeta(
        run_id=run_id,
        kind="manual",
        status=RunStatus.OK,
        started_at=now,
        finished_at=now,
        trading_day=today,
    )
    return meta, picks, posture
