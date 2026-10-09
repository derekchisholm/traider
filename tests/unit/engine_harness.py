"""A bench for driving the engine one step at a time with a controllable clock and broker."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from tests.unit.helpers import T0, make_quote
from traider.alerts import LogAlerter
from traider.broker.paper import PaperBroker
from traider.config import Config, RiskLimits
from traider.control import ControlState
from traider.engine import Engine
from traider.marketdata import MarketData
from traider.models import Bar, BrokerOrder, OrderRequest, OrderStatus, Quote, Side, Target
from traider.risk import RiskManager
from traider.session import SessionTracker, StaticSessionProvider
from traider.settings import Settings
from traider.settings_store import LiveSettings
from traider.state.memory import MemoryStateStore
from traider.strategy.base import Strategy, StrategyContext
from traider.timeutil import ManualClock, trading_date


class ScriptedStrategy(Strategy):
    """Returns whatever targets the test queued, on the next bar or quote."""

    name = "scripted"

    def __init__(self, symbols) -> None:
        super().__init__(symbols, {})
        self.bar_targets: list[Target] = []
        self.quote_targets: list[Target] = []
        self.bars_seen: list[Bar] = []
        self.quotes_seen: list[Quote] = []
        self.contexts: list[StrategyContext] = []
        self.fail_with: Exception | None = None

    def on_bar(self, bar, ctx):
        self.bars_seen.append(bar)
        self.contexts.append(ctx)
        if self.fail_with is not None:
            raise self.fail_with
        out, self.bar_targets = self.bar_targets, []
        return out

    def on_quote(self, quote, ctx):
        self.quotes_seen.append(quote)
        out, self.quote_targets = self.quote_targets, []
        return out


class MutableControl:
    def __init__(self, value: str) -> None:
        self.value = value
        self.error: Exception | None = None

    async def read(self):
        if self.error is not None:
            raise self.error
        return self.value


class FakeBroker(PaperBroker):
    """A paper broker with switches for the awkward things a real broker does."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: Counter[str] = Counter()
        self.placed: list[OrderRequest] = []
        self.cancelled: list[str] = []
        self.hold_fills = False  # orders rest instead of filling
        self.place_error: Exception | None = None  # refuse before accepting
        self.place_error_after_accepting: Exception | None = None  # accept, then fail the reply
        self.drop_order_id = False  # accept but return no id
        self.account_error: Exception | None = None
        self.order_error: Exception | None = None
        self.cancel_error: Exception | None = None
        self.ignore_cancels = False  # accept cancel requests but leave the order working
        self.find_error: Exception | None = None
        self.frozen_account = None  # serve this stale snapshot instead of the truth
        self.foreign_orders: list[BrokerOrder] = []  # open orders somebody else placed
        self.hide_fill_prices = False  # report fills without saying at what price

    async def get_account(self):
        self.calls["get_account"] += 1
        if self.account_error is not None:
            raise self.account_error
        if self.frozen_account is not None:
            return replace(self.frozen_account, as_of=self._clock.now())
        return await super().get_account()

    async def get_open_orders(self):
        self.calls["get_open_orders"] += 1
        if self.account_error is not None:
            raise self.account_error
        return [*(await super().get_open_orders()), *self.foreign_orders]

    async def place(self, request):
        self.calls["place"] += 1
        if self.place_error is not None:
            raise self.place_error
        order_id = await super().place(request)
        self.placed.append(request)
        if self.place_error_after_accepting is not None:
            raise self.place_error_after_accepting
        return None if self.drop_order_id else order_id

    async def get_order(self, order_id):
        self.calls["get_order"] += 1
        if self.order_error is not None:
            raise self.order_error
        order = await super().get_order(order_id)
        return replace(order, avg_fill_price=None) if self.hide_fill_prices else order

    async def cancel(self, order_id):
        self.calls["cancel"] += 1
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancelled.append(order_id)
        if self.ignore_cancels:
            return
        if any(o.order_id == order_id for o in self.foreign_orders):
            self.foreign_orders = [o for o in self.foreign_orders if o.order_id != order_id]
            return
        await super().cancel(order_id)

    async def find_order(self, symbol, side, quantity, since):
        self.calls["find_order"] += 1
        if self.find_error is not None:
            raise self.find_error
        return await super().find_order(symbol, side, quantity, since)

    async def _evaluate(self, order_id):
        if not self.hold_fills:
            await super()._evaluate(order_id)

    # -- test conveniences ----------------------------------------------------

    def true_position(self, symbol: str) -> int:
        held = self._holdings.get(symbol)
        return held.quantity if held else 0

    def fill_partially_then_cancel(self, order_id: str, quantity: int) -> None:
        request, order = self._orders[order_id]
        quote = self._market.quote(request.symbol)
        price = quote.ask if request.side is Side.BUY else quote.bid
        assert self._apply_fill(replace(request, quantity=quantity), price)
        self._orders[order_id] = (
            request,
            replace(
                order,
                status=OrderStatus.CANCELED,
                raw_status="CANCELED",
                filled_quantity=quantity,
                avg_fill_price=price,
            ),
        )

    def fill_part(self, order_id: str, quantity: int) -> None:
        """Fill some of a resting order and leave the rest working (keep hold_fills on)."""
        request, order = self._orders[order_id]
        quote = self._market.quote(request.symbol)
        price = quote.ask if request.side is Side.BUY else quote.bid
        assert self._apply_fill(replace(request, quantity=quantity), price)
        self._orders[order_id] = (
            request,
            replace(order, filled_quantity=quantity, avg_fill_price=price),
        )

    def add_foreign_order(self, symbol="SPY", order_id="F1", side=Side.BUY, quantity=1):
        self.foreign_orders.append(
            BrokerOrder(
                order_id,
                symbol,
                side,
                quantity,
                0,
                OrderStatus.WORKING,
                entered_at=self._clock.now(),
                raw_status="WORKING",
            )
        )


