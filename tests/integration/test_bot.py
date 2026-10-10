"""The whole bot, wired the way production wires it, against a fake Schwab on localhost.

Time is a manual clock that the test runs fast, so minutes of bot behaviour take
well under a second.
"""

import asyncio
import contextlib
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import aiohttp
import boto3
import pytest
from moto import mock_aws

from tests.fakes.schwab_server import ACCOUNT_HASH, APP_KEY, APP_SECRET
from traider import app
from traider.config import Config
from traider.engine import Engine
from traider.schwab.oauth import REFRESH_TOKEN_LIFETIME_S
from traider.schwab.tokens import FileTokenStore, Grant
from traider.timeutil import ManualClock

START = datetime(2026, 10, 8, 15, 0, 20, tzinfo=UTC)  # Thursday 11:00:20 New York
MINUTE = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)
CONTROL = "/traider/test/control"
TABLE = "traider-test"


def candle(start: datetime, close: float) -> dict:
    return {
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 100,
        "datetime": int(start.timestamp() * 1000),
    }


async def until(predicate, timeout=5.0, what="condition"):
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} not reached in time")
        await asyncio.sleep(0.005)


class World:
    """A running bot plus the fake Schwab it talks to and a clock running fast."""

    def __init__(self, schwab, tmp_path, monkeypatch):
        self.schwab = schwab
        self.tmp_path = tmp_path
        self.clock = ManualClock(START)
        schwab.now = lambda: self.clock.now().timestamp()
        self.token_file = tmp_path / "token.json"
        self.bot: app.Bot | None = None
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self.price = {"SPY": (100.00, 100.02)}
        self._bar_minute = MINUTE
        # The engine's idle wake-up shrinks to match the fast clock.
        monkeypatch.setattr(Engine, "IDLE_TICK_S", 0.005)

    def sign_in(self) -> None:
        """What the sign-in Lambda does: store a grant for a fresh refresh token."""
        issued = int(self.clock.now().timestamp())
        grant = Grant(
            self.schwab.seed_refresh_token(), issued, issued + REFRESH_TOKEN_LIFETIME_S, "g"
        )
        FileTokenStore(self.token_file).save(grant)

    def config(self, **overrides) -> Config:
        fields = {
            "symbols": ("SPY",),
            "strategy": "sma_cross",
            "strategy_params": {"fast": 1, "slow": 2, "position_usd": 300},
            "schwab_app_key": APP_KEY,
            "schwab_app_secret": APP_SECRET,
            "schwab_token_file": str(self.token_file),
            "heartbeat_file": str(self.tmp_path / "heartbeat"),
            "risk": {"order_cooldown_s": 0},
        }
        fields.update(overrides)
        return Config.model_validate(fields)

    async def start(self, config: Config) -> None:
        self.http = aiohttp.ClientSession()
        self.bot = await app.build_bot(
            config,
            http=self.http,
            clock=self.clock,
            schwab_base_url=self.schwab.base_url,
            token_url=self.schwab.token_url,
            sleep=self._fast_sleep,
        )
        self._tasks = [
            asyncio.create_task(self.bot.run(self._stop)),
            asyncio.create_task(self._run_clock()),
            asyncio.create_task(self._publish_quotes()),
        ]

    # The clock gains 0.5 simulated seconds every 4 ms: 125 times real time.
    SPEED = 125.0

    async def _fast_sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds / self.SPEED)

    async def _run_clock(self) -> None:
        while True:
            await asyncio.sleep(0.004)
            self.clock.advance(0.5)

    async def ready(self, *, stream: bool = True) -> None:
        """Wait until the bot knows today's hours and (optionally) the stream is up."""
        await until(lambda: self.bot.session.session is not None, what="session hours")
        if stream:
            await until(self.bot.feed.stream_healthy, what="stream connection")

    async def _publish_quotes(self) -> None:
        """Keep quotes fresh on both the stream and the REST endpoint."""
        while True:
            for symbol, (bid, ask) in self.price.items():
                self.schwab.set_quote(symbol, bid, ask)
                await self.schwab.push_quote(symbol, bid, ask)
            await asyncio.sleep(0.01)

    async def bar(self, close: float, symbol="SPY") -> None:
        """Close one more minute at this price, on the stream and in price history."""
        self._bar_minute += timedelta(minutes=1)
        self.clock.set(max(self.clock.now(), self._bar_minute + timedelta(seconds=61)))
        self.price[symbol] = (close - 0.01, close + 0.01)
        # Quote first, then the bar, so the order that follows is priced off the new quote.
        self.schwab.set_quote(symbol, *self.price[symbol])
        await self.schwab.push_quote(symbol, *self.price[symbol])
        self.schwab.candles.setdefault(symbol, []).append(candle(self._bar_minute, close))
        await self.schwab.push_bar(
            symbol,
            close,
            close,
            close,
            close,
            100,
            start_ms=int(self._bar_minute.timestamp() * 1000),
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._tasks:
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(self._tasks[0], timeout=5)
            for task in self._tasks[1:]:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.bot is not None:
            await self.http.close()


@pytest.fixture
def aws():
    """Mocked AWS with the control parameter and state table the live bot needs.

    Every test in this module runs inside it, and it is set up before ``world`` so that
    it is still active while the bot shuts down. Nothing here can reach real AWS.
    """
    with mock_aws():
        boto3.client("ssm").put_parameter(Name=CONTROL, Value="live", Type="String")
        boto3.client("dynamodb").create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
        )
        yield


