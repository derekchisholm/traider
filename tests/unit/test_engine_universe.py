"""The engine's universe follows the pinned symbols live and never drops a held one."""

from decimal import Decimal

from tests.unit.engine_harness import Harness
from traider.models import OrderRequest, OrderType, Side, Target
from traider.settings_store import MemorySettingsStore

QQQ_CALL = "QQQ   261016C00400000"  # eight days out from the bench's clock


async def write_pinned(h: Harness, store, symbols) -> None:
    current = await store.latest()
    await store.write(
        current.settings.model_copy(update={"pinned_symbols": tuple(symbols)}),
        expected_version=current.version,
        author="test",
        note="",
        now=h.clock.now(),
    )


async def pin(h: Harness, store, symbols) -> None:
    await write_pinned(h, store, symbols)
    await h.tick(11)


async def ask_for_on(h: Harness, underlying: str, symbol: str, quantity: int) -> None:
    """The strategy asks for an option position while hearing a bar of the underlying."""
    h.strategy.bar_targets.append(Target(symbol, quantity, "test"))
    h.bar(underlying)
    await h.engine.step()


async def test_pinning_a_symbol_adds_it_live(tmp_path):
    store = MemorySettingsStore()
    h = await Harness.create(tmp_path, settings_store=store)
    await pin(h, store, ("SPY", "QQQ"))
    assert h.engine.universe == ("SPY", "QQQ")
    assert h.universe_calls[-1] == ("SPY", "QQQ")
    assert h.strategy.symbols == ("SPY", "QQQ")
    changed = await h.events("universe_changed")
    assert changed[-1]["data"]["added"] == ["QQQ"]
    h.price("QQQ", "50.00", "50.02")
    await h.target("QQQ", 2)
    await h.settle()
    assert h.position("QQQ") == 2


async def test_unpinning_a_held_symbol_keeps_it_until_it_is_sold(tmp_path):
    store = MemorySettingsStore()
    h = await Harness.create(tmp_path, symbols=("SPY", "QQQ"), settings_store=store)
    await h.target("QQQ", 2)
    await h.settle()
    await pin(h, store, ("SPY",))
    assert "QQQ" in h.engine.universe  # still held
    await h.target("QQQ", 0)
    await h.settle()
    await h.tick(31)  # next account snapshot
    assert h.engine.universe == ("SPY",)
    assert (await h.events("universe_changed"))[-1]["data"]["dropped"] == ["QQQ"]


async def test_a_strategy_that_raises_on_universe_stops_entries(tmp_path):
    store = MemorySettingsStore()
    h = await Harness.create(tmp_path, settings_store=store)

    def boom(symbols):
        raise RuntimeError("bad")

    h.strategy.on_universe = boom
    await pin(h, store, ("SPY", "QQQ"))
    assert "strategy_error" in h.alert_keys()
    h.price("QQQ", "50.00", "50.02")
    await h.target("QQQ", 2)
    await h.settle()
    assert h.position("QQQ") == 0
    blocked = await h.events("order_blocked")
    assert "entries_halted" in blocked[-1]["data"]["codes"]


async def test_unpinning_before_the_first_account_read_keeps_a_held_symbol(tmp_path):
    store = MemorySettingsStore()
    old = await Harness.create(tmp_path, symbols=("SPY", "QQQ"), settings_store=store)
    await old.target("QQQ", 2)
    await old.settle()
    # A new process starts on the settings that pin QQQ, and a version that unpins it
    # arrives before the process has read the account even once.
    h = await Harness.create(
        tmp_path,
        symbols=("SPY", "QQQ"),
        settings_store=store,
        restart_of=old,
        begin=False,
    )
    await write_pinned(h, store, ("SPY",))
    await h.engine.start()
    await h.engine.step()
    await h.tick(31)
    assert "QQQ" in h.engine.universe  # held, so still managed
    await h.target("QQQ", 0)
    await h.settle()
    assert h.position("QQQ") == 0  # and the exit works


async def test_a_held_option_keeps_its_unpinned_underlying_until_it_is_sold(tmp_path):
    store = MemorySettingsStore()
    h = await Harness.create(
        tmp_path,
        symbols=("SPY", "QQQ"),
        settings_store=store,
        risk={"allow_options": True},
    )
    h.price(QQQ_CALL, "2.00", "2.10")
    await ask_for_on(h, "QQQ", QQQ_CALL, 1)
    await h.settle()
    assert h.position(QQQ_CALL) == 1
    await pin(h, store, ("SPY",))
    assert "QQQ" in h.engine.universe
    assert QQQ_CALL in h.market.watched()
    await ask_for_on(h, "QQQ", QQQ_CALL, 0)
    await h.settle()
    assert h.position(QQQ_CALL) == 0
    await h.tick(31)
    assert h.engine.universe == ("SPY",)
    assert h.market.watched() == ()
    assert h.engine.target(QQQ_CALL) is None


async def test_options_on_a_newly_pinned_symbol_can_be_traded(tmp_path):
    store = MemorySettingsStore()
    h = await Harness.create(tmp_path, settings_store=store, risk={"allow_options": True})
    h.price("QQQ", "50.00", "50.02")
    h.price(QQQ_CALL, "2.00", "2.10")
    await ask_for_on(h, "QQQ", QQQ_CALL, 1)
    await h.settle()
    assert h.position(QQQ_CALL) == 0  # QQQ is not the bot's yet
    await pin(h, store, ("SPY", "QQQ"))
    await ask_for_on(h, "QQQ", QQQ_CALL, 1)
    await h.settle()
    assert h.position(QQQ_CALL) == 1


