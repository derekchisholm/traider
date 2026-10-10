"""Command line: ``traider run | check | login | backtest | settings | research``.

``check`` is the first thing to run against the real Schwab API. It only reads:
it signs in, looks at the account, the calendar and the quotes, and tells you
whether the bot would be able to trade. Nothing in it can place an order.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

import aiohttp
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from traider import app
from traider.backtest import (
    BacktestError,
    BacktestResult,
    check_picks,
    load_bars_csv,
    load_picks_jsonl,
    pick_symbols,
    run_backtest,
)
from traider.broker.base import BrokerError
from traider.broker.schwab import SchwabBroker
from traider.config import Config, ConfigError, RiskLimits, check_symbols
from traider.log import setup_logging
from traider.models import Bar
from traider.research.intraday import run_intraday
from traider.research.run import RunDeps, RunOutcome, run_premarket
from traider.research.scorecard_run import run_scorecard
from traider.research.scrub import scrub
from traider.research.seed import build_manual_run
from traider.research.source import ResearchSource
from traider.research.store import DynamoResearchStore, ResearchStore
from traider.research.wiring import DEFAULT_TRAIL_DIR, SetupError, build_deps, close_llm
from traider.schwab.client import API_BASE, SchwabClient, SchwabError
from traider.schwab.hours import SchwabSessionProvider
from traider.schwab.oauth import (
    TOKEN_URL,
    OAuthError,
    authorize_url,
    code_from_redirect,
    exchange_code,
)
from traider.schwab.parse import ParseError, parse_candles, parse_option_chain, parse_quotes
from traider.schwab.tokens import (
    AuthUnavailable,
    CredentialsError,
    StaticCredentials,
    TokenManager,
    TokenStoreError,
    new_grant,
)
from traider.settings import Settings
from traider.settings_store import (
    DynamoSettingsStore,
    SettingsConflict,
    SettingsInvalid,
    SettingsStore,
    SettingsSuperseded,
)
from traider.timeutil import ET, SystemClock, trading_date

log = logging.getLogger(__name__)


class _Report:
    def __init__(self, out: TextIO) -> None:
        self._out = out
        self.failures = 0

    def ok(self, what: str, detail: str) -> None:
        self._out.write(f"ok    {what}: {detail}\n")

    def fail(self, what: str, detail: str) -> None:
        self.failures += 1
        self._out.write(f"FAIL  {what}: {detail}\n")

    def note(self, detail: str) -> None:
        self._out.write(f"      {detail}\n")

    def finish(self) -> int:
        if self.failures:
            self._out.write(f"\n{self.failures} check(s) failed. The bot would not trade.\n")
            return 1
        self._out.write("\nAll checks passed.\n")
        return 0


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _check_symbol(config: Config) -> tuple[str, str]:
    """The one symbol `check` reads history and an option chain for, and why that one.
    With no pinned symbols (research chooses them) there is nothing of ours to look at,
    so it uses SPY, which is only a probe that the market data works."""
    if config.symbols:
        return config.symbols[0], "first pinned symbol"
    return "SPY", "no pinned symbols, research chooses the symbols"


# ---------------------------------------------------------------------------- check


async def check(
    config: Config,
    out: TextIO,
    *,
    schwab_base_url: str = API_BASE,
    token_url: str = TOKEN_URL,
) -> int:
    report = _Report(out)
    out.write("traider check: read-only, no orders are sent\n")
    pinned = ", ".join(config.symbols) or "none pinned (research chooses)"
    out.write(f"mode {config.trading_mode} | symbols {pinned}\n\n")
    aws = app.Aws(config.aws_region)
    try:
        creds = await asyncio.to_thread(app.credentials(config, aws).load)
    except CredentialsError as exc:
        report.fail("app credentials", str(exc))
        return report.finish()
    if creds is None:
        report.fail("app credentials", "the Schwab app key and secret are not set")
        return report.finish()
    report.ok("app credentials", "present")

    clock = SystemClock()
    tokens = TokenManager(
        store=app.token_store(config, aws),
        credentials=StaticCredentials(creds),
        clock=clock,
        token_url=token_url,
    )
    try:
        await tokens.access_token()
    except AuthUnavailable:
        how = f"sign in at {config.reauth_url}" if config.reauth_url else "run `traider login`"
        report.fail("sign-in", f"{tokens.describe()}. To sign in to Schwab, {how}")
        return report.finish()
    report.ok("sign-in", f"valid, {tokens.seconds_left() / 86400:.1f} days left")

    async with aiohttp.ClientSession() as http:
        client = SchwabClient(http, tokens, base_url=schwab_base_url)
        await _check_account(config, client, clock, report)
        await _check_market(config, client, clock, report)
    return report.finish()


async def _check_account(
    config: Config, client: SchwabClient, clock: SystemClock, report: _Report
) -> None:
    broker = SchwabBroker(
        client, clock, account_hash=config.account_hash, account_last4=config.account_last4
    )
    try:
        chosen = await broker.describe_account()
        account = await broker.get_account()
        open_orders = await broker.get_open_orders()
    except (BrokerError, SchwabError) as exc:
        report.fail("account", str(exc))
        return
    report.ok("account", f"using {chosen}")
    limits = config.risk
    if account.equity is None:
        report.fail("equity", "Schwab did not report it, so the daily loss limit cannot work")
    else:
        report.ok("equity", _money(account.equity))
    if account.cash_available is None:
        if limits.require_cash:
            report.fail(
                "cash",
                "Schwab did not report cash; buys would be blocked (risk.require_cash is on)",
            )
    else:
        report.ok("cash the bot may spend", _money(account.cash_available))
    _check_account_type(account.account_type, limits, report)
    held = [f"{s} {account.position(s)}" for s in config.symbols if account.position(s)]
    report.note(f"positions in configured symbols: {', '.join(held) or 'none'}")
    mine = [o for o in open_orders if o.symbol in config.symbols]
    if mine:
        listing = ", ".join(f"{o.side.value} {o.quantity} {o.symbol} (#{o.order_id})" for o in mine)
        report.fail(
            "open orders",
            f"{listing}. The bot will not trade a symbol with an open order it did not place",
        )
    else:
        report.ok("open orders", f"none on configured symbols ({len(open_orders)} in the account)")


def _check_account_type(kind: str | None, limits: RiskLimits, report: _Report) -> None:
    """Money from a sale settles the next business day. A cash account that buys with
    it and sells again before then commits a good-faith violation; a margin account
    does not have that problem."""
    settled_only = limits.require_cash and limits.settled_cash_only
    if kind == "MARGIN":
        report.ok("account type", "margin")
        if settled_only:
            report.note(
                "risk.settled_cash_only is on, so money from a sale is not spent again until "
                "the next day. A margin account does not need that: set it to false to let "
                "the bot reuse the day's proceeds"
            )
    elif settled_only:
        named = "cash" if kind == "CASH" else "not reported by Schwab, so treated as cash"
        report.ok("account type", f"{named}; the bot only buys with settled cash")
    else:
        named = "cash" if kind == "CASH" else "not reported by Schwab"
        report.fail(
            "account type",
            f"{named}, and the bot may buy with money from a same-day sale. Selling again "
            "before that money settles is a good-faith violation. Turn risk.require_cash "
            "and risk.settled_cash_only back on",
        )


async def _check_market(
    config: Config, client: SchwabClient, clock: SystemClock, report: _Report
) -> None:
    now = clock.now()
    try:
        session = await SchwabSessionProvider(client).session_for(trading_date(now))
    except (SchwabError, ParseError) as exc:
        report.fail("market hours", str(exc))
    else:
        if session is None or session.open is None or session.close is None:
            report.ok("market hours", "no regular session today")
        else:
            state = "open now" if session.view(now).is_open else "closed now"
            report.ok(
                "market hours",
                f"{session.open.astimezone(ET):%H:%M} to {session.close.astimezone(ET):%H:%M} "
                f"New York, {state}",
            )
    symbol, why = _check_symbol(config)
    used_for = "price history and option chain" if config.risk.allow_options else "price history"
    report.note(f"checked {symbol} for {used_for}: {why}")
    quoted = config.symbols or (symbol,)
    try:
        quotes = parse_quotes(await client.quotes(quoted), now)
    except SchwabError as exc:
        report.fail("quotes", str(exc))
        quotes = {}
    else:
        for name in quoted:
            quote = quotes.get(name)
            if quote is None:
                report.fail(f"quote {name}", "Schwab returned no quote for this symbol")
            elif quote.delayed:
                report.fail(
                    f"quote {name}",
                    f"{quote.bid:.2f} x {quote.ask:.2f} but delayed; "
                    "the bot only trades on real-time quotes",
                )
            else:
                report.ok(f"quote {name}", f"{quote.bid:.2f} x {quote.ask:.2f}")
    try:
        raw = await client.price_history(symbol, now - timedelta(days=5), now)
        bars = parse_candles(raw, symbol)
    except SchwabError as exc:
        report.fail("price history", str(exc))
    else:
        last = f", last close {bars[-1].close}" if bars else ""
        report.ok("price history", f"{len(bars)} one-minute bars for {symbol}{last}")
    if config.risk.allow_options:
        await _check_options(config, client, now, report)


async def _check_options(
    config: Config, client: SchwabClient, now: datetime, report: _Report
) -> None:
    """Options are on: can the bot see a chain, and a real-time quote for a contract?"""
    symbol, _ = _check_symbol(config)
    today = trading_date(now)
    try:
        raw = await client.option_chain(
            symbol,
            today,
            today + timedelta(days=config.option_chain_days),
            strikes=config.option_chain_strikes,
        )
    except SchwabError as exc:
        report.fail("option chain", str(exc))
        return
    chain = parse_option_chain(raw)
    if not chain:
        report.fail(
            "option chain",
            f"Schwab returned no option contracts for {symbol} in the next "
            f"{config.option_chain_days} days",
        )
        return
    report.ok("option chain", f"{len(chain)} contracts for {symbol}")
    contract = chain[len(chain) // 2].symbol
    try:
        raw_quotes = await client.quotes([contract])
        quote = parse_quotes(raw_quotes, now).get(contract)
    except SchwabError as exc:
        report.fail("option quote", str(exc))
        return
    if quote is None:
        report.fail("option quote", f"Schwab returned no quote for {contract}")
    elif quote.delayed:
        report.fail(
            "option quote",
            f"{contract}: {quote.bid:.2f} x {quote.ask:.2f} but delayed; "
            "the bot only trades on real-time quotes",
        )
    elif quote.halted:
        status = raw_quotes.get(contract, {}).get("quote", {}).get("securityStatus")
        report.fail(
            "option quote",
            f"{contract}: Schwab reports its status as {status!r}, not 'Normal'; "
            "the bot would not trade it",
        )
    elif quote.bid <= 0:
        report.fail(
            "option quote",
            f"{contract}: no bid ({quote.bid:.2f} x {quote.ask:.2f}); the bot would not trade it",
        )
    else:
        report.ok("option quote", f"{contract}: {quote.bid:.2f} x {quote.ask:.2f}")


# ---------------------------------------------------------------------------- login


def login(
    config: Config,
    out: TextIO,
    *,
    read_line: Callable[[str], str] = input,
    token_url: str = TOKEN_URL,
    now: Callable[[], float] = time.time,
) -> int:
    """Sign in to Schwab by hand: open a link, log in, paste the address you land on."""
    if not (config.schwab_token_secret_id or config.schwab_token_file):
        out.write(
            "Nowhere to store the sign-in. Set TRAIDER_SCHWAB_TOKEN_SECRET_ID (AWS) or "
            "TRAIDER_SCHWAB_TOKEN_FILE (local).\n"
        )
        return 1
    aws = app.Aws(config.aws_region)
    try:
        creds = app.credentials(config, aws).load()
    except CredentialsError as exc:
        out.write(f"{exc}\n")
        return 1
    if creds is None:
        out.write("The Schwab app key and secret are not set.\n")
        return 1
    callback = config.schwab_callback_url or "https://127.0.0.1"
    state = secrets.token_urlsafe(16)
    out.write(
        "1. Open this link, log in to Schwab and approve access:\n\n"
        f"   {authorize_url(creds.app_key, callback, state)}\n\n"
        f"2. Your browser is then sent to an address starting with {callback}\n"
        "   (an error page is expected). Copy the whole address and paste it below.\n"
        "   You have about 30 seconds.\n\n"
    )
    out.flush()
    try:
        code, returned = code_from_redirect(read_line("Address: "))
    except OAuthError as exc:
        out.write(f"That does not look right: {exc}\n")
        return 1
    if returned is not None and returned != state:
        out.write(
            "The state in that address does not match: it is from a different sign-in "
            "attempt. Start again.\n"
        )
        return 1
    try:
        grant = new_grant(exchange_code(creds, code, callback, token_url=token_url), now())
        app.token_store(config, aws).save(grant)
    except OAuthError as exc:
        out.write(
            f"Schwab did not accept the code ({exc}). Codes are single-use and expire "
            "within about 30 seconds. Start again.\n"
        )
        return 1
    except TokenStoreError as exc:
        out.write(f"Signed in, but the sign-in could not be stored: {exc}\n")
        return 1
    expires = time.strftime("%A %d %B %Y %H:%M UTC", time.gmtime(grant.expires_at))
    out.write(f"Signed in. Valid for 7 days, until {expires}.\n")
    return 0


# ------------------------------------------------------------------------- backtest


async def download_bars(
    config: Config,
    *,
    days: int,
    picks: Sequence[Mapping[str, Any]] | None = None,
    schwab_base_url: str = API_BASE,
    token_url: str = TOKEN_URL,
) -> list[Bar]:
    """One-minute regular-session bars from Schwab for the pinned symbols and, with
    ``picks``, every symbol the picks name."""
    symbols = _backtest_symbols(config, picks)
    if not symbols:
        raise BacktestError("no symbols to download: pin symbols or give --picks")
    try:
        check_symbols(tuple(symbols))
    except ValueError as exc:  # a pick row names something that is not an equity symbol
        raise BacktestError(f"cannot download bars: {exc}") from None
    aws = app.Aws(config.aws_region)
    clock = SystemClock()
    tokens = TokenManager(
        store=app.token_store(config, aws),
        credentials=app.credentials(config, aws),
        clock=clock,
        token_url=token_url,
    )
    now = clock.now()
    bars: list[Bar] = []
    async with aiohttp.ClientSession() as http:
        client = SchwabClient(http, tokens, base_url=schwab_base_url)
        for symbol in symbols:
            raw = await client.price_history(symbol, now - timedelta(days=days), now)
            bars.extend(parse_candles(raw, symbol))
    return bars


def _backtest_symbols(config: Config, picks: Sequence[Mapping[str, Any]] | None) -> list[str]:
    """The pinned symbols, then the symbols named by pick rows."""
    return [*config.symbols, *(s for s in pick_symbols(picks or ()) if s not in config.symbols)]


def print_result(
    result: BacktestResult,
    config: Config,
    out: TextIO,
    picks: Sequence[Mapping[str, Any]] | None = None,
) -> None:
    symbols = ", ".join(_backtest_symbols(config, picks)) or "none"
    out.write(f"Backtest: {symbols} | {config.strategy} {config.strategy_params}\n")
    out.write(f"{result.bars} bars replayed, {len(result.trades)} trades\n\n")
    out.write(f"Start equity   {_money(result.start_equity):>14}\n")
    out.write(f"End equity     {_money(result.end_equity):>14}\n")
    out.write(f"Return         {result.return_pct:>13.2f}%\n")
    out.write(f"Max drawdown   {result.max_drawdown_pct:>13.2f}%\n")
    out.write(
        f"Round trips    {result.round_trips:>14}  ({result.wins} profitable, "
        f"realized {_money(result.realized_pnl)})\n"
    )
    positions = ", ".join(f"{s} {q}" for s, q in result.open_positions.items()) or "none"
    out.write(f"Open at end    {positions:>14}\n")
    if result.blocked:
        rules = ", ".join(f"{code} x{count}" for code, count in result.blocked.most_common())
        out.write(f"Blocked by risk rules: {rules}\n")
    if result.trades:
        out.write("\nTrades (New York time):\n")
        shown = result.trades[:50]
        for trade in shown:
            out.write(
                f"  {trade.time.astimezone(ET):%Y-%m-%d %H:%M}  {trade.side.value:<4} "
                f"{trade.quantity:>5} {trade.symbol:<6} @ {trade.price:.2f}\n"
            )
        if len(result.trades) > len(shown):
            out.write(f"  ... and {len(result.trades) - len(shown)} more\n")
    out.write(
        "\nFills are simulated at each bar's close plus or minus half the spread, with no\n"
        "queueing, partial fills or market impact. This checks that the strategy and its\n"
        "limits behave as intended. It does not predict live results.\n"
    )


async def _backtest(args: argparse.Namespace, config: Config, out: TextIO) -> int:
    picks = None
    if args.picks is not None:
        picks = load_picks_jsonl(args.picks)
        check_picks(picks)  # a bad file fails here, before any download
    if args.csv:
        bars = [bar for path in args.csv for bar in load_bars_csv(path, args.symbol)]
    else:
        bars = await download_bars(config, days=args.schwab_days, picks=picks)
    result = await run_backtest(
        bars,
        config,
        spread_bps=Decimal(str(args.spread_bps)),
        starting_cash=Decimal(str(args.cash)) if args.cash is not None else None,
        picks=picks,
    )
    print_result(result, config, out, picks)
    return 0


# ------------------------------------------------------------------------- settings


async def settings_show(
    store: SettingsStore, out: TextIO, err: TextIO, *, version: int | None = None
) -> int:
    """Print one version's settings as JSON on ``out`` and nothing else, so the output can be
    saved and given back to ``apply``. The version line and any problem go to ``err``."""
    try:
        shown = await (store.latest() if version is None else store.get(version))
    except SettingsInvalid as exc:
        err.write(f"{exc}\n")
        return 1
    if shown is None:
        if version is None:
            err.write("no settings stored yet; the bot writes version 1 when it first starts\n")
        else:
            err.write(f"no settings version {version}; `traider settings history` lists them\n")
        return 1
    err.write(f"version {shown.version} by {shown.author} at {shown.at.isoformat()}\n")
    out.write(json.dumps(shown.settings.model_dump(mode="json"), indent=2) + "\n")
    return 0


async def settings_history(store: SettingsStore, out: TextIO, *, limit: int = 20) -> int:
    for version in await store.history(limit):
        changed = ", ".join(version.diff) or "-"
        out.write(
            f"{version.version} {version.at.isoformat()} {version.author} "
            f"[{changed}] {version.note}\n"
        )
    return 0


def _read_settings_file(path: str) -> Settings:
    with open(path, encoding="utf-8") as handle:
        return Settings.model_validate(json.load(handle))


async def settings_apply(
    store: SettingsStore, path: str, out: TextIO, *, note: str, now: datetime
) -> int:
    try:
        settings = await asyncio.to_thread(_read_settings_file, path)
    except (OSError, ValueError, ValidationError) as exc:
        out.write(f"invalid settings file: {exc}\n")
        return 1
    try:
        latest = await store.latest()
        expected = latest.version if latest is not None else 0
    except SettingsInvalid as exc:
        if exc.version < 0:
            # No version number can be read, so there is nothing to write on top of.
            out.write(
                "newest settings item is damaged and has no readable version; "
                "delete it from the settings table, then apply again\n"
            )
            return 1
        # The newest version is damaged. Writing on top of it is how an operator repairs it.
        expected = exc.version
    try:
        written = await store.write(
            settings, expected_version=expected, author="cli", note=note, now=now
        )
    except SettingsSuperseded as exc:
        out.write(
            f"written as version {exc.written}, but version {exc.newest} was written at the "
            "same time and is now the newest version; run `traider settings show`\n"
        )
        return 1
    except SettingsConflict as exc:
        out.write(f"not written: {exc}. Run `traider settings show` and try again.\n")
        return 1
    out.write(f"wrote version {written.version}\n")
    for key, (old, new) in written.diff.items():
        out.write(f"  {key}: {old} -> {new}\n")
    return 0


def _settings_store(config: Config) -> SettingsStore:
    assert config.settings_table is not None
    return DynamoSettingsStore(app.Aws(config.aws_region).table(config.settings_table))


# ------------------------------------------------------------------------- research


def _read_seed_file(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


async def research_seed(store: ResearchStore, path: str, out: TextIO, *, now: datetime) -> int:
    """Write one hand-made research run. The whole file is checked first: a bad file writes
    nothing."""
    try:
        data = await asyncio.to_thread(_read_seed_file, path)
        meta, picks, posture = build_manual_run(data, now)
    except (OSError, ValueError) as exc:  # includes bad JSON and pydantic's ValidationError
        out.write(f"invalid seed file: {exc}\n")
        return 1
    await store.write_run(meta, picks, posture)
    shown = posture.level.value if posture is not None else "none"
    out.write(f"wrote run {meta.run_id}: {len(picks)} pick(s), posture {shown}\n")
    return 0


async def research_show(source: ResearchSource, out: TextIO, *, now: datetime) -> int:
    """Read research once, the way the bot does, and print what it would act on."""
    await source.refresh(now)
    view = source.view
    if view.as_of != now:
        # A failed read keeps the empty view; do not let that pass for "no research".
        out.write("research could not be read: check the research table and your AWS access\n")
        return 1
    if view.posture is None:
        note = "no posture today, so the bot stands aside"
    else:
        note = "; ".join(view.posture.reasons)
    out.write(f"posture {view.level.value}{': ' + note if note else ''}\n")
    live = sorted(view.live_picks(now).values(), key=lambda p: (-p.score, p.rank, p.symbol))
    if not live:
        out.write("no live picks\n")
    for p in live:
        out.write(
            f"{p.rank} {p.symbol} {p.side.value} {p.horizon.value} {p.score} "
            f"{p.expires_at.astimezone(ET):%Y-%m-%d %H:%M} New York\n"
        )
    return 0


RESEARCH_KINDS = ("premarket", "intraday", "scorecard")

DRY_RUN_NOTICE = (
    "Dry run: real calls to Schwab, Finnhub and Claude on Amazon Bedrock. The Bedrock calls "
    "cost real money (about $1-2 a run with the default model). Nothing is written to the "
    "research table (no picks, posture, cost or lock) and no alert is sent. The trail is "
    "written to {where}.\n\n"
)

INTRADAY_DRY_RUN_NOTICE = (
    "Dry run: real calls to Schwab, Finnhub and Claude on Amazon Bedrock, and reads of the "
    "research table and the bot's ledger. The Bedrock calls cost real money (at most "
    "research_jobs.budget.intraday_run_usd, $0.75 by default). It needs the session open "
    "and an ok posture today, and ignores research_jobs.intraday.last_start. Nothing is "
    "written to the research table (no picks, posture, cost or lock) and no alert is sent. "
    "The trail is written to {where}.\n\n"
)

SCORECARD_DRY_RUN_NOTICE = (
    "Dry run: real calls to Schwab (daily bars) and reads of the research table and the "
    "bot's event log; no model is called. Nothing is written to the research table (no "
    "outcomes, summary or lock) and no alert is sent. The trail is written to {where}.\n\n"
)

BuildDeps = Callable[..., Awaitable[RunDeps]]
Runner = Callable[..., Awaitable[RunOutcome]]


def _runners() -> dict[str, Runner]:
    """Each kind's runner. Built when the run starts, so tests can replace a runner."""
    return {"premarket": run_premarket, "intraday": run_intraday, "scorecard": run_scorecard}


