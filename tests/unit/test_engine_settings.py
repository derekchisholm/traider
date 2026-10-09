"""The engine follows the settings store: new limits apply within seconds, the rest waits."""

from decimal import Decimal

from tests.unit.engine_harness import Harness
from traider.engine import Engine
from traider.models import AccountSnapshot, Position
from traider.settings import Settings
from traider.settings_store import MemorySettingsStore


class FlakyStore(MemorySettingsStore):
    def __init__(self) -> None:
        super().__init__()
        self.error: Exception | None = None

    async def latest(self):
        if self.error is not None:
            raise self.error
        return await super().latest()


async def write(h: Harness, store, **risk) -> None:
    current = await store.latest()
    new = current.settings.model_copy(
        update={"risk": current.settings.risk.model_copy(update=risk)}
    )
    await store.write(
        new, expected_version=current.version, author="test", note="", now=h.clock.now()
    )


async def test_without_a_store_the_engine_uses_the_configuration(tmp_path):
    h = await Harness.create(tmp_path)
    assert h.engine.settings == Settings.from_config(h.config)


async def test_a_new_risk_limit_applies_within_one_refresh(tmp_path):
    store = MemorySettingsStore()
    h = await Harness.create(tmp_path, settings_store=store)
    await write(h, store, max_order_usd=Decimal(150))
    await h.tick(11)
    assert h.engine.settings.risk.max_order_usd == Decimal(150)
    await h.target("SPY", 5)  # 5 x ~100 = 500, over the new 150 cap
    await h.settle()
    assert h.position("SPY") == 0
    blocked = await h.events("order_blocked")
    assert "max_order_usd" in blocked[-1]["data"]["codes"]
    applied = await h.events("settings_applied")
    assert applied[-1]["data"]["version"] == 2
    assert applied[-1]["data"]["diff"] == {"risk.max_order_usd": ["1000", "150"]}
    assert "settings_applied:2" in h.alert_keys()


async def test_a_restart_only_change_waits_and_is_announced(tmp_path):
    store = MemorySettingsStore()
    h = await Harness.create(tmp_path, settings_store=store)
    current = await store.latest()
    await store.write(
        current.settings.model_copy(update={"pinned_symbols": ("QQQ",)}),
        expected_version=1,
        author="test",
        note="",
        now=h.clock.now(),
    )
    await h.tick(11)
    assert h.engine.settings.pinned_symbols == ("SPY",)
    pending = await h.events("settings_pending_restart")
    assert pending[-1]["data"] == {"version": 2, "fields": "pinned_symbols"}
    assert "settings_restart:2" in h.alert_keys()


def restart_body(h: Harness) -> str:
    return next(body for key, _, body in h.alerts.sent if key.startswith("settings_restart:"))


async def write_pinned(h: Harness, store, symbols: tuple[str, ...]) -> None:
    current = await store.latest()
    await store.write(
        current.settings.model_copy(update={"pinned_symbols": symbols}),
        expected_version=current.version,
        author="test",
        note="",
        now=h.clock.now(),
    )


async def test_dropping_a_pinned_symbol_with_a_position_warns_before_the_restart(tmp_path):
    store = MemorySettingsStore()
    h = await Harness.create(tmp_path, settings_store=store)
    await h.target("SPY", 2)
    await h.settle()
    await write_pinned(h, store, ("QQQ",))
    await h.tick(11)
    body = restart_body(h)
    assert "After the restart the bot will no longer manage: SPY." in body
    assert "Sell them first or keep them pinned." in body


async def test_dropping_a_pinned_symbol_names_options_on_it_too(tmp_path):
    store = MemorySettingsStore()
    h = await Harness.create(tmp_path, settings_store=store)
    call = "SPY   261016C00500000"
    positions = {
        call: Position(call, 1, Decimal(2)),
        "SPY": Position("SPY", 3, Decimal(100)),
        "IWM": Position("IWM", 5, Decimal(100)),  # not pinned: not the bot's to manage
        "QQQ": Position("QQQ", 0, Decimal(100)),
    }
    h.engine._account = AccountSnapshot(Decimal(10000), Decimal(10000), positions, h.clock.now())
    await write_pinned(h, store, ("QQQ",))
    await h.tick(11)
    assert "no longer manage: SPY, SPY   261016C00500000. Sell" in restart_body(h)


