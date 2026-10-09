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
    MemorySettingsStore,
    SettingsConflict,
    SettingsInvalid,
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


async def test_history_is_newest_first_and_skips_invalid_versions(store):
    await store.write(settings(), expected_version=0, author="a", note="", now=T0)
    await store.write(
        settings(max_order_usd=Decimal(250)), expected_version=1, author="b", note="", now=T0
    )
    put_invalid(store, 3)
    assert [v.version for v in await store.history()] == [2, 1]
    assert [v.version for v in await store.history(limit=1)] == [2]
