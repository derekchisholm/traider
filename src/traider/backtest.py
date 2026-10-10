"""Replay historical one-minute bars through the same engine that trades live.

The strategy, the risk checks, the order sizing and the session rules are the
real ones; only the broker is simulated. That makes a backtest a good check that
a strategy and its limits behave as intended. It is a poor forecast of profit:

* orders fill instantly at the bar's closing price plus or minus half the
  spread you choose, with no queue, no partial fills and no market impact
* a quote hook (``on_quote``) only sees one price per minute
* the calendar is weekdays 09:30 to 16:00 New York, with no holidays
"""

from __future__ import annotations

import csv
import json
import os
from collections import Counter, defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from traider.alerts import LogAlerter
from traider.broker.paper import PaperBroker
from traider.config import Config
from traider.control import ControlState, StaticControl
from traider.engine import Engine
from traider.marketdata import MarketData
from traider.models import BPS, Bar, Quote, Side
from traider.research.models import PostureLevel
from traider.research.seed import build_manual_run
from traider.research.source import ResearchSource
from traider.research.store import MemoryResearchStore
from traider.risk import RiskManager
from traider.session import Session, SessionTracker, StaticSessionProvider
from traider.state.memory import MemoryStateStore
from traider.strategy import create_strategy
from traider.timeutil import ET, ManualClock, trading_date

_MINUTE = timedelta(minutes=1)
# After each bar the engine gets a few simulated seconds to see its fill and the new position.
_SETTLE_STEPS_S = (1, 2, 3)


class BacktestError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Trade:
    time: datetime
    symbol: str
    side: Side
    quantity: int
    price: Decimal


@dataclass(frozen=True, slots=True)
class BacktestResult:
    bars: int  # regular-session bars replayed
    start_equity: Decimal
    end_equity: Decimal
    trades: tuple[Trade, ...]
    equity_curve: tuple[tuple[datetime, Decimal], ...]
    open_positions: dict[str, int]
    round_trips: int  # sells, each closing shares bought earlier
    wins: int
    realized_pnl: Decimal
    max_drawdown_pct: Decimal
    blocked: Counter[str]  # how often each risk rule refused an order

    @property
    def return_pct(self) -> Decimal:
        return (self.end_equity - self.start_equity) / self.start_equity * 100


def max_drawdown(equity: Sequence[Decimal]) -> Decimal:
    """Largest fall from a peak to a later low, as a percentage of the peak."""
    worst = Decimal(0)
    peak: Decimal | None = None
    for value in equity:
        if peak is None or value > peak:
            peak = value
        elif peak > 0:
            worst = max(worst, (peak - value) / peak * 100)
    return worst


async def run_backtest(
    bars: Sequence[Bar],
    config: Config,
    *,
    spread_bps: Decimal = Decimal(2),
    starting_cash: Decimal | None = None,
    picks: Sequence[Mapping[str, Any]] | None = None,
) -> BacktestResult:
    """Replay ``bars`` through the engine.

    With ``picks`` (see ``pick_symbols`` and ``load_picks_jsonl``) the engine runs with
    research on: each row is a pick or a posture for one day, and a day with no rows has
    no posture, so it trades nothing.
    """
    if not bars:
        raise BacktestError("no bars to replay")
    research_store = await _research_store(picks) if picks is not None else None
    allowed = [*config.symbols, *(s for s in pick_symbols(picks or ()) if s not in config.symbols)]
    strangers = sorted({bar.symbol for bar in bars} - set(allowed))
    if strangers:
        raise BacktestError(
            f"bars for symbols that are not configured: {', '.join(strangers)} "
            f"(configured: {', '.join(allowed)})"
        )
    # Whatever the configuration says, a backtest only ever trades on paper. Its research
    # (with picks) is its own in-memory store: no research table is ever read.
    config = config.model_copy(update={"trading_mode": "paper", "heartbeat_file": os.devnull})
    cash = starting_cash if starting_cash is not None else config.paper_starting_cash

    calendar = StaticSessionProvider()
    sessions: dict[date, Session | None] = {}
    in_session: list[Bar] = []
    for bar in sorted(bars, key=lambda b: (b.start, allowed.index(b.symbol))):
        day = trading_date(bar.start)
        if day not in sessions:
            sessions[day] = await calendar.session_for(day)
        session = sessions[day]
        if session and session.open and session.close and session.open <= bar.start < session.close:
            in_session.append(bar)

    clock = ManualClock(in_session[0].start if in_session else bars[0].start)
    market = MarketData()
    store = MemoryStateStore("backtest")
    broker = PaperBroker(market, clock, starting_cash=cash)
    research = (
        ResearchSource(research_store, lambda: config.research)
        if research_store is not None
        else None
    )
    engine = Engine(
        config=config,
        clock=clock,
        market=market,
        strategy=create_strategy(config.strategy, config.symbols, config.strategy_params),
        risk=RiskManager(config.risk),
        broker=broker,
        state=store,
        control=ControlState(StaticControl("paper")),
        session=SessionTracker(calendar),
        alerts=LogAlerter(),
        instance_id="backtest",
        on_universe=(lambda _symbols: None) if research is not None else None,
        research=research,
    )
    await engine.start()
    if research is not None:
        await research.refresh(clock.now())

    half_spread = spread_bps / BPS / 2
    closes: dict[str, Decimal] = {}  # only the symbols that printed a bar this minute

    def publish_quotes() -> None:
        now = clock.now()
        for symbol, close in closes.items():
            edge = close * half_spread
            market.on_quote(
                Quote(symbol, close - edge, close + edge, close, ts=now, received_at=now)
            )

    curve: list[tuple[datetime, Decimal]] = []
    days: set[str] = set()
    index = 0
    while index < len(in_session):
        moment = in_session[index].start
        clock.set(moment + _MINUTE)  # the bar is known once its minute has closed
        days.add(trading_date(clock.now()).isoformat())
        closes.clear()  # a symbol with no bar gets no fresh quote, so it cannot trade
        while index < len(in_session) and in_session[index].start == moment:
            bar = in_session[index]
            closes[bar.symbol] = bar.close
            market.on_bar(bar)
            index += 1
        publish_quotes()
        await engine.step()
        for _ in _SETTLE_STEPS_S:
            clock.advance(1)
            publish_quotes()
            await engine.step()
        equity = (await broker.get_account()).equity
        curve.append((moment + _MINUTE, equity if equity is not None else Decimal(0)))

    trades = tuple(
        Trade(
            time=order.entered_at or datetime.min.replace(tzinfo=UTC),
            symbol=order.symbol,
            side=order.side,
            quantity=order.filled_quantity,
            price=order.avg_fill_price or Decimal(0),
        )
        for order in broker.filled_orders()
    )
    round_trips, wins, realized = _score(trades)
    blocked: Counter[str] = Counter()
    for traded_day in sorted(days):
        for event in await store.events(traded_day):
            if event["kind"] == "order_blocked":
                blocked.update(event["data"]["codes"])
    final = await broker.get_account()
    return BacktestResult(
        bars=len(in_session),
        start_equity=cash,
        end_equity=final.equity if final.equity is not None else cash,
        trades=trades,
        equity_curve=tuple(curve),
        open_positions={symbol: p.quantity for symbol, p in final.positions.items()},
        round_trips=round_trips,
        wins=wins,
        realized_pnl=realized,
        max_drawdown_pct=max_drawdown([cash, *(equity for _, equity in curve)]),
        blocked=blocked,
    )