@pytest.fixture
async def world(aws, schwab, tmp_path, monkeypatch):
    built = World(schwab, tmp_path, monkeypatch)
    yield built
    await built.stop()


# --- paper trading on live data -----------------------------------------------------


async def test_paper_bot_buys_on_a_rising_market_and_sells_on_a_falling_one(world):
    world.sign_in()
    await world.start(world.config())
    await world.ready()
    paper = world.bot.broker

    await world.bar(100.0)
    await world.bar(101.0)  # the latest close is above the two-bar average: go long

    async def position():
        return (await paper.get_account()).position("SPY")

    async def holding(quantity):
        return await position() == quantity

    await until(lambda: holding(2), what="paper buy of 2 shares")  # 300 dollars at 101
    assert world.schwab.orders == {}  # paper mode never sends an order to Schwab

    await world.bar(99.0)  # below the average: go flat
    await until(lambda: holding(0), what="paper sell")


async def test_strategy_is_warmed_up_from_history_so_it_can_act_on_the_first_live_bar(world):
    world.sign_in()
    world.schwab.candles["SPY"] = [candle(MINUTE - timedelta(minutes=1), 100.0)]
    await world.start(world.config())
    await world.ready()
    await world.bar(101.0)  # one live bar plus one warm-up bar fills the two-bar window

    async def long():
        return (await world.bot.broker.get_account()).position("SPY") == 2

    await until(long, what="buy after a single live bar")


async def test_bot_falls_back_to_polling_when_the_stream_will_not_connect(world):
    world.sign_in()
    world.schwab.stream_login_code = 3
    await world.start(world.config(poll_interval_s=1))
    await world.ready(stream=False)
    await world.bar(100.0)
    await world.bar(101.0)

    async def long():
        return (await world.bot.broker.get_account()).position("SPY") == 2

    await until(long, what="buy using polled data")
    assert world.schwab.calls("GET", "/marketdata/v1/quotes")


# --- signing in -----------------------------------------------------------------------------


async def test_bot_started_before_any_sign_in_waits_then_trades_once_signed_in(world):
    await world.start(world.config(reauth_url="https://example.test/start?k=abc"))
    await until(
        lambda: any(key == "auth" for key, _, _ in world.bot.alerts.sent),
        what="sign-in alert",
    )
    (_, subject, message) = next(a for a in world.bot.alerts.sent if a[0] == "auth")
    assert "sign-in" in subject.lower()
    assert "https://example.test/start?k=abc" in message

    world.sign_in()  # the user taps the link and completes the Schwab login
    await world.ready()
    await world.bar(100.0)
    await world.bar(101.0)

    async def long():
        return (await world.bot.broker.get_account()).position("SPY") == 2

    await until(long, what="trade after sign-in")
    assert any("restored" in subject.lower() for _, subject, _ in world.bot.alerts.sent)


