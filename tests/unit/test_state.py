"""One contract, two implementations: in-memory and DynamoDB (through moto)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from traider.models import OrderRecord, OrderStatus, OrderType, Side
from traider.state.base import LedgerEntry
from traider.state.dynamo import DynamoStateStore
from traider.state.memory import MemoryStateStore

T0 = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)
DAY = "2026-10-08"
TABLE = "traider-test"


def make_table():
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
    return boto3.resource("dynamodb").Table(TABLE)


@pytest.fixture(params=["memory", "dynamo"])
def make_store(request):
    """Returns a factory so a test can open several stores on the same backing data."""
    if request.param == "memory":
        shared: dict = {}
        yield lambda namespace="paper": MemoryStateStore(namespace, shared)
    else:
        with mock_aws():
            table = make_table()
            yield lambda namespace="paper": DynamoStateStore(table, namespace)


@pytest.fixture
def store(make_store):
    return make_store()


def record(order_id="1001", **overrides) -> OrderRecord:
    fields = {
        "order_id": order_id,
        "symbol": "SPY",
        "side": Side.BUY,
        "quantity": 3,
        "order_type": OrderType.LIMIT,
        "limit_price": Decimal("512.34"),
        "submitted_at": T0,
        "position_before": 0,
        "reason": "fast above slow",
    }
    fields.update(overrides)
    return OrderRecord(**fields)


# --- lease: only one bot instance may trade -----------------------------------


async def test_first_instance_gets_the_lease(store):
    assert await store.acquire_lease("a", 30, T0) is True


async def test_second_instance_is_refused_while_the_lease_is_held(store):
    await store.acquire_lease("a", 30, T0)
    assert await store.acquire_lease("b", 30, T0 + timedelta(seconds=10)) is False


async def test_holder_can_renew_its_own_lease(store):
    await store.acquire_lease("a", 30, T0)
    assert await store.acquire_lease("a", 30, T0 + timedelta(seconds=10)) is True


async def test_renewal_extends_the_lease(store):
    await store.acquire_lease("a", 30, T0)
    await store.acquire_lease("a", 30, T0 + timedelta(seconds=25))
    # 31s after the first acquire, but only 6s after the renewal.
    assert await store.acquire_lease("b", 30, T0 + timedelta(seconds=31)) is False


async def test_expired_lease_can_be_taken_over(store):
    await store.acquire_lease("a", 30, T0)
    assert await store.acquire_lease("b", 30, T0 + timedelta(seconds=31)) is True


async def test_old_holder_cannot_renew_after_a_takeover(store):
    await store.acquire_lease("a", 30, T0)
    await store.acquire_lease("b", 30, T0 + timedelta(seconds=31))
    assert await store.acquire_lease("a", 30, T0 + timedelta(seconds=32)) is False


async def test_released_lease_is_immediately_available(store):
    await store.acquire_lease("a", 30, T0)
    await store.release_lease("a")
    assert await store.acquire_lease("b", 30, T0 + timedelta(seconds=1)) is True


async def test_release_by_a_non_holder_changes_nothing(store):
    await store.acquire_lease("a", 30, T0)
    await store.release_lease("b")
    assert await store.acquire_lease("b", 30, T0 + timedelta(seconds=1)) is False


# --- per-day counters ---------------------------------------------------------


async def test_a_new_day_starts_empty(store):
    day = await store.get_day(DAY)
    assert (day.start_equity, day.orders, day.halted_reason) == (None, 0, None)
    assert day.sold_usd == Decimal(0)


async def test_start_equity_is_recorded_once_and_then_kept(store):
    assert await store.init_start_equity(DAY, Decimal("10000.25")) == Decimal("10000.25")
    assert await store.init_start_equity(DAY, Decimal("9000")) == Decimal("10000.25")
    assert (await store.get_day(DAY)).start_equity == Decimal("10000.25")


async def test_order_counter_increments_and_persists(store):
    assert await store.incr_orders(DAY) == 1
    assert await store.incr_orders(DAY) == 2
    assert (await store.get_day(DAY)).orders == 2


async def test_day_halt_is_recorded_and_the_first_reason_wins(store):
    await store.halt_day(DAY, "down 120.00")
    await store.halt_day(DAY, "something else")
    assert (await store.get_day(DAY)).halted_reason == "down 120.00"


async def test_sale_proceeds_add_up_to_the_cent_and_persist(store):
    assert await store.add_sold(DAY, Decimal("410.53")) == Decimal("410.53")
    assert await store.add_sold(DAY, Decimal("99.4725")) == Decimal("510.0025")
    assert (await store.get_day(DAY)).sold_usd == Decimal("510.0025")


async def test_sale_proceeds_do_not_disturb_the_other_counters(store):
    await store.incr_orders(DAY)
    await store.init_start_equity(DAY, Decimal("5000"))
    await store.add_sold(DAY, Decimal("12.50"))
    day = await store.get_day(DAY)
    assert (day.orders, day.start_equity, day.sold_usd) == (1, Decimal("5000"), Decimal("12.50"))


async def test_days_do_not_share_counters(store):
    await store.incr_orders(DAY)
    await store.halt_day(DAY, "down 120.00")
    await store.add_sold(DAY, Decimal("300"))
    tomorrow = await store.get_day("2026-10-09")
    assert (tomorrow.orders, tomorrow.halted_reason, tomorrow.sold_usd) == (0, None, Decimal(0))


async def test_counters_survive_a_restart(make_store):
    first = make_store()
    await first.incr_orders(DAY)
    await first.init_start_equity(DAY, Decimal("5000"))
    await first.add_sold(DAY, Decimal("250.75"))
    restarted = make_store()
    day = await restarted.get_day(DAY)
    assert (day.orders, day.start_equity, day.sold_usd) == (1, Decimal("5000"), Decimal("250.75"))


# --- the bot's own open orders ---------------------------------------------------


async def test_open_order_round_trips_with_every_field(store):
    original = record(cancel_requested=True, filled_quantity=1, avg_fill_price=Decimal("512.30"))
    await store.save_order(original)
    assert await store.open_orders() == [original]


async def test_market_order_without_a_limit_price_round_trips(store):
    original = record(order_type=OrderType.MARKET, limit_price=None)
    await store.save_order(original)
    assert await store.open_orders() == [original]


async def test_saving_again_updates_the_open_order(store):
    await store.save_order(record())
    await store.save_order(record(filled_quantity=2))
    (found,) = await store.open_orders()
    assert found.filled_quantity == 2


@pytest.mark.parametrize(
    "final", [OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED]
)
async def test_finished_orders_leave_the_open_list(store, final):
    await store.save_order(record())
    await store.save_order(record(status=final))
    assert await store.open_orders() == []


async def test_open_orders_survive_a_restart(make_store):
    await make_store().save_order(record("7"))
    assert [r.order_id for r in await make_store().open_orders()] == ["7"]


# --- audit log ---------------------------------------------------------------------


async def test_events_are_kept_in_order_with_their_data(store):
    await store.log_event("order_submitted", {"symbol": "SPY", "price": Decimal("512.34")}, T0)
    await store.log_event("order_filled", {"symbol": "SPY"}, T0 + timedelta(seconds=2))
    events = await store.events(DAY)
    assert [e["kind"] for e in events] == ["order_submitted", "order_filled"]
    assert events[0]["data"] == {"symbol": "SPY", "price": "512.34"}


async def test_events_are_filed_under_the_new_york_trading_day(store):
    late_evening_utc = datetime(2026, 10, 9, 1, 0, tzinfo=UTC)  # still Oct 8 in New York
    await store.log_event("note", {}, late_evening_utc)
    assert len(await store.events(DAY)) == 1


# --- paper account -----------------------------------------------------------------


async def test_paper_account_is_absent_until_saved(store):
    assert await store.load_paper() is None


async def test_paper_account_round_trips(store):
    data = {"cash": "9500.00", "positions": {"SPY": {"quantity": 1, "avg_price": "500.00"}}}
    await store.save_paper(data)
    assert await store.load_paper() == data


# --- paper and live never share state --------------------------------------------------


async def test_namespaces_are_isolated(make_store):
    paper, live = make_store("paper"), make_store("live")
    await paper.incr_orders(DAY)
    await paper.save_order(record())
    await paper.save_paper({"cash": "1"})
    await paper.halt_day(DAY, "paper halt")
    await paper.add_sold(DAY, Decimal("75"))
    live_day = await live.get_day(DAY)
    assert (live_day.orders, live_day.halted_reason, live_day.sold_usd) == (0, None, Decimal(0))
    assert await live.open_orders() == []
    assert await live.load_paper() is None


# --- position ledger: the positions the bot opened itself ---------------------------


def entry(symbol="NVDA", **overrides) -> LedgerEntry:
    fields = {"symbol": symbol, "horizon": "intraday", "side": "long", "opened_at": T0}
    return LedgerEntry(**(fields | overrides))


async def test_the_ledger_starts_empty(store):
    assert await store.ledger() == {}


async def test_ledger_entries_are_kept_replaced_and_deleted(store):
    await store.put_ledger(entry("NVDA", pick_run_id="r1", pick_rank=2))
    await store.put_ledger(entry("AMD", horizon="swing"))
    await store.put_ledger(entry("NVDA", horizon="swing"))  # same symbol: replaced
    ledger = await store.ledger()
    assert set(ledger) == {"NVDA", "AMD"}
    assert ledger["NVDA"].horizon == "swing"
    assert ledger["AMD"] == entry("AMD", horizon="swing")
    await store.delete_ledger("NVDA")
    await store.delete_ledger("MISSING")  # deleting nothing is fine
    assert set(await store.ledger()) == {"AMD"}


async def test_the_ledger_survives_a_restart_and_keeps_modes_apart(make_store):
    await make_store("paper").put_ledger(entry())
    assert set(await make_store("paper").ledger()) == {"NVDA"}
    assert await make_store("live").ledger() == {}


async def test_pick_provenance_survives_a_round_trip(store):
    original = entry("NVDA", pick_run_id="run-42", pick_rank=3)
    await store.put_ledger(original)
    assert (await store.ledger())["NVDA"] == original