async def test_a_pinned_change_that_strands_nothing_adds_no_warning(tmp_path):
    store = MemorySettingsStore()
    h = await Harness.create(tmp_path, settings_store=store)
    await write_pinned(h, store, ("SPY", "QQQ"))
    await h.tick(11)
    assert "no longer manage" not in restart_body(h)


async def test_a_restart_change_other_than_the_symbols_adds_no_warning(tmp_path):
    store = MemorySettingsStore()
    h = await Harness.create(tmp_path, settings_store=store)
    await h.target("SPY", 2)
    await h.settle()
    current = await store.latest()
    await store.write(
        current.settings.model_copy(update={"option_chain_days": 30}),
        expected_version=1,
        author="test",
        note="",
        now=h.clock.now(),
    )
    await h.tick(11)
    assert "no longer manage" not in restart_body(h)


async def test_an_invalid_version_is_ignored_and_alerted(tmp_path):
    store = MemorySettingsStore()
    h = await Harness.create(tmp_path, settings_store=store)
    store.put_raw(2, h.engine.settings.model_dump(mode="json") | {"strategy": "nope"})
    await h.tick(11)
    assert (await h.events("settings_rejected"))[-1]["data"]["version"] == 2
    assert "settings_rejected:2" in h.alert_keys()
    (_, _, body) = alerts_for(h, "settings_rejected:2")[0]
    assert body.endswith("The bot keeps the settings it was running with.")
    await h.target("SPY", 2)
    await h.settle()
    assert h.position("SPY") == 2  # the last good settings still trade


async def test_no_entries_until_settings_have_loaded_once(tmp_path):
    store = FlakyStore()
    store.error = RuntimeError("no network")
    h = await Harness.create(tmp_path, settings_store=store)
    await h.target("SPY", 2)
    await h.settle()
    assert h.position("SPY") == 0
    blocked = await h.events("order_blocked")
    assert "settings not loaded" in blocked[-1]["data"]["detail"]
    store.error = None
    await h.tick(11)
    await h.target("SPY", 2)
    await h.settle()
    assert h.position("SPY") == 2


async def test_exits_still_work_while_settings_are_unreadable(tmp_path):
    store = FlakyStore()
    h = await Harness.create(tmp_path, settings_store=store)
    await h.target("SPY", 2)
    await h.settle()
    store.error = RuntimeError("throttled")
    await h.tick(11)
    await h.target("SPY", 0)
    await h.settle()
    assert h.position("SPY") == 0


async def test_exits_work_when_settings_never_loaded(tmp_path):
    first = await Harness.create(tmp_path)
    await first.target("SPY", 2)
    await first.settle()
    assert first.position("SPY") == 2
    store = FlakyStore()
    store.error = RuntimeError("no network")
    h = await Harness.create(tmp_path, restart_of=first, settings_store=store)
    assert not h.live_settings.loaded
    await h.target("SPY", 0)
    await h.settle()
    assert h.position("SPY") == 0


async def test_a_version_rejected_at_start_is_reported_and_blocks_entries(tmp_path):
    store = MemorySettingsStore()
    base = await Harness.create(tmp_path, begin=False)
    store.put_raw(1, base.engine.settings.model_dump(mode="json") | {"strategy": "nope"})
    h = await Harness.create(tmp_path, settings_store=store)
    assert (await h.events("settings_rejected"))[-1]["data"]["version"] == 1
    assert "settings_rejected:1" in h.alert_keys()
    # The event stays, but the "check the table and the task role" alert would mislead.
    assert [e["data"] for e in await h.events("entries_halted")] == [
        {"reason": "settings not loaded"}
    ]
    assert alerts_for(h, "settings_not_loaded") == []
    (_, _, body) = alerts_for(h, "settings_rejected:1")[0]
    assert body.endswith(
        "No settings have loaded in this process, so no new positions open until a valid "
        "version is written. Exits still work."
    )
    assert "keeps the settings it was running with" not in body
    await h.target("SPY", 2)
    await h.settle()
    assert h.position("SPY") == 0
    blocked = await h.events("order_blocked")
    assert "settings not loaded" in blocked[-1]["data"]["detail"]


