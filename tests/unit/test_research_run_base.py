"""The scaffolding every research run kind shares (``RunBase`` and ``run_locked``), tried
on a small made-up kind. The pre-market run's own tests prove it still behaves as before."""

import asyncio
import json
from datetime import date
from decimal import Decimal
from typing import ClassVar

import pytest

import traider.research.run as run_module
from tests.fakes.research import NOW, TODAY, golden_llm, market_day
from tests.unit.test_research_run import Trails
from traider.alerts import LogAlerter
from traider.research.models import RunKind, RunMeta, RunStatus
from traider.research.run import (
    EXIT_FAILED,
    EXIT_LOCKED,
    EXIT_OK,
    LOCK_SPARE_S,
    RUN_BOX_MARGIN_S,
    RunBase,
    RunDeps,
    RunOutcome,
    run_locked,
)
from traider.research.store import MemoryResearchStore
from traider.settings import Settings
from traider.timeutil import ManualClock

DAY = TODAY.isoformat()


class RecordingStore(MemoryResearchStore):
    def __init__(self) -> None:
        super().__init__()
        self.order: list[str] = []

    async def add_day_cost(self, day, usd):
        self.order.append(f"cost {usd}")
        return await super().add_day_cost(day, usd)

    async def put_meta(self, meta):
        self.order.append(f"meta {meta.status.value}")
        await super().put_meta(meta)


class Probe(RunBase):
    """A made-up kind: spends a cent, then writes its META."""

    kind: ClassVar[RunKind] = "manual"
    lock_name: ClassVar[str] = "probe"
    title: ClassVar[str] = "Probe"
    seen_force: bool | None = None
    explode: Exception | None = None

    def time_limit(self) -> float:
        return 100.0

    def models(self) -> tuple[str, ...]:
        return ("model-a",)

    async def _execute(self, *, force: bool) -> RunOutcome:
        self.seen_force = force
        await self.begin(Decimal("0.50"))
        self.stage = "work"
        if self.explode is not None:
            raise self.explode
        self.note("one note")
        meta = self.meta(RunStatus.OK)
        outcome = RunOutcome("ok", EXIT_OK, self.run_id, meta=meta)

        async def write() -> None:
            self.deps.store.order.append("write")
            await self.deps.store.put_meta(meta)

        return await self.commit(outcome, write, "probe done", event="probe_done")


def deps(store=None) -> RunDeps:
    market, events = market_day()
    return RunDeps(
        store=store or RecordingStore(),
        market=market,
        events=events,
        llm=golden_llm(),
        trail=Trails(),
        alerts=LogAlerter(),
        settings=Settings(),
        clock=ManualClock(NOW),
        monotonic=lambda: 0.0,
    )


def probe(d: RunDeps, *, dry_run: bool = False) -> Probe:
    return Probe(d, NOW, "manual-x", dry_run=dry_run)


async def stored_meta(store, run_id="manual-x") -> RunMeta:
    return RunMeta.model_validate(json.loads(store.raw(f"RUN#{run_id}", "META")["body"]))


async def test_a_kind_runs_under_its_own_lock_and_releases_it():
    d = deps()
    taken = []
    real = d.store.acquire_lock

    async def spy(name, owner, ttl_s, now):
        taken.append((name, owner, ttl_s, now))
        return await real(name, owner, ttl_s, now)

    d.store.acquire_lock = spy
    outcome = await run_locked(probe(d), force=False)
    assert (outcome.status, outcome.exit_code) == ("ok", EXIT_OK)
    assert taken == [("probe", "manual-x", 100.0 + LOCK_SPARE_S, NOW)]
    assert ("LOCK#probe", "LOCK") not in d.store.keys