async def test_bot_keeps_running_when_the_sign_in_is_revoked_and_recovers_after_a_new_one(world):
    world.sign_in()
    await world.start(world.config())
    await until(lambda: world.schwab.stream_logins >= 1)

    world.schwab.revoke_refresh_tokens()
    world.schwab.expire_access_tokens()
    await world.schwab.drop_streams()
    await until(
        lambda: world.bot.tokens.state.value == "expired", what="bot noticing the lost sign-in"
    )

    world.sign_in()
    await until(lambda: world.bot.tokens.state.value == "ok", what="bot adopting the new sign-in")
    await until(lambda: world.schwab.stream_logins >= 2, what="stream reconnect")


# --- live trading ------------------------------------------------------------------------------


def live_config(world, **overrides) -> Config:
    return world.config(
        trading_mode="live",
        account_last4="5678",
        control_param=CONTROL,
        state_table=TABLE,
        aws_region="us-west-2",
        **overrides,
    )


async def test_live_bot_sends_a_real_order_to_schwab(world, aws):
    world.sign_in()
    await world.start(live_config(world))
    await world.ready()
    await world.bar(100.0)
    await world.bar(101.0)
    await until(lambda: world.schwab.positions.get("SPY", (0, 0))[0] == 2, what="live buy")

    (order,) = world.schwab.orders.values()
    assert order.account_hash == ACCOUNT_HASH
    assert order.body == {
        "orderType": "LIMIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "price": "101.06",  # ask 101.01 plus 5 bps, rounded down to the cent
        "orderLegCollection": [
            {
                "instruction": "BUY",
                "quantity": 2,
                "instrument": {"symbol": "SPY", "assetType": "EQUITY"},
            }
        ],
    }


async def test_live_order_count_and_audit_trail_are_stored_durably(world, aws):
    world.sign_in()
    await world.start(live_config(world))
    await world.ready()
    await world.bar(100.0)
    await world.bar(101.0)
    await until(lambda: world.schwab.positions.get("SPY", (0, 0))[0] == 2)

    table = boto3.resource("dynamodb").Table(TABLE)
    day = table.get_item(Key={"pk": "DAY#live#2026-10-08", "sk": "STATE"})["Item"]
    assert int(day["orders"]) == 1
    assert Decimal(day["start_equity"]) == Decimal("10000.0")

    async def logged():
        events = await world.bot.state.events("2026-10-08")
        return {"target", "order_submitted", "order_done"} <= {e["kind"] for e in events}

    await until(logged, what="audit events in DynamoDB")


async def test_live_bot_stays_idle_until_control_is_set_to_live(world, aws):
    boto3.client("ssm").put_parameter(Name=CONTROL, Value="paper", Type="String", Overwrite=True)
    world.sign_in()
    await world.start(live_config(world))
    await world.ready()
    await world.bar(100.0)
    await world.bar(101.0)
    await until(lambda: world.bot.engine.target("SPY") is not None, what="strategy target")
    await asyncio.sleep(0.2)  # many engine steps at fast-clock speed
    assert world.schwab.orders == {}

    boto3.client("ssm").put_parameter(Name=CONTROL, Value="live", Type="String", Overwrite=True)
    await until(lambda: world.schwab.positions.get("SPY", (0, 0))[0] == 2, what="buy once armed")


async def test_setting_control_to_halt_stops_live_trading_and_cancels_working_orders(world, aws):
    world.sign_in()
    world.schwab.fill_on_place = False  # orders rest at the broker
    await world.start(live_config(world, order_timeout_s=3600))
    await world.ready()
    await world.bar(100.0)
    await world.bar(101.0)
    await until(lambda: len(world.schwab.orders) == 1, what="resting live order")

    boto3.client("ssm").put_parameter(Name=CONTROL, Value="halt", Type="String", Overwrite=True)
    await until(
        lambda: next(iter(world.schwab.orders.values())).status == "CANCELED",
        what="cancel after halt",
    )
    await asyncio.sleep(0.2)
    assert len(world.schwab.orders) == 1  # and nothing new was sent


