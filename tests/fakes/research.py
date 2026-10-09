"""Stand-ins for what the research jobs talk to: Schwab market data, Finnhub and Bedrock.

Each records what it was asked and can be told to fail.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from traider.research.events import EarningsEvent, EventsUnavailable, NewsItem, Profile
from traider.research.market import DailyBar, MarketQuote, PutContract, QuoteBatch
from traider.session import Session
from traider.timeutil import ET, previous_weekday

TODAY = date(2026, 10, 9)  # a Friday
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)  # 08:00 New York


def quote(
    symbol: str,
    last: float | None,
    prev_close: float | None,
    *,
    asset_type: str = "EQUITY",
    sub_type: str | None = "COE",
    exchange: str | None = "NASDAQ",
    avg_volume: float | None = 1_000_000,
    high_52w: float | None = None,
    halted: bool = False,
) -> MarketQuote:
    return MarketQuote(
        symbol=symbol,
        asset_type=asset_type,
        asset_sub_type=sub_type,
        exchange=exchange,
        last=last,
        prev_close=prev_close,
        halted=halted,
        avg_volume=avg_volume,
        high_52w=high_52w,
        low_52w=None,
        pe=25.0,
        div_yield=0.5,
    )


def trading_days(before: date, n: int) -> list[date]:
    """The ``n`` weekdays before ``before``, oldest first."""
    days = [previous_weekday(before)]
    while len(days) < n:
        days.append(previous_weekday(days[-1]))
    return days[::-1]


def flat_bars(
    close: float, volume: int, *, last_volume: int | None = None, n: int = 260, before: date = TODAY
) -> list[DailyBar]:
    """Bars that close at ``close`` every day with a 2% range: ATR14 is 2% of ``close``."""
    bars = [
        DailyBar(
            day=day,
            open=close,
            high=close * 1.01,
            low=close * 0.99,
            close=close,
            volume=volume,
        )
        for day in trading_days(before, n)
    ]
    if last_volume is not None:
        bars[-1] = bars[-1].model_copy(update={"volume": last_volume})
    return bars


def rising_bars(start: float, step: float, *, n: int = 260, before: date = TODAY) -> list[DailyBar]:
    bars = []
    for i, day in enumerate(trading_days(before, n)):
        close = start + i * step
        bars.append(
            DailyBar(
                day=day, open=close, high=close + 1, low=close - 1, close=close, volume=50_000_000
            )
        )
    return bars


class FakeMarketData:
    def __init__(self) -> None:
        self.open_today = True
        self.quote_map: dict[str, MarketQuote] = {}
        self.mover_lists: dict[tuple[str, str], list[str]] = {}
        self.bars: dict[str, list[DailyBar]] = {}
        self.put_chains: dict[str, list[PutContract]] = {}
        self.failures: dict[str, Exception] = {}  # method name -> raised on every call
        self.calls: list[tuple[str, Any]] = []

    def _enter(self, name: str, detail: Any) -> None:
        self.calls.append((name, detail))
        if name in self.failures:
            raise self.failures[name]

    def called(self, name: str) -> list[Any]:
        return [detail for called, detail in self.calls if called == name]

    async def market_session(self, day: date) -> Session:
        self._enter("market_session", day)
        if not self.open_today:
            return Session(day, None, None)
        return Session(
            day,
            datetime.combine(day, time(9, 30), tzinfo=ET),
            datetime.combine(day, time(16, 0), tzinfo=ET),
        )

    async def movers(self, index: str, sort: str) -> list[str]:
        self._enter("movers", (index, sort))
        return list(self.mover_lists.get((index, sort), []))

    async def quotes(self, symbols: Sequence[str]) -> QuoteBatch:
        self._enter("quotes", tuple(symbols))
        return QuoteBatch({s: self.quote_map[s] for s in symbols if s in self.quote_map})

    async def daily_bars(self, symbol: str, before: date, days: int) -> list[DailyBar]:
        self._enter("daily_bars", symbol)
        return [bar for bar in self.bars.get(symbol, []) if bar.day < before][-days:]

    async def puts(self, symbol: str, price: float, today: date) -> list[PutContract]:
        self._enter("puts", symbol)
        return list(self.put_chains.get(symbol, []))


SECTOR_ETFS = ("XLK", "XLF", "XLV", "XLE", "XLY", "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC")


def calm_context(market: FakeMarketData, *, vix: float = 18.0, spy_gap: float = 0.2) -> None:
    """VIX, the index ETFs and the sector ETFs on an ordinary morning, and SPY's history:
    rising, so SPY is above its 50-day average."""
    market.quote_map["$VIX"] = quote("$VIX", vix, 17.5, asset_type="INDEX", sub_type=None)
    spy_bars = rising_bars(400.0, 0.4)
    spy_close = spy_bars[-1].close
    market.bars["SPY"] = spy_bars
    market.quote_map["SPY"] = quote(
        "SPY", spy_close * (1 + spy_gap / 100), spy_close, asset_type="COLLECTIVE_INVESTMENT"
    )
    market.quote_map["QQQ"] = quote("QQQ", 480.5, 480.0, asset_type="COLLECTIVE_INVESTMENT")
    market.quote_map["IWM"] = quote("IWM", 220.2, 220.0, asset_type="COLLECTIVE_INVESTMENT")
    for i, etf in enumerate(SECTOR_ETFS):
        market.quote_map[etf] = quote(
            etf, 100.0 + i / 10, 100.0, asset_type="COLLECTIVE_INVESTMENT"
        )


# ----------------------------------------------------------------------------- events


class FakeEvents:
    def __init__(self) -> None:
        self.calendar: list[EarningsEvent] = []
        self.news: dict[str, list[NewsItem]] = {}
        self.general: list[NewsItem] = []
        self.profiles: dict[str, Profile] = {}
        self.failures: dict[str, Exception] = {}  # method name -> raised on every call
        self.calls: list[tuple[str, Any]] = []

    def _enter(self, name: str, detail: Any) -> None:
        self.calls.append((name, detail))
        if name in self.failures:
            raise self.failures[name]

    def called(self, name: str) -> list[Any]:
        return [detail for called, detail in self.calls if called == name]

    def fail_everything(self) -> None:
        for name in ("earnings_calendar", "company_news", "market_news", "profile"):
            self.failures[name] = EventsUnavailable(f"finnhub {name}: HTTP 503")

    async def earnings_calendar(self, start: date, end: date) -> list[EarningsEvent]:
        self._enter("earnings_calendar", (start, end))
        return [e for e in self.calendar if start <= e.day <= end]

    async def company_news(self, symbol: str, start: date, end: date) -> list[NewsItem]:
        self._enter("company_news", symbol)
        return [n for n in self.news.get(symbol, []) if start <= n.at.date() <= end]

    async def market_news(self, limit: int) -> list[NewsItem]:
        self._enter("market_news", limit)
        return self.general[:limit]

    async def profile(self, symbol: str) -> Profile | None:
        self._enter("profile", symbol)
        return self.profiles.get(symbol)


def news(symbol: str, count: int, *, day: date = TODAY, text: str = "") -> list[NewsItem]:
    at = datetime.combine(day, time(6, 0), tzinfo=ET)
    return [
        NewsItem(
            at=at - timedelta(hours=i),
            source="Wire",
            headline=f"{symbol} headline {i}",
            summary=text or f"{symbol} summary {i}",
        )
        for i in range(count)
    ]
