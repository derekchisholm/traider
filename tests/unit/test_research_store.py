"""One contract, two implementations: in-memory and DynamoDB (through moto)."""

from datetime import UTC, date, datetime

import boto3
import pytest
from moto import mock_aws

from traider.research.models import Pick, Posture, RunMeta
from traider.research.store import DynamoResearchStore, MemoryResearchStore

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 9, 13, 0, tzinfo=UTC)
DAY = "2026-10-09"
TABLE = "traider-test-research"


def meta(run_id="r1", status="ok", day=DAY) -> RunMeta:
    return RunMeta(
        run_id=run_id,
        kind="premarket",
        status=status,
        started_at=T0,
        finished_at=T0,
        trading_day=date.fromisoformat(day),
    )


def pick(symbol="NVDA", rank=1, run_id="r1", **overrides) -> Pick:
    fields = {
        "run_id": run_id,
        "rank": rank,
        "symbol": symbol,
        "side": "long",
        "horizon": "intraday",
        "score": 80,
        "pre_score": 70,
        "thesis": "t",
        "invalidation": "10",
        "expires_at": "2026-10-09T20:00:00+00:00",
    }
    return Pick.model_validate(fields | overrides)


def posture(level="trade", run_id="r1", at=T0) -> Posture:
    return Posture(level=level, reasons=("calm",), run_id=run_id, at=at)


