"""The code screen: which candidates are worth a deep-dive. Pure functions only.

Formulas (``bars`` are daily bars before today, oldest first; ``price`` is the quote's last):

* ``gap_pct``      (price / previous close - 1) x 100; previous close from the quote, else
                   the last bar's close
* ``rvol``         last bar's volume / mean volume of the 20 bars before it (0 if that is 0)
* ``atr``          mean true range of the last 14 bars; a bar's true range is
                   max(high - low, |high - previous close|, |low - previous close|)
* ``atr_pct``      atr / price x 100
* ``trend20_pct``  (price / mean of the last 20 closes - 1) x 100; ``trend50_pct`` likewise
* ``ret5_pct``     (last close / the close five bars earlier - 1) x 100
* ``off_high_pct`` (1 - price / 52-week high) x 100; the high from the quote, else the bars
* ``dollar_vol_m`` average volume x price / 1,000,000; average volume from the quote's
                   fundamentals, else the mean of the last 20 bars
* ``earnings_days`` weekdays from today to the next earnings date; -1 if none within the
                   lookahead, -2 if the calendar is unknown
* ``news_3d``      company news items in the last three days (filled in later)
* ``bias``         +1 if gap >= 0 and price > SMA20; -1 if gap < 0 and price < SMA20; else 0

``pre_score`` is ``round(100 x sum(weight x component))`` (halves round up) with components:
move = rank of |gap_pct|, participation = rank of rvol, liquidity = rank of dollar_vol_m,
catalyst = 1 if earnings are within one weekday either side of today, else rank of news_3d,
alignment = 1 if bias != 0, else 0. A rank is the percentile within the surviving set:
(number below + (number equal - 1) / 2) / (n - 1), and 1 when there is one name.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date

from traider.config import check_symbols
from traider.research.events import EarningsEvent
from traider.research.job_settings import ScreenSettings, ScreenWeights
from traider.research.market import DailyBar, MarketQuote
from traider.research.numbers import round_half_up
from traider.timeutil import next_weekday, previous_weekday, weekdays_between

MIN_BARS = 60
ATR_DAYS = 14

# Drop reasons, in the order the checks run.
DROP_SYMBOL = "symbol"
DROP_NO_QUOTE = "no_quote"
DROP_ASSET_TYPE = "asset_type"
DROP_OTC = "otc"
DROP_PRICE = "price"
DROP_HISTORY = "history"
DROP_HISTORY_ERROR = "history_error"
DROP_DOLLAR_VOLUME = "dollar_volume"


@dataclass(frozen=True, slots=True)
class Candidate:
    symbol: str
    sources: tuple[str, ...]  # watchlist, earnings, movers


@dataclass(frozen=True, slots=True)
class Features:
    gap_pct: float
    rvol: float
    atr_pct: float
    trend20_pct: float
    trend50_pct: float
    ret5_pct: float
    off_high_pct: float
    dollar_vol_m: float
    earnings_days: float
    news_3d: float
    bias: float

    def as_dict(self) -> dict[str, float]:
        return {name: float(value) for name, value in asdict(self).items()}


@dataclass(frozen=True, slots=True)
class ScreenRow:
    symbol: str
    price: float
    atr: float
    features: Features
    earnings_near: bool
    pre_score: int = 0


# ------------------------------------------------------------------- candidates


def build_candidates(
    *,
    watchlist: Sequence[str],
    earnings_names: Sequence[str],
    movers: Sequence[str],
    pinned: Iterable[str],
    cap: int,
) -> list[Candidate]:
    """The union of the three sources in priority order (watchlist, earnings, movers),
    without pinned symbols, capped at ``cap``. A name keeps every source it came from."""
    skip = set(pinned)
    order: list[str] = []
    sources: dict[str, list[str]] = {}
    by_source = (("watchlist", watchlist), ("earnings", earnings_names), ("movers", movers))
    for source, names in by_source:
        for name in names:
            if name in skip:
                continue
            if name not in sources:
                order.append(name)
                sources[name] = []
            if source not in sources[name]:
                sources[name].append(source)
    return [Candidate(name, tuple(sources[name])) for name in order[:cap]]


def earnings_candidates(events: Sequence[EarningsEvent], today: date) -> list[str]:
    """Names that reported yesterday after the close or today before the open."""
    yesterday = previous_weekday(today)
    found: list[str] = []
    for e in events:
        fresh = (e.day == yesterday and e.hour == "amc") or (e.day == today and e.hour == "bmo")
        if fresh and e.symbol not in found:
            found.append(e.symbol)
    return found


# ---------------------------------------------------------------------- filters


def symbol_ok(symbol: str) -> bool:
    try:
        check_symbols((symbol,))
    except ValueError:
        return False
    return True


def quote_filter(symbol: str, quote: MarketQuote | None, settings: ScreenSettings) -> str | None:
    """The first quote-level check ``symbol`` fails, or None."""
    if not symbol_ok(symbol):
        return DROP_SYMBOL
    if quote is None or quote.last is None or quote.last <= 0:
        return DROP_NO_QUOTE
    if quote.is_etf:
        if not settings.allow_etfs:
            return DROP_ASSET_TYPE
    elif quote.asset_type != "EQUITY":
        return DROP_ASSET_TYPE
    if quote.is_otc:
        return DROP_OTC
    if not float(settings.min_price) <= quote.last <= float(settings.max_price):
        return DROP_PRICE
    return None


def average_volume(quote: MarketQuote, bars: Sequence[DailyBar]) -> float:
    if quote.avg_volume is not None and quote.avg_volume > 0:
        return quote.avg_volume
    recent = bars[-20:]
    return sum(b.volume for b in recent) / len(recent) if recent else 0.0


def history_filter(
    quote: MarketQuote, bars: Sequence[DailyBar], settings: ScreenSettings
) -> str | None:
    """The first history check a name that passed ``quote_filter`` fails, or None."""
    if len(bars) < MIN_BARS:
        return DROP_HISTORY
    if quote.last is None:
        return DROP_NO_QUOTE
    if average_volume(quote, bars) * quote.last < float(settings.min_dollar_volume):
        return DROP_DOLLAR_VOLUME
    return None


# --------------------------------------------------------------------- features


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def atr(bars: Sequence[DailyBar], days: int = ATR_DAYS) -> float:
    ranges: list[float] = []
    for i, bar in enumerate(bars):
        if i == 0:
            ranges.append(bar.high - bar.low)
            continue
        prev = bars[i - 1].close
        ranges.append(max(bar.high - bar.low, abs(bar.high - prev), abs(bar.low - prev)))
    recent = ranges[-days:]
    return _mean(recent) if recent else 0.0


def earnings_days(
    events: Sequence[EarningsEvent], today: date, *, earnings_ok: bool, lookahead: int
) -> int:
    if not earnings_ok:
        return -2
    upcoming = sorted(e.day for e in events if e.day >= today)
    if not upcoming:
        return -1
    days = weekdays_between(today, upcoming[0])
    return days if days <= lookahead else -1


def earnings_near(events: Sequence[EarningsEvent], today: date) -> bool:
    near = {previous_weekday(today), today, next_weekday(today)}
    return any(e.day in near for e in events)


def compute_features(
    quote: MarketQuote,
    bars: Sequence[DailyBar],
    *,
    today: date,
    events: Sequence[EarningsEvent],
    earnings_ok: bool,
    lookahead: int,
    news_3d: int = 0,
) -> tuple[Features, float]:
    """The features and the ATR for one name that passed both filters."""
    if quote.last is None or len(bars) < MIN_BARS:
        raise ValueError("features need a quote price and 60 bars: run the filters first")
    price = quote.last
    closes = [b.close for b in bars]
    if price <= 0 or any(close <= 0 for close in closes[-50:]):
        raise ValueError("features need a positive price and positive recent closes")
    prev_close = quote.prev_close if quote.prev_close and quote.prev_close > 0 else closes[-1]
    sma20 = _mean(closes[-20:])
    sma50 = _mean(closes[-50:])
    base_volume = _mean([b.volume for b in bars[-21:-1]])
    high = quote.high_52w or max(b.high for b in bars[-252:])
    gap = (price / prev_close - 1) * 100
    if gap >= 0 and price > sma20:
        bias = 1.0
    elif gap < 0 and price < sma20:
        bias = -1.0
    else:
        bias = 0.0
    range_ = atr(bars)
    features = Features(
        gap_pct=gap,
        rvol=bars[-1].volume / base_volume if base_volume > 0 else 0.0,
        atr_pct=range_ / price * 100,
        trend20_pct=(price / sma20 - 1) * 100,
        trend50_pct=(price / sma50 - 1) * 100,
        ret5_pct=(closes[-1] / closes[-6] - 1) * 100,
        off_high_pct=(1 - price / high) * 100 if high > 0 else 0.0,
        dollar_vol_m=average_volume(quote, bars) * price / 1_000_000,
        earnings_days=float(
            earnings_days(events, today, earnings_ok=earnings_ok, lookahead=lookahead)
        ),
        news_3d=float(news_3d),
        bias=bias,
    )
    return features, range_


# ---------------------------------------------------------------------- scoring


def percentile_ranks(values: Sequence[float]) -> list[float]:
    n = len(values)
    if n == 1:
        return [1.0]
    ranks = []
    for value in values:
        below = sum(1 for v in values if v < value)
        equal = sum(1 for v in values if v == value)
        ranks.append((below + (equal - 1) / 2) / (n - 1))
    return ranks


def score_rows(rows: Sequence[ScreenRow], weights: ScreenWeights) -> list[ScreenRow]:
    """``rows`` with ``pre_score`` set, in the same order."""
    if not rows:
        return []
    move = percentile_ranks([abs(r.features.gap_pct) for r in rows])
    participation = percentile_ranks([r.features.rvol for r in rows])
    liquidity = percentile_ranks([r.features.dollar_vol_m for r in rows])
    news = percentile_ranks([r.features.news_3d for r in rows])
    scored = []
    for i, row in enumerate(rows):
        catalyst = 1.0 if row.earnings_near else news[i]
        alignment = 1.0 if row.features.bias != 0 else 0.0
        total = (
            weights.move * move[i]
            + weights.participation * participation[i]
            + weights.liquidity * liquidity[i]
            + weights.catalyst * catalyst
            + weights.alignment * alignment
        )
        scored.append(replace(row, pre_score=max(0, min(100, round_half_up(100 * total)))))
    return scored


def with_news(row: ScreenRow, count: int) -> ScreenRow:
    return replace(row, features=replace(row.features, news_3d=float(count)))


def top_k(rows: Sequence[ScreenRow], k: int) -> list[ScreenRow]:
    return sorted(rows, key=lambda r: (-r.pre_score, r.symbol))[:k]


def drop_counts(dropped: Mapping[str, str]) -> dict[str, int]:
    return dict(Counter(dropped.values()))
