"""One contract, two implementations: in-memory and DynamoDB (through moto)."""

from datetime import UTC, date, datetime
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from traider.research.models import Pick, PickOutcome, Posture, RunMeta, ScoreSummary
from traider.research.store import DynamoResearchStore, MemoryResearchStore, ResearchWriter

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


# --- the research jobs' side of the table (C1) ----------------------------------------------

LATER = datetime(2026, 10, 9, 12, 30, tzinfo=UTC)


def job_meta(run_id, kind="premarket", status="ok", day=DAY) -> RunMeta:
    return meta(run_id, status, day).model_copy(update={"kind": kind})


async def test_a_running_meta_is_written_alone_and_found_by_day_and_kind(store):
    await store.put_meta(job_meta("premarket-a", status="running"))
    (found,) = await store.runs_for_day(DAY, "premarket")
    assert (found.run_id, found.status.value) == ("premarket-a", "running")
    assert await store.runs_for_day(DAY, "intraday") == []
    assert await store.runs_for_day("2026-10-08", "premarket") == []


async def test_the_final_meta_replaces_the_running_one(store):
    await store.put_meta(job_meta("premarket-a", status="running"))
    await store.write_run(job_meta("premarket-a", status="partial"), [], None)
    (found,) = await store.runs_for_day(DAY, "premarket")
    assert found.status.value == "partial"


async def test_runs_for_day_lists_every_run_of_that_kind(store):
    await store.write_run(job_meta("premarket-a"), [], None)
    await store.write_run(job_meta("premarket-b", status="failed"), [], None)
    await store.write_run(job_meta("manual-c", kind="manual"), [], None)
    found = await store.runs_for_day(DAY, "premarket")
    assert [m.run_id for m in found] == ["premarket-a", "premarket-b"]


async def test_an_unreadable_meta_is_skipped_by_runs_for_day(store):
    await store.write_run(job_meta("premarket-a"), [], None)
    if isinstance(store, MemoryResearchStore):
        store.raw("RUN#premarket-a", "META")["body"] = "{not json"
    else:
        store._table.update_item(
            Key={"pk": "RUN#premarket-a", "sk": "META"},
            UpdateExpression="SET body = :b",
            ExpressionAttributeValues={":b": "{not json"},
        )
    assert await store.runs_for_day(DAY, "premarket") == []


async def test_day_cost_adds_up_and_starts_at_zero(store):
    assert await store.day_cost(DAY) == Decimal(0)
    assert await store.add_day_cost(DAY, Decimal("1.1200")) == Decimal("1.12")
    assert await store.add_day_cost(DAY, Decimal("0.0350")) == Decimal("1.155")
    assert await store.day_cost(DAY) == Decimal("1.155")
    assert await store.day_cost("2026-10-08") == Decimal(0)


@pytest.mark.parametrize("bad", [Decimal("-1"), Decimal("Infinity"), Decimal("NaN")])
async def test_a_cost_cannot_be_taken_back(store, bad):
    await store.add_day_cost(DAY, Decimal("2"))
    with pytest.raises(ValueError, match="only grow"):
        await store.add_day_cost(DAY, bad)
    assert await store.day_cost(DAY) == Decimal("2")


class SpyTable:
    """Wraps a table and records every call made through it."""

    def __init__(self, table):
        self._table = table
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        fn = getattr(self._table, name)

        def spy(**kwargs):
            self.calls.append((name, kwargs))
            return fn(**kwargs)

        return spy


async def test_adding_to_the_day_cost_is_one_atomic_update(store):
    if isinstance(store, MemoryResearchStore):
        pytest.skip("atomicity is a DynamoDB matter")
    spy = SpyTable(store._table)
    store._table = spy
    assert await store.add_day_cost(DAY, Decimal("1.5")) == Decimal("1.5")
    assert [name for name, _ in spy.calls] == ["update_item"]
    assert spy.calls[0][1]["UpdateExpression"] == "ADD usd :usd"
    assert spy.calls[0][1]["Key"] == {"pk": f"COST#{DAY}", "sk": "TOTAL"}


def test_both_stores_satisfy_the_writer_protocol():
    memory: ResearchWriter = MemoryResearchStore()
    dynamo: ResearchWriter = DynamoResearchStore(None)
    assert memory is not None and dynamo is not None


async def test_only_one_holder_of_a_lock_until_it_expires(store):
    assert await store.acquire_lock("premarket", "run-a", 3600, T0)
    assert not await store.acquire_lock("premarket", "run-b", 3600, T0)
    assert not await store.acquire_lock("premarket", "run-b", 3600, LATER)  # 30 of 60 minutes
    assert await store.acquire_lock("other-kind", "run-b", 3600, T0)


async def test_an_expired_lock_can_be_taken_over(store):
    assert await store.acquire_lock("premarket", "run-a", 60, T0)
    assert await store.acquire_lock("premarket", "run-b", 60, LATER)


async def test_a_released_lock_is_free_and_only_its_owner_can_release_it(store):
    assert await store.acquire_lock("premarket", "run-a", 3600, T0)
    await store.release_lock("premarket", "run-b")  # not the owner: nothing happens
    assert not await store.acquire_lock("premarket", "run-b", 3600, T0)
    await store.release_lock("premarket", "run-a")
    assert await store.acquire_lock("premarket", "run-b", 3600, T0)


async def test_releasing_a_lock_nobody_holds_is_harmless(store):
    await store.release_lock("premarket", "run-a")


async def test_the_bots_day_read_ignores_the_jobs_own_items(store):
    await store.add_day_cost(DAY, Decimal(1))
    await store.acquire_lock("premarket", "run-a", 600, T0)
    await store.put_meta(job_meta("premarket-a", status="running"))
    day = await store.day(DAY)
    assert (day.picks, day.postures, dict(day.runs), day.invalid) == ((), (), {}, 0)


