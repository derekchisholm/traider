"""Wiring: build every part from the configuration and run them together.

Three things run side by side on one event loop:

* the **engine** (decisions and orders)
* the **feed** (market data in)
* the **auth watcher** (keeps the Schwab login fresh and tells you when to sign in)

If the feed or the watcher ever stops, the engine is shut down cleanly and the
process exits non-zero so the container is restarted.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import aiohttp
import boto3
from botocore.config import Config as BotoConfig

from traider.alerts import Alerter, LogAlerter, SnsAlerter
from traider.broker.base import Broker
from traider.broker.paper import PaperBroker
from traider.broker.schwab import SchwabBroker
from traider.config import Config
from traider.control import ControlSource, ControlState, SsmControl, StaticControl
from traider.engine import Engine
from traider.feed import Feed
from traider.marketdata import MarketData
from traider.risk import RiskManager
from traider.schwab.client import API_BASE, SchwabClient
from traider.schwab.hours import SchwabSessionProvider
from traider.schwab.oauth import TOKEN_URL, AppCredentials
from traider.schwab.tokens import (
    AuthState,
    CredentialsProvider,
    FileTokenStore,
    MemoryTokenStore,
    SecretsManagerCredentials,
    SecretsManagerTokenStore,
    StaticCredentials,
    TokenManager,
    TokenStore,
)
from traider.session import SessionTracker
from traider.settings import Settings, settings_diff
from traider.settings_store import DynamoSettingsStore, LiveSettings
from traider.state.base import StateStore
from traider.state.dynamo import DynamoStateStore
from traider.state.memory import MemoryStateStore
from traider.strategy import create_strategy
from traider.strategy.base import Strategy
from traider.timeutil import Clock, SystemClock

log = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]

AUTH_POLL_S = 20.0
AUTH_ERROR_ALERT_AFTER_S = 300.0

# AWS calls run in worker threads; keep a stuck one from stalling the loop for long.
_BOTO = BotoConfig(
    connect_timeout=3, read_timeout=5, retries={"max_attempts": 2, "mode": "standard"}
)


class Aws:
    """boto3 clients, created on first use. Nothing here touches AWS until asked."""

    def __init__(self, region: str | None) -> None:
        self._region = region
        self._clients: dict[str, Any] = {}

    def client(self, name: str) -> Any:
        if name not in self._clients:
            self._clients[name] = boto3.client(name, region_name=self._region, config=_BOTO)
        return self._clients[name]

    def table(self, name: str) -> Any:
        return boto3.resource("dynamodb", region_name=self._region, config=_BOTO).Table(name)


def token_store(config: Config, aws: Aws) -> TokenStore:
    if config.schwab_token_secret_id:
        return SecretsManagerTokenStore(config.schwab_token_secret_id, aws.client("secretsmanager"))
    if config.schwab_token_file:
        return FileTokenStore(config.schwab_token_file)
    return MemoryTokenStore()


def credentials(config: Config, aws: Aws) -> CredentialsProvider:
    if config.schwab_app_secret_id:
        return SecretsManagerCredentials(config.schwab_app_secret_id, aws.client("secretsmanager"))
    if config.schwab_app_key and config.schwab_app_secret:
        return StaticCredentials(AppCredentials(config.schwab_app_key, config.schwab_app_secret))
    return StaticCredentials(None)


def describe(config: Config, settings: Settings | None = None) -> dict[str, Any]:
    """What the bot is set up to do, safe to log: no keys, no secrets, no sign-in link."""
    s = settings if settings is not None else Settings.from_config(config)
    return {
        "trading_mode": config.trading_mode,
        "symbols": list(s.pinned_symbols),
        "strategy": s.strategy,
        "strategy_params": s.strategy_params,
        "order_type": s.order_type,
        "limit_offset_bps": str(s.limit_offset_bps),
        "order_timeout_s": s.order_timeout_s,
        "flatten_before_close_min": s.flatten_before_close_min,
        "cancel_unknown_orders": s.cancel_unknown_orders,
        "feed": config.feed,
        "account": f"...{config.account_last4}" if config.account_last4 else "by hash or only one",
        "risk": {name: str(value) for name, value in s.risk.model_dump().items()},
        "control": config.control_param or f"static:{config.control}",
        "state": config.state_table or "memory",
        "settings": config.settings_table or "environment",
        "alerts": "sns" if config.alert_topic_arn else "log only",
        "token_store": (
            "secrets manager"
            if config.schwab_token_secret_id
            else "file"
            if config.schwab_token_file
            else "none"
        ),
    }


@dataclass(slots=True)
class Bot:
    config: Config
    settings: Settings
    clock: Clock
    tokens: TokenManager
    client: SchwabClient
    market: MarketData
    session: SessionTracker
    strategy: Strategy
    broker: Broker
    state: StateStore
    alerts: Alerter
    engine: Engine
    feed: Feed
    sleep: Sleep
    #: Settings (dotted names, no values) where the stack's environment and the loaded
    #: settings table disagree. The table wins; this only exists to say so.
    settings_drift: list[str] = field(default_factory=list)

    async def run(self, stop: asyncio.Event) -> None:
        """Run until ``stop`` is set. Raises if a background task dies on its own."""
        await self._announce()
        engine_task = asyncio.create_task(self.engine.run(stop), name="engine")
        side_tasks = [
            asyncio.create_task(self.feed.run(warmup_bars=self.strategy.warmup_bars), name="feed"),
            asyncio.create_task(self._watch_auth(), name="auth"),
        ]
        crashed: list[str] = []
        try:
            done, _ = await asyncio.wait(
                [engine_task, *side_tasks], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task is not engine_task:
                    crashed.append(task.get_name())
                    log.error("background task %s stopped: %r", task.get_name(), task.exception())
            if not engine_task.done():
                stop.set()  # let the engine cancel its orders and hand back the lease
            await engine_task
        finally:
            for task in side_tasks:
                task.cancel()
            await asyncio.gather(*side_tasks, return_exceptions=True)
        if crashed:
            raise RuntimeError(f"background task stopped: {', '.join(crashed)}")

    async def _announce(self) -> None:
        summary = describe(self.config, self.settings)
        log.info("starting: %s", summary)
        if self.config.trading_mode == "live":
            body = (
                "Real orders go out once the control switch is set to live.\n"
                f"Symbols: {', '.join(self.settings.pinned_symbols)}\n"
                f"Strategy: {self.settings.strategy} {self.settings.strategy_params}\n"
                f"Limits: {summary['risk']}"
            )
            if self.settings_drift:
                body += (
                    "\nSettings table differs from the stack's settings in: "
                    f"{', '.join(self.settings_drift)} (the table is in force)"
                )
            await self.alerts.send("startup", "traider started in LIVE mode", body)

    async def _watch_auth(self) -> None:
        """Keep the access token warm and report changes in the sign-in state."""
        previous: AuthState | None = None
        error_since: datetime | None = None
        while True:
            await self.tokens.poll()
            state = self.tokens.state
            now = self.clock.now()
            if state is not previous:
                await self._auth_changed(previous, state)
                previous = state
            if state is AuthState.ERROR:
                error_since = error_since or now
                if (now - error_since).total_seconds() >= AUTH_ERROR_ALERT_AFTER_S:
                    await self.alerts.send(
                        "auth_error", "Cannot reach Schwab sign-in", self.tokens.describe()
                    )
            else:
                error_since = None
            await self.sleep(AUTH_POLL_S)

    async def _auth_changed(self, previous: AuthState | None, state: AuthState) -> None:
        link = self.config.reauth_url or "(no sign-in link configured)"
        if state is AuthState.OK:
            if previous not in (None, AuthState.OK, AuthState.ERROR):
                days = self.tokens.seconds_left() / 86400
                await self.alerts.send(
                    "auth_ok",
                    "Schwab sign-in restored",
                    f"The bot is connected to Schwab again. Sign-in lasts {days:.1f} more days.",
                )
        elif state in (AuthState.NO_GRANT, AuthState.EXPIRED):
            await self.alerts.send(
                "auth",
                "Schwab sign-in needed",
                f"{self.tokens.describe()}\n\nThe bot cannot trade or read market data until "
                f"you sign in:\n{link}",
            )
        elif state is AuthState.NO_CREDENTIALS:
            await self.alerts.send(
                "auth",
                "Schwab app key and secret not set",
                "Store the app key and secret from the Schwab developer portal in the "
                "app-credentials secret, then sign in.",
            )


async def build_bot(
    config: Config,
    *,
    http: aiohttp.ClientSession,
    clock: Clock | None = None,
    schwab_base_url: str = API_BASE,
    token_url: str = TOKEN_URL,
    sleep: Sleep = asyncio.sleep,
) -> Bot:
    clock = clock or SystemClock()
    aws = Aws(config.aws_region)
    tokens = TokenManager(
        store=token_store(config, aws),
        credentials=credentials(config, aws),
        clock=clock,
        token_url=token_url,
    )
    client = SchwabClient(http, tokens, base_url=schwab_base_url)
    market = MarketData()
    session = SessionTracker(SchwabSessionProvider(client))
    settings = Settings.from_config(config)
    live_settings: LiveSettings | None = None
    drift: list[str] = []
    if config.settings_table:
        live_settings = LiveSettings(
            DynamoSettingsStore(aws.table(config.settings_table)), settings
        )
        for update in await live_settings.start(clock.now()):
            log.warning("settings at start-up: %s %s", update.kind, update.detail)
        if live_settings.loaded:
            drift = list(settings_diff(settings, live_settings.current))
            if drift:
                log.warning(
                    "stack settings differ from the settings table in: %s (the table wins)",
                    ", ".join(drift),
                )
        settings = live_settings.current
    strategy = create_strategy(settings.strategy, settings.pinned_symbols, settings.strategy_params)

    state: StateStore
    if config.state_table:
        state = DynamoStateStore(aws.table(config.state_table), config.trading_mode)
    else:
        state = MemoryStateStore(config.trading_mode)

    source: ControlSource
    if config.control_param:
        source = SsmControl(config.control_param, aws.client("ssm"))
    else:
        source = StaticControl(config.control)

    alerts: Alerter
    if config.alert_topic_arn:
        alerts = SnsAlerter(config.alert_topic_arn, aws.client("sns"), clock)
    else:
        alerts = LogAlerter()

    broker: Broker
    if config.trading_mode == "live":
        broker = SchwabBroker(
            client, clock, account_hash=config.account_hash, account_last4=config.account_last4
        )
    else:
        paper = PaperBroker(market, clock, starting_cash=config.paper_starting_cash, store=state)
        await paper.load()
        broker = paper

    engine = Engine(
        config=config,
        clock=clock,
        market=market,
        strategy=strategy,
        risk=RiskManager(settings.risk),
        broker=broker,
        state=state,
        control=ControlState(source),
        session=session,
        alerts=alerts,
        instance_id=f"{socket.gethostname()}-{os.getpid()}",
        auth_seconds_left=tokens.seconds_left,
        settings=live_settings,
    )
    feed = Feed(
        config=config,
        http=http,
        client=client,
        tokens=tokens,
        market=market,
        clock=clock,
        session=session,
        settings=settings,
        sleep=sleep,
    )
    return Bot(
        config=config,
        settings=settings,
        clock=clock,
        tokens=tokens,
        client=client,
        market=market,
        session=session,
        strategy=strategy,
        broker=broker,
        state=state,
        alerts=alerts,
        engine=engine,
        feed=feed,
        sleep=sleep,
        settings_drift=drift,
    )


async def run(config: Config) -> None:
    """Run the bot until SIGTERM or SIGINT."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    async with aiohttp.ClientSession() as http:
        bot = await build_bot(config, http=http)
        await bot.run(stop)
