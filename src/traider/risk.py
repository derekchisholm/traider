"""Pre-trade risk checks.

``RiskManager.check`` is a pure function of its inputs. It never talks to the
broker and holds no state, so every rule can be tested exactly and the same
rules run in paper trading, backtests and live trading.

The bot is long-only: a BUY opens or adds to a position (an *entry*), a SELL
reduces one (an *exit*). Exits are exempt from the caps that exist to stop the
bot taking on risk, because blocking an exit leaves risk on the table. Exits
still need the control switch, an open market and sane market data.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from traider.config import RiskLimits
from traider.control import Permissions
from traider.models import BPS, AccountSnapshot, OrderRequest, OrderType, Quote, Side

_Reject = Callable[[str, str], None]


@dataclass(frozen=True, slots=True)
class SessionView:
    is_open: bool
    minutes_since_open: float | None
    minutes_to_close: float | None


@dataclass(frozen=True, slots=True)
class RiskContext:
    now: datetime
    order: OrderRequest
    position: int  # shares currently held in order.symbol
    quote: Quote | None
    feed_alive_at: datetime | None  # last time the market-data feed proved it was alive
    account: AccountSnapshot | None
    exposure_usd: Decimal  # value of everything the bot currently holds
    orders_today: int
    entries_halted: str | None  # reason new entries are off (daily loss limit, flatten, ...)
    permissions: Permissions
    session: SessionView
    token_seconds_left: float | None  # None when there is no login to expire (backtests)
    seconds_since_last_order: float | None  # for this symbol
    unsettled_usd: Decimal  # proceeds of the bot's sales today, which settle tomorrow


@dataclass(frozen=True, slots=True)
class Rejection:
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True, slots=True)
class Decision:
    rejections: tuple[Rejection, ...]
    reducing: bool

    @property
    def allowed(self) -> bool:
        return not self.rejections

    @property
    def codes(self) -> set[str]:
        return {r.code for r in self.rejections}

    def summary(self) -> str:
        return "; ".join(str(r) for r in self.rejections) or "ok"


class RiskManager:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    def check(self, ctx: RiskContext) -> Decision:
        order = ctx.order
        is_entry = order.side is Side.BUY
        out: list[Rejection] = []

        def reject(code: str, detail: str) -> None:
            out.append(Rejection(code, detail))

        self._check_control(ctx, is_entry, reject)
        self._check_shape(ctx, reject)
        self._check_session(ctx, is_entry, reject)
        reference = self._check_market_data(ctx, is_entry, reject)
        if ctx.account is None:
            reject("no_account", "no account snapshot from the broker")
        if is_entry:
            self._check_entry_caps(ctx, reference, reject)
            self._check_entry_activity(ctx, reject)
        return Decision(tuple(out), reducing=not is_entry)

    # -- individual rule groups ------------------------------------------------

    @staticmethod
    def _check_control(ctx: RiskContext, is_entry: bool, reject: _Reject) -> None:
        allowed = ctx.permissions.allow_entries if is_entry else ctx.permissions.allow_exits
        if not allowed:
            reject("control", ctx.permissions.reason)

    @staticmethod
    def _check_shape(ctx: RiskContext, reject: _Reject) -> None:
        order = ctx.order
        if order.quantity <= 0:
            reject("quantity", f"quantity must be positive, got {order.quantity}")
        if order.order_type is OrderType.LIMIT and (
            order.limit_price is None or order.limit_price <= 0
        ):
            reject("limit_price", "limit order without a positive limit price")
        if order.side is Side.SELL and order.quantity > ctx.position:
            reject(
                "short",
                f"sell {order.quantity} exceeds the {ctx.position} held; the bot never shorts",
            )

    def _check_session(self, ctx: RiskContext, is_entry: bool, reject: _Reject) -> None:
        session = ctx.session
        if not session.is_open:
            reject("session_closed", "regular trading session is not open")
            return
        if not is_entry:
            return
        since_open, to_close = session.minutes_since_open, session.minutes_to_close
        if since_open is None or since_open < self.limits.entry_delay_min_after_open:
            reject(
                "entry_window",
                f"no entries in the first {self.limits.entry_delay_min_after_open} min",
            )
        if to_close is None or to_close < self.limits.entry_cutoff_min_before_close:
            reject(
                "entry_window",
                f"no entries in the last {self.limits.entry_cutoff_min_before_close} min",
            )

    def _check_market_data(
        self, ctx: RiskContext, is_entry: bool, reject: _Reject
    ) -> Decimal | None:
        """Validate the quote. Returns the price to value the order at, if there is one."""
        limits, order, quote = self.limits, ctx.order, ctx.quote
        if ctx.feed_alive_at is None:
            reject("feed_silent", "market-data feed has not delivered anything yet")
        else:
            silence = (ctx.now - ctx.feed_alive_at).total_seconds()
            if silence > limits.max_feed_silence_s:
                reject("feed_silent", f"market-data feed silent for {silence:.0f}s")
        if quote is None:
            reject("no_quote", f"no quote for {order.symbol}")
            return None
        if quote.delayed:
            reject("quote_delayed", "quote is delayed, not real-time")
        age = (ctx.now - quote.received_at).total_seconds()
        if age > limits.max_quote_age_s:
            reject("quote_stale", f"quote is {age:.0f}s old, limit {limits.max_quote_age_s:.0f}s")
        # A positive bid with an ask at or above it; that also rules out a zero ask.
        if quote.bid <= 0 or quote.ask < quote.bid:
            reject("bad_quote", f"unusable quote {quote.bid} x {quote.ask}")
            return None

        if is_entry:
            spread = quote.spread_bps
            if spread is not None and spread > limits.max_spread_bps:
                reject("spread", f"spread {spread:.1f} bps over the {limits.max_spread_bps} limit")
            if quote.ask < limits.min_price:
                reject("min_price", f"price {quote.ask} under the {limits.min_price} minimum")

        if order.order_type is OrderType.LIMIT and order.limit_price and order.limit_price > 0:
            tolerance = limits.max_limit_deviation_bps / BPS
            if is_entry and order.limit_price > quote.ask * (1 + tolerance):
                reject("limit_price", f"buy limit {order.limit_price} far above ask {quote.ask}")
            if not is_entry and order.limit_price < quote.bid * (1 - tolerance):
                reject("limit_price", f"sell limit {order.limit_price} far below bid {quote.bid}")
            return order.limit_price
        if order.order_type is OrderType.MARKET:
            return quote.ask if is_entry else quote.bid
        return None

    def _check_entry_caps(
        self, ctx: RiskContext, reference: Decimal | None, reject: _Reject
    ) -> None:
        limits, order = self.limits, ctx.order
        if order.quantity > limits.max_shares_per_order:
            reject(
                "max_shares", f"{order.quantity} shares over the {limits.max_shares_per_order} cap"
            )
        if reference is None or order.quantity <= 0:
            return  # already rejected for the missing price or bad quantity
        notional = reference * order.quantity
        if notional > limits.max_order_usd:
            reject(
                "max_order_usd", f"order value {notional:.2f} over the {limits.max_order_usd} cap"
            )
        position_value = reference * (ctx.position + order.quantity)
        if position_value > limits.max_position_usd:
            reject(
                "max_position_usd",
                f"position would be {position_value:.2f}, cap {limits.max_position_usd}",
            )
        exposure = ctx.exposure_usd + notional
        if exposure > limits.max_total_exposure_usd:
            reject(
                "max_total_exposure_usd",
                f"total exposure would be {exposure:.2f}, cap {limits.max_total_exposure_usd}",
            )
        if limits.require_cash and ctx.account is not None:
            cash = ctx.account.cash_available
            if cash is None:
                reject("cash", "broker did not report available cash")
            elif notional > cash:
                reject("cash", f"order value {notional:.2f} over available cash {cash:.2f}")
            elif limits.settled_cash_only and notional > cash - ctx.unsettled_usd:
                reject(
                    "unsettled_cash",
                    f"order value {notional:.2f} over settled cash "
                    f"{cash - ctx.unsettled_usd:.2f}: {ctx.unsettled_usd:.2f} from today's "
                    "sales settles on the next business day",
                )

    def _check_entry_activity(self, ctx: RiskContext, reject: _Reject) -> None:
        limits = self.limits
        if ctx.orders_today >= limits.max_orders_per_day:
            reject(
                "max_orders_per_day",
                f"{ctx.orders_today} orders today, cap {limits.max_orders_per_day}",
            )
        if ctx.entries_halted:
            reject("entries_halted", ctx.entries_halted)
        since = ctx.seconds_since_last_order
        if since is not None and since < limits.order_cooldown_s:
            reject(
                "cooldown",
                f"{since:.0f}s since the last order, cooldown {limits.order_cooldown_s:.0f}s",
            )
        left = ctx.token_seconds_left
        if left is not None and left < limits.min_token_hours_for_entry * 3600:
            reject("token_expiring", f"Schwab login expires in {left / 3600:.1f}h")
