"""The engine follows the settings store: new limits apply within seconds, the rest waits."""

from decimal import Decimal

from tests.unit.engine_harness import Harness
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


async def test_an_invalid_version_is_ignored_and_alerted(tmp_path):
    store = MemorySettingsStore()
    h = await Harness.create(tmp_path, settings_store=store)
    store.put_raw(2, h.engine.settings.model_dump(mode="json") | {"strategy": "nope"})
    await h.tick(11)
    assert (await h.events("settings_rejected"))[-1]["data"]["version"] == 2
    assert "settings_rejected:2" in h.alert_keys()
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
    await h.target("SPY", 2)
    await h.settle()
    assert h.position("SPY") == 0
    blocked = await h.events("order_blocked")
    assert "settings not loaded" in blocked[-1]["data"]["detail"]
