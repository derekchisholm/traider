"""Rank and validate (code): turn the model's assessments into picks, or into reasons why
not. Per assessment, in order; the first failure drops the name with its reason code:

1. ``passed``            the model passed
2. ``no_quote``/``halted`` no fresh last price, or not trading normally
3. ``bad_invalidation``  the stop is not ``min_stop_atr``..``max_stop_atr`` ATR14 away on
                         the right side (long: price - invalidation; bearish: invalidation -
                         price)
4. ``illiquid_puts``     bearish, and no put 7-45 days out within 5% of the price with a bid,
                         a spread within ``max_put_spread_pct`` and open interest of at least
                         ``min_put_oi``
5. expiry                intraday: today's close. Swing: the close ``swing_days`` weekdays out,
                         but never past the close of ``calendar_end``, the last day the
                         earnings calendar covers (a date after it is unknown). Refused as
                         ``earnings_unknown`` without a calendar, when this symbol's earnings
                         were not confirmed by a symbol-scoped call, or for a share-class
                         symbol (a "/" or "." in it: vendors spell those differently). Then
                         moved to the close of the last weekday before an earnings date in
                         ``[today, expiry]`` (not one today before the open); a swing pick
                         left expiring today or earlier is ``earnings_too_close``. Intraday
                         picks are flat by the close, so only an earnings release today at an
                         unknown hour (it may come during the session) refuses one.
6. blended score         ``round(llm_weight x llm_score + (1 - llm_weight) x pre_score)``
7. sort by score; at most ``max_per_sector`` per sector (unknown is one sector):
   ``sector_cap``; keep the top ``max_picks``: ``below_cut``. Ranks start at 1.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from decimal import Decimal

from traider.research.dive import Assessment
from traider.research.events import EarningsEvent
from traider.research.job_settings import RankSettings
from traider.research.market import MarketData, MarketQuote, PutContract, liquid_puts
from traider.research.models import Horizon, Pick, PickSide
from traider.research.numbers import round_half_up
from traider.timeutil import ET, previous_weekday, weekdays_after

log = logging.getLogger(__name__)

THESIS_MAX_CHARS = 2000
_EPSILON = 1e-9  # so a stop exactly on a bound is not lost to float rounding


@dataclass(frozen=True, slots=True)
class RankInput:
    symbol: str
    assessment: Assessment
    pre_score: int
    features: Mapping[str, float]
    atr: float
    sector: str | None
    earnings: tuple[EarningsEvent, ...]  # this symbol's, from the calendar
    # A symbol-scoped calendar call answered for this name. Without it a swing pick is
    # refused (``earnings_unknown``); intraday picks do not need it.
    earnings_confirmed: bool = False


@dataclass(frozen=True, slots=True)
class Rejection:
    symbol: str
    reason: str


@dataclass(frozen=True, slots=True)
class RankResult:
    picks: tuple[Pick, ...]
    rejected: tuple[Rejection, ...]
    chain_failures: int = 0  # put-chain reads that failed (counted as illiquid)


def close_of(day: date) -> datetime:
    """16:00 New York on ``day``, in UTC. Half days are not known for future dates."""
    return datetime.combine(day, time(16, 0), tzinfo=ET).astimezone(UTC)


def share_class(symbol: str) -> bool:
    """A share-class symbol (BRK/B, BRK.B): its spelling differs between vendors, so its
    earnings cannot be matched by symbol with confidence."""
    return "/" in symbol or "." in symbol


def swing_expiry_day(
    today: date, swing_days: int, earnings: Sequence[EarningsEvent], calendar_end: date
) -> date | None:
    """The day a swing pick expires at the close, or None when earnings come too soon.
    Never after ``calendar_end``: the earnings calendar says nothing past it."""
    expiry = min(weekdays_after(today, swing_days), calendar_end)
    for event in sorted(earnings, key=lambda e: e.day):
        if not today <= event.day <= expiry:
            continue
        if event.day == today and event.hour == "bmo":
            continue  # already out before the open: it is today's news, not a risk ahead
        expiry = min(expiry, previous_weekday(event.day))
    return expiry if expiry > today else None


def blended_score(llm_score: int, pre_score: int, llm_weight: float) -> int:
    value = llm_weight * llm_score + (1 - llm_weight) * pre_score
    return max(0, min(100, round_half_up(value)))


def _thesis(assessment: Assessment) -> str:
    text = assessment.thesis
    if assessment.risks:
        text += "\nRisks: " + "; ".join(assessment.risks)
    return text[:THESIS_MAX_CHARS]


def validate_and_rank(
    inputs: Sequence[RankInput],
    *,
    fresh: Mapping[str, MarketQuote],
    puts: Mapping[str, Sequence[PutContract] | None],
    run_id: str,
    today: date,
    close: datetime,
    earnings_ok: bool,
    calendar_end: date,
    settings: RankSettings,
) -> RankResult:
    rejected: list[Rejection] = []
    passing: list[tuple[int, RankInput, Horizon, datetime, float]] = []
    for item in inputs:
        a = item.assessment
        if a.side == "pass":
            rejected.append(Rejection(item.symbol, "passed"))
            continue
        q = fresh.get(item.symbol)
        if q is None or q.last is None or not math.isfinite(q.last) or q.last <= 0:
            rejected.append(Rejection(item.symbol, "no_quote"))
            continue
        if q.halted:
            rejected.append(Rejection(item.symbol, "halted"))
            continue
        price = q.last
        distance = price - a.invalidation if a.side == "long" else a.invalidation - price
        # Unknown ATR (not finite, or not positive) can't size a stop: drop, never guess.
        known_atr = math.isfinite(item.atr) and item.atr > 0
        in_atr = distance / item.atr if known_atr else -1.0
        low, high = settings.min_stop_atr - _EPSILON, settings.max_stop_atr + _EPSILON
        if not low <= in_atr <= high:
            rejected.append(Rejection(item.symbol, "bad_invalidation"))
            continue
        if a.side == "bearish":
            chain = puts.get(item.symbol)
            liquid = liquid_puts(
                chain or (),
                max_spread_pct=settings.max_put_spread_pct,
                min_open_interest=settings.min_put_oi,
            )
            if not liquid:
                rejected.append(Rejection(item.symbol, "illiquid_puts"))
                continue
        if a.horizon == "intraday":
            today_unknown = any(e.day == today and e.hour == "unknown" for e in item.earnings)
            if today_unknown:  # refused even without a calendar flag: this event is in hand
                rejected.append(Rejection(item.symbol, "earnings_too_close"))
                continue
            horizon, expires = Horizon.INTRADAY, close
        else:
            if not earnings_ok or not item.earnings_confirmed or share_class(item.symbol):
                rejected.append(Rejection(item.symbol, "earnings_unknown"))
                continue
            assert a.swing_days is not None
            day = swing_expiry_day(today, a.swing_days, item.earnings, calendar_end)
            if day is None:
                rejected.append(Rejection(item.symbol, "earnings_too_close"))
                continue
            horizon, expires = Horizon.SWING, close_of(day)
        score = blended_score(a.score, item.pre_score, settings.llm_weight)
        passing.append((score, item, horizon, expires, price))

    passing.sort(key=lambda p: (-p[0], -p[1].pre_score, p[1].symbol))
    per_sector: dict[str | None, int] = {}
    picks: list[Pick] = []
    for score, item, horizon, expires, price in passing:
        sector = item.sector or None
        if per_sector.get(sector, 0) >= settings.max_per_sector:
            rejected.append(Rejection(item.symbol, "sector_cap"))
            continue
        if len(picks) >= settings.max_picks:
            rejected.append(Rejection(item.symbol, "below_cut"))
            continue
        per_sector[sector] = per_sector.get(sector, 0) + 1
        upcoming = sorted(e.day for e in item.earnings if e.day >= today)
        features = {
            **{k: v for k, v in item.features.items() if math.isfinite(v)},
            "llm_score": float(item.assessment.score),
            "atr": item.atr,
            "price_at_pick": price,
        }
        picks.append(
            Pick(
                run_id=run_id,
                rank=len(picks) + 1,
                symbol=item.symbol,
                side=PickSide.LONG if item.assessment.side == "long" else PickSide.BEARISH,
                horizon=horizon,
                score=score,
                pre_score=item.pre_score,
                thesis=_thesis(item.assessment),
                invalidation=Decimal(str(item.assessment.invalidation)),
                earnings_date=upcoming[0] if upcoming else None,
                expires_at=expires,
                features=features,
            )
        )
    return RankResult(tuple(picks), tuple(rejected))


async def rank_and_validate(
    inputs: Sequence[RankInput],
    *,
    market: MarketData,
    run_id: str,
    today: date,
    close: datetime,
    earnings_ok: bool,
    calendar_end: date,
    settings: RankSettings,
) -> RankResult:
    """Fetch a fresh quote for every name (one batch) and puts for bearish ones, then
    ``validate_and_rank``. A failed quote batch raises; a failed chain read means no puts."""
    wanted = [i.symbol for i in inputs if i.assessment.side != "pass"]
    fresh = (await market.quotes(wanted)).quotes if wanted else {}
    puts: dict[str, Sequence[PutContract] | None] = {}
    failed = 0
    for item in inputs:
        q = fresh.get(item.symbol)
        if item.assessment.side != "bearish" or q is None or q.last is None or q.halted:
            continue
        try:
            puts[item.symbol] = await market.puts(item.symbol, q.last, today)
        except Exception as exc:
            # Type name only: the message could carry vendor text.
            log.warning("put chain read failed for %s: %s", item.symbol, type(exc).__name__)
            puts[item.symbol] = None  # unknown liquidity counts as illiquid
            failed += 1
    result = validate_and_rank(
        inputs,
        fresh=fresh,
        puts=puts,
        run_id=run_id,
        today=today,
        close=close,
        earnings_ok=earnings_ok,
        calendar_end=calendar_end,
        settings=settings,
    )
    return replace(result, chain_failures=failed)
