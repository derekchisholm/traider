"""Market data for research: quotes with fundamentals, daily bars, movers, puts, hours.

Research reads Schwab, like the bot. Everything here is read-only. Parsing is defensive:
an entry that cannot be read is skipped and counted, never guessed at.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from traider.schwab.client import SchwabClient
from traider.schwab.parse import ParseError, parse_candles, parse_market_hours
from traider.session import Session
from traider.timeutil import trading_date

QUOTE_CHUNK = 100  # symbols per quotes request
QUOTE_FIELDS = "quote,fundamental,reference"
PUT_MIN_DAYS = 7
PUT_MAX_DAYS = 45
PUT_STRIKE_BAND = 0.05  # within 5% of the price
_ETF_SUB_TYPES = {"ETF", "ETN"}


class MarketQuote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    asset_type: str  # Schwab's assetMainType: EQUITY, INDEX, COLLECTIVE_INVESTMENT, ...
    asset_sub_type: str | None = None  # COE (common stock), ETF, ETN, ...
    exchange: str | None = None  # reference.exchangeName
    last: float | None = None
    prev_close: float | None = None
    halted: bool = False
    avg_volume: float | None = None  # fundamental.avg10DaysVolume
    high_52w: float | None = None
    low_52w: float | None = None
    pe: float | None = None
    div_yield: float | None = None

    @property
    def gap_pct(self) -> float | None:
        if self.last is None or not self.prev_close:
            return None
        return (self.last / self.prev_close - 1) * 100

    @property
    def is_etf(self) -> bool:
        sub_type = self.asset_sub_type or ""
        return self.asset_type == "COLLECTIVE_INVESTMENT" or sub_type in _ETF_SUB_TYPES

    @property
    def is_otc(self) -> bool:
        """OTC or pink sheets. An unknown exchange counts as OTC: fail closed."""
        if not self.exchange:
            return True
        name = self.exchange.lower()
        return "otc" in name or "pink" in name


class DailyBar(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    day: date
    open: float
    high: float
    low: float
    close: float
    volume: int


class PutContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    strike: float
    days: int
    bid: float
    ask: float
    open_interest: int

    @property
    def spread_pct(self) -> float | None:
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            return None
        return (self.ask - self.bid) / ((self.ask + self.bid) / 2) * 100


@dataclass(frozen=True, slots=True)
class QuoteBatch:
    quotes: dict[str, MarketQuote]
    skipped: int = 0  # entries in the reply that could not be read


class MarketData(Protocol):
    async def market_session(self, day: date) -> Session:
        """The regular session for ``day`` (``open is None`` when closed). Raises on failure."""
        ...

    async def movers(self, index: str, sort: str) -> list[str]: ...

    async def quotes(self, symbols: Sequence[str]) -> QuoteBatch: ...

    async def daily_bars(self, symbol: str, before: date, days: int) -> list[DailyBar]:
        """Up to ``days`` daily bars, oldest first, all strictly before ``before``."""
        ...

    async def puts(self, symbol: str, price: float, today: date) -> list[PutContract]:
        """Puts 7 to 45 days out with a strike within 5% of ``price``."""
        ...


def put_summary(contracts: Sequence[PutContract]) -> dict[str, float | int | None]:
    spreads = [c.spread_pct for c in contracts if c.spread_pct is not None]
    return {
        "count": len(contracts),
        "best_spread_pct": round(min(spreads), 2) if spreads else None,
        "max_open_interest": max((c.open_interest for c in contracts), default=0),
    }


def liquid_puts(
    contracts: Sequence[PutContract], *, max_spread_pct: float, min_open_interest: int
) -> list[PutContract]:
    return [
        c
        for c in contracts
        if c.bid > 0
        and (spread := c.spread_pct) is not None
        and spread <= max_spread_pct
        and c.open_interest >= min_open_interest
    ]


# ------------------------------------------------------------------------ parsing


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_movers(raw: Any) -> list[str]:
    screeners = _mapping(raw).get("screeners")
    if not isinstance(screeners, list):
        raise ParseError("movers response has no screeners list")
    found: list[str] = []
    for item in screeners:
        symbol = _mapping(item).get("symbol")
        if isinstance(symbol, str) and symbol and symbol not in found:
            found.append(symbol)
    return found


def parse_market_quotes(raw: Any) -> QuoteBatch:
    quotes: dict[str, MarketQuote] = {}
    skipped = 0
    for symbol, item in _mapping(raw).items():
        if symbol == "errors":  # Schwab lists symbols it does not know here
            continue
        entry = _mapping(item)
        fields = entry.get("quote")
        if not isinstance(fields, Mapping) or not isinstance(entry.get("assetMainType"), str):
            skipped += 1
            continue
        fundamental = _mapping(entry.get("fundamental"))
        reference = _mapping(entry.get("reference"))
        status = fields.get("securityStatus")
        avg_volume = _float(fundamental.get("avg10DaysVolume"))
        if avg_volume is None:
            avg_volume = _float(fundamental.get("avg1YearVolume"))
        quotes[symbol] = MarketQuote(
            symbol=symbol,
            asset_type=entry["assetMainType"],
            asset_sub_type=_text(entry.get("assetSubType")),
            exchange=_text(reference.get("exchangeName")),
            last=_float(fields.get("lastPrice")),
            prev_close=_float(fields.get("closePrice")),
            halted=status is not None and status != "Normal",
            avg_volume=avg_volume,
            high_52w=_float(fields.get("52WeekHigh")),
            low_52w=_float(fields.get("52WeekLow")),
            pe=_float(fundamental.get("peRatio")),
            div_yield=_float(fundamental.get("divYield")),
        )
    return QuoteBatch(quotes, skipped)


def parse_daily_bars(raw: Any, symbol: str) -> list[DailyBar]:
    """Daily candles as New York trading days. A day that appears twice keeps the last."""
    by_day: dict[date, DailyBar] = {}
    for bar in parse_candles(raw, symbol):
        day = trading_date(bar.start)
        by_day[day] = DailyBar(
            day=day,
            open=float(bar.open),
            high=float(bar.high),
            low=float(bar.low),
            close=float(bar.close),
            volume=bar.volume,
        )
    return [by_day[day] for day in sorted(by_day)]


def parse_puts(
    raw: Any,
    price: float,
    *,
    min_days: int = PUT_MIN_DAYS,
    max_days: int = PUT_MAX_DAYS,
    band: float = PUT_STRIKE_BAND,
) -> list[PutContract]:
    found: dict[str, PutContract] = {}
    if price <= 0:
        return []
    for strikes in _mapping(_mapping(raw).get("putExpDateMap")).values():
        for entries in _mapping(strikes).values():
            for item in entries if isinstance(entries, list) else []:
                entry = _mapping(item)
                symbol, days = entry.get("symbol"), entry.get("daysToExpiration")
                strike, bid, ask = (
                    _float(entry.get(name)) for name in ("strikePrice", "bid", "ask")
                )
                oi = entry.get("openInterest")
                if not isinstance(symbol, str) or not isinstance(days, int):
                    continue
                if strike is None or bid is None or ask is None:
                    continue
                if not isinstance(oi, int) or isinstance(oi, bool):
                    oi = 0
                if not min_days <= days <= max_days or abs(strike - price) / price > band:
                    continue
                found[symbol] = PutContract(
                    symbol=symbol, strike=strike, days=days, bid=bid, ask=ask, open_interest=oi
                )
    return [found[s] for s in sorted(found)]


# ------------------------------------------------------------------------ adapter


class SchwabMarketData:
    """``MarketData`` over the bot's Schwab client."""

    def __init__(self, client: SchwabClient, *, put_strikes: int = 20) -> None:
        self._client = client
        self._put_strikes = put_strikes

    async def market_session(self, day: date) -> Session:
        return parse_market_hours(await self._client.market_hours(day), day)

    async def movers(self, index: str, sort: str) -> list[str]:
        return parse_movers(await self._client.movers(index, sort=sort))

    async def quotes(self, symbols: Sequence[str]) -> QuoteBatch:
        unique = list(dict.fromkeys(symbols))
        quotes: dict[str, MarketQuote] = {}
        skipped = 0
        for start in range(0, len(unique), QUOTE_CHUNK):
            chunk = unique[start : start + QUOTE_CHUNK]
            batch = parse_market_quotes(await self._client.quotes(chunk, fields=QUOTE_FIELDS))
            quotes.update({s: q for s, q in batch.quotes.items() if s in chunk})
            skipped += batch.skipped
        return QuoteBatch(quotes, skipped)

    async def daily_bars(self, symbol: str, before: date, days: int) -> list[DailyBar]:
        # Enough calendar days to cover ``days`` trading days, holidays included.
        start = before - timedelta(days=days * 7 // 5 + 15)
        raw = await self._client.daily_history(symbol, start, before - timedelta(days=1))
        bars = [bar for bar in parse_daily_bars(raw, symbol) if bar.day < before]
        return bars[-days:]

    async def puts(self, symbol: str, price: float, today: date) -> list[PutContract]:
        raw = await self._client.option_chain(
            symbol,
            today + timedelta(days=PUT_MIN_DAYS),
            today + timedelta(days=PUT_MAX_DAYS),
            strikes=self._put_strikes,
            contract_type="PUT",
        )
        return parse_puts(raw, price)