async def test_meta_items_are_indexed_by_day_and_kind_for_the_jobs(store):
    if isinstance(store, MemoryResearchStore):
        pytest.skip("the index is a DynamoDB feature")
    await store.write_run(job_meta("premarket-a"), [pick(run_id="premarket-a")], None)
    item = store._table.get_item(Key={"pk": "RUN#premarket-a", "sk": "META"})["Item"]
    assert (item["gsi1pk"], item["gsi1sk"]) == (f"RUNDAY#{DAY}", "premarket#premarket-a")


# --- C2a: the scorecard's items ------------------------------------------------------------


def outcome(run_id="r1", rank=1, **overrides) -> PickOutcome:
    fields = {
        "run_id": run_id,
        "rank": rank,
        "symbol": "NVDA",
        "side": "long",
        "horizon": "intraday",
        "score": 80,
        "pre_score": 70,
        "pick_day": DAY,
        "run_status": "ok",
        "status": "pending",
        "updated_at": T1.isoformat(),
    }
    return PickOutcome.model_validate(fields | overrides)


async def test_outcomes_read_back_by_key_and_missing_ones_are_absent(store):
    first, second = outcome(rank=1), outcome(rank=2, status="final", ret_1d=2.5)
    await store.put_outcome(first)
    await store.put_outcome(second)
    found = await store.outcomes([first.key, second.key, "PICK#nobody#001", first.key])
    assert found == {first.key: first, second.key: second}
    assert await store.outcomes([]) == {}


async def test_an_outcome_is_overwritten_not_duplicated(store):
    await store.put_outcome(outcome())
    await store.put_outcome(outcome(status="partial", ret_1d=-1.0))
    (found,) = (await store.outcomes(["PICK#r1#001"])).values()
    assert (found.status.value, found.ret_1d) == ("partial", -1.0)


async def test_an_unreadable_outcome_or_one_under_the_wrong_key_is_absent(store):
    put_raw(store, "PICK#r1#001", "OUTCOME", "{not json")
    put_raw(store, "PICK#r1#002", "OUTCOME", outcome(rank=3).model_dump_json())
    assert await store.outcomes(["PICK#r1#001", "PICK#r1#002"]) == {}


async def test_outcomes_read_more_than_one_batch(store):
    many = [outcome(rank=rank) for rank in range(1, 251)]
    for item in many:
        await store.put_outcome(item)
    found = await store.outcomes([o.key for o in many])
    assert sorted(found) == sorted(o.key for o in many)


async def test_dynamo_asks_again_for_unprocessed_keys(store):
    if isinstance(store, MemoryResearchStore):
        pytest.skip("unprocessed keys are a DynamoDB feature")
    await store.put_outcome(outcome(rank=1))
    await store.put_outcome(outcome(rank=2))
    client = store._table.meta.client
    real = client.batch_get_item
    calls = []

    def flaky(**kwargs):
        calls.append(kwargs)
        response = real(**kwargs)
        if len(calls) == 1:  # the first answer leaves the second key for later
            keys = kwargs["RequestItems"][TABLE]["Keys"]
            response["Responses"][TABLE] = [
                i for i in response["Responses"][TABLE] if i["pk"] == keys[0]["pk"]
            ]
            response["UnprocessedKeys"] = {TABLE: {"Keys": keys[1:], "ConsistentRead": True}}
        return response

    client.batch_get_item = flaky
    found = await store.outcomes(["PICK#r1#001", "PICK#r1#002"])
    assert sorted(found) == ["PICK#r1#001", "PICK#r1#002"]
    assert [len(c["RequestItems"][TABLE]["Keys"]) for c in calls] == [2, 1]
    assert all(c["RequestItems"][TABLE]["ConsistentRead"] for c in calls)


async def test_outcome_items_never_show_up_in_the_bots_day_read(store):
    await store.put_outcome(outcome())
    day = await store.day(DAY)
    assert (day.picks, day.postures, day.invalid) == ((), (), 0)


async def test_picks_between_reads_each_weekday_with_its_runs(store):
    await store.write_run(meta("r1", day="2026-10-08"), [pick(run_id="r1")], posture("trade", "r1"))
    await store.write_run(meta("r2", day=DAY), [pick("AMD", run_id="r2")], None)
    days = await store.picks_between(date(2026, 10, 2), date(2026, 10, 9))
    # Friday 10-02 to Friday 10-09: six weekdays, the weekend left out.
    assert [d.day for d in days] == [
        "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09",
    ]  # fmt: skip
    by_day = {d.day: d for d in days}
    assert [p.symbol for p in by_day["2026-10-08"].picks] == ["NVDA"]
    assert set(by_day["2026-10-08"].runs) == {"r1"}
    assert [p.symbol for p in by_day[DAY].picks] == ["AMD"]
    assert await store.picks_between(date(2026, 10, 10), date(2026, 10, 11)) == []


async def test_a_summary_is_stored_under_its_day_and_only_there(store):
    summary = ScoreSummary(day=date(2026, 10, 9), run_id="scorecard-x", picks=3, updated_at=T1)
    await store.put_score_summary(DAY, summary)
    if isinstance(store, MemoryResearchStore):
        body = store.raw(f"SCORE#{DAY}", "SUMMARY")["body"]
    else:
        body = store._table.get_item(Key={"pk": f"SCORE#{DAY}", "sk": "SUMMARY"})["Item"]["body"]
    assert ScoreSummary.model_validate_json(body) == summary
    with pytest.raises(ValueError, match="cannot be stored under"):
        await store.put_score_summary("2026-10-08", summary)
