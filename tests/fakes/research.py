"""Stand-ins for what the research jobs talk to: Schwab market data, Finnhub and Bedrock.

Each records what it was asked and can be told to fail.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from traider.research.events import EarningsEvent, EventsUnavailable, NewsItem, Profile
from traider.research.llm import LLMError, LLMReply, Usage
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
        # symbol -> raised by a symbol-scoped earnings_calendar call for it
        self.symbol_failures: dict[str, Exception] = {}
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

    async def earnings_calendar(
        self, start: date, end: date, symbol: str | None = None
    ) -> list[EarningsEvent]:
        if symbol is None:
            self._enter("earnings_calendar", (start, end))
        else:
            self._enter("earnings_calendar", (start, end, symbol))
            if symbol in self.symbol_failures:
                raise self.symbol_failures[symbol]
        return [
            e
            for e in self.calendar
            if start <= e.day <= end and (symbol is None or e.symbol == symbol)
        ]

    async def company_news(self, symbol: str, start: date, end: date) -> list[NewsItem]:
        self._enter("company_news", symbol)
        found = [n for n in self.news.get(symbol, []) if start <= n.at.date() <= end]
        return sorted(found, key=lambda n: n.at, reverse=True)

    async def market_news(self, limit: int) -> list[NewsItem]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._enter("market_news", limit)
        return sorted(self.general, key=lambda n: n.at, reverse=True)[:limit]

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


# -------------------------------------------------------------------------------- LLM


def tool_use(name: str, tool_input: dict[str, Any], *, call_id: str | None = None) -> dict:
    return {"type": "tool_use", "id": call_id or f"toolu_{name}", "name": name, "input": tool_input}


def reply(*blocks: dict, input_tokens: int = 1000, output_tokens: int = 200) -> LLMReply:
    return LLMReply(tuple(blocks), Usage(input_tokens, output_tokens), "tool_use")


def submit(**fields: Any) -> LLMReply:
    return reply(tool_use("submit_assessment", fields, call_id="toolu_submit"))


def posture_reply(
    level: str, *reasons: str, input_tokens: int = 1000, output_tokens: int = 200
) -> LLMReply:
    return reply(
        tool_use("submit_posture", {"level": level, "reasons": list(reasons)}),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


class ScriptedLLM:
    """Answers from scripts: one for the posture review, one per deep-dive symbol (dives
    run concurrently, so each is routed by the ``Symbol:`` line that starts it). Every
    request is recorded. An exhausted script, or an exception in it, raises."""

    def __init__(
        self,
        *,
        posture: Sequence[LLMReply | Exception] = (),
        dives: dict[str, Sequence[LLMReply | Exception]] | None = None,
    ) -> None:
        self.posture = list(posture)
        self.dives = {symbol: list(script) for symbol, script in (dives or {}).items()}
        self.requests: list[dict[str, Any]] = []

    def requests_for(self, symbol: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if _dive_symbol(r) == symbol]

    async def create(self, **request: Any) -> LLMReply:
        request = copy.deepcopy(request)  # as sent: the caller keeps adding to its messages
        self.requests.append(request)
        names = {tool["name"] for tool in request["tools"]}
        if "submit_posture" in names:
            script = self.posture
        else:
            script = self.dives.setdefault(_dive_symbol(request) or "?", [])
        if not script:
            raise LLMError("script exhausted")
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _dive_symbol(request: dict[str, Any]) -> str | None:
    first = request["messages"][0]["content"]
    if isinstance(first, str) and first.startswith("Symbol: "):
        return first.split("\n", 1)[0].removeprefix("Symbol: ").strip()
    return None


# ----------------------------------------------------------------- a whole morning


def market_day() -> tuple[FakeMarketData, FakeEvents]:
    """One fixed pre-market morning, 2026-10-09 at 08:00 New York.

    Eight candidates. Four survive the screen: NVDA (gap +4%, 3x volume), AMD (gap -4%,
    2x volume, reported last night), MSFT (flat, very liquid) and PLTR (gap +2%). TINY is
    under $5, OTCX trades over the counter, ETFQ is an ETF and NEWCO has 30 days of history.
    The market is calm: VIX 18, SPY up 0.2% and above its 50-day average.
    """
    market = FakeMarketData()
    calm_context(market)
    market.mover_lists = {
        ("EQUITY_ALL", "PERCENT_CHANGE_UP"): ["NVDA", "PLTR", "TINY"],
        ("EQUITY_ALL", "PERCENT_CHANGE_DOWN"): ["AMD"],
        ("NYSE", "VOLUME"): ["ETFQ", "NEWCO"],
        ("NASDAQ", "VOLUME"): ["MSFT", "NVDA", "OTCX"],
    }
    market.quote_map.update(
        {
            "NVDA": quote("NVDA", 104.0, 100.0, avg_volume=1_000_000, high_52w=130.0),
            "AMD": quote("AMD", 48.0, 50.0, avg_volume=2_000_000),
            "MSFT": quote("MSFT", 401.0, 400.0, avg_volume=1_000_000),
            "PLTR": quote("PLTR", 20.4, 20.0, avg_volume=3_000_000),
            "TINY": quote("TINY", 2.0, 1.9),
            "OTCX": quote("OTCX", 10.0, 9.0, exchange="OTC Markets"),
            "ETFQ": quote("ETFQ", 50.0, 49.0, sub_type="ETF"),
            "NEWCO": quote("NEWCO", 30.0, 29.0),
        }
    )
    market.bars.update(
        {
            "NVDA": flat_bars(100.0, 1_000_000, last_volume=3_000_000),
            "AMD": flat_bars(50.0, 1_000_000, last_volume=2_000_000),
            "MSFT": flat_bars(400.0, 1_000_000),
            "PLTR": flat_bars(20.0, 1_000_000, last_volume=1_500_000),
            "NEWCO": flat_bars(29.0, 1_000_000, n=30),
        }
    )
    market.put_chains["AMD"] = [
        PutContract(
            symbol="AMD   261023P00048000",
            strike=48.0,
            days=14,
            bid=1.0,
            ask=1.05,
            open_interest=500,
        )
    ]
    events = FakeEvents()
    events.calendar = [EarningsEvent(symbol="AMD", day=date(2026, 10, 8), hour="amc")]
    events.news = {"NVDA": news("NVDA", 5), "AMD": news("AMD", 2), "PLTR": news("PLTR", 1)}
    events.general = news("MARKET", 3)
    events.profiles = {
        "NVDA": Profile(symbol="NVDA", industry="Semiconductors", market_cap_m=2.5e6),
        "AMD": Profile(symbol="AMD", industry="Semiconductors", market_cap_m=2.4e5),
        "MSFT": Profile(symbol="MSFT", industry="Technology", market_cap_m=3.0e6),
        "PLTR": Profile(symbol="PLTR", industry="Technology", market_cap_m=1.5e5),
    }
    return market, events


NVDA_SWING = {
    "side": "long",
    "horizon": "swing",
    "score": 82,
    "thesis": "Gap up on three times normal volume, holding above its averages.",
    "invalidation": 101.0,
    "swing_days": 5,
    "risks": ["export rules", "crowded trade"],
}
AMD_BEARISH = {
    "side": "bearish",
    "horizon": "intraday",
    "score": 70,
    "thesis": "Weak guidance after last night's report; gap down below its averages.",
    "invalidation": 49.5,
    "risks": ["short squeeze"],
}
PLTR_LONG = {
    "side": "long",
    "horizon": "intraday",
    "score": 65,
    "thesis": "Steady buying into a +2% gap.",
    "invalidation": 20.0,
    "risks": [],
}
MSFT_PASS = {
    "side": "pass",
    "horizon": "intraday",
    "score": 20,
    "thesis": "Nothing new.",
    "invalidation": 390.0,
    "risks": [],
}


def golden_llm() -> ScriptedLLM:
    """The model's side of the golden morning: posture reduced; NVDA looks at bars then
    goes long swing; AMD bearish intraday; MSFT passes; PLTR first submits a score of 150,
    is told why it is wrong, and resubmits."""
    return ScriptedLLM(
        posture=[posture_reply("reduced", "CPI at 08:30")],
        dives={
            "NVDA": [reply(tool_use("daily_bars", {"days": 20}, call_id="t1")),
                     submit(**NVDA_SWING)],
            "AMD": [submit(**AMD_BEARISH)],
            "MSFT": [submit(**MSFT_PASS)],
            "PLTR": [submit(**(PLTR_LONG | {"score": 150})), submit(**PLTR_LONG)],
        },
    )  # fmt: skip