async def test_live_bot_refuses_to_guess_between_two_accounts(world, aws):
    from tests.fakes.schwab_server import SECOND_ACCOUNT_HASH

    world.schwab.accounts[SECOND_ACCOUNT_HASH] = "99995678"  # same last four digits
    world.sign_in()
    await world.start(live_config(world))
    await world.ready()
    await world.bar(100.0)
    await world.bar(101.0)
    await asyncio.sleep(0.3)
    assert world.schwab.orders == {}


async def test_stopping_the_bot_cancels_resting_orders_and_releases_the_lease(world, aws):
    world.sign_in()
    world.schwab.fill_on_place = False
    await world.start(live_config(world, order_timeout_s=3600))
    await world.ready()
    await world.bar(100.0)
    await world.bar(101.0)
    await until(lambda: len(world.schwab.orders) == 1)

    await world.stop()
    assert next(iter(world.schwab.orders.values())).status == "CANCELED"
    table = boto3.resource("dynamodb").Table(TABLE)
    assert "Item" not in table.get_item(Key={"pk": "LEASE#live", "sk": "bot"})


async def test_live_start_is_announced(world, aws):
    world.sign_in()
    await world.start(live_config(world))
    await until(lambda: any(key == "startup" for key, _, _ in world.bot.alerts.sent))
    (_, subject, _) = next(a for a in world.bot.alerts.sent if a[0] == "startup")
    assert "LIVE" in subject


def test_configuration_never_appears_in_the_startup_summary_with_secrets(world):
    summary = json.dumps(app.describe(world.config()))
    assert APP_SECRET not in summary
    assert APP_KEY not in summary


SETTINGS_TABLE = "traider-test-settings"


def create_settings_table() -> None:
    boto3.client("dynamodb").create_table(
        TableName=SETTINGS_TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
    )


def settings_store():
    from traider.settings_store import DynamoSettingsStore

    return DynamoSettingsStore(boto3.resource("dynamodb").Table(SETTINGS_TABLE))


async def test_bot_seeds_the_settings_table_and_starts_on_its_values(world, aws):
    create_settings_table()
    world.sign_in()
    config = world.config(settings_table=SETTINGS_TABLE)
    await world.start(config)
    latest = await settings_store().latest()
    assert latest is not None and latest.author == "bootstrap"
    assert world.bot.settings == latest.settings
    assert app.describe(config, world.bot.settings)["settings"] == SETTINGS_TABLE


async def test_bot_starts_on_the_stored_settings_and_gives_the_same_ones_to_the_engine(world, aws):
    from traider.settings import Settings

    create_settings_table()
    world.sign_in()
    config = world.config(settings_table=SETTINGS_TABLE)
    stored = Settings.from_config(config)
    stored = stored.model_copy(
        update={"risk": stored.risk.model_copy(update={"max_order_usd": Decimal(250)})}
    )
    await settings_store().write(stored, expected_version=0, author="test", note="", now=START)
    await world.start(config)
    assert world.bot.settings == stored
    assert world.bot.settings.risk.max_order_usd == Decimal(250)
    # The engine is on the same stored values, not the environment's 500.
    assert world.bot.engine.settings == stored


async def test_strategy_feed_and_engine_are_built_from_the_stored_settings(world, aws):
    from traider.settings import Settings

    create_settings_table()
    world.sign_in()
    config = world.config(settings_table=SETTINGS_TABLE)  # the environment says SPY, fast 1/slow 2
    stored = Settings.from_config(config).model_copy(
        update={
            "pinned_symbols": ("QQQ",),
            "strategy_params": {"fast": 2, "slow": 3, "position_usd": 300},
        }
    )
    await settings_store().write(stored, expected_version=0, author="test", note="", now=START)
    await world.start(config)
    bot = world.bot
    assert bot.feed._symbols == ("QQQ",)
    assert bot.strategy.symbols == ("QQQ",)
    assert (bot.strategy.fast, bot.strategy.slow) == (2, 3)
    assert bot.engine.settings.pinned_symbols == ("QQQ",)
    assert bot.settings == stored