def make_table():
    boto3.client("dynamodb").create_table(
        TableName=TABLE,
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
    return boto3.resource("dynamodb").Table(TABLE)


@pytest.fixture(params=["memory", "dynamo"])
def store(request):
    if request.param == "memory":
        yield MemoryResearchStore()
    else:
        with mock_aws():
            yield DynamoResearchStore(make_table())


def put_raw(store, pk, sk, body) -> None:
    if isinstance(store, MemoryResearchStore):
        store.put_raw(pk, sk, body)
    else:
        store._table.put_item(Item={"pk": pk, "sk": sk, "body": body})


async def test_an_empty_day_has_nothing(store):
    day = await store.day(DAY)
    assert (day.picks, day.postures, dict(day.runs), day.invalid) == ((), (), {}, 0)


async def test_a_written_run_reads_back_whole(store):
    await store.write_run(meta(), [pick("NVDA", 1), pick("AMD", 2)], posture())
    day = await store.day(DAY)
    assert [p.symbol for p in day.picks] == ["NVDA", "AMD"]
    assert day.postures == (posture(),)
    assert day.runs["r1"] == meta()


async def test_runs_on_other_days_are_not_mixed_in(store):
    await store.write_run(meta("r0", day="2026-10-08"), [pick(run_id="r0")], None)
    await store.write_run(meta("r1"), [pick("AMD", run_id="r1")], None)
    day = await store.day(DAY)
    assert [p.symbol for p in day.picks] == ["AMD"]
    assert set(day.runs) == {"r1"}


async def test_unparseable_items_are_counted_and_left_out(store):
    await store.write_run(meta(), [pick()], posture())
    put_raw(store, f"DAY#{DAY}", "PICK#r1#002", '{"symbol": "AMD"}')
    put_raw(store, f"DAY#{DAY}", "POSTURE#x", "not json")
    day = await store.day(DAY)
    assert [p.symbol for p in day.picks] == ["NVDA"]
    assert len(day.postures) == 1
    assert day.invalid == 2


async def test_unreadable_postures_are_counted_apart_from_other_bad_items(store):
    await store.write_run(meta(), [pick()], posture())
    put_raw(store, f"DAY#{DAY}", "POSTURE#x", "not json")
    put_raw(store, f"DAY#{DAY}", "PICK#r1#002", '{"symbol": "AMD"}')
    day = await store.day(DAY)
    assert day.invalid_postures == 1
    assert day.invalid == 2


async def test_a_run_with_unreadable_meta_is_absent(store):
    await store.write_run(meta(), [pick()], None)
    put_raw(store, "RUN#r1", "META", '{"run_id": "r1"}')
    day = await store.day(DAY)
    assert "r1" not in day.runs
    assert day.invalid == 1


class _FailingDict(dict):
    """Memory store backing that refuses the second pick, as a throttled write would."""

    def __setitem__(self, key, value):
        if key[1].endswith("#002"):
            raise RuntimeError("throttled")
        super().__setitem__(key, value)


class _TableWrapper:
    """A DynamoDB table stand-in that passes every call through unless a subclass changes it."""

    def __init__(self, table):
        self._table = table

    def __getattr__(self, name):
        return getattr(self._table, name)


class _FailingTable(_TableWrapper):
    """Refuses the second pick and passes everything else."""

    def put_item(self, Item):
        if Item["sk"].endswith("#002"):
            raise RuntimeError("throttled")
        return self._table.put_item(Item=Item)


class _PagedTable(_TableWrapper):
    """Forces every query to a page of two, so reads must follow LastEvaluatedKey."""

    def __init__(self, table):
        super().__init__(table)
        self.query_calls = 0

    def query(self, **kwargs):
        self.query_calls += 1
        return self._table.query(**(kwargs | {"Limit": 2}))


class _SpyTable(_TableWrapper):
    """Records the keyword arguments of every query and get_item."""

    def __init__(self, table):
        super().__init__(table)
        self.calls: list[tuple[str, dict]] = []

    def query(self, **kwargs):
        self.calls.append(("query", kwargs))
        return self._table.query(**kwargs)

    def get_item(self, **kwargs):
        self.calls.append(("get_item", kwargs))
        return self._table.get_item(**kwargs)


async def test_a_day_with_more_items_than_one_page_reads_back_whole(store):
    if isinstance(store, MemoryResearchStore):
        pytest.skip("paging is a DynamoDB feature")
    store._table = paged = _PagedTable(store._table)
    await store.write_run(meta(), [pick("NVDA", 1), pick("AMD", 2), pick("MU", 3)], posture())
    day = await store.day(DAY)
    assert [p.symbol for p in day.picks] == ["NVDA", "AMD", "MU"]
    assert day.postures == (posture(),)
    assert paged.query_calls > 1


async def test_day_and_meta_reads_are_consistent(store):
    if isinstance(store, MemoryResearchStore):
        pytest.skip("consistent reads are a DynamoDB feature")
    await store.write_run(meta(), [pick()], None)
    store._table = spy = _SpyTable(store._table)
    await store.day(DAY)
    queries = [kw for name, kw in spy.calls if name == "query"]
    gets = [kw for name, kw in spy.calls if name == "get_item"]
    assert queries and gets
    assert all(kw.get("ConsistentRead") is True for kw in queries + gets)


async def test_a_run_whose_picks_part_failed_to_write_is_never_seen(store):
    if isinstance(store, MemoryResearchStore):
        store._items = _FailingDict(store._items)
    else:
        store._table = _FailingTable(store._table)
    with pytest.raises(RuntimeError):
        await store.write_run(meta(), [pick("NVDA", 1), pick("AMD", 2)], None)
    day = await store.day(DAY)
    assert [p.symbol for p in day.picks] == ["NVDA"]
    assert "r1" not in day.runs


async def test_run_ids_with_hash_and_posture_only_runs_are_both_listed(store):
    await store.write_run(meta("a#b"), [pick(run_id="a#b")], None)
    await store.write_run(meta("r2"), [], posture(run_id="r2", at=T1))
    day = await store.day(DAY)
    assert set(day.runs) == {"a#b", "r2"}
    assert [p.symbol for p in day.picks] == ["NVDA"]
    assert day.postures == (posture(run_id="r2", at=T1),)


@pytest.mark.parametrize(
    ("picks", "run_posture", "problem"),
    [
        pytest.param([pick("NVDA", 1, run_id="r2")], None, "pick from another run", id="pick"),
        pytest.param([pick("NVDA", 1)], posture(run_id="r2"), "posture", id="posture"),
        pytest.param([pick("NVDA", 1), pick("AMD", 1)], None, "duplicate rank", id="rank"),
    ],
)
async def test_a_run_whose_parts_do_not_belong_together_is_refused_whole(
    store, picks, run_posture, problem
):
    with pytest.raises(ValueError):
        await store.write_run(meta(), picks, run_posture)
    day = await store.day(DAY)
    assert (day.picks, day.postures, dict(day.runs)) == ((), (), {})


async def test_picks_are_indexed_by_symbol_for_reports(store):
    if isinstance(store, MemoryResearchStore):
        pytest.skip("the index is a DynamoDB feature")
    await store.write_run(meta(), [pick("NVDA")], None)
    response = store._table.query(
        IndexName="gsi1",
        KeyConditionExpression="gsi1pk = :p",
        ExpressionAttributeValues={":p": "SYM#NVDA"},
    )
    assert [item["gsi1sk"] for item in response["Items"]] == [f"{DAY}#r1"]