class Harness:
    clock: ManualClock
    market: MarketData
    store: MemoryStateStore
    broker: FakeBroker
    strategy: ScriptedStrategy
    control_source: MutableControl
    alerts: LogAlerter
    config: Config
    engine: Engine

    @classmethod
    async def create(
        cls,
        tmp_path,
        *,
        symbols=("SPY",),
        trading_mode="paper",
        control="paper",
        risk: dict | None = None,
        config: dict | None = None,
        backing: dict | None = None,
        cash="10000",
        start: datetime = T0,
        instance="bot-1",
        session_provider=None,
        begin=True,
        restart_of: Harness | None = None,
        settings_store=None,
    ) -> Harness:
        """``restart_of`` models a new process: same broker account, same durable state and
        the same wall clock, but an engine with empty memory."""
        self = cls()
        if restart_of is not None:
            self.clock = restart_of.clock
            self.market = restart_of.market
            self.backing = restart_of.backing
            self.store = MemoryStateStore(trading_mode, self.backing)
            self.broker = restart_of.broker
        else:
            self.clock = ManualClock(start)
            self.market = MarketData()
            self.backing = backing if backing is not None else {}
            self.store = MemoryStateStore(trading_mode, self.backing)
            self.broker = FakeBroker(
                self.market, self.clock, starting_cash=Decimal(cash), store=self.store
            )
            await self.broker.load()
        self.strategy = ScriptedStrategy(symbols)
        self.control_source = MutableControl(control)
        self.alerts = LogAlerter()
        self.auth_seconds_left: float | None = None
        fields: dict[str, Any] = {
            "symbols": tuple(symbols),
            "trading_mode": trading_mode,
            # Roomier than the production defaults so tests can use round numbers.
            "risk": RiskLimits(**{"order_cooldown_s": 0, "max_order_usd": 1000, **(risk or {})}),
            "heartbeat_file": str(tmp_path / "heartbeat"),
            **(config or {}),
        }
        if trading_mode == "live":
            fields.update(
                account_last4="1234",
                control_param="/traider/test/control",
                state_table="traider-test",
            )
        self.config = Config(**fields)
        self.prices = dict.fromkeys(symbols, ("100.00", "100.02"))
        # The market data outlives a restart here, and it drops bars it has already seen,
        # so a restarted bench must carry on from the last bar rather than start over.
        self._bars = restart_of._bars if restart_of is not None else 0
        self.live_settings = None
        if settings_store is not None:
            self.live_settings = LiveSettings(settings_store, Settings.from_config(self.config))
            await self.live_settings.start(self.clock.now())
        self.engine = Engine(
            config=self.config,
            clock=self.clock,
            market=self.market,
            strategy=self.strategy,
            risk=RiskManager(self.config.risk),
            broker=self.broker,
            state=self.store,
            control=ControlState(self.control_source),
            session=SessionTracker(session_provider or StaticSessionProvider()),
            alerts=self.alerts,
            instance_id=instance,
            auth_seconds_left=lambda: self.auth_seconds_left,
            settings=self.live_settings,
        )
        self.requote()
        if begin:
            await self.engine.start()
            await self.engine.step()
        return self

    # -- driving ----------------------------------------------------------------

    def requote(self) -> None:
        for symbol, (bid, ask) in self.prices.items():
            self.market.on_quote(make_quote(symbol, bid, ask, at=self.clock.now()))

    def price(self, symbol: str, bid: str, ask: str) -> None:
        self.prices[symbol] = (bid, ask)
        self.requote()

    async def tick(self, seconds: float = 1.0, *, requote: bool = True) -> None:
        self.clock.advance(seconds)
        if requote:
            self.requote()
        await self.engine.step()

    async def run_for(self, seconds: float, *, step: float = 1.0) -> None:
        elapsed = 0.0
        while elapsed < seconds:
            await self.tick(step)
            elapsed += step

    def bar(self, symbol="SPY", close="100", *, warmup=False) -> None:
        self._bars += 1
        start = datetime(2026, 10, 8, 13, 30, tzinfo=UTC) + timedelta(minutes=self._bars)
        price = Decimal(close)
        self.market.on_bar(Bar(symbol, start, price, price, price, price, 100), warmup=warmup)

    async def target(self, symbol: str, quantity: int, reason: str = "test") -> None:
        """Have the strategy ask for ``quantity`` shares on the next bar, then step once."""
        self.strategy.bar_targets.append(Target(symbol, quantity, reason))
        self.bar(symbol)
        await self.engine.step()

    async def settle(self, seconds: float = 5.0) -> None:
        """Let fills be noticed and the account catch up."""
        await self.run_for(seconds)

    async def set_control(self, value: str) -> None:
        self.control_source.value = value
        await self.tick(11)  # the engine re-reads control every 10 seconds

    # -- inspection ---------------------------------------------------------------

    def position(self, symbol="SPY") -> int:
        return self.broker.true_position(symbol)

    async def events(self, kind: str | None = None) -> list[dict]:
        day = trading_date(self.clock.now()).isoformat()
        events = await self.store.events(day)
        return [e for e in events if kind is None or e["kind"] == kind]

    def alert_keys(self) -> list[str]:
        return [key for key, _, _ in self.alerts.sent]
