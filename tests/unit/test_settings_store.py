"""One contract, two implementations: in-memory and DynamoDB (through moto)."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3
import pytest
from moto import mock_aws

from traider.config import Config
from traider.settings import Settings
from traider.settings_store import (
    DynamoSettingsStore,
    LiveSettings,
    MemorySettingsStore,
    SettingsConflict,
    SettingsInvalid,
    SettingsSuperseded,
    SettingsVersion,
)

T0 = datetime(2026, 10, 9, 13, 0, tzinfo=UTC)
TABLE = "traider-test-settings"


def settings(**risk) -> Settings:
    base = Settings.from_config(Config(symbols=("SPY",)))
    return base.model_copy(update={"risk": base.risk.model_copy(update=risk)})


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
def store(request):
    if request.param == "memory":
        yield MemorySettingsStore()
    else:
        with mock_aws():
            yield DynamoSettingsStore(make_table())


def put_body(store, version: int, body: dict[str, Any], *, at: str | None = None) -> None:
    """Store a body exactly as given, the way a buggy writer might."""
    if isinstance(store, MemorySettingsStore):
        store.put_raw(version, body, at=at or "")
    else:
        store._table.put_item(
            Item={
                "pk": "SETTINGS",
                "sk": f"V#{version:09d}",
                "version": version,
                "author": "test",
                "at": at or T0.isoformat(),
                "note": "",
                "body": json.dumps(body),
                "diff": "{}",
            }
        )


def put_invalid(store, version: int) -> None:
    put_body(store, version, settings().model_dump(mode="json") | {"strategy": "nope"})


async def test_an_empty_store_has_no_latest(store):
    assert await store.latest() is None
    assert await store.history() == []


async def test_first_write_is_version_one(store):
    written = await store.write(settings(), expected_version=0, author="bootstrap", note="", now=T0)
    assert written.version == 1
    latest = await store.latest()
    assert latest is not None
    assert (latest.version, latest.author, latest.at) == (1, "bootstrap", T0)
    assert latest.settings == settings()


async def test_each_write_records_what_changed(store):
    await store.write(settings(), expected_version=0, author="cli", note="", now=T0)
    second = await store.write(
        settings(max_order_usd=Decimal(250)),
        expected_version=1,
        author="cli",
        note="smaller",
        now=T0,
    )
    assert second.version == 2
    assert second.diff == {"risk.max_order_usd": ["500", "250"]}
    assert (await store.latest()).note == "smaller"


async def test_a_stale_writer_gets_a_conflict_and_nothing_is_written(store):
    await store.write(settings(), expected_version=0, author="a", note="", now=T0)
    await store.write(
        settings(max_order_usd=Decimal(250)), expected_version=1, author="a", note="", now=T0
    )
    with pytest.raises(SettingsConflict):
        await store.write(
            settings(max_order_usd=Decimal(100)), expected_version=1, author="b", note="", now=T0
        )
    assert (await store.latest()).settings.risk.max_order_usd == Decimal(250)


async def test_two_writers_racing_on_one_version_exactly_one_wins(store):
    await store.write(settings(), expected_version=0, author="a", note="", now=T0)
    results = []
    for author in ("a", "b"):
        try:
            results.append(
                await store.write(settings(), expected_version=1, author=author, note="", now=T0)
            )
        except SettingsConflict:
            results.append(None)
    assert [r is not None for r in results] == [True, False]


async def test_an_invalid_latest_version_is_reported_with_its_number(store):
    await store.write(settings(), expected_version=0, author="a", note="", now=T0)
    put_invalid(store, 2)
    with pytest.raises(SettingsInvalid) as raised:
        await store.latest()
    assert raised.value.version == 2
    assert "unknown strategy 'nope'" in str(raised.value)


async def test_a_strategy_that_raises_a_non_value_error_is_still_invalid_not_a_crash(store):
    # int(None) raises TypeError inside sma_cross, which pydantic does not turn into a
    # ValidationError. The reader must still report the version, not die on it.
    await store.write(settings(), expected_version=0, author="a", note="", now=T0)
    put_body(store, 2, settings().model_dump(mode="json") | {"strategy_params": {"fast": None}})
    with pytest.raises(SettingsInvalid) as raised:
        await store.latest()
    assert raised.value.version == 2
    assert str(raised.value).split(": ", 1)[1].startswith("TypeError: ")


async def test_a_malformed_timestamp_is_invalid_not_a_crash(store):
    await store.write(settings(), expected_version=0, author="a", note="", now=T0)
    put_body(store, 2, settings().model_dump(mode="json"), at="not-a-date")
    with pytest.raises(SettingsInvalid) as raised:
        await store.latest()
    assert raised.value.version == 2
    assert "ValueError" in str(raised.value)
    assert [v.version for v in await store.history()] == [1]


async def test_a_write_on_top_of_an_invalid_latest_version_succeeds_with_an_empty_diff(store):
    await store.write(settings(), expected_version=0, author="a", note="", now=T0)
    put_invalid(store, 2)
    written = await store.write(
        settings(max_order_usd=Decimal(250)), expected_version=2, author="b", note="", now=T0
    )
    assert written.version == 3
    assert written.diff == {}
    latest = await store.latest()
    assert latest is not None
    assert (latest.version, latest.settings.risk.max_order_usd) == (3, Decimal(250))


async def test_a_negative_expected_version_is_a_value_error(store):
    with pytest.raises(ValueError, match="expected_version"):
        await store.write(settings(), expected_version=-1, author="a", note="", now=T0)
    assert await store.latest() is None


async def test_writing_ahead_of_the_latest_version_is_refused_and_leaves_no_gap(store):
    with pytest.raises(SettingsConflict):
        await store.write(settings(), expected_version=1, author="a", note="", now=T0)
    assert await store.latest() is None
    await store.write(settings(), expected_version=0, author="a", note="", now=T0)
    with pytest.raises(SettingsConflict):
        await store.write(settings(), expected_version=3, author="a", note="", now=T0)
    assert (await store.latest()).version == 1


@pytest.mark.parametrize(("sort_key", "version"), [("V#000000002", 2), ("V#junk", -1)])
async def test_a_damaged_item_without_a_version_is_reported_under_its_sort_key(sort_key, version):
    # Dynamo only: the memory store always records a version number.
    with mock_aws():
        store = DynamoSettingsStore(make_table())
        store._table.put_item(
            Item={
                "pk": "SETTINGS",
                "sk": sort_key,
                "body": json.dumps(settings().model_dump(mode="json") | {"strategy": "nope"}),
                "at": T0.isoformat(),
            }
        )
        with pytest.raises(SettingsInvalid) as raised:
            await store.latest()
    assert raised.value.version == version


class RacingTable:
    """A table where a rival writes the next version right after our put, before our read."""

    def __init__(self, table, *, rival_body: dict[str, Any] | None = None) -> None:
        self._table = table
        self._rival_body = rival_body

    def __getattr__(self, name: str) -> Any:
        return getattr(self._table, name)

    def put_item(self, **kwargs: Any) -> Any:
        result = self._table.put_item(**kwargs)
        mine = kwargs["Item"]
        rival = dict(mine)
        rival["version"] = mine["version"] + 1
        rival["sk"] = f"V#{rival['version']:09d}"
        rival["author"] = "rival"
        if self._rival_body is not None:
            rival["body"] = json.dumps(self._rival_body)
        self._table.put_item(Item=rival)
        return result


async def test_a_later_version_written_during_our_write_is_reported_as_superseded():
    with mock_aws():
        table = make_table()
        plain = DynamoSettingsStore(table)
        await plain.write(settings(), expected_version=0, author="a", note="", now=T0)
        racing = DynamoSettingsStore(RacingTable(table))
        with pytest.raises(SettingsSuperseded) as raised:
            await racing.write(
                settings(max_order_usd=Decimal(250)),
                expected_version=1,
                author="b",
                note="",
                now=T0,
            )
        assert (raised.value.written, raised.value.newest) == (2, 3)
        assert isinstance(raised.value, SettingsConflict)
        assert (await plain.get(2)).author == "b"  # ours is kept in history
        assert (await plain.latest()).author == "rival"


async def test_an_unreadable_later_version_during_our_write_still_counts_as_superseded():
    bad = settings().model_dump(mode="json") | {"strategy": "nope"}
    with mock_aws():
        table = make_table()
        plain = DynamoSettingsStore(table)
        await plain.write(settings(), expected_version=0, author="a", note="", now=T0)
        racing = DynamoSettingsStore(RacingTable(table, rival_body=bad))
        with pytest.raises(SettingsSuperseded) as raised:
            await racing.write(settings(), expected_version=1, author="b", note="", now=T0)
        assert (raised.value.written, raised.value.newest) == (2, 3)


async def test_history_is_newest_first_and_skips_invalid_versions(store):
    await store.write(settings(), expected_version=0, author="a", note="", now=T0)
    await store.write(
        settings(max_order_usd=Decimal(250)), expected_version=1, author="b", note="", now=T0
    )
    put_invalid(store, 3)
    assert [v.version for v in await store.history()] == [2, 1]
    assert [v.version for v in await store.history(limit=1)] == [2]


async def test_get_returns_any_stored_version(store):
    await store.write(settings(), expected_version=0, author="a", note="first", now=T0)
    await store.write(
        settings(max_order_usd=Decimal(250)), expected_version=1, author="b", note="", now=T0
    )
    old = await store.get(1)
    assert old is not None
    assert (old.version, old.author, old.note, old.at) == (1, "a", "first", T0)
    assert old.settings == settings()
    new = await store.get(2)
    assert new is not None and new.settings.risk.max_order_usd == Decimal(250)


@pytest.mark.parametrize(("stored", "version"), [(0, 1), (1, 0), (1, 2), (1, 99)])
async def test_get_of_a_version_that_does_not_exist_is_none(store, stored, version):
    for n in range(stored):
        await store.write(settings(), expected_version=n, author="a", note="", now=T0)
    assert await store.get(version) is None


async def test_get_of_an_invalid_version_raises_with_its_number(store):
    await store.write(settings(), expected_version=0, author="a", note="", now=T0)
    put_invalid(store, 2)
    with pytest.raises(SettingsInvalid) as raised:
        await store.get(2)
    assert raised.value.version == 2
    assert (await store.get(1)).version == 1


class BrokenStore(MemorySettingsStore):
    def __init__(self) -> None:
        super().__init__()
        self.error: Exception | None = None

    async def latest(self):
        if self.error is not None:
            raise self.error
        return await super().latest()


async def test_start_seeds_an_empty_store_from_the_fallback():
    store = MemorySettingsStore()
    live = LiveSettings(store, settings())
    await live.start(T0)
    assert live.loaded
    assert live.version == 1
    assert (await store.latest()).author == "bootstrap"


async def test_start_takes_the_stored_version_in_full_restart_fields_included():
    store = MemorySettingsStore()
    stored = settings().model_copy(update={"pinned_symbols": ("QQQ",)})
    await store.write(stored, expected_version=0, author="cli", note="", now=T0)
    live = LiveSettings(store, settings())
    await live.start(T0)
    assert live.current.pinned_symbols == ("QQQ",)


async def test_start_without_a_readable_store_keeps_the_fallback_and_is_not_loaded():
    store = BrokenStore()
    store.error = RuntimeError("no network")
    live = LiveSettings(store, settings())
    updates = await live.start(T0)
    assert not live.loaded
    assert live.current == settings()
    assert [u.kind for u in updates] == ["unreadable"]


async def test_start_with_an_invalid_latest_version_is_not_loaded():
    store = MemorySettingsStore()
    store.put_raw(1, settings().model_dump(mode="json") | {"strategy": "nope"})
    live = LiveSettings(store, settings())
    updates = await live.start(T0)
    assert not live.loaded
    assert [(u.kind, u.version) for u in updates] == [("rejected", 1)]


async def test_refresh_applies_a_new_version_and_reports_the_diff():
    store = MemorySettingsStore()
    live = LiveSettings(store, settings())
    await live.start(T0)
    await store.write(
        settings(max_order_usd=Decimal(250)), expected_version=1, author="cli", note="", now=T0
    )
    updates = await live.refresh(T0)
    assert [(u.kind, u.version) for u in updates] == [("applied", 2)]
    assert updates[0].diff == {"risk.max_order_usd": ["500", "250"]}
    assert live.current.risk.max_order_usd == Decimal(250)
    assert await live.refresh(T0) == []  # nothing new


async def test_refresh_keeps_restart_fields_and_says_a_restart_is_needed():
    store = MemorySettingsStore()
    live = LiveSettings(store, settings())
    await live.start(T0)
    changed = settings(max_order_usd=Decimal(250)).model_copy(
        update={"option_chain_days": 30, "pinned_symbols": ("QQQ",)}
    )
    await store.write(changed, expected_version=1, author="cli", note="", now=T0)
    updates = await live.refresh(T0)
    assert [u.kind for u in updates] == ["applied", "pending_restart"]
    assert updates[1].detail == "option_chain_days"
    assert live.current.option_chain_days == 45
    assert live.current.pinned_symbols == ("QQQ",)  # live: the engine's universe follows it
    assert live.current.risk.max_order_usd == Decimal(250)


class ForcedLatest(MemorySettingsStore):
    """Serves a version that was built without validation, as a buggy reader might."""

    def __init__(self) -> None:
        super().__init__()
        self.forced: SettingsVersion | None = None

    async def latest(self) -> SettingsVersion | None:
        return self.forced if self.forced is not None else await super().latest()


def unchecked(version: int, *, at: datetime = T0, **fields) -> SettingsVersion:
    # model_copy does not validate, so this is a Settings that validation would refuse.
    return SettingsVersion(version, settings().model_copy(update=fields), "test", at)


async def test_a_merged_result_that_does_not_validate_is_rejected_and_the_last_good_stays():
    store = ForcedLatest()
    live = LiveSettings(store, settings())
    await live.start(T0)
    store.forced = unchecked(2, order_timeout_s=-5.0)
    first = await live.refresh(T0)
    assert [(u.kind, u.version) for u in first] == [("rejected", 2)]
    assert "order_timeout_s" in first[0].detail
    assert (live.version, live.loaded, live.current) == (1, True, settings())
    assert live.pending is None
    assert await live.refresh(T0) == []  # reported once, not on every refresh
    # A later good version still applies.
    store.forced = unchecked(3, order_timeout_s=30.0)
    again = await live.refresh(T0)
    assert [(u.kind, u.version) for u in again] == [("applied", 3)]
    assert live.current.order_timeout_s == 30.0


LATER = datetime(2026, 10, 9, 13, 5, tzinfo=UTC)


def delete_item(store, version: int) -> None:
    if isinstance(store, MemorySettingsStore):
        store._items.pop(version)
    else:
        store._table.delete_item(Key={"pk": "SETTINGS", "sk": f"V#{version:09d}"})


async def test_an_invalid_item_that_is_deleted_and_rewritten_as_valid_is_applied(store):
    await store.write(settings(), expected_version=0, author="a", note="", now=T0)
    live = LiveSettings(store, settings())
    await live.start(T0)
    put_invalid(store, 2)
    assert [(u.kind, u.version) for u in await live.refresh(T0)] == [("rejected", 2)]
    assert await live.refresh(T0) == []  # the same item is still reported once only
    delete_item(store, 2)
    await store.write(
        settings(max_order_usd=Decimal(250)), expected_version=1, author="b", note="", now=LATER
    )
    updates = await live.refresh(LATER)
    assert [(u.kind, u.version) for u in updates] == [("applied", 2)]
    assert live.version == 2
    assert live.current.risk.max_order_usd == Decimal(250)


async def test_a_merge_rejected_item_that_is_replaced_by_a_valid_one_is_applied():
    store = ForcedLatest()
    live = LiveSettings(store, settings())
    await live.start(T0)
    store.forced = unchecked(2, order_timeout_s=-5.0)
    assert [(u.kind, u.version) for u in await live.refresh(T0)] == [("rejected", 2)]
    assert await live.refresh(T0) == []
    store.forced = unchecked(2, at=LATER, order_timeout_s=30.0)  # same number, a new item
    updates = await live.refresh(LATER)
    assert [(u.kind, u.version) for u in updates] == [("applied", 2)]
    assert live.current.order_timeout_s == 30.0


async def test_a_start_rejected_item_that_is_replaced_by_a_valid_one_loads():
    store = MemorySettingsStore()
    store.put_raw(1, settings().model_dump(mode="json") | {"strategy": "nope"})
    live = LiveSettings(store, settings())
    assert [(u.kind, u.version) for u in await live.start(T0)] == [("rejected", 1)]
    assert await live.refresh(T0) == []
    store._items.pop(1)
    await store.write(
        settings(max_order_usd=Decimal(250)), expected_version=0, author="b", note="", now=LATER
    )
    assert [(u.kind, u.version) for u in await live.refresh(LATER)] == [("applied", 1)]
    assert live.loaded and live.current.risk.max_order_usd == Decimal(250)


async def test_an_error_while_merging_is_a_rejection_not_a_crash(monkeypatch):
    store = MemorySettingsStore()
    live = LiveSettings(store, settings())
    await live.start(T0)
    await store.write(
        settings(max_order_usd=Decimal(250)), expected_version=1, author="cli", note="", now=T0
    )

    def boom(running, new):
        raise KeyError("strategy_params")

    monkeypatch.setattr("traider.settings_store.merge_live", boom)
    updates = await live.refresh(T0)
    assert [(u.kind, u.version) for u in updates] == [("rejected", 2)]
    assert "KeyError" in updates[0].detail
    assert (live.version, live.current) == (1, settings())


async def test_pending_holds_the_stored_settings_while_a_restart_field_differs():
    store = MemorySettingsStore()
    live = LiveSettings(store, settings())
    await live.start(T0)
    assert live.pending is None
    changed = settings(max_order_usd=Decimal(250)).model_copy(
        update={"option_chain_days": 30, "pinned_symbols": ("QQQ",)}
    )
    await store.write(changed, expected_version=1, author="cli", note="", now=T0)
    await live.refresh(T0)
    assert live.pending == changed  # as written, not merged
    assert live.current.option_chain_days == 45
    # A later version that puts the restart field back clears it.
    await store.write(
        settings(max_order_usd=Decimal(100)), expected_version=2, author="cli", note="", now=T0
    )
    await live.refresh(T0)
    assert live.pending is None


async def test_refresh_ignores_an_invalid_version_once_and_keeps_the_last_good():
    store = MemorySettingsStore()
    live = LiveSettings(store, settings())
    await live.start(T0)
    store.put_raw(2, settings().model_dump(mode="json") | {"strategy": "nope"})
    first = await live.refresh(T0)
    assert [(u.kind, u.version) for u in first] == [("rejected", 2)]
    assert await live.refresh(T0) == []  # reported once
    assert live.version == 1
    assert live.current == settings()


async def test_refresh_keeps_the_last_good_when_the_store_is_unreadable():
    store = BrokenStore()
    live = LiveSettings(store, settings())
    await live.start(T0)
    store.error = RuntimeError("throttled")
    updates = await live.refresh(T0)
    assert [u.kind for u in updates] == ["unreadable"]
    assert live.loaded
    assert live.current == settings()


async def test_a_later_refresh_loads_settings_that_could_not_be_read_at_start():
    store = BrokenStore()
    store.error = RuntimeError("no network")
    live = LiveSettings(store, settings())
    await live.start(T0)
    store.error = None
    updates = await live.refresh(T0)
    assert live.loaded
    assert updates[0].kind == "applied"


async def test_a_refresh_after_an_unreadable_start_keeps_the_running_restart_fields():
    # start() may have built the strategy and feed from the fallback, so they stay.
    store = BrokenStore()
    store.error = RuntimeError("no network")
    live = LiveSettings(store, settings())
    await live.start(T0)
    changed = settings(max_order_usd=Decimal(250)).model_copy(
        update={"option_chain_days": 30, "pinned_symbols": ("QQQ",)}
    )
    await store.write(changed, expected_version=0, author="cli", note="", now=T0)
    store.error = None
    updates = await live.refresh(T0)
    assert live.loaded
    assert live.current.option_chain_days == 45
    assert live.current.risk.max_order_usd == Decimal(250)
    assert [u.kind for u in updates] == ["applied", "pending_restart"]


async def test_a_refresh_seeds_an_empty_store_after_an_unreadable_start():
    store = BrokenStore()
    store.error = RuntimeError("no network")
    live = LiveSettings(store, settings())
    await live.start(T0)
    store.error = None
    updates = await live.refresh(T0)
    assert [u.kind for u in updates] == ["applied"]
    assert live.loaded
    assert live.version == 1
    assert (await store.latest()).author == "bootstrap"


async def test_a_loaded_refresh_never_seeds_a_store_that_has_become_empty():
    store = MemorySettingsStore()
    live = LiveSettings(store, settings())
    await live.start(T0)
    store._items.clear()
    assert await live.refresh(T0) == []
    assert await store.latest() is None
    assert live.loaded
    assert live.current == settings()


class RacingStore:
    """Empty on the first read; a rival writer gets the bootstrap write in first."""

    def __init__(self, winner: SettingsVersion) -> None:
        self._winner = winner
        self._reads = 0

    async def latest(self) -> SettingsVersion | None:
        self._reads += 1
        return None if self._reads == 1 else self._winner

    async def write(
        self, settings: Settings, *, expected_version: int, author: str, note: str, now: datetime
    ) -> SettingsVersion:
        raise SettingsConflict("another writer got there first")

    async def history(self, limit: int = 20) -> list[SettingsVersion]:
        return [self._winner]


class VanishingStore:
    """Always empty, and every write loses to a rival that then cannot be read back."""

    async def latest(self) -> SettingsVersion | None:
        return None

    async def write(
        self, settings: Settings, *, expected_version: int, author: str, note: str, now: datetime
    ) -> SettingsVersion:
        raise SettingsConflict("another writer got there first")

    async def history(self, limit: int = 20) -> list[SettingsVersion]:
        return []


async def test_an_empty_store_after_a_lost_bootstrap_race_is_unreadable_not_silent():
    live = LiveSettings(VanishingStore(), settings())  # type: ignore[arg-type]
    first = await live.start(T0)
    assert [(u.kind, u.detail) for u in first] == [
        ("unreadable", "no settings version after bootstrap")
    ]
    again = await live.refresh(T0)
    assert [(u.kind, u.version, u.detail) for u in again] == [
        ("unreadable", None, "no settings version after bootstrap")
    ]
    assert not live.loaded


async def test_start_adopts_the_version_a_rival_wrote_during_bootstrap():
    rival = await MemorySettingsStore().write(
        settings(max_order_usd=Decimal(250)), expected_version=0, author="cli", note="", now=T0
    )
    live = LiveSettings(RacingStore(rival), settings())
    assert await live.start(T0) == []
    assert live.loaded
    assert live.version == 1
    assert live.current == rival.settings


async def test_an_invalid_version_is_reported_once_across_start_and_refresh():
    store = MemorySettingsStore()
    store.put_raw(1, settings().model_dump(mode="json") | {"strategy": "nope"})
    live = LiveSettings(store, settings())
    assert [(u.kind, u.version) for u in await live.start(T0)] == [("rejected", 1)]
    assert await live.refresh(T0) == []
    assert not live.loaded


async def test_start_keeps_what_it_returned_in_start_updates():
    store = BrokenStore()
    store.error = RuntimeError("no network")
    live = LiveSettings(store, settings())
    assert live.start_updates == []
    updates = await live.start(T0)
    assert updates and live.start_updates == updates