def _runner(kind: str) -> Runner:
    try:
        return _runners()[kind]
    except KeyError:
        raise ValueError(f"no runner for research kind {kind!r}") from None


def _error_text(exc: BaseException) -> str:
    """An exception as text that is safe to print: scrubbed, like META and alerts."""
    text = str(exc) if isinstance(exc, SetupError) else f"{type(exc).__name__}: {exc}"
    return scrub(text)


async def research_run(
    config: Config,
    out: TextIO,
    *,
    kind: str,
    dry_run: bool,
    force: bool,
    trail_dir: str,
    build: BuildDeps = build_deps,
    now: datetime | None = None,
) -> int:
    """One research run now. Returns the run's exit code: 0 done (or nothing to do),
    1 failed or could not start, 2 another run holds the lock.

    ``now`` is read after the setup, right before the run: the lock's lifetime counts from
    it. Every error printed is scrubbed. The HTTP session and the model client are closed
    on every path."""
    if kind not in RESEARCH_KINDS:
        out.write(f"unknown research kind {kind!r}; one of {', '.join(RESEARCH_KINDS)}\n")
        return 2
    clock = SystemClock()
    if dry_run:
        notices = {"scorecard": SCORECARD_DRY_RUN_NOTICE, "intraday": INTRADAY_DRY_RUN_NOTICE}
        notice = notices.get(kind, DRY_RUN_NOTICE)
        out.write(notice.format(where=trail_dir))
        out.flush()
    async with aiohttp.ClientSession() as http:
        try:
            deps = await build(
                config, http, kind=kind, dry_run=dry_run, trail_dir=Path(trail_dir), clock=clock
            )
        except Exception as exc:
            text = _error_text(exc)
            log.error("the research run cannot start: %s", text)
            out.write(f"cannot start the research run: {text}\n")
            return 1
        try:
            started = now or clock.now()
            outcome = await _runner(kind)(deps, started, dry_run=dry_run, force=force)
        except Exception as exc:
            # A run records its own failures; this is a bug, so fail closed.
            text = _error_text(exc)
            log.error("the research run failed: %s", text)
            out.write(f"research run failed: {text}\n")
            return 1
        finally:
            await close_llm(deps.llm)
    if dry_run:
        out.write(json.dumps(outcome.report(), indent=2) + "\n")
    else:
        detail = f" ({outcome.detail})" if outcome.detail else ""
        out.write(f"research {kind} {outcome.status}: {outcome.run_id}{detail}\n")
    return outcome.exit_code