async def test_bot_builds_and_starts_on_the_environment_when_the_settings_table_is_missing(
    world, aws
):
    world.sign_in()
    config = world.config(settings_table="no-such-table")
    await world.start(config)
    assert world.bot.settings.pinned_symbols == ("SPY",)
    assert world.bot.engine.settings == world.bot.settings
    assert world.bot.engine._live_settings is not None
    assert world.bot.engine._live_settings.loaded is False
    assert world.bot.settings_drift == []  # nothing loaded, so nothing to compare


async def write_differing_version(config) -> None:
    from traider.settings import Settings

    stored = Settings.from_config(config)
    stored = stored.model_copy(
        update={
            "risk": stored.risk.model_copy(update={"max_order_usd": Decimal(250)}),
            "order_timeout_s": 45.0,
        }
    )
    await settings_store().write(stored, expected_version=0, author="test", note="", now=START)


async def test_drift_is_logged_and_added_to_the_live_start_alert(world, aws, caplog):
    create_settings_table()
    world.sign_in()
    config = live_config(world, settings_table=SETTINGS_TABLE)
    await write_differing_version(config)
    with caplog.at_level("WARNING", logger="traider.app"):
        await world.start(config)
    assert world.bot.settings_drift == ["order_timeout_s", "risk.max_order_usd"]
    warning = next(r.getMessage() for r in caplog.records if "differ" in r.getMessage())
    assert "order_timeout_s, risk.max_order_usd" in warning
    assert "250" not in warning and "45" not in warning  # keys only, no values

    await until(lambda: any(key == "startup" for key, _, _ in world.bot.alerts.sent))
    (_, _, body) = next(a for a in world.bot.alerts.sent if a[0] == "startup")
    assert (
        "Settings table differs from the stack's settings in: order_timeout_s, "
        "risk.max_order_usd (the table is in force)"
    ) in body
    assert "250" not in body.split("differs", 1)[1] and "45" not in body.split("differs", 1)[1]
    assert all(key != "settings_drift" for key, _, _ in world.bot.alerts.sent)


async def test_paper_mode_gets_the_drift_in_the_log_only(world, aws, caplog):
    create_settings_table()
    world.sign_in()
    config = world.config(settings_table=SETTINGS_TABLE)
    await write_differing_version(config)
    with caplog.at_level("WARNING", logger="traider.app"):
        await world.start(config)
    assert world.bot.settings_drift == ["order_timeout_s", "risk.max_order_usd"]
    assert any("differ" in r.getMessage() for r in caplog.records)
    await asyncio.sleep(0.1)
    assert all(key not in ("startup", "settings_drift") for key, _, _ in world.bot.alerts.sent)


async def test_a_table_that_matches_the_stack_has_no_drift(world, aws):
    create_settings_table()
    world.sign_in()
    await world.start(world.config(settings_table=SETTINGS_TABLE))  # seeds from the stack
    assert world.bot.settings_drift == []


async def test_no_settings_table_means_no_drift_to_report(world, aws):
    world.sign_in()
    await world.start(world.config())
    assert world.bot.settings_drift == []


RESEARCH_TABLE = "traider-test-research"