def alerts_for(h: Harness, key: str) -> list[tuple[str, str, str]]:
    return [alert for alert in h.alerts.sent if alert[0] == key]


async def test_settings_that_are_not_loaded_at_start_are_halted_and_alerted(tmp_path):
    store = FlakyStore()
    store.error = RuntimeError("no network")
    h = await Harness.create(tmp_path, settings_store=store)
    halted = await h.events("entries_halted")
    assert [e["data"] for e in halted] == [{"reason": "settings not loaded"}]
    (_, subject, body) = alerts_for(h, "settings_not_loaded")[0]
    assert subject == "Settings not loaded"
    assert "No new positions open" in body
    assert "Exits still work" in body
    assert "settings table and the task role" in body


async def test_settings_that_load_at_start_raise_no_not_loaded_alert(tmp_path):
    h = await Harness.create(tmp_path, settings_store=MemorySettingsStore())
    assert alerts_for(h, "settings_not_loaded") == []
    assert await h.events("entries_halted") == []


async def test_an_outage_past_five_minutes_is_reported_once_with_what_is_in_force(tmp_path):
    store = FlakyStore()
    h = await Harness.create(tmp_path, settings_store=store)
    store.error = RuntimeError("throttled")
    await h.run_for(420, step=5)
    events = await h.events("settings_unreadable")
    assert len(events) == 1
    assert events[0]["data"]["detail"] == "RuntimeError: throttled"
    assert events[0]["data"]["since"].startswith("2026-")
    (alert,) = alerts_for(h, "settings_unreadable")
    assert alert[1] == "Settings table unreadable"
    assert "The last good version, 1, stays in force" in alert[2]
    assert "tighter limits" in alert[2]


async def test_an_outage_with_nothing_loaded_says_no_new_positions(tmp_path):
    store = FlakyStore()
    store.error = RuntimeError("denied")
    h = await Harness.create(tmp_path, settings_store=store)
    await h.run_for(420, step=5)
    assert len(await h.events("settings_unreadable")) == 1
    (alert,) = alerts_for(h, "settings_unreadable")
    assert "No settings have loaded in this process, so no new positions open" in alert[2]


async def test_a_short_outage_raises_nothing(tmp_path):
    store = FlakyStore()
    h = await Harness.create(tmp_path, settings_store=store)
    store.error = RuntimeError("blip")
    await h.run_for(240, step=5)
    store.error = None
    await h.run_for(60, step=5)
    assert await h.events("settings_unreadable") == []
    assert alerts_for(h, "settings_unreadable") == []


async def test_a_good_read_restarts_the_outage_clock(tmp_path):
    store = FlakyStore()
    h = await Harness.create(tmp_path, settings_store=store)
    for _ in range(2):  # two outages, each shorter than the limit but together longer
        store.error = RuntimeError("blip")
        await h.run_for(240, step=5)
        store.error = None
        await h.run_for(20, step=5)
    assert alerts_for(h, "settings_unreadable") == []
    store.error = RuntimeError("down")
    await h.run_for(330, step=5)
    store.error = None
    await h.run_for(30, step=5)
    store.error = RuntimeError("down again")
    await h.run_for(330, step=5)
    assert len(await h.events("settings_unreadable")) == 2  # one per outage
    assert len(alerts_for(h, "settings_unreadable")) == 2


def test_the_unreadable_alert_waits_five_minutes():
    assert Engine.SETTINGS_ALERT_AFTER_S == 300.0
