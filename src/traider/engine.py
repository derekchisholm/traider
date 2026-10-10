"""The trading engine.

One loop, strictly sequential: read controls, look at the account, hear the
strategy, then for each symbol trade the difference between what the strategy
wants and what the broker says is held.

The rules that keep it from doing damage:

* **The broker is the source of truth.** Positions are never tracked locally.
  Right before any order the engine re-reads the account and the open orders.
* **One order per symbol at a time**, and none while another order's outcome
  is unclear.
* **Never resend on doubt.** If a placement might have gone through, the engine
  waits for the position to show what happened. If the account then disagrees
  with a confirmed fill, the symbol is frozen and a person is alerted.
* **Fail closed.** No control value, no lease, no session hours, no fresh
  account, no usable quote: no order.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path
from typing import Any

from traider.alerts import Alerter
from traider.broker.base import Broker, BrokerUnavailable, OrderRejected
from traider.config import Config
from traider.control import ControlMode, ControlState, Permissions, effective_permissions
from traider.marketdata import MarketData
from traider.models import (
    BPS,
    AccountSnapshot,
    BrokerOrder,
    OrderRecord,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    Target,
)
from traider.options import contract_size, is_option_symbol, parse_option_symbol
from traider.research.models import PostureLevel
from traider.research.source import ResearchSource, ResearchUpdate
from traider.risk import Decision, ResearchGate, RiskContext, RiskManager
from traider.session import SessionTracker
from traider.settings import Settings
from traider.settings_store import LiveSettings, SettingsUpdate
from traider.state.base import DayState, LedgerEntry, StateStore
from traider.strategy.base import Strategy, StrategyContext
from traider.timeutil import Clock, previous_weekday, trades_without_settling, trading_date
from traider.universe import compute_universe, root_symbol

log = logging.getLogger(__name__)

_CENT = Decimal("0.01")
_HALTED = Permissions(False, False, False, "control not read yet")
# Used when the research gate cannot be built: it blocks every entry and no exit.
_NO_ENTRIES_GATE = ResearchGate(pick_side=None, pinned=False, posture="stand_aside")


@dataclass(slots=True)
class _Pending:
    """A position change the engine is waiting to see in the broker's account."""

    expected: int
    before: int
    since: datetime
    confirmed: bool  # True: the broker reported the fill. False: the order's fate is unknown.
    order: OrderRequest | None = None  # set when unconfirmed, so the order can be looked up
    search_attempt_at: datetime | None = None


@dataclass(slots=True)
class _SymbolState:
    target: Target | None = None
    working: OrderRecord | None = None
    pending: _Pending | None = None
    frozen: str | None = None
    hold_until: datetime | None = None
    last_order_at: datetime | None = None
    last_poll_at: datetime | None = None
    poll_failures: int = 0
    unknown_since: datetime | None = None
    last_block: tuple[frozenset[str], datetime] | None = None