async def test_without_a_store_the_universe_is_the_pinned_symbols_and_never_changes(tmp_path):
    h = await Harness.create(tmp_path, symbols=("SPY", "QQQ"))
    h.price("QQQ", "50.00", "50.02")
    await h.target("QQQ", 2)
    await h.settle()
    await h.tick(31)
    await h.target("QQQ", 0)
    await h.settle()
    await h.tick(31)
    assert h.engine.universe == ("SPY", "QQQ")
    assert h.universe_calls == []
    assert await h.events("universe_changed") == []


async def test_a_symbol_dropped_while_it_is_being_traded_gets_no_order(tmp_path):
    """The pre-trade account read can drop a symbol mid-pass. Its old state is gone from
    the engine then, so no order may go out on it."""
    h = await Harness.create(tmp_path, symbols=("SPY", "QQQ"))
    h.price("QQQ", "50.00", "50.02")
    # As if a version unpinning QQQ had applied without a universe refresh yet; the
    # pre-trade account read below is the next refresh.
    h.engine._settings = h.engine._settings.model_copy(update={"pinned_symbols": ("SPY",)})
    await h.target("QQQ", 2)
    await h.settle()
    assert h.broker.placed == []
    assert h.engine.universe == ("SPY",)


async def test_unpinning_a_symbol_with_an_order_working_keeps_it(tmp_path):
    store = MemorySettingsStore()
    h = await Harness.create(tmp_path, symbols=("SPY", "QQQ"), settings_store=store)
    h.broker.hold_fills = True
    await h.target("QQQ", 2)  # the buy rests at the broker
    await pin(h, store, ("SPY",))
    assert "QQQ" in h.engine.universe  # not held yet, but an order is working
    h.broker.hold_fills = False
    await h.settle(40)
    assert h.position("QQQ") == 2
    assert "QQQ" in h.engine.universe  # held now


def alerts_for(h: Harness, prefix: str) -> list[tuple[str, str, str]]:
    return [alert for alert in h.alerts.sent if alert[0].startswith(prefix)]


async def test_unpinning_a_held_symbol_says_it_is_managed_only_until_a_restart(tmp_path):
    store = MemorySettingsStore()
    h = await Harness.create(tmp_path, symbols=("SPY", "QQQ"), settings_store=store)
    await h.target("QQQ", 2)
    await h.settle()
    await pin(h, store, ("SPY",))
    [(key, subject, body)] = alerts_for(h, "unpinned_but_held:")
    assert key == "unpinned_but_held:2"
    assert subject == "Unpinned symbols are still held"
    assert "QQQ" in body
    assert "until they are flat or until the next restart" in body
    assert "sell them or keep them pinned" in body


async def test_unpinning_a_symbol_that_is_not_held_raises_no_held_alert(tmp_path):
    store = MemorySettingsStore()
    h = await Harness.create(tmp_path, symbols=("SPY", "QQQ"), settings_store=store)
    await pin(h, store, ("SPY",))
    assert alerts_for(h, "unpinned_but_held:") == []


async def test_a_holding_outside_the_universe_is_reported_once_after_a_restart(tmp_path):
    old = await Harness.create(tmp_path, symbols=("SPY", "QQQ"))
    await old.target("QQQ", 2)
    await old.settle()
    h = await Harness.create(tmp_path, symbols=("SPY",), restart_of=old)
    await h.run_for(65)  # a few account snapshots
    [(key, subject, body)] = alerts_for(h, "unmanaged_holding:")
    assert key == "unmanaged_holding:QQQ"
    assert subject == "QQQ is held but not managed"
    assert "not pinned" in body
    assert "expiry" not in body  # only options expire
    events = await h.events("unmanaged_holding")
    assert [e["data"] for e in events] == [{"symbol": "QQQ", "quantity": 2}]


async def test_a_failed_universe_callback_is_retried_until_it_goes_through(tmp_path):
    store = MemorySettingsStore()
    calls: list[tuple[str, ...]] = []
    failures = [RuntimeError("feed not ready")]

    def callback(symbols):
        calls.append(symbols)
        if failures:
            raise failures.pop()

    h = await Harness.create(tmp_path, settings_store=store, on_universe=callback)
    await pin(h, store, ("SPY", "QQQ"))  # the error does not escape the step
    assert calls == [("SPY", "QQQ")]
    await h.tick(31)  # next account snapshot: same set, but the callback is owed
    assert calls == [("SPY", "QQQ"), ("SPY", "QQQ")]
    await h.tick(31)
    assert len(calls) == 2  # delivered: not repeated


async def test_an_unmanaged_option_is_reported_with_no_expiry_cover(tmp_path):
    h = await Harness.create(tmp_path, risk={"allow_options": True})
    h.price(QQQ_CALL, "2.00", "2.10")
    await h.broker.place(OrderRequest(QQQ_CALL, Side.BUY, 1, OrderType.LIMIT, Decimal("2.10")))
    await h.run_for(31)
    [(_, _, body)] = alerts_for(h, f"unmanaged_holding:{QQQ_CALL}")
    assert "no expiry alerts" in body
    assert "will not sell it before it expires" in body
