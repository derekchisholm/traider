"""Research reads the bot's ledger and event log, in the bot's own namespace, and never
writes them."""

from datetime import UTC, datetime

import pytest
from moto import mock_aws

from tests.unit.test_state import TABLE as STATE_TABLE
from tests.unit.test_state import make_table as make_state_table
from traider import app
from traider.config import Config, ConfigError
from traider.research.botstate import BotState, held_symbols
from traider.research.wiring import SetupError, bot_state
from traider.state.base import LedgerEntry
from traider.state.dynamo import DynamoStateStore
from traider.state.memory import MemoryStateStore

T0 = datetime(2026, 10, 9, 14, 0, tzinfo=UTC)


def entry(symbol: str) -> LedgerEntry:
    return LedgerEntry(symbol, horizon="swing", side="long", opened_at=T0)


def test_both_state_stores_satisfy_the_read_protocol():
    memory: BotState = MemoryStateStore()
    dynamo: BotState = DynamoStateStore(None, "paper")
    assert memory is not None and dynamo is not None


async def test_held_symbols_are_the_ledgers_roots():
    state = MemoryStateStore()
    for symbol in ("NVDA", "AMD   261016P00048000", "PLTR"):
        await state.put_ledger(entry(symbol))
    assert await held_symbols(state) == {"NVDA", "AMD", "PLTR"}
    assert await held_symbols(MemoryStateStore()) == set()


def test_the_namespace_comes_from_the_environment_and_is_paper_or_live():
    env = {"TRAIDER_RESEARCH_TABLE": "r", "TRAIDER_STATE_NAMESPACE": "live"}
    assert Config.from_env(env).state_namespace == "live"
    assert Config.from_env({"TRAIDER_RESEARCH_TABLE": "r"}).state_namespace is None
    with pytest.raises(ConfigError, match="state_namespace"):
        Config.from_env({**env, "TRAIDER_STATE_NAMESPACE": "backtest"})


def test_without_a_state_table_there_is_nothing_to_read():
    assert bot_state(Config(research_table="r"), app.Aws("us-west-2")) is None


def test_a_state_table_without_a_namespace_is_refused():
    config = Config(research_table="r", state_table=STATE_TABLE)
    with pytest.raises(SetupError, match="TRAIDER_STATE_NAMESPACE"):
        bot_state(config, app.Aws("us-west-2"))


async def test_research_reads_the_namespace_it_is_told_and_only_that_one():
    with mock_aws():
        table = make_state_table()
        await DynamoStateStore(table, "live").put_ledger(entry("NVDA"))
        await DynamoStateStore(table, "paper").put_ledger(entry("AMD"))
        await DynamoStateStore(table, "live").log_event("order_submitted", {"symbol": "NVDA"}, T0)
        config = Config(
            research_table="r",
            state_table=STATE_TABLE,
            state_namespace="live",
            aws_region="us-west-2",
        )
        state = bot_state(config, app.Aws("us-west-2"))
        assert state is not None
        assert await held_symbols(state) == {"NVDA"}
        (event,) = await state.events("2026-10-09")
        assert (event["kind"], event["data"]) == ("order_submitted", {"symbol": "NVDA"})


def test_research_can_only_read_the_bots_state():
    methods = {name for name in vars(BotState) if not name.startswith("_")}
    assert methods == {"ledger", "events"}


def test_the_reader_research_gets_has_no_way_to_write():
    with mock_aws():
        make_state_table()
        config = Config(research_table="r", state_table=STATE_TABLE, state_namespace="paper")
        state = bot_state(config, app.Aws("us-west-2"))
        writes = {
            name
            for name in vars(DynamoStateStore)
            if not name.startswith("_") and name not in {"ledger", "events"}
        }
        assert writes >= {"put_ledger", "log_event", "save_order", "acquire_lease"}
        assert not any(hasattr(state, name) for name in writes)