class Engine:
    # Intervals, in seconds.
    CONTROL_REFRESH_S = 10.0
    SETTINGS_REFRESH_S = 10.0
    SETTINGS_ALERT_AFTER_S = 300.0
    LEASE_TTL_S = 30.0
    LEASE_RENEW_S = 10.0
    LEASE_MARGIN_S = 5.0  # no order is sent this close to the lease running out
    ACCOUNT_REFRESH_S = 30.0
    ACCOUNT_RETRY_S = 2.0  # while the snapshot is known to be out of date
    ORDER_POLL_S = 1.0
    POST_ORDER_HOLD_S = 2.0
    PRETRADE_RETRY_S = 5.0
    UNAVAILABLE_HOLD_S = 10.0
    REJECT_HOLD_S = 60.0
    UNKNOWN_ORDER_HOLD_S = 15.0
    UNKNOWN_ORDER_ALERT_S = 120.0
    FIND_RETRY_S = 5.0
    FIND_CLOCK_SKEW_S = 10.0  # tolerance between our clock and the broker's order timestamps
    UNCONFIRMED_TIMEOUT_S = 60.0
    SETTLE_TIMEOUT_S = 120.0
    STUCK_ORDER_ALERT_S = 300.0
    BLOCK_LOG_INTERVAL_S = 60.0
    DAY_RETRY_S = 5.0
    LEDGER_RETRY_S = 5.0
    LEDGER_ALERT_AFTER_S = 120.0
    HEARTBEAT_S = 2.0
    IDLE_TICK_S = 1.0
    MAX_OPTION_SYMBOLS = 20  # contracts tracked at once; each one is polled for quotes

    def __init__(
        self,
        *,
        config: Config,
        clock: Clock,
        market: MarketData,
        strategy: Strategy,
        risk: RiskManager,
        broker: Broker,
        state: StateStore,
        control: ControlState,
        session: SessionTracker,
        alerts: Alerter,
        instance_id: str,
        auth_seconds_left: Callable[[], float | None] | None = None,
        settings: LiveSettings | None = None,
        on_universe: Callable[[tuple[str, ...]], None] | None = None,
        research: ResearchSource | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._market = market
        self._strategy = strategy
        self._risk = risk
        self._broker = broker
        self._state = state
        self._control = control
        self._session = session
        self._alerts = alerts
        self._instance = instance_id
        self._auth_seconds_left = auth_seconds_left or (lambda: None)
        self._live_settings = settings
        self._settings: Settings = (
            settings.current if settings is not None else Settings.from_config(config)
        )
        self._risk.limits = self._settings.risk
        self._settings_at: datetime | None = None
        self._settings_unreadable_since: datetime | None = None
        self._settings_unreadable_reported = False
        # Research picks and the day's posture. None: research is off and the old rules hold.
        self._research = research
        self._research_at: datetime | None = None
        self._no_posture_day: date | None = None  # the day research_no_posture was reported
        # The positions the bot opened itself. Only used with research on: then the bot
        # trades what it opened (and pinned symbols) and leaves every other holding alone.
        self._ledger: dict[str, LedgerEntry] = {}
        self._ledger_ok = False
        self._ledger_attempt_at: datetime | None = None
        self._ledger_error = ""
        self._ledger_down_since: datetime | None = None  # start of the current outage
        self._ledger_down_noted = False  # entries_halted recorded for this outage
        self._ledger_down_alerted = False
        self._ledger_close_alerted = False
        self._foreign: set[str] = set()  # held, not opened by the bot, not pinned

        # The equity symbols the bot trades right now: see _refresh_universe.
        self._on_universe = on_universe
        self._universe: tuple[str, ...] = self._settings.pinned_symbols
        self._universe_owed = False  # on_universe failed and must be called again
        self._unmanaged_noticed: set[str] = set()  # holdings already reported as unmanaged
        self._symbols: dict[str, _SymbolState] = {s: _SymbolState() for s in self._universe}
        self._perms = _HALTED
        self._leader = False
        self._day = DayState(trading_date(clock.now()).isoformat(), halted_reason="not loaded yet")
        self._day_ok = False
        self._day_attempt_at: datetime | None = None
        self._carried_usd = Decimal(0)  # earlier sales that have not settled by today
        self._unsaved_sold = Decimal(0)  # day-state writes that failed and await a retry
        self._unsaved_halt: str | None = None
        self._unsaved_attempt_at: datetime | None = None
        self._account: AccountSnapshot | None = None
        self._account_at: datetime | None = None
        self._account_attempt_at: datetime | None = None
        self._account_dirty = True
        self._equity_unknown = True
        self._strategy_error: str | None = None
        self._control_at: datetime | None = None
        self._lease_at: datetime | None = None
        self._lease_until: datetime | None = None
        self._heartbeat_at: datetime | None = None
        self._pretrade: tuple[datetime, AccountSnapshot, list[BrokerOrder]] | None = None
        self._throttled: dict[str, datetime] = {}
        self._seen_order_ids: set[str] = set()  # every order this process has tracked
        self._expiry_noticed: set[str] = set()  # contracts already announced as expiring today

    # ------------------------------------------------------------------ public

    @property
    def is_leader(self) -> bool:
        return self._leader

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def universe(self) -> tuple[str, ...]:
        return self._universe

    def target(self, symbol: str) -> Target | None:
        st = self._symbols.get(symbol)
        return st.target if st is not None else None

    async def start(self) -> None:
        """Load today's counters. Open orders are picked up when the lease is won."""
        now = self._clock.now()
        await self._roll_day(now)
        if not await self._load_ledger(now):
            await self._ledger_outage(now)
        if self._live_settings is not None:
            for update in self._live_settings.start_updates:
                await self._on_settings(update, now)
            if not self._live_settings.loaded:
                await self._event("entries_halted", {"reason": "settings not loaded"}, now)
                if not any(u.kind == "rejected" for u in self._live_settings.start_updates):
                    # A rejected version has its own alert, which says what to do.
                    await self._alerts.send(
                        "settings_not_loaded",
                        "Settings not loaded",
                        "The bot could not read a valid settings version at start-up. No new "
                        "positions open until the settings table can be read. Exits still "
                        "work. Check the settings table and the task role.",
                    )

    async def _adopt_open_orders(self) -> None:
        """Take over the orders the previous holder of the lease left open. Only the
        instance that holds the lease may touch them, so this runs on gaining it."""
        for record in await self._state.open_orders():
            st = self._symbols.setdefault(record.symbol, _SymbolState())
            if self._is_our_option(record.symbol):
                self._market.watch(record.symbol)
            if st.working is None:
                self._seen_order_ids.add(record.order_id)
                st.working = record
                st.last_poll_at = None
                log.info("resuming order %s (%s)", record.order_id, record.symbol)
            elif st.working.order_id != record.order_id:
                # Two open orders on one symbol should not happen. The pre-trade check
                # will see the extra as an order the bot does not manage and keep out.
                log.warning("extra open order %s on %s", record.order_id, record.symbol)

    async def step(self) -> None:
        """One pass of the loop. Safe to call as often as you like."""
        now = self._clock.now()
        await self._housekeeping(now)
        await self._resolve_unconfirmed(now)
        await self._manage_working_orders(now)
        await self._run_strategy(now)
        for symbol, st in list(self._symbols.items()):
            await self._reconcile(symbol, st, now)
        self._heartbeat(now)

    async def run(self, stop: asyncio.Event) -> None:
        """Run until ``stop`` is set. Wakes on market data, or every second without it."""
        wake = asyncio.Event()
        self._market.set_waker(wake.set)
        await self.start()
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=self.IDLE_TICK_S)
            wake.clear()
            try:
                await self.step()
            except Exception as exc:
                log.exception("engine step failed")
                await self._alerts.send(
                    "engine_error", "Engine error", f"{type(exc).__name__}: {exc}"
                )
                await asyncio.sleep(1)
        await self.shutdown()

    async def shutdown(self) -> None:
        """Cancel what is working and let another instance take over. Positions are kept."""
        for st in self._symbols.values():
            if st.working is not None:
                await self._cancel_quietly(st.working.order_id)
        try:
            await self._state.release_lease(self._instance)
        except Exception:
            log.exception("could not release the lease")
        self._leader = False

    # ------------------------------------------------------------ housekeeping

    async def _housekeeping(self, now: datetime) -> None:
        if self._live_settings is not None and _due(
            self._settings_at, now, self.SETTINGS_REFRESH_S
        ):
            self._settings_at = now
            updates = await self._live_settings.refresh(now)
            if not any(update.kind == "unreadable" for update in updates):
                self._settings_unreadable_since = None
                self._settings_unreadable_reported = False
            for update in updates:
                await self._on_settings(update, now)
        if self._research is not None and not self._ledger_ok:
            retry = _due(self._ledger_attempt_at, now, self.LEDGER_RETRY_S)
            if retry and await self._load_ledger(now):
                await self._refresh_universe(now)  # the bot's positions rejoin the universe
            else:
                await self._ledger_outage(now)
        if self._research is not None and _due(
            self._research_at, now, self._settings.research.poll_s
        ):
            self._research_at = now
            try:
                research_updates = await self._research.refresh(now)
            except Exception as exc:
                # Never let research stop the rest of the step: exits, the lease and the
                # account refresh come after this.
                self._log_throttled(
                    "research", now, "research refresh failed: %s: %s", type(exc).__name__, exc
                )
                research_updates = []
            for research_update in research_updates:
                await self._on_research(research_update, now)
            await self._refresh_universe(now)
        if _due(self._control_at, now, self.CONTROL_REFRESH_S):
            self._control_at = now
            await self._control.refresh(now)
        await self._update_permissions(now)
        if _due(self._lease_at, now, self.LEASE_RENEW_S):
            self._lease_at = now
            await self._renew_lease(now)
        await self._session.refresh(now)
        await self._roll_day(now)
        await self._save_unsaved(now)
        interval = self.ACCOUNT_RETRY_S if self._account_dirty else self.ACCOUNT_REFRESH_S
        if _due(self._account_attempt_at, now, interval):
            await self._refresh_account(now)
        await self._check_posture(now)

    async def _update_permissions(self, now: datetime) -> None:
        mode = self._control.mode(now)
        perms = effective_permissions(self._config.trading_mode, mode)
        if perms != self._perms:
            log.info(
                "permissions: entries=%s exits=%s (%s)",
                perms.allow_entries,
                perms.allow_exits,
                perms.reason,
            )
        self._perms = perms
        mismatch = mode in (ControlMode.PAPER, ControlMode.LIVE) and not perms.allow_exits
        if mismatch:
            await self._alerts.send(
                "control_mismatch", "Control does not match deploy", perms.reason
            )

    async def _on_settings(self, update: SettingsUpdate, now: datetime) -> None:
        live = self._live_settings
        assert live is not None
        version = update.version
        if update.kind == "applied":
            unpinned = set(self._settings.pinned_symbols) - set(live.current.pinned_symbols)
            self._settings = live.current
            self._risk.limits = self._settings.risk
            await self._event(
                "settings_applied",
                {"version": version, "author": update.detail, "diff": update.diff},
                now,
            )
            changes = "\n".join(f"{k}: {old} -> {new}" for k, (old, new) in update.diff.items())
            await self._alerts.send(
                f"settings_applied:{version}",
                f"Settings version {version} applied",
                changes or "No change to the settings in force.",
            )
            await self._refresh_universe(now)
            await self._warn_unpinned_but_held(unpinned, version, now)
        elif update.kind == "pending_restart":
            await self._event(
                "settings_pending_restart", {"version": version, "fields": update.detail}, now
            )
            await self._alerts.send(
                f"settings_restart:{version}",
                f"Settings version {version} needs a restart",
                f"These fields only change when the bot restarts: {update.detail}. "
                "Everything else in the version is in force now.",
            )
        elif update.kind == "rejected":
            await self._event(
                "settings_rejected", {"version": version, "detail": update.detail}, now
            )
            if live.loaded:
                in_force = "The bot keeps the settings it was running with."
            else:
                in_force = (
                    "No settings have loaded in this process, so no new positions open "
                    "until a valid version is written. Exits still work."
                )
            await self._alerts.send(
                f"settings_rejected:{version}",
                f"Settings version {version} rejected",
                f"{update.detail}. {in_force}",
            )
        else:
            self._log_throttled("settings", now, "settings unreadable: %s", update.detail)
            await self._settings_unreadable(update, now)

    async def _on_research(self, update: ResearchUpdate, now: datetime) -> None:
        if update.kind == "stale":
            await self._event("research_stale", {"detail": update.detail}, now)
            await self._alerts.send(
                "research_stale",
                "Research is stale",
                f"Research could not be read for longer than "
                f"{self._settings.research.max_stale_s:.0f}s ({update.detail}). No new "
                "positions open until research is readable again. Exits still work. Check "
                "the research table, the task role and the research jobs.",
            )
        else:
            await self._event("research_restored", {}, now)
            await self._alerts.send(
                "research_restored",
                "Research readable again",
                "The research table is readable again. New positions follow the live picks "
                "and the day's posture.",
            )

    async def _check_posture(self, now: datetime) -> None:
        """Once per trading day, ``research.posture_alert_after_open_min`` after the open:
        with research on and the session open, say so if research, read since then, has
        no usable posture for today. The bot stands aside either way; this makes it loud.
        A posture research set to ``stand_aside`` is not missing, and staleness has its
        own alert."""
        research = self._research
        today = trading_date(now)
        if research is None or self._no_posture_day == today:
            return
        session = self._session.view(now)
        if not session.is_open or session.minutes_since_open is None:
            return
        delay = self._settings.research.posture_alert_after_open_min
        if session.minutes_since_open < delay:
            return
        view = research.view
        alert_from = now - timedelta(minutes=session.minutes_since_open - delay)
        if view.as_of is None or view.as_of < alert_from or not view.posture_missing:
            return  # not read since the alert time yet, or nothing is missing
        self._no_posture_day = today
        await self._event("research_no_posture", {"day": today.isoformat()}, now)
        await self._alerts.send(
            "research_no_posture",
            "No research posture today",
            "No usable research posture for today; the bot is standing aside. Check the "
            "pre-market run (alerts, META, the DLQ).",
        )

    async def _settings_unreadable(self, update: SettingsUpdate, now: datetime) -> None:
        """Say so, once, when the settings have stayed unreadable for a while."""
        live = self._live_settings
        assert live is not None
        if self._settings_unreadable_since is None:
            self._settings_unreadable_since = now
        since = self._settings_unreadable_since
        if (
            self._settings_unreadable_reported
            or (now - since).total_seconds() < self.SETTINGS_ALERT_AFTER_S
        ):
            return
        self._settings_unreadable_reported = True
        await self._event(
            "settings_unreadable", {"since": since.isoformat(), "detail": update.detail}, now
        )
        if live.loaded:
            in_force = (
                f"The last good version, {live.version}, stays in force. Newer versions "
                "(including tighter limits) will not apply until the table is readable."
            )
        else:
            in_force = (
                "No settings have loaded in this process, so no new positions open. "
                "Exits still work."
            )
        await self._alerts.send(
            "settings_unreadable",
            "Settings table unreadable",
            f"The settings table has been unreadable since {since.isoformat()} "
            f"({update.detail}). {in_force} Check the settings table and the task role.",
        )

    async def _renew_lease(self, now: datetime) -> None:
        try:
            leader = await self._state.acquire_lease(self._instance, self.LEASE_TTL_S, now)
            if leader:
                self._lease_until = now + timedelta(seconds=self.LEASE_TTL_S)
                if not self._leader:
                    await self._adopt_open_orders()
                    # The previous holder may have opened or closed positions since this
                    # process last read the ledger.
                    await self._load_ledger(now)
        except Exception as exc:
            self._log_throttled("lease", now, "lease check failed, not trading: %s", exc)
            leader = False
        if not leader and self._leader:
            # Whoever holds the lease now owns the open orders, and the ledger.
            for st in self._symbols.values():
                st.working = None
            self._foreign = set()
        if self._leader and not leader:
            await self._alerts.send(
                "lease_lost",
                "Lost the trading lease",
                f"{self._instance} is no longer the active instance and has stopped trading.",
            )
        if leader and not self._leader:
            log.info("%s holds the trading lease", self._instance)
        self._leader = leader

    async def _roll_day(self, now: datetime) -> None:
        day = trading_date(now).isoformat()
        if self._day_ok and self._day.day == day:
            return
        if self._day.day == day and not _due(self._day_attempt_at, now, self.DAY_RETRY_S):
            return
        self._day_attempt_at = now
        if self._day.day != day:
            # A new trading day. Yesterday's wishes are not today's: the strategy must
            # ask again, on today's prices, before anything is bought or sold.
            for st in self._symbols.values():
                st.target = None
            self._forget_idle_options()
            self._expiry_noticed.clear()
            self._unsaved_sold, self._unsaved_halt = Decimal(0), None
        try:
            loaded = await self._state.get_day(day)
            carried = Decimal(0)
            today = date.fromisoformat(day)
            if trades_without_settling(today):
                # Banks are shut, so the last trading day's sales have not settled yet.
                carried = (await self._state.get_day(previous_weekday(today).isoformat())).sold_usd
            self._carried_usd = carried
            # Keep what could not be written earlier today.
            self._day = replace(
                loaded,
                sold_usd=loaded.sold_usd + self._unsaved_sold,
                halted_reason=loaded.halted_reason or self._unsaved_halt,
            )
            self._day_ok = True
        except Exception as exc:
            # Without today's counters the limits cannot be enforced: no entries.
            self._day = DayState(day, halted_reason=f"state unavailable: {exc}")
            self._day_ok = False
            self._log_throttled("day", now, "could not load today's counters: %s", exc)

    async def _refresh_account(self, now: datetime) -> AccountSnapshot | None:
        self._account_attempt_at = now
        try:
            account = await self._broker.get_account()
        except Exception as exc:
            self._log_throttled("account", now, "account refresh failed: %s", exc)
            return None
        await self._on_account(account, now)
        return account

    async def _on_account(self, account: AccountSnapshot, now: datetime) -> None:
        self._account = account
        self._account_at = now
        self._account_dirty = False
        if self._risk.limits.allow_options:
            for symbol, held in account.positions.items():
                if held.quantity > 0 and self._track_option(symbol, held=True) is not None:
                    await self._warn_of_expiry(symbol, held.quantity, now)
        await self._settle_pending(account, now)
        await self._check_daily_loss(account, now)
        await self._update_ledger(account, now)
        await self._refresh_universe(now)
        await self._report_unmanaged(account, now)

    # ------------------------------------------------------------------ ledger

    async def _load_ledger(self, now: datetime) -> bool:
        """Read the positions the bot opened. Until this works there are no entries and
        nothing counts as foreign: a missing ledger must not block the bot's own exits."""
        if self._research is None:
            return False
        self._ledger_attempt_at = now
        try:
            self._ledger = dict(await self._state.ledger())
        except Exception as exc:
            self._ledger_ok = False
            self._ledger_error = f"{type(exc).__name__}: {exc}"
            if self._ledger_down_since is None:
                self._ledger_down_since = now
            self._log_throttled(
                "ledger",
                now,
                "position ledger unreadable, no entries: %s: %s",
                type(exc).__name__,
                exc,
            )
            return False
        self._ledger_ok = True
        self._ledger_down_since = None
        self._ledger_down_noted = self._ledger_down_alerted = self._ledger_close_alerted = False
        return True

    async def _ledger_outage(self, now: datetime) -> None:
        """Say so while the ledger cannot be read: no entries, and no intraday flatten."""
        if self._research is None or self._ledger_ok:
            return
        if not self._ledger_down_noted:
            self._ledger_down_noted = True
            await self._event("entries_halted", {"reason": "position ledger not loaded"}, now)
        since = self._ledger_down_since or now
        down_s = (now - since).total_seconds()
        if not self._ledger_down_alerted and down_s >= self.LEDGER_ALERT_AFTER_S:
            self._ledger_down_alerted = True
            await self._alerts.send(
                "ledger_not_loaded",
                "Position ledger not loaded",
                f"The bot has not been able to read its position ledger for {down_s:.0f}s "
                f"({self._ledger_error}). No new positions are being opened. Intraday "
                "positions are not flattened automatically until the ledger loads, so check "
                "them by hand before the close. Exits the strategy asks for still work. "
                "Check the state table and the task role.",
            )
        if not self._ledger_close_alerted and self._intraday_closing(now):
            self._ledger_close_alerted = True
            minutes = self._settings.research.intraday_flatten_min
            await self._alerts.send(
                "ledger_not_loaded_close",
                "Position ledger not loaded at the close",
                f"The close is under {minutes} minutes away and the bot still cannot read "
                "its position ledger, so it does not know which positions are intraday and "
                "will not sell them. Sell intraday positions yourself now, or accept holding "
                "them overnight. No new positions are being opened.",
            )

    async def _update_ledger(self, account: AccountSnapshot, now: datetime) -> None:
        """Forget positions the bot has closed, and spot holdings it did not open. Only
        the lease holder does this: a standby's ledger may be out of date."""
        if self._research is None or not self._ledger_ok or not self._leader:
            return
        for symbol in list(self._ledger):
            st = self._symbols.get(symbol)
            if account.position(symbol) != 0 or (st is not None and self._busy(st)):
                continue
            try:
                await self._state.delete_ledger(symbol)
            except Exception as exc:
                # Kept in memory and retried on the next snapshot.
                self._log_throttled(
                    "ledger-delete", now, "could not clear %s from the ledger: %s", symbol, exc
                )
                continue
            del self._ledger[symbol]
        pinned = set(self._settings.pinned_symbols)
        await self._book_pinned(account, pinned, now)
        foreign = {
            symbol: held.quantity
            for symbol, held in account.positions.items()
            if held.quantity != 0
            and root_symbol(symbol) not in pinned
            and symbol not in self._ledger
        }
        new = sorted(foreign.keys() - self._foreign)
        self._foreign = set(foreign)  # flat (or pinned) again: no longer foreign
        for symbol in new:
            await self._event(
                "unknown_holding", {"symbol": symbol, "quantity": foreign[symbol]}, now
            )
            await self._alerts.send(
                f"unknown_holding:{symbol}",
                f"{symbol} is held but the bot did not open it",
                "The bot will not trade it, sells included. Use an account that is the "
                "bot's alone.",
            )

    async def _book_pinned(self, account: AccountSnapshot, pinned: set[str], now: datetime) -> None:
        """Book held positions on pinned symbols that the ledger does not have yet (say,
        held before research was on). They are the bot's to manage, and booked they stay
        so after they are unpinned, until they are flat. A failed write is tried again on
        the next snapshot."""
        for symbol, held in account.positions.items():
            if held.quantity == 0 or root_symbol(symbol) not in pinned or symbol in self._ledger:
                continue
            put = is_option_symbol(symbol) and parse_option_symbol(symbol).right == "P"
            entry = LedgerEntry(
                symbol, horizon="swing", side="bearish" if put else "long", opened_at=now
            )
            try:
                await self._state.put_ledger(entry)
            except Exception as exc:
                self._log_throttled(
                    "ledger-book", now, "could not book pinned %s in the ledger: %s", symbol, exc
                )
                continue
            self._ledger[symbol] = entry

    def _ledger_entry(self, symbol: str) -> LedgerEntry | None:
        """The symbol's ledger entry, for decisions that depend on how it is held. None
        while the ledger is not loaded: a copy kept from before a failed reload may be out
        of date. (Universe membership still uses that copy, so nothing is dropped.)"""
        return self._ledger.get(symbol) if self._ledger_ok else None

    def _ledger_horizon(self, symbol: str) -> str:
        entry = self._ledger_entry(symbol)
        return entry.horizon if entry is not None else "swing"

    def _intraday_closing(self, now: datetime) -> bool:
        """Inside the window before the close where intraday positions are sold."""
        if self._research is None:
            return False
        view = self._session.view(now)
        return (
            view.is_open
            and view.minutes_to_close is not None
            and view.minutes_to_close <= self._settings.research.intraday_flatten_min
        )

    def _intraday_flatten(self, symbol: str, now: datetime) -> bool:
        """An intraday position the bot opened, and the close is near: it is sold. Never
        while the ledger is not loaded (see _ledger_outage, which alerts instead)."""
        entry = self._ledger_entry(symbol)
        return entry is not None and entry.horizon == "intraday" and self._intraday_closing(now)

    # ---------------------------------------------------------------- universe

    def _required_symbols(self) -> set[str]:
        """Roots that must stay in the universe: anything in it with an order working or
        unsettled (or frozen), anything in it the bot holds, shares or options, and
        everything in the bot's ledger (so a restart keeps positions whose pick expired).
        Holdings the bot did not open are not required."""
        required = {
            root
            for symbol, st in self._symbols.items()
            if self._busy(st) and (root := root_symbol(symbol)) in self._universe
        }
        required |= {root_symbol(symbol) for symbol in self._ledger}
        account = self._account
        if account is None:
            # Nothing is known to be flat before the first account read: drop nothing.
            return required | set(self._universe)
        for symbol, held in account.positions.items():
            root = root_symbol(symbol)
            if held.quantity != 0 and root in self._universe and symbol not in self._foreign:
                required.add(root)
        return required

    def _wanted_picks(self, now: datetime) -> list[tuple[str, int]]:
        """Live research picks with their scores. Empty without research."""
        if self._research is None:
            return []
        return [
            (symbol, pick.score) for symbol, pick in self._research.view.live_picks(now).items()
        ]

    async def _refresh_universe(self, now: datetime) -> None:
        """Recompute the universe and, when its members change, tell everyone. A dropped
        symbol's state, and that of options on it, goes only when it is idle and flat."""
        new = compute_universe(
            required=self._required_symbols(),
            pinned=self._settings.pinned_symbols,
            picks=self._wanted_picks(now),
            cap=self._settings.research.max_symbols,
        )
        old = self._universe
        if set(new) == set(old):
            # At most the order differs: nothing to add, drop or tell, unless the last
            # call to on_universe failed.
            await self._send_universe(now)
            return
        self._universe = new
        added = [s for s in new if s not in old]
        dropped = [s for s in old if s not in new]
        for symbol in added:
            self._symbols.setdefault(symbol, _SymbolState())
        for symbol, st in list(self._symbols.items()):
            if root_symbol(symbol) in dropped and not self._busy(st):
                del self._symbols[symbol]
                if is_option_symbol(symbol):
                    self._market.unwatch(symbol)
        await self._event(
            "universe_changed", {"added": added, "dropped": dropped, "universe": list(new)}, now
        )
        try:
            self._strategy.on_universe(new)
        except Exception as exc:
            log.exception("strategy raised in on_universe")
            await self._strategy_failed(exc)
        self._universe_owed = self._on_universe is not None
        await self._send_universe(now)

    async def _send_universe(self, now: datetime) -> None:
        """Tell the on_universe callback (the feed) the universe it still has to hear."""
        callback = self._on_universe
        if callback is None or not self._universe_owed:
            return
        try:
            callback(self._universe)
        except Exception as exc:
            self._log_throttled("on_universe", now, "universe callback failed, will retry: %s", exc)
            return
        self._universe_owed = False

    def _held_on(self, account: AccountSnapshot, roots: set[str]) -> list[str]:
        """Held positions (shares or options) whose root is one of ``roots``."""
        return sorted(
            symbol
            for symbol, held in account.positions.items()
            if held.quantity != 0 and root_symbol(symbol) in roots
        )

    async def _warn_unpinned_but_held(
        self, unpinned: set[str], version: int | None, now: datetime
    ) -> None:
        """A version unpinned symbols the bot still holds. With research off they stay
        managed for now, but a new process starts from the pinned symbols and will not pick
        them up. With research on, held pinned positions are booked in the ledger and stay
        managed until they are flat; only one whose booking has not gone through is lost."""
        account = self._account
        if account is None or not unpinned:
            return
        held = self._held_on(account, unpinned & set(self._universe))
        if self._research is not None:
            # A standby, or a process without its ledger, cannot tell what is booked.
            if not (self._leader and self._ledger_ok):
                return
            held = [symbol for symbol in held if symbol not in self._ledger]
            if not held:
                return
            await self._event("unpinned_but_held", {"version": version, "symbols": held}, now)
            await self._alerts.send(
                f"unpinned_not_in_ledger:{version}",
                "Unpinned symbols are not in the ledger",
                f"{', '.join(held)} are not in the bot's ledger; once unpinned, the bot leaves "
                "them alone, sells included. Pin them again or sell them yourself. If the bot "
                "tried to book them and could not, its log says why.",
            )
            return
        if not held:
            return
        await self._event("unpinned_but_held", {"version": version, "symbols": held}, now)
        await self._alerts.send(
            f"unpinned_but_held:{version}",
            "Unpinned symbols are still held",
            f"Settings version {version} unpins symbols the bot still holds: "
            f"{', '.join(held)}. The bot keeps managing them until they are flat or until "
            "the next restart (the schedule restarts it every morning). After that it will "
            "not manage them, so sell them or keep them pinned.",
        )

    async def _report_unmanaged(self, account: AccountSnapshot, now: datetime) -> None:
        """Say once per symbol when the account holds something outside the universe:
        the bot neither trades it nor watches its expiry.

        Only while research is off: with research on, the bot's own ledger says which
        holdings it did not open."""
        if self._research is not None:
            return
        held = {
            symbol: held.quantity
            for symbol, held in account.positions.items()
            if held.quantity != 0 and root_symbol(symbol) not in self._universe
        }
        self._unmanaged_noticed &= held.keys()  # flat again: report it if it comes back
        for symbol in sorted(held.keys() - self._unmanaged_noticed):
            self._unmanaged_noticed.add(symbol)
            quantity = held[symbol]
            await self._event("unmanaged_holding", {"symbol": symbol, "quantity": quantity}, now)
            option = (
                ", so as an option it gets no expiry alerts and is not sold before it expires"
                if is_option_symbol(symbol)
                else ""
            )
            # It may be the operator's own holding, so pinning is not the default advice:
            # a pinned symbol is the strategy's to buy and sell.
            await self._alerts.send(
                f"unmanaged_holding:{symbol}",
                f"{symbol} is held but not managed",
                f"The bot does not trade {symbol} because it is not pinned{option}. If the bot "
                "bought it before it was unpinned, sell it yourself or pin it again. If the bot "
                "did not buy it, you can ignore this; pinning would hand it to the strategy, "
                "which may sell it.",
            )

    def _is_our_option(self, symbol: str) -> bool:
        """An option contract on one of the universe's symbols."""
        if symbol in self._universe or not is_option_symbol(symbol):
            return False
        return parse_option_symbol(symbol).underlying in self._universe

    def _track_option(self, symbol: str, *, held: bool = False) -> _SymbolState | None:
        """The state for an option contract on one of the bot's symbols, created on first
        sight. None for anything else, or when too many contracts are in play already.
        A contract that is in the account is always tracked: it may need selling."""
        if not self._is_our_option(symbol):
            return None
        st = self._symbols.get(symbol)
        if st is not None:
            return st
        options = sum(1 for known in self._symbols if self._is_our_option(known))
        if options >= self.MAX_OPTION_SYMBOLS and not held:
            self._log_throttled(
                "options-cap",
                self._clock.now(),
                "already tracking %d option contracts, ignoring %s",
                options,
                symbol,
            )
            return None
        st = self._symbols[symbol] = _SymbolState()
        self._market.watch(symbol)
        return st

    async def _warn_of_expiry(self, symbol: str, quantity: int, now: datetime) -> None:
        """Tell the operator about a held option on its last day. This runs on every
        account snapshot and does not depend on the bot being able to sell: a halt, a
        frozen symbol or unknown market hours are exactly when a person has to act."""
        if parse_option_symbol(symbol).days_to_expiry(trading_date(now)) > 0:
            return
        exercise = (
            "An option left to expire in the money is exercised into 100 shares per contract."
        )
        if symbol not in self._expiry_noticed:
            self._expiry_noticed.add(symbol)
            await self._alerts.send(
                f"option_expiring:{symbol}",
                f"{symbol} expires today",
                f"The account holds {quantity} contract(s) of {symbol}, which expire today. "
                f"The bot will try to sell them in the last "
                f"{self._risk.limits.option_expiry_exit_min} minutes before the close, if it "
                f"is allowed to trade then. {exercise}",
            )
        if self._expiring_now(symbol, now):
            await self._alerts.send(
                f"option_unsold:{symbol}",
                f"{symbol} expires today and is still held",
                f"{quantity} contract(s) of {symbol} are still in the account and the close "
                "is near. The bot sells them if it can; if this alert repeats, it is not "
                f"getting it done. Sell them at Schwab or tell Schwab not to exercise. {exercise}",
            )

    def _forget_idle_options(self) -> None:
        """Stop tracking contracts that are not being traded. The ones still held are
        picked up again from the next account snapshot."""
        for symbol, st in list(self._symbols.items()):
            if not self._is_our_option(symbol) or self._busy(st):
                continue
            del self._symbols[symbol]
            self._market.unwatch(symbol)

    async def _settle_pending(self, account: AccountSnapshot, now: datetime) -> None:
        for symbol, st in list(self._symbols.items()):
            pending = st.pending
            if pending is None:
                continue
            position = account.position(symbol)
            age = (now - pending.since).total_seconds()
            if position == pending.expected:
                st.pending = None
                request = pending.order  # only there when the order's fate was unknown
                if request is not None and request.side is Side.BUY:
                    await self._ensure_booked(symbol, now)  # so the fill is never foreign
                if request is not None and request.side is Side.SELL:
                    # The order was never seen, but the shares are gone: it sold.
                    await self._note_sale(
                        symbol, pending.before - pending.expected, request.limit_price, now
                    )
            elif age >= self.SETTLE_TIMEOUT_S and (pending.confirmed or position != pending.before):
                # A fill the account never reflected, or a position that is neither the old
                # nor the expected one. Do not guess: stop trading the symbol.
                await self._freeze(
                    symbol,
                    st,
                    now,
                    f"expected to hold {pending.expected} after an order "
                    f"(was {pending.before}) but the broker shows {position}",
                )
            if st.pending is not None:
                self._account_dirty = True  # keep looking until it resolves

    async def _resolve_unconfirmed(self, now: datetime) -> None:
        """Find out what happened to orders whose placement reply never arrived.

        Ask the broker for an order with the same details. If there is one, adopt it and
        manage it normally. Only when the broker has been asked, has no such order, and the
        position has not moved for a while is the order treated as never placed.
        """
        for symbol, st in list(self._symbols.items()):
            pending = st.pending
            if pending is None or pending.confirmed or pending.order is None:
                continue
            if not _due(pending.search_attempt_at, now, self.FIND_RETRY_S):
                continue
            pending.search_attempt_at = now
            request = pending.order
            since = pending.since - timedelta(seconds=self.FIND_CLOCK_SKEW_S)
            try:
                found = await self._broker.find_order(symbol, request.side, request.quantity, since)
            except Exception as exc:
                self._log_throttled(
                    f"find:{symbol}", now, "%s: order lookup failed: %s", symbol, exc
                )
                continue
            if found is not None and found.order_id not in self._seen_order_ids:
                record = OrderRecord(
                    order_id=found.order_id,
                    symbol=symbol,
                    side=request.side,
                    quantity=request.quantity,
                    order_type=request.order_type,
                    limit_price=request.limit_price,
                    submitted_at=pending.since,
                    position_before=pending.before,
                    reason=request.reason,
                )
                st.pending = None
                st.working = record
                st.last_poll_at = None
                self._seen_order_ids.add(found.order_id)
                if request.side is Side.BUY:
                    await self._ensure_booked(symbol, now)  # so the fill is never foreign
                await self._save_order(record)
                await self._event(
                    "order_adopted",
                    {"symbol": symbol, "order_id": found.order_id, "status": found.status.value},
                    now,
                )
                continue
            age = (now - pending.since).total_seconds()
            account, account_at = self._account, self._account_at
            if age < self.UNCONFIRMED_TIMEOUT_S or account is None or account_at is None:
                continue
            fresh = (now - account_at).total_seconds() <= self.FIND_RETRY_S
            if fresh and account.position(symbol) == pending.before:
                log.warning(
                    "%s: no matching order at the broker and the position has not "
                    "moved in %.0fs; treating the order as never placed",
                    symbol,
                    age,
                )
                st.pending = None

    async def _freeze(self, symbol: str, st: _SymbolState, now: datetime, why: str) -> None:
        st.pending = None
        st.frozen = why
        log.error("%s frozen: %s", symbol, why)
        await self._event("symbol_frozen", {"symbol": symbol, "reason": why}, now)
        await self._alerts.send(
            f"symbol_frozen:{symbol}",
            f"{symbol} frozen: position mismatch",
            f"{why}. The bot will not trade {symbol} again until it is restarted. "
            "Check the account before restarting.",
        )

    async def _check_daily_loss(self, account: AccountSnapshot, now: datetime) -> None:
        if account.equity is None or not self._day_ok:
            self._equity_unknown = True
            return
        if self._day.start_equity is None:
            try:
                start = await self._state.init_start_equity(self._day.day, account.equity)
            except Exception as exc:
                self._equity_unknown = True
                self._log_throttled("equity", now, "could not record opening equity: %s", exc)
                return
            self._day = replace(self._day, start_equity=start)
        self._equity_unknown = False
        start_equity = self._day.start_equity
        if start_equity is None:
            return
        loss = start_equity - account.equity
        limit = self._risk.limits.max_daily_loss_usd
        if loss < limit:
            return
        reason = f"equity down {loss:.2f} from {start_equity:.2f} today (limit {limit})"
        await self._halt_day("Daily loss limit hit", reason, now)

    async def _halt_day(self, subject: str, reason: str, now: datetime) -> None:
        """No more entries today. Exits keep working."""
        if self._day.halted_reason:
            return
        self._day = replace(self._day, halted_reason=reason)  # in memory first: fail closed
        try:
            await self._state.halt_day(self._day.day, reason)
        except Exception:
            log.exception("could not persist the daily halt; will retry")
            self._unsaved_halt = reason
        await self._event("entries_halted", {"reason": reason}, now)
        await self._alerts.send(
            "day_halted",
            subject,
            f"{reason}. No new entries today. Existing positions are untouched.",
        )

    async def _note_sale(
        self, symbol: str, quantity: int, price: Decimal | None, now: datetime
    ) -> None:
        """Remember what a sale brought in. That money settles on the next business day,
        and until then the settled-cash rule keeps it from funding another buy."""
        if price is None:
            # Without a price, settled and unsettled cash cannot be told apart.
            await self._halt_day(
                "A sale could not be valued",
                f"sold {quantity} {symbol} but the broker reported no fill price",
                now,
            )
            return
        proceeds = (price * quantity * contract_size(symbol)).quantize(_CENT, rounding=ROUND_UP)
        # In memory first, so the rule holds even if the write below fails.
        self._day = replace(self._day, sold_usd=self._day.sold_usd + proceeds)
        try:
            await self._state.add_sold(self._day.day, proceeds)
        except Exception:
            log.exception("could not persist today's sale proceeds; will retry")
            self._unsaved_sold += proceeds

    async def _save_unsaved(self, now: datetime) -> None:
        """Retry day-state writes that failed, so a restart does not forget them."""
        if not (self._unsaved_sold or self._unsaved_halt):
            return
        if not _due(self._unsaved_attempt_at, now, self.DAY_RETRY_S):
            return
        self._unsaved_attempt_at = now
        try:
            if self._unsaved_sold:
                await self._state.add_sold(self._day.day, self._unsaved_sold)
                self._unsaved_sold = Decimal(0)
            if self._unsaved_halt:
                await self._state.halt_day(self._day.day, self._unsaved_halt)
                self._unsaved_halt = None
        except Exception as exc:
            self._log_throttled("unsaved", now, "day state still cannot be written: %s", exc)

    # ------------------------------------------------------------ working orders

    def _lease_is_safe(self) -> bool:
        """True while this instance certainly still holds the lease. Checked against the
        clock right now, because a slow broker call can outlast the lease mid-step."""
        until = self._lease_until
        margin = timedelta(seconds=self.LEASE_MARGIN_S)
        return self._leader and until is not None and self._clock.now() < until - margin

    async def _manage_working_orders(self, now: datetime) -> None:
        # Only the lease holder ever has working orders: they are adopted on winning
        # the lease and dropped on losing it.
        for symbol, st in list(self._symbols.items()):
            record = st.working
            if record is None or not _due(st.last_poll_at, now, self.ORDER_POLL_S):
                continue
            st.last_poll_at = now
            try:
                order = await self._broker.get_order(record.order_id)
            except Exception as exc:
                st.poll_failures += 1
                self._log_throttled(
                    f"poll:{symbol}", now, "order %s status failed: %s", record.order_id, exc
                )
                if st.poll_failures >= 30:
                    await self._alerts.send(
                        f"order_stuck:{symbol}",
                        f"Cannot read order status for {symbol}",
                        f"Order {record.order_id}: {exc}",
                    )
                continue
            st.poll_failures = 0
            record.filled_quantity = order.filled_quantity
            record.avg_fill_price = order.avg_fill_price
            if order.status.is_terminal:
                await self._finish_order(symbol, st, record, order, now)
                continue

            age = (now - record.submitted_at).total_seconds()
            allowed = (
                self._perms.allow_entries if record.side is Side.BUY else self._perms.allow_exits
            )
            if (
                not allowed or age > self._settings.order_timeout_s
            ) and not record.cancel_requested:
                try:
                    await self._broker.cancel(record.order_id)
                except Exception as exc:
                    self._log_throttled(
                        f"cancel:{symbol}", now, "cancel of %s failed: %s", record.order_id, exc
                    )
                    continue
                record.cancel_requested = True
                log.info("%s: cancel requested for order %s", symbol, record.order_id)
                await self._save_order(record)
            if age > self.STUCK_ORDER_ALERT_S:
                await self._alerts.send(
                    f"order_stuck:{symbol}",
                    f"Order on {symbol} is not finishing",
                    f"Order {record.order_id} has been open for {age:.0f}s "
                    f"(broker status {order.raw_status}).",
                )

    async def _finish_order(
        self, symbol: str, st: _SymbolState, record: OrderRecord, order: BrokerOrder, now: datetime
    ) -> None:
        record.status = order.status
        filled = order.filled_quantity
        if filled > 0 and record.side is Side.SELL:
            # Counted before the order leaves the open list: a crash in between then
            # counts the sale twice after the restart, never zero times.
            await self._note_sale(symbol, filled, order.avg_fill_price or record.limit_price, now)
        await self._save_order(record)
        st.working = None
        if filled > 0:
            signed = filled if record.side is Side.BUY else -filled
            st.pending = _Pending(
                record.position_before + signed, record.position_before, now, confirmed=True
            )
        rejected = order.status is OrderStatus.REJECTED
        st.hold_until = now + timedelta(
            seconds=self.REJECT_HOLD_S if rejected else self.POST_ORDER_HOLD_S
        )
        self._account_dirty = True
        await self._event(
            "order_done",
            {
                "order_id": record.order_id,
                "symbol": symbol,
                "side": record.side.value,
                "quantity": record.quantity,
                "status": order.status.value,
                "broker_status": order.raw_status,
                "filled_quantity": filled,
                "avg_fill_price": order.avg_fill_price,
            },
            now,
        )
        if rejected:
            await self._alerts.send(
                f"order_rejected:{symbol}",
                f"Broker rejected an order on {symbol}",
                f"Order {record.order_id}: {record.side.value} {record.quantity} {symbol}.",
            )

    # ---------------------------------------------------------------- strategy

    async def _run_strategy(self, now: datetime) -> None:
        account = self._account
        positions = {s: account.position(s) for s in self._symbols} if account else {}
        ctx = StrategyContext(now=now, positions=positions, chains=self._market.chains())
        if self._research is not None:
            view = self._research.view
            ctx = replace(ctx, picks=view.live_picks(now), posture=view.level)
        for bar, _warmup in self._market.drain_bars():
            await self._hear(lambda bar=bar: self._strategy.on_bar(bar, ctx), now)  # type: ignore[misc]
        for symbol in self._market.drain_dirty():
            quote = self._market.quote(symbol)
            if quote is not None:
                await self._hear(lambda quote=quote: self._strategy.on_quote(quote, ctx), now)  # type: ignore[misc]

    async def _hear(self, call: Callable[[], Sequence[Target]], now: datetime) -> None:
        try:
            targets = list(call())
        except Exception as exc:
            log.exception("strategy raised")
            await self._strategy_failed(exc)
            return
        for target in targets:
            await self._apply_target(target, now)

    async def _strategy_failed(self, exc: Exception) -> None:
        """No new entries until a restart. Exits keep working."""
        if self._strategy_error is None:
            self._strategy_error = f"{type(exc).__name__}: {exc}"
        await self._alerts.send(
            "strategy_error",
            "Strategy error",
            f"{self._strategy_error}. New entries are off until the bot is restarted. "
            "Exits still work.",
        )

    async def _apply_target(self, target: Target, now: datetime) -> None:
        if target.symbol in self._universe:
            st: _SymbolState | None = self._symbols.setdefault(target.symbol, _SymbolState())
        else:
            st = self._track_option(target.symbol)
        if st is None:
            log.debug("ignoring target for unconfigured symbol %s", target.symbol)
            return
        quantity = max(0, target.quantity)  # long-only
        if st.target is None or st.target.quantity != quantity:
            await self._event(
                "target",
                {"symbol": target.symbol, "quantity": quantity, "reason": target.reason},
                now,
            )
        st.target = Target(target.symbol, quantity, target.reason)

    # --------------------------------------------------------------- reconcile

    async def _reconcile(self, symbol: str, st: _SymbolState, now: datetime) -> None:
        if self._busy(st) or not self._leader:
            return
        if st.hold_until is not None and now < st.hold_until:
            return
        target = self._effective_target(symbol, st, now)
        account = self._account
        if target is None or account is None:
            return
        position = account.position(symbol)
        if target == position:
            return

        # First pass on what we already know. This costs nothing, so an order that is
        # blocked anyway (cap reached, market closed) never causes broker calls.
        order = self._build_order(symbol, st, target, position)
        decision = self._check(order, position, account, st, now)
        if not decision.allowed:
            await self._note_blocked(st, order, decision, now)
            return

        # It would go out. Look at the broker right now before sending anything.
        fresh = await self._pretrade_refresh(now)
        if fresh is None:
            st.hold_until = now + timedelta(seconds=self.PRETRADE_RETRY_S)
            return
        account, open_orders = fresh
        if self._symbols.get(symbol) is not st:
            return  # the refresh dropped the symbol from the universe: it is not ours now
        known = {s.working.order_id for s in self._symbols.values() if s.working is not None}
        unknown = [o for o in open_orders if o.symbol == symbol and o.order_id not in known]
        if unknown:
            await self._handle_unknown_orders(symbol, st, unknown, now)
            return
        st.unknown_since = None
        if self._busy(st):  # the refresh may have settled or frozen the symbol
            return
        position = account.position(symbol)
        if target == position or position < 0:
            return
        order = self._build_order(symbol, st, target, position)
        decision = self._check(order, position, account, st, now)
        if not decision.allowed:
            await self._note_blocked(st, order, decision, now)
            st.hold_until = now + timedelta(seconds=self.PRETRADE_RETRY_S)
            return
        await self._place(symbol, st, order, position, now)

    @staticmethod
    def _busy(st: _SymbolState) -> bool:
        """An order is working, its outcome is unsettled, or the symbol is frozen."""
        return bool(st.frozen) or st.working is not None or st.pending is not None

    def _effective_target(self, symbol: str, st: _SymbolState, now: datetime) -> int | None:
        if self._expiring_now(symbol, now):
            return 0  # whatever the strategy says: see _expiring_now
        if self._intraday_flatten(symbol, now):
            return 0  # whatever the strategy says: intraday positions are flat by the close
        if st.target is None:
            return None  # the strategy has said nothing: hands off
        return 0 if self._flatten_now(now) else st.target.quantity

    def _expiring_now(self, symbol: str, now: datetime) -> bool:
        """True for an option on its last day once the close is near. It is sold then,
        because one left to expire in the money is exercised into a hundred shares per
        contract, which no limit here was sized for."""
        limits = self._risk.limits
        if not limits.allow_options or not self._is_our_option(symbol):
            return False
        if parse_option_symbol(symbol).days_to_expiry(trading_date(now)) > 0:
            return False
        view = self._session.view(now)
        return (
            view.is_open
            and view.minutes_to_close is not None
            and view.minutes_to_close <= limits.option_expiry_exit_min
        )

    def _flatten_now(self, now: datetime) -> bool:
        minutes = self._settings.flatten_before_close_min
        if minutes is None:
            return False
        view = self._session.view(now)
        return (
            view.is_open and view.minutes_to_close is not None and view.minutes_to_close <= minutes
        )

    def _build_order(
        self, symbol: str, st: _SymbolState, target: int, position: int
    ) -> OrderRequest:
        delta = target - position
        side = Side.BUY if delta > 0 else Side.SELL
        quantity = delta if delta > 0 else min(-delta, position)
        reason = st.target.reason if st.target is not None else ""
        now = self._clock.now()
        if self._flatten_now(now) and side is Side.SELL:
            reason = "flatten before close"
        if self._intraday_flatten(symbol, now) and side is Side.SELL:
            reason = "intraday position: flatten before close"
        option = is_option_symbol(symbol)
        if option and self._expiring_now(symbol, now) and side is Side.SELL:
            reason = "option expires today"
        if self._settings.order_type == "MARKET" and not option:
            return OrderRequest(symbol, side, quantity, OrderType.MARKET, None, reason)
        quote = self._market.quote(symbol)
        price: Decimal | None = None
        if quote is not None and quote.bid > 0 and quote.ask > 0:
            # Options: a limit at the quoted price itself. Their spreads are wide enough
            # that a market order, or a limit pushed through the quote, gives too much away,
            # and the quoted price is always on a valid price increment.
            offset = Decimal(0) if option else self._settings.limit_offset_bps / BPS
            if side is Side.BUY:
                price = (quote.ask * (1 + offset)).quantize(_CENT, rounding=ROUND_DOWN)
            else:
                price = (quote.bid * (1 - offset)).quantize(_CENT, rounding=ROUND_UP)
        # With no usable quote the price stays None and the risk check rejects the order.
        return OrderRequest(symbol, side, quantity, OrderType.LIMIT, price, reason)

    def _check(
        self,
        order: OrderRequest,
        position: int,
        account: AccountSnapshot,
        st: _SymbolState,
        now: datetime,
    ) -> Decision:
        since = None if st.last_order_at is None else (now - st.last_order_at).total_seconds()
        try:
            gate = self._gate(order, account, now)
        except Exception as exc:
            # Fail closed for entries only: exits never depend on research.
            self._log_throttled(
                "gate", now, "research gate failed, no entries: %s: %s", type(exc).__name__, exc
            )
            # A holding the bot did not open stays untouched, gate or no gate.
            gate = replace(_NO_ENTRIES_GATE, foreign=order.symbol in self._foreign)
        return self._risk.check(
            RiskContext(
                now=now,
                order=order,
                position=position,
                quote=self._market.quote(order.symbol),
                feed_alive_at=self._market.feed_alive_at,
                account=account,
                exposure_usd=self._exposure(account),
                orders_today=self._day.orders,
                entries_halted=self._entries_halted(now),
                permissions=self._perms,
                session=self._session.view(now),
                token_seconds_left=self._auth_seconds_left(),
                seconds_since_last_order=since,
                unsettled_usd=self._day.sold_usd
                + self._carried_usd
                + self._in_flight(Side.SELL, account),
                committed_usd=self._in_flight(Side.BUY, account),
                research=gate,
            )
        )

    def _gate(
        self, order: OrderRequest, account: AccountSnapshot, now: datetime
    ) -> ResearchGate | None:
        """What research says about the order's symbol (or its underlying). None without
        research, so the research rules do not apply."""
        if self._research is None:
            return None
        view = self._research.view
        s = self._settings.research
        root = root_symbol(order.symbol)
        pick = view.pick(root, now)
        level = view.level
        factor = s.reduced_factor if level is PostureLevel.REDUCED else Decimal(1)
        entry = self._ledger_entry(order.symbol)
        if entry is not None:
            horizon = entry.horizon
        else:
            horizon = pick.horizon.value if pick is not None else "swing"
        share = s.intraday_share if horizon == "intraday" else 1 - s.intraday_share
        return ResearchGate(
            pick_side=pick.side.value if pick else None,
            pinned=root in self._settings.pinned_symbols,
            posture=level.value,
            foreign=order.symbol in self._foreign,
            horizon=horizon,
            horizon_exposure_usd=self._exposure(account, horizon=horizon),
            horizon_cap_usd=self._risk.limits.max_total_exposure_usd * share,
            cap_factor=factor,
            intraday_closing=horizon == "intraday" and self._intraday_closing(now),
        )

    def _in_flight(self, side: Side, account: AccountSnapshot) -> Decimal:
        """Dollar value of the bot's orders on ``side`` that the account snapshot may not
        reflect yet: working orders in full, and fills still waiting to show up.

        For buys this is cash already spoken for. For sells it is money that is, or is
        about to be, in the account but has not settled. Counting a working order in
        full errs on the side of not trading.
        """
        total = Decimal(0)
        for symbol, st in list(self._symbols.items()):
            quote = self._market.quote(symbol)
            shares, price = 0, None
            record, pending = st.working, st.pending
            if record is not None and record.side is side:
                shares, price = record.quantity, record.limit_price
            elif pending is not None:
                moved = pending.expected - account.position(symbol)
                if (moved > 0) == (side is Side.BUY) and moved != 0:
                    shares = abs(moved)
                    price = pending.order.limit_price if pending.order is not None else None
            if shares == 0:
                continue
            if price is None and quote is not None:
                price = quote.ask if side is Side.BUY else quote.bid
            if price is None:
                held = account.positions.get(symbol)
                price = held.avg_price if held is not None else Decimal(0)
            total += price * shares * contract_size(symbol)
        return total

    def _entries_halted(self, now: datetime) -> str | None:
        if self._live_settings is not None and not self._live_settings.loaded:
            return "settings not loaded"
        if self._research is not None and not self._ledger_ok:
            return "position ledger not loaded"
        if self._day.halted_reason:
            return self._day.halted_reason
        if self._equity_unknown:
            return "account equity unknown, so the daily loss limit cannot be checked"
        if self._strategy_error:
            return f"strategy error: {self._strategy_error}"
        if self._flatten_now(now):
            return "flattening before the close"
        return None

    def _exposure(self, account: AccountSnapshot, *, horizon: str | None = None) -> Decimal:
        """Value of what the bot holds, plus buys that are on their way. With ``horizon``,
        only the symbols held under it (by their ledger entry; "swing" without one)."""
        total = Decimal(0)
        for symbol, st in list(self._symbols.items()):
            if horizon is not None and self._ledger_horizon(symbol) != horizon:
                continue
            held = account.positions.get(symbol)
            size = contract_size(symbol)
            quote = self._market.quote(symbol)
            bid = quote.bid if quote is not None and quote.bid > 0 else None
            ask = quote.ask if quote is not None and quote.ask > 0 else None
            position = held.quantity if held is not None else 0
            if held is not None and position > 0:
                total += (bid if bid is not None else held.avg_price) * position * size
            incoming = 0
            record = st.working
            if record is not None and record.side is Side.BUY:
                incoming += record.quantity - record.filled_quantity
            if st.pending is not None:
                incoming += max(0, st.pending.expected - position)
            if incoming > 0:
                price = ask or (record.limit_price if record is not None else None)
                if price is None and held is not None:
                    price = held.avg_price
                total += (price or Decimal(0)) * incoming * size
        return total

    async def _note_blocked(
        self, st: _SymbolState, order: OrderRequest, decision: Decision, now: datetime
    ) -> None:
        codes = frozenset(decision.codes)
        if st.last_block is not None:
            last_codes, last_at = st.last_block
            recently = (now - last_at).total_seconds() < self.BLOCK_LOG_INTERVAL_S
            if last_codes == codes and recently:
                return
        st.last_block = (codes, now)
        await self._event(
            "order_blocked",
            {
                "symbol": order.symbol,
                "side": order.side.value,
                "quantity": order.quantity,
                "codes": sorted(codes),
                "detail": decision.summary(),
            },
            now,
        )

    async def _pretrade_refresh(
        self, now: datetime
    ) -> tuple[AccountSnapshot, list[BrokerOrder]] | None:
        """Account and open orders, straight from the broker. Shared within one step."""
        if self._pretrade is not None and self._pretrade[0] == now:
            return self._pretrade[1], self._pretrade[2]
        self._account_attempt_at = now
        try:
            account = await self._broker.get_account()
            open_orders = await self._broker.get_open_orders()
        except Exception as exc:
            self._log_throttled("pretrade", now, "pre-trade refresh failed, not trading: %s", exc)
            return None
        await self._on_account(account, now)
        self._pretrade = (now, account, open_orders)
        return account, open_orders

    async def _handle_unknown_orders(
        self, symbol: str, st: _SymbolState, unknown: list[BrokerOrder], now: datetime
    ) -> None:
        ids = [o.order_id for o in unknown]
        if st.unknown_since is None:
            st.unknown_since = now
            await self._event("unknown_order", {"symbol": symbol, "order_ids": ids}, now)
        age = (now - st.unknown_since).total_seconds()
        if self._settings.cancel_unknown_orders and age >= self._settings.order_timeout_s:
            for order in unknown:
                log.warning("%s: cancelling unknown order %s", symbol, order.order_id)
                await self._cancel_quietly(order.order_id)
        if age >= self.UNKNOWN_ORDER_ALERT_S:
            await self._alerts.send(
                f"unknown_order:{symbol}",
                f"Open order on {symbol} the bot did not place",
                f"Order(s) {', '.join(ids)} have been open for {age:.0f}s. The bot will not "
                f"trade {symbol} while they are open. Cancel them, or let them finish.",
            )
        st.hold_until = now + timedelta(seconds=self.UNKNOWN_ORDER_HOLD_S)

    # ------------------------------------------------------------------- place

    async def _place(
        self, symbol: str, st: _SymbolState, order: OrderRequest, position: int, now: datetime
    ) -> None:
        if not self._lease_is_safe():
            self._log_throttled("lease-late", now, "lease about to lapse, not placing an order")
            self._lease_at = None  # renew on the next pass
            st.hold_until = now + timedelta(seconds=self.ORDER_POLL_S)
            return
        # Count the order before sending it. If the counter cannot be written, do not trade.
        try:
            count = await self._state.incr_orders(self._day.day)
        except Exception as exc:
            self._log_throttled("counter", now, "order counter unavailable, not trading: %s", exc)
            st.hold_until = now + timedelta(seconds=self.PRETRADE_RETRY_S)
            return
        self._day = replace(self._day, orders=count)
        if not await self._book_entry(symbol, st, order, now):
            return
        details: dict[str, Any] = {
            "symbol": symbol,
            "side": order.side.value,
            "quantity": order.quantity,
            "order_type": order.order_type.value,
            "limit_price": order.limit_price,
            "position_before": position,
            "reason": order.reason,
            "mode": self._config.trading_mode,
        }
        signed = order.quantity if order.side is Side.BUY else -order.quantity
        try:
            order_id = await self._broker.place(order)
        except OrderRejected as exc:
            st.hold_until = now + timedelta(seconds=self.REJECT_HOLD_S)
            await self._event("order_rejected", {**details, "error": str(exc)}, now)
            await self._alerts.send(
                f"order_rejected:{symbol}",
                f"Broker rejected an order on {symbol}",
                f"{order.side.value} {order.quantity} {symbol}: {exc}",
            )
            return
        except BrokerUnavailable as exc:
            # Raised only when nothing was sent, so a plain retry later is safe.
            st.hold_until = now + timedelta(seconds=self.UNAVAILABLE_HOLD_S)
            self._log_throttled(f"place:{symbol}", now, "%s: broker unavailable: %s", symbol, exc)
            return
        except Exception as exc:
            # The order may or may not exist. Do not resend: wait for the position to tell us.
            log.exception("%s: order outcome unknown", symbol)
            st.last_order_at = now
            st.pending = _Pending(position + signed, position, now, confirmed=False, order=order)
            self._account_dirty = True
            await self._event("order_unconfirmed", {**details, "error": str(exc)}, now)
            await self._alerts.send(
                f"order_unconfirmed:{symbol}",
                f"Order on {symbol} may or may not have gone through",
                f"{order.side.value} {order.quantity} {symbol} failed with: {exc}. The bot is "
                "waiting to see whether the position changes and will not resend blindly.",
            )
            return

        st.last_order_at = now
        st.last_block = None
        if order_id is None:
            # Accepted with no id (Schwab does this for instant fills). Look the order up
            # by its details; until then nothing else is sent for this symbol.
            st.pending = _Pending(position + signed, position, now, confirmed=False, order=order)
            self._account_dirty = True
            await self._event("order_submitted", {**details, "order_id": None}, now)
            return
        record = OrderRecord(
            order_id=order_id,
            symbol=symbol,
            side=order.side,
            quantity=order.quantity,
            order_type=order.order_type,
            limit_price=order.limit_price,
            submitted_at=now,
            position_before=position,
            reason=order.reason,
        )
        st.working = record
        st.last_poll_at = None
        self._seen_order_ids.add(order_id)
        await self._save_order(record)
        await self._event("order_submitted", {**details, "order_id": order_id}, now)

    async def _book_entry(
        self, symbol: str, st: _SymbolState, order: OrderRequest, now: datetime
    ) -> bool:
        """With research on, record a buy in the ledger before it is sent: a position the
        bot cannot prove it opened would be foreign after a restart. False: do not send."""
        if order.side is not Side.BUY:
            return True
        if await self._ensure_booked(symbol, now):
            return True
        st.hold_until = now + timedelta(seconds=self.PRETRADE_RETRY_S)
        return False

    async def _ensure_booked(self, symbol: str, now: datetime) -> bool:
        """Make sure the ledger has an entry for a symbol the bot is buying or has bought.
        True when it has one (always, without research)."""
        research = self._research
        if research is None or symbol in self._ledger:
            return True
        pick = research.view.pick(root_symbol(symbol), now)
        entry = LedgerEntry(
            symbol,
            horizon=pick.horizon.value if pick is not None else "swing",
            side=pick.side.value if pick is not None else "long",
            opened_at=now,
            pick_run_id=pick.run_id if pick is not None else "",
            pick_rank=pick.rank if pick is not None else 0,
        )
        try:
            await self._state.put_ledger(entry)
        except Exception as exc:
            self._log_throttled(
                "ledger-write", now, "%s: could not write the ledger entry: %s", symbol, exc
            )
            return False
        self._ledger[symbol] = entry
        return True

    # ----------------------------------------------------------------- helpers

    async def _save_order(self, record: OrderRecord) -> None:
        try:
            await self._state.save_order(record)
        except Exception:
            log.exception("could not persist order %s", record.order_id)

    async def _cancel_quietly(self, order_id: str) -> None:
        try:
            await self._broker.cancel(order_id)
        except Exception as exc:
            log.warning("cancel of %s failed: %s", order_id, exc)

    async def _event(self, kind: str, data: dict[str, Any], now: datetime) -> None:
        log.info("%s %s", kind, data)
        try:
            await self._state.log_event(kind, data, now)
        except Exception as exc:
            self._log_throttled("audit", now, "could not write the audit log: %s", exc)

    def _heartbeat(self, now: datetime) -> None:
        if not _due(self._heartbeat_at, now, self.HEARTBEAT_S):
            return
        self._heartbeat_at = now
        try:
            Path(self._config.heartbeat_file).write_text(f"{now.timestamp():.3f}")
        except OSError as exc:
            self._log_throttled("heartbeat", now, "could not write heartbeat: %s", exc)

    def _log_throttled(self, key: str, now: datetime, message: str, *args: object) -> None:
        last = self._throttled.get(key)
        if last is not None and (now - last).total_seconds() < self.BLOCK_LOG_INTERVAL_S:
            return
        self._throttled[key] = now
        log.warning(message, *args)


def _due(last: datetime | None, now: datetime, interval_s: float) -> bool:
    return last is None or (now - last).total_seconds() >= interval_s