# ------------------------------------------------------------------------ research

_POSTURE_ROW_KEYS = {"day", "posture"}
_BACKTEST_RUN_HOUR = time(8, 0)  # New York: research is written before the open


def pick_symbols(picks: Sequence[Mapping[str, Any]]) -> list[str]:
    """The symbols named by pick rows, in order of first appearance."""
    found: list[str] = []
    for row in picks:
        symbol = row.get("symbol")
        if isinstance(symbol, str) and symbol not in found:
            found.append(symbol)
    return found


@dataclass(frozen=True, slots=True)
class _Day:
    pick_rows: list[dict[str, Any]]
    posture: str


def _where(row: Mapping[str, Any], number: int) -> str:
    """Where a row came from: its file line when it was read from a file, else its position."""
    line = getattr(row, "line", None)
    return f"line {line}" if line is not None else f"row {number}"


def check_picks(picks: Sequence[Mapping[str, Any]]) -> dict[date, _Day]:
    """Validate every row, naming the file line (or row number) of the first bad one.

    A day with a pick row and no posture row is a ``trade`` day.
    """
    pick_rows: dict[date, list[dict[str, Any]]] = {}
    postures: dict[date, str] = {}
    symbols: dict[date, set[str]] = {}
    for number, row in enumerate(picks, start=1):
        where = _where(row, number)
        if not isinstance(row, Mapping):
            raise BacktestError(f"{where}: must be an object")
        raw_day = row.get("day")
        try:
            if not isinstance(raw_day, str):
                raise ValueError
            day = date.fromisoformat(raw_day)
        except ValueError:
            raise BacktestError(f"{where}: 'day' must be a date like 2026-10-06") from None
        pick_rows.setdefault(day, [])
        if "posture" in row:
            extra = sorted(set(row) - _POSTURE_ROW_KEYS)
            if extra:
                raise BacktestError(
                    f"{where}: a posture row has only 'day' and 'posture' (also has "
                    f"{', '.join(extra)})"
                )
            if day in postures:
                raise BacktestError(f"{where}: a second posture for {day}")
            try:
                postures[day] = PostureLevel(row["posture"]).value
            except ValueError as exc:
                raise BacktestError(f"{where}: posture: {exc}") from None
            continue
        fields = {k: v for k, v in row.items() if k != "day"}
        at = datetime.combine(day, _BACKTEST_RUN_HOUR, tzinfo=ET)
        try:
            build_manual_run({"picks": [fields], "posture": {"level": "trade"}}, at)
        except ValueError as exc:
            raise BacktestError(f"{where}: {str(exc).removeprefix('pick 1: ')}") from None
        symbol = fields["symbol"]
        if symbol in symbols.setdefault(day, set()):
            raise BacktestError(f"{where}: {symbol} is already picked for {day}")
        symbols[day].add(symbol)
        pick_rows[day].append(fields)
    return {
        day: _Day(rows, postures.get(day, PostureLevel.TRADE.value))
        for day, rows in sorted(pick_rows.items())
    }