async def test_the_cost_goes_in_before_the_write_and_the_alert_comes_last():
    d = deps()
    run = probe(d)
    await run_locked(run, force=True)
    assert d.store.order == ["meta running", "cost 0.0000", "write", "meta ok"]
    assert d.alerts.sent == [("probe_done", f"Probe {DAY}: ok", "probe done")]
    assert run.seen_force is True
    meta = await stored_meta(d.store)
    assert (meta.kind, meta.models, meta.notes) == ("manual", ("model-a",), ("one note",))


async def test_a_held_lock_exits_2_and_writes_nothing():
    d = deps()
    await d.store.acquire_lock("probe", "someone-else", 3600, NOW)
    outcome = await run_locked(probe(d), force=False)
    assert (outcome.status, outcome.exit_code) == ("locked", EXIT_LOCKED)
    assert d.store.order == [] and d.alerts.sent == []


async def test_another_kinds_lock_does_not_stop_it():
    d = deps()
    await d.store.acquire_lock("premarket", "someone-else", 3600, NOW)
    assert (await run_locked(probe(d), force=False)).status == "ok"


async def test_a_failure_records_meta_failed_alerts_with_the_kind_and_frees_the_lock():
    d = deps()
    run = probe(d)
    run.explode = RuntimeError("boom with token d1c2b3a4e5f6a7b8c9d0e1f2")
    outcome = await run_locked(run, force=False)
    assert (outcome.status, outcome.exit_code) == ("failed", EXIT_FAILED)
    meta = await stored_meta(d.store)
    assert meta.status is RunStatus.FAILED
    assert meta.error.startswith("work: RuntimeError: boom")
    assert "d1c2b3a4e5f6a7b8c9d0e1f2" not in meta.error
    ((event, subject, message),) = d.alerts.sent
    assert (event, subject) == ("research_run_failed", f"Probe {DAY}: failed")
    assert message.startswith(f"traider research manual {DAY} failed: work: RuntimeError")
    assert ("LOCK#probe", "LOCK") not in d.store.keys


async def test_a_dry_run_takes_no_lock_writes_nothing_and_runs_forced():
    d = deps()
    await d.store.acquire_lock("probe", "someone-else", 3600, NOW)
    run = probe(d, dry_run=True)
    outcome = await run_locked(run, force=False)
    assert outcome.status == "ok" and run.seen_force is True
    assert d.store.order == [] and d.alerts.sent == []


async def test_the_time_box_follows_the_kinds_own_limit():
    loop = asyncio.get_running_loop()
    run = probe(deps())
    assert run.max_run_s == 100.0
    box = run.box - loop.time()
    assert 100 + LOCK_SPARE_S - RUN_BOX_MARGIN_S - 1 < box <= 100 + LOCK_SPARE_S - RUN_BOX_MARGIN_S


async def test_a_run_past_its_box_fails_with_its_own_limit_in_the_error(monkeypatch):
    class Slow(Probe):
        async def _execute(self, *, force):
            await self.begin(Decimal("0.50"))
            await asyncio.Event().wait()
            raise AssertionError("never")

    monkeypatch.setattr(
        run_module, "_run_deadline", lambda max_run_s: asyncio.get_running_loop().time() + 0.2
    )
    d = deps()
    slow = Slow(d, NOW, "manual-x", dry_run=False)
    outcome = await asyncio.wait_for(run_locked(slow, force=False), 5)
    assert outcome.status == "failed"
    assert outcome.meta.error == "start: RunDeadline: the run did not finish within 640s"


@pytest.mark.parametrize(
    ("status", "found"),
    [("ok", "manual-b"), ("partial", "manual-b"), ("failed", None), ("running", None)],
)
async def test_done_today_finds_only_a_finished_run_of_this_kind(status, found):
    d = deps()
    for run_id, kind, st in (("manual-b", "manual", status), ("premarket-a", "premarket", "ok")):
        await d.store.put_meta(
            RunMeta(
                run_id=run_id, kind=kind, status=st, started_at=NOW, trading_day=date(2026, 10, 9)
            )
        )
    done = await probe(d).done_today()
    assert (done.run_id if done else None) == found