def _research_store(config: Config) -> ResearchStore:
    assert config.research_table is not None
    return DynamoResearchStore(app.Aws(config.aws_region).table(config.research_table))


# ----------------------------------------------------------------------------- main


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="traider",
        description="Rule-based trading bot for a Schwab account. "
        "Configuration comes from TRAIDER_* environment variables.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="run the bot until stopped")
    commands.add_parser("check", help="read-only check of sign-in, account, calendar and quotes")
    commands.add_parser("login", help="sign in to Schwab by pasting the redirect address")
    backtest = commands.add_parser("backtest", help="replay one-minute bars through the bot")
    source = backtest.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", action="append", metavar="FILE", help="bars from a CSV file")
    source.add_argument(
        "--schwab-days",
        type=int,
        metavar="N",
        help="download the last N days of bars from Schwab (needs a sign-in)",
    )
    backtest.add_argument("--symbol", help="symbol for CSV files that have no symbol column")
    backtest.add_argument(
        "--spread-bps",
        type=float,
        default=2.0,
        help="assumed bid-ask spread in basis points (default 2)",
    )
    backtest.add_argument("--cash", type=float, help="starting cash (default from configuration)")
    backtest.add_argument(
        "--picks",
        metavar="FILE",
        help="replay research: a JSON Lines file of picks and postures, one row per line, "
        'each with a "day"',
    )
    settings = commands.add_parser("settings", help="read or change the bot's versioned settings")
    actions = settings.add_subparsers(dest="action", required=True)
    show = actions.add_parser("show", help="print the settings in force as JSON")
    show.add_argument("--version", type=int, metavar="N", help="print version N instead")
    history = actions.add_parser("history", help="list earlier versions")
    history.add_argument("--limit", type=int, default=20)
    apply = actions.add_parser("apply", help="write a JSON file as the next version")
    apply.add_argument("file")
    apply.add_argument("--note", default="", help="why, kept with the version")
    research = commands.add_parser(
        "research", help="run the research jobs, or write or read research"
    )
    research_actions = research.add_subparsers(dest="action", required=True)
    run_research = research_actions.add_parser(
        "run", help="run a research job now (what its schedule runs)"
    )
    run_research.add_argument("--kind", required=True, choices=RESEARCH_KINDS)
    run_research.add_argument(
        "--dry-run",
        action="store_true",
        help="make every real call but write nothing to the research table and send no "
        "alert; print what the run decided. premarket and intraday make real Bedrock calls "
        "(they cost money); the scorecard calls no model",
    )
    run_research.add_argument(
        "--force",
        action="store_true",
        help="run even if today's run already finished; for intraday, start after "
        "research_jobs.intraday.last_start",
    )
    run_research.add_argument(
        "--trail-dir",
        default=DEFAULT_TRAIL_DIR,
        metavar="DIR",
        help=f"where the trail goes without a trail bucket, and on a dry run "
        f"(default {DEFAULT_TRAIL_DIR})",
    )
    seed = research_actions.add_parser("seed", help="write a JSON file as a manual research run")
    seed.add_argument("file")
    research_actions.add_parser("show", help="print the posture and live picks the bot would see")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = Config.from_env(os.environ)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if args.command == "run":
        setup_logging(config.log_level)
        try:
            asyncio.run(app.run(config))
        except KeyboardInterrupt:
            return 0
        except Exception:
            log.exception("traider stopped with an error")
            return 1
        return 0
    logging.basicConfig(level=logging.ERROR, stream=sys.stderr)
    try:
        if args.command == "check":
            return asyncio.run(check(config, sys.stdout))
        if args.command == "login":
            return login(config, sys.stdout)
        if args.command == "settings":
            if not config.settings_table:
                print("TRAIDER_SETTINGS_TABLE is not set", file=sys.stderr)
                return 2
            store = _settings_store(config)
            if args.action == "show":
                return asyncio.run(
                    settings_show(store, sys.stdout, sys.stderr, version=args.version)
                )
            if args.action == "history":
                return asyncio.run(settings_history(store, sys.stdout, limit=args.limit))
            return asyncio.run(
                settings_apply(
                    store, args.file, sys.stdout, note=args.note, now=SystemClock().now()
                )
            )
        if args.command == "research":
            if not config.research_table:
                print("TRAIDER_RESEARCH_TABLE is not set", file=sys.stderr)
                return 2
            if args.action == "run":
                if args.dry_run:
                    logging.getLogger().setLevel(logging.WARNING)
                else:
                    setup_logging(config.log_level)
                return asyncio.run(
                    research_run(
                        config,
                        sys.stdout,
                        kind=args.kind,
                        dry_run=args.dry_run,
                        force=args.force,
                        trail_dir=args.trail_dir,
                    )
                )
            research_store = _research_store(config)
            if args.action == "seed":
                return asyncio.run(
                    research_seed(research_store, args.file, sys.stdout, now=SystemClock().now())
                )
            return asyncio.run(
                research_show(
                    ResearchSource(research_store, lambda: config.research),
                    sys.stdout,
                    now=SystemClock().now(),
                )
            )
        return asyncio.run(_backtest(args, config, sys.stdout))
    except (
        BacktestError,
        SchwabError,
        ParseError,
        OSError,
        BotoCoreError,
        ClientError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