async def _research_store(picks: Sequence[Mapping[str, Any]]) -> MemoryResearchStore:
    """One ``backtest-<day>`` run per day that has rows, written before that day opens.

    A day with no rows at all has no run, so it has no posture and the bot stands aside:
    it fails closed.
    """
    store = MemoryResearchStore()
    for day, content in check_picks(picks).items():
        data: dict[str, Any] = {"picks": content.pick_rows, "posture": {"level": content.posture}}
        at = datetime.combine(day, _BACKTEST_RUN_HOUR, tzinfo=ET)
        meta, day_picks, day_posture = build_manual_run(
            data, at, run_id=f"backtest-{day.isoformat()}", kind="backtest"
        )
        await store.write_run(meta, day_picks, day_posture)
    return store


def _score(trades: Sequence[Trade]) -> tuple[int, int, Decimal]:
    """Match each sell against the oldest shares bought (first in, first out)."""
    lots: dict[str, deque[list[Decimal]]] = defaultdict(deque)  # [quantity, price] per buy
    round_trips = wins = 0
    realized = Decimal(0)
    for trade in trades:
        if trade.side is Side.BUY:
            lots[trade.symbol].append([Decimal(trade.quantity), trade.price])
            continue
        remaining = Decimal(trade.quantity)
        pnl = Decimal(0)
        queue = lots[trade.symbol]
        while remaining > 0 and queue:
            lot = queue[0]
            used = min(remaining, lot[0])
            pnl += used * (trade.price - lot[1])
            lot[0] -= used
            remaining -= used
            if lot[0] == 0:
                queue.popleft()
        round_trips += 1
        wins += pnl > 0
        realized += pnl
    return round_trips, wins, realized


class PickRow(dict[str, Any]):
    """A row read from a file; ``line`` is its line number there, for error messages."""

    line: int


def load_picks_jsonl(path: str | os.PathLike[str]) -> list[PickRow]:
    """Read pick and posture rows from a JSON Lines file. Blank lines are skipped."""
    rows: list[PickRow] = []
    with Path(path).open() as handle:
        for line, text in enumerate(handle, start=1):
            if not text.strip():
                continue
            try:
                row = json.loads(text)
            except ValueError as exc:
                raise BacktestError(f"{path} line {line}: not JSON ({exc})") from None
            if not isinstance(row, dict):
                raise BacktestError(f"{path} line {line}: must be a JSON object")
            picked = PickRow(row)
            picked.line = line
            rows.append(picked)
    return rows


# ------------------------------------------------------------------------------ CSV

_TIME_COLUMNS = ("timestamp", "time", "datetime", "date")


def load_bars_csv(path: str | os.PathLike[str], symbol: str | None = None) -> list[Bar]:
    """Read bars from a CSV with a header row.

    Columns: a time column (``timestamp``, ``time``, ``datetime`` or ``date``), ``open``,
    ``high``, ``low``, ``close``, optional ``volume`` and optional ``symbol``. Times are
    ISO 8601 with a timezone, or Unix seconds or milliseconds.
    """
    bars: list[Bar] = []
    with Path(path).open(newline="") as handle:
        reader = csv.DictReader(handle)
        columns = {(name or "").strip().lower(): name for name in reader.fieldnames or []}
        time_column = next((columns[c] for c in _TIME_COLUMNS if c in columns), None)
        if time_column is None:
            raise BacktestError(f"{path}: no time column (one of {', '.join(_TIME_COLUMNS)})")
        for needed in ("open", "high", "low", "close"):
            if needed not in columns:
                raise BacktestError(f"{path}: missing column '{needed}'")
        if "symbol" not in columns and symbol is None:
            raise BacktestError(f"{path}: no symbol column; pass the symbol with --symbol")
        for line, row in enumerate(reader, start=2):
            try:
                start = _parse_time(row[time_column])
                prices = [
                    Decimal(row[columns[c]].strip()) for c in ("open", "high", "low", "close")
                ]
                volume = int(Decimal((row.get(columns.get("volume", "")) or "0").strip()))
            except (InvalidOperation, ValueError, AttributeError) as exc:
                raise BacktestError(f"{path} line {line}: {exc or type(exc).__name__}") from None
            name = (row[columns["symbol"]] if "symbol" in columns else symbol or "").strip().upper()
            bars.append(Bar(name, start, prices[0], prices[1], prices[2], prices[3], volume))
    bars.sort(key=lambda bar: bar.start)
    return bars


def _parse_time(text: str) -> datetime:
    value = text.strip()
    if value.replace(".", "", 1).isdigit():
        number = float(value)
        return datetime.fromtimestamp(number / 1000 if number >= 1e11 else number, UTC)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp {value!r} has no timezone")
    return parsed.astimezone(UTC)
