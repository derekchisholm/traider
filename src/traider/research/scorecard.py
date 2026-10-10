"""The scorecard's maths: how a pick actually did, from daily bars. Pure functions.

Every return is a percentage from the entry (the pick day's open), signed by the pick's
side: a long gains when the price rises, a bearish pick (long puts) when it falls.

    ret_<h>d          close of the h-th trading day from the pick day (1 = the pick day)
    ret_0d            intraday picks only: the pick day's close
    mfe_pct, mae_pct  the best and the worst signed move inside the live window
    hit_invalidation  long: a low at or below the invalidation; bearish: a high at or above
    expired_return    the close of the expiry day (the last bar of the window once it closed)

The live window runs from the pick day to the expiry day, capped at today. An outcome is
``pending`` without an entry, ``final`` once the 20-day return and the expiry are both
known (or the pick is 30 weekdays old), and ``partial`` in between.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final

from traider.research.market import DailyBar
from traider.research.models import (
    Horizon,
    HorizonStats,
    OutcomeStatus,
    Pick,
    PickOutcome,
    PickSide,
    RunStatus,
    ScoreBucket,
    ScoreSummary,
)
from traider.timeutil import trading_date, weekdays_between, weekdays_from
from traider.universe import root_symbol

HORIZONS: Final = (1, 5, 20)
FINAL_AFTER_WEEKDAYS: Final = 30
BUCKETS: Final = ((60, 69), (70, 79), (80, 89), (90, 100))
DIGITS: Final = 4


@dataclass(frozen=True, slots=True)
class Scored:
    outcome: PickOutcome
    # The horizons whose return became known today ("ret_1d", "ret_5d", "ret_20d").
    matured: frozenset[str] = frozenset()


def signed_pct(entry: float, price: float, side: PickSide) -> float:
    """The move from ``entry`` to ``price`` in percent, positive when it favours ``side``."""
    move = (price / entry - 1) * 100
    return round(move if side is PickSide.LONG else -move, DIGITS)


def _positive(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) and value > 0 else None


def _usable(bar: DailyBar) -> bool:
    return all(math.isfinite(v) and v > 0 for v in (bar.open, bar.high, bar.low, bar.close))


def _llm_score(pick: Pick) -> int | None:
    value = pick.features.get("llm_score")
    if value is None or not 0 <= value <= 100:
        return None
    return round(value)


def score_pick(
    pick: Pick,
    *,
    pick_day: date,
    run_status: RunStatus,
    bars: Sequence[DailyBar],
    today: date,
    traded: bool | None,
    now: datetime,
) -> Scored:
    """One pick's outcome as of ``today``. ``bars`` are the symbol's daily bars; any
    outside the pick day to today are ignored, and so is everything from the first bar
    with an unusable price."""
    in_range = sorted((b for b in bars if pick_day <= b.day <= today), key=lambda b: b.day)
    # A bar with a non-finite or non-positive price is unusable. Nothing is guessed in its
    # place, and since a horizon counts bars by position, nothing after it is used either.
    series = []
    for b in in_range:
        if not _usable(b):
            break
        series.append(b)
    old = weekdays_between(pick_day, today) >= FINAL_AFTER_WEEKDAYS
    fields: dict[str, Any] = {
        "run_id": pick.run_id,
        "rank": pick.rank,
        "symbol": pick.symbol,
        "side": pick.side,
        "horizon": pick.horizon,
        "score": pick.score,
        "pre_score": pick.pre_score,
        "llm_score": _llm_score(pick),
        "pick_day": pick_day,
        "run_status": run_status,
        "price_at_pick": _positive(pick.features.get("price_at_pick")),
        "traded": traded,
        "updated_at": now,
    }
    if not series or series[0].day != pick_day:
        # No entry price: nothing can be measured. A pick that old never will be.
        fields["status"] = OutcomeStatus.FINAL if old else OutcomeStatus.PENDING
        return Scored(PickOutcome.model_validate(fields))
    side = pick.side
    entry = series[0].open
    matured: set[str] = set()
    for h in HORIZONS:
        name = f"ret_{h}d"
        if len(series) >= h:
            fields[name] = signed_pct(entry, series[h - 1].close, side)
            if series[h - 1].day == today:
                matured.add(name)
    if pick.horizon is Horizon.INTRADAY:
        fields["ret_0d"] = signed_pct(entry, series[0].close, side)
    expiry = trading_date(pick.expires_at)
    window = [b for b in series if b.day <= expiry]
    moves = [signed_pct(entry, price, side) for b in window for price in (b.high, b.low)]
    invalidation = float(pick.invalidation)
    if side is PickSide.LONG:
        hit = any(b.low <= invalidation for b in window)
    else:
        hit = any(b.high >= invalidation for b in window)
    closed = window[-1].day == expiry or series[-1].day > expiry
    expired = signed_pct(entry, window[-1].close, side) if closed else None
    done = "ret_20d" in fields and expired is not None
    fields |= {
        "entry": entry,
        "mfe_pct": max(moves),
        "mae_pct": min(moves),
        "hit_invalidation": hit,
        "expired_return": expired,
        "status": OutcomeStatus.FINAL if done or old else OutcomeStatus.PARTIAL,
    }
    return Scored(PickOutcome.model_validate(fields), frozenset(matured))


def _bought(event: Mapping[str, Any], symbol: str, start: datetime, end: datetime) -> bool:
    """A buy of ``symbol`` (shares, or an option on it) submitted between start and end."""
    if event.get("kind") != "order_submitted":
        return False
    data = event.get("data")
    if not isinstance(data, Mapping) or data.get("side") != "BUY":
        return False
    try:
        at = datetime.fromisoformat(str(event.get("at")))
        bought = root_symbol(str(data.get("symbol")))
    except ValueError:
        return False
    return at.tzinfo is not None and bought == symbol and start <= at <= end


def traded_from_logs(
    logs: Mapping[date, Sequence[Mapping[str, Any]] | None],
    symbol: str,
    start: datetime,
    end: datetime,
) -> bool | None:
    """Whether the bot submitted a buy in ``symbol`` while the pick was live, from its
    event log, one list per trading day. A day missing from ``logs`` or read as None was
    unreadable: then the answer is None unless a buy was found on another day."""
    unknown = False
    for day in weekdays_from(trading_date(start), trading_date(end)):
        events = logs.get(day)
        if events is None:
            unknown = True
            continue
        if any(_bought(event, symbol, start, end) for event in events):
            return True
    return None if unknown else False


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), DIGITS) if values else None


def _horizon_stats(scored: Sequence[Scored], name: str) -> HorizonStats:
    values = [
        value
        for s in scored
        if name in s.matured and (value := getattr(s.outcome, name)) is not None
    ]
    return HorizonStats(
        matured=len(values), hits=sum(1 for v in values if v > 0), mean_pct=_mean(values)
    )


def summarize(
    scored: Sequence[Scored],
    *,
    day: date,
    run_id: str,
    kinds: Mapping[str, str],
    now: datetime,
) -> ScoreSummary:
    """The day's summary over every pick in the window. ``kinds`` maps a run id to its
    kind (premarket, intraday, ...)."""
    by_kind: dict[str, int] = {}
    by_side: dict[str, int] = {}
    for s in scored:
        kind = kinds.get(s.outcome.run_id, "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_side[s.outcome.side.value] = by_side.get(s.outcome.side.value, 0) + 1
    buckets = []
    for low, high in BUCKETS:
        members = [s.outcome for s in scored if low <= s.outcome.score <= high]
        known = [o.ret_1d for o in members if o.ret_1d is not None]
        buckets.append(
            ScoreBucket(low=low, high=high, count=len(members), mean_ret_1d_pct=_mean(known))
        )
    return ScoreSummary(
        day=day,
        run_id=run_id,
        picks=len(scored),
        by_kind=dict(sorted(by_kind.items())),
        by_side=dict(sorted(by_side.items())),
        ret_1d=_horizon_stats(scored, "ret_1d"),
        ret_5d=_horizon_stats(scored, "ret_5d"),
        buckets=tuple(buckets),
        updated_at=now,
    )


def _stats_text(stats: HorizonStats, horizon: str) -> str:
    if not stats.matured or stats.mean_pct is None:
        return f"no picks matured {horizon}"
    return (
        f"{stats.matured} picks matured {horizon}, hit {stats.hits}/{stats.matured}, "
        f"mean {stats.mean_pct:+.1f}%"
    )


def summary_text(summary: ScoreSummary) -> str:
    """The alert: short and plain. Counts and numbers only, no symbols or model text."""
    return (
        f"traider scorecard {summary.day.isoformat()}: {_stats_text(summary.ret_1d, '1d')}; "
        f"{_stats_text(summary.ret_5d, '5d')}; {summary.picks} picks in the window"
    )
