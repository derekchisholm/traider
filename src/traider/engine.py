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
from datetime import datetime, timedelta
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
from traider.risk import Decision, RiskContext, RiskManager
from traider.session import SessionTracker
from traider.state.base import DayState, StateStore
from traider.strategy.base import Strategy, StrategyContext
from traider.timeutil import Clock, trading_date

log = logging.getLogger(__name__)

_CENT = Decimal("0.01")
_HALTED = Permissions(False, False, False, "control not read yet")


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
    LEASE_TTL_S = 30.0
    LEASE_RENEW_S = 10.0
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
    HEARTBEAT_S = 2.0
    IDLE_TICK_S = 1.0

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

        self._symbols: dict[str, _SymbolState] = {s: _SymbolState() for s in config.symbols}
        self._perms = _HALTED
        self._leader = False
        self._day = DayState(trading_date(clock.now()).isoformat(), halted_reason="not loaded yet")
        self._day_ok = False
        self._day_attempt_at: datetime | None = None
        self._account: AccountSnapshot | None = None
        self._account_at: datetime | None = None
        self._account_attempt_at: datetime | None = None
        self._account_dirty = True
        self._equity_unknown = True
        self._strategy_error: str | None = None
        self._control_at: datetime | None = None
        self._lease_at: datetime | None = None
        self._heartbeat_at: datetime | None = None
        self._pretrade: tuple[datetime, AccountSnapshot, list[BrokerOrder]] | None = None
        self._throttled: dict[str, datetime] = {}
        self._seen_order_ids: set[str] = set()  # every order this process has tracked

    # ------------------------------------------------------------------ public

    @property
    def is_leader(self) -> bool:
        return self._leader

    def target(self, symbol: str) -> Target | None:
        return self._symbols[symbol].target

    async def start(self) -> None:
        """Load today's counters and pick up any orders a previous run left open."""
        now = self._clock.now()
        await self._roll_day(now)
        for record in await self._state.open_orders():
            self._seen_order_ids.add(record.order_id)
            st = self._symbols.setdefault(record.symbol, _SymbolState())
            if st.working is None:
                st.working = record
                log.info("resuming order %s (%s)", record.order_id, record.symbol)
            else:
                # Two open orders on one symbol should not happen. Get rid of the extra.
                log.warning("extra open order %s on %s: cancelling", record.order_id, record.symbol)
                await self._cancel_quietly(record.order_id)

    async def step(self) -> None:
        """One pass of the loop. Safe to call as often as you like."""
        now = self._clock.now()
        await self._housekeeping(now)
        await self._resolve_unconfirmed(now)
        await self._manage_working_orders(now)
        await self._run_strategy(now)
        for symbol, st in self._symbols.items():
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
        if _due(self._control_at, now, self.CONTROL_REFRESH_S):
            self._control_at = now
            await self._control.refresh(now)
        await self._update_permissions(now)
        if _due(self._lease_at, now, self.LEASE_RENEW_S):
            self._lease_at = now
            await self._renew_lease(now)
        await self._session.refresh(now)
        await self._roll_day(now)
        interval = self.ACCOUNT_RETRY_S if self._account_dirty else self.ACCOUNT_REFRESH_S
        if _due(self._account_attempt_at, now, interval):
            await self._refresh_account(now)

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

    async def _renew_lease(self, now: datetime) -> None:
        try:
            leader = await self._state.acquire_lease(self._instance, self.LEASE_TTL_S, now)
        except Exception as exc:
            self._log_throttled("lease", now, "lease check failed, not trading: %s", exc)
            leader = False
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
        try:
            self._day = await self._state.get_day(day)
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
        await self._settle_pending(account, now)
        await self._check_daily_loss(account, now)

    async def _settle_pending(self, account: AccountSnapshot, now: datetime) -> None:
        for symbol, st in self._symbols.items():
            pending = st.pending
            if pending is None:
                continue
            position = account.position(symbol)
            age = (now - pending.since).total_seconds()
            if position == pending.expected:
                st.pending = None
                request = pending.order  # only there when the order's fate was unknown
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
        for symbol, st in self._symbols.items():
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
            log.exception("could not persist the daily halt")
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
        proceeds = (price * quantity).quantize(_CENT, rounding=ROUND_UP)
        # In memory first, so the rule holds even if the write below fails.
        self._day = replace(self._day, sold_usd=self._day.sold_usd + proceeds)
        try:
            await self._state.add_sold(self._day.day, proceeds)
        except Exception:
            log.exception("could not persist today's sale proceeds")

    # ------------------------------------------------------------ working orders

    async def _manage_working_orders(self, now: datetime) -> None:
        for symbol, st in self._symbols.items():
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
            if (not allowed or age > self._config.order_timeout_s) and not record.cancel_requested:
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
        ctx = StrategyContext(now=now, positions=positions)
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
            if self._strategy_error is None:
                self._strategy_error = f"{type(exc).__name__}: {exc}"
            await self._alerts.send(
                "strategy_error",
                "Strategy error",
                f"{self._strategy_error}. New entries are off until the bot is restarted. "
                "Exits still work.",
            )
            return
        for target in targets:
            await self._apply_target(target, now)

    async def _apply_target(self, target: Target, now: datetime) -> None:
        st = self._symbols.get(target.symbol)
        if st is None or target.symbol not in self._config.symbols:
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
        target = self._effective_target(st, now)
        account = self._account
        if target is None or account is None:
            return
        position = account.position(symbol)
        if position < 0:
            self._log_throttled(
                f"short:{symbol}",
                now,
                "%s: account is short %s; the bot does not manage shorts",
                symbol,
                position,
            )
            return
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

    def _effective_target(self, st: _SymbolState, now: datetime) -> int | None:
        if st.target is None:
            return None  # the strategy has said nothing: hands off
        return 0 if self._flatten_now(now) else st.target.quantity

    def _flatten_now(self, now: datetime) -> bool:
        minutes = self._config.flatten_before_close_min
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
        if self._flatten_now(self._clock.now()) and side is Side.SELL:
            reason = "flatten before close"
        if self._config.order_type == "MARKET":
            return OrderRequest(symbol, side, quantity, OrderType.MARKET, None, reason)
        quote = self._market.quote(symbol)
        price: Decimal | None = None
        if quote is not None and quote.bid > 0 and quote.ask > 0:
            offset = self._config.limit_offset_bps / BPS
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
                unsettled_usd=self._day.sold_usd,
            )
        )

    def _entries_halted(self, now: datetime) -> str | None:
        if self._day.halted_reason:
            return self._day.halted_reason
        if self._equity_unknown:
            return "account equity unknown, so the daily loss limit cannot be checked"
        if self._strategy_error:
            return f"strategy error: {self._strategy_error}"
        if self._flatten_now(now):
            return "flattening before the close"
        return None

    def _exposure(self, account: AccountSnapshot) -> Decimal:
        """Value of what the bot holds, plus buys that are on their way."""
        total = Decimal(0)
        for symbol, st in self._symbols.items():
            held = account.positions.get(symbol)
            quote = self._market.quote(symbol)
            bid = quote.bid if quote is not None and quote.bid > 0 else None
            ask = quote.ask if quote is not None and quote.ask > 0 else None
            position = held.quantity if held is not None else 0
            if held is not None and position > 0:
                total += (bid if bid is not None else held.avg_price) * position
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
                total += (price or Decimal(0)) * incoming
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
        if self._config.cancel_unknown_orders and age >= self._config.order_timeout_s:
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
        # Count the order before sending it. If the counter cannot be written, do not trade.
        try:
            count = await self._state.incr_orders(self._day.day)
        except Exception as exc:
            self._log_throttled("counter", now, "order counter unavailable, not trading: %s", exc)
            st.hold_until = now + timedelta(seconds=self.PRETRADE_RETRY_S)
            return
        self._day = replace(self._day, orders=count)
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