def create_research_table() -> None:
    boto3.client("dynamodb").create_table(
        TableName=RESEARCH_TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "gsi1pk", "AttributeType": "S"},
            {"AttributeName": "gsi1sk", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "gsi1",
                "KeySchema": [
                    {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                    {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )


async def seed_spy_pick(world, *, posture: bool = True) -> None:
    """One ok run with a live SPY pick (and, unless told otherwise, a "trade" posture)."""
    from datetime import date

    from traider.research.models import Pick, Posture, RunMeta
    from traider.research.store import DynamoResearchStore

    create_research_table()
    store = DynamoResearchStore(boto3.resource("dynamodb").Table(RESEARCH_TABLE))
    now = world.clock.now()
    await store.write_run(
        RunMeta(
            run_id="r1",
            kind="manual",
            status="ok",
            started_at=now,
            finished_at=now,
            trading_day=date(2026, 10, 8),
        ),
        [
            Pick.model_validate(
                {
                    "run_id": "r1",
                    "rank": 1,
                    "symbol": "SPY",
                    "side": "long",
                    "horizon": "intraday",
                    "score": 90,
                    "pre_score": 90,
                    "thesis": "t",
                    "invalidation": "1",
                    "expires_at": (now + timedelta(hours=4)).isoformat(),
                }
            )
        ],
        Posture(level="trade", run_id="r1", at=now) if posture else None,
    )


async def test_bot_trades_a_researched_symbol_end_to_end(world, aws):
    await seed_spy_pick(world)
    world.sign_in()
    config = world.config(symbols=(), research_table=RESEARCH_TABLE)
    await world.start(config)
    await world.ready()
    assert world.bot.engine.universe == ("SPY",)
    assert world.bot.feed._symbols == ("SPY",)
    assert world.bot.strategy.symbols == ("SPY",)
    for close in (100, 101, 102, 103):
        await world.bar(close)

    async def long():
        return (await world.bot.broker.get_account()).position("SPY") > 0

    await until(long, what="a buy of the researched symbol")


async def test_a_pick_with_no_posture_today_is_watched_but_not_bought(world, aws):
    await seed_spy_pick(world, posture=False)
    world.sign_in()
    await world.start(world.config(symbols=(), research_table=RESEARCH_TABLE))
    await world.ready()
    assert world.bot.engine.universe == ("SPY",)
    for close in (100, 101, 102, 103):
        await world.bar(close)
    # The strategy has seen the bars and wants the position, and research has been read...
    await until(lambda: world.bot.engine.target("SPY") is not None, what="strategy target")
    polled = world.bot.engine._research_at
    await until(lambda: world.bot.engine._research_at != polled, what="another research poll")
    await asyncio.sleep(0.2)  # ...and many engine steps pass without a buy.
    assert (await world.bot.broker.get_account()).positions == {}


async def test_with_research_off_a_newly_pinned_symbol_reaches_the_feed(world, aws):
    from traider.settings import Settings

    create_settings_table()
    world.sign_in()
    config = world.config(settings_table=SETTINGS_TABLE)
    await world.start(config)
    await world.ready()
    assert world.bot.feed._symbols == ("SPY",)
    current = await settings_store().latest()
    pinned = current.settings.model_copy(update={"pinned_symbols": ("SPY", "QQQ")})
    assert isinstance(pinned, Settings)
    await settings_store().write(
        pinned, expected_version=current.version, author="test", note="", now=START
    )
    await until(lambda: "QQQ" in world.bot.feed._symbols, what="the feed following the pin")
    assert world.bot.engine.universe == ("SPY", "QQQ")


async def test_the_live_start_alert_lists_the_universe(world, aws):
    world.sign_in()
    await world.start(live_config(world))
    await until(lambda: any(key == "startup" for key, _, _ in world.bot.alerts.sent))
    (_, _, body) = next(a for a in world.bot.alerts.sent if a[0] == "startup")
    assert "Symbols: SPY\n" in body


async def test_the_live_start_alert_says_research_chooses_when_nothing_is_pinned(world, aws):
    create_research_table()
    world.sign_in()
    await world.start(live_config(world, symbols=(), research_table=RESEARCH_TABLE))
    await until(lambda: any(key == "startup" for key, _, _ in world.bot.alerts.sent))
    (_, _, body) = next(a for a in world.bot.alerts.sent if a[0] == "startup")
    assert "Symbols: chosen by research\n" in body


def test_the_startup_summary_says_whether_research_is_on(world):
    assert app.describe(world.config())["research"] == "off"
    on = world.config(symbols=(), research_table=RESEARCH_TABLE)
    assert app.describe(on)["research"] == RESEARCH_TABLE


async def test_a_stored_version_with_no_pinned_symbols_is_rejected_when_research_is_off(world, aws):
    from traider.settings import Settings

    create_settings_table()
    world.sign_in()
    config = world.config(settings_table=SETTINGS_TABLE)
    empty = Settings.from_config(config).model_copy(update={"pinned_symbols": ()})
    await settings_store().write(empty, expected_version=0, author="test", note="", now=START)
    await world.start(config)
    assert world.bot.settings.pinned_symbols == ("SPY",)
    assert world.bot.engine._live_settings.loaded is False
