# A2: Research picks, dynamic universe, position ledger — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The bot trades only what research has picked. It reads ranked picks and a day posture from a research table, keeps a universe of symbols that changes during the session, records which positions it opened itself, and enforces new entry rules: no pick, wrong side, stand aside, reduced size, intraday/swing budgets, and intraday flatten.

**Architecture:**
- New `traider.research` package:
  - pydantic models (`Pick`, `Posture`, `RunMeta`);
  - a store for runs, picks and posture (memory and DynamoDB);
  - a `ResearchSource` that polls the store and exposes a `ResearchView` of live picks and today's posture, failing closed when the data is stale.
- A pure `compute_universe` function decides which equity symbols the engine, feed and strategy work with.
- The engine gains three optional collaborators: the research source, an `on_universe` callback for the feed, and the position ledger in the state store. Research rules are passed to the risk manager as a `ResearchGate`, so a buggy strategy cannot get around them.
- With no research table configured, everything behaves exactly as in A1.

**Tech Stack:** Python 3.13, pydantic 2, boto3 DynamoDB, aiohttp WebSocket (Schwab stream), moto, Pulumi (Python).

**Spec:** `docs/superpowers/specs/2026-10-09-research-driven-trading-design.md`, sections A.3–A.12. Settings were done in plan A1 and are on the base branch.

**Decisions recorded here (and added to the spec in the docs task):**
- **Research is opt-in per stack.** Set `traider:research: true` in Pulumi. Until the research jobs exist (sub-project C), a stack with research on would sit out every day, because there is no posture and that means stand aside. So it stays off by default.
- **The ledger is only used when research is on.** Without research, held positions in pinned symbols are the bot's, as today.
- **`pinned_symbols` applies live.** The universe now changes while the bot runs, so a pinned symbol that is dropped stays in the universe while it is held or has an order working.
- **Not every rule applies to pinned symbols.** They are exempt from `no_pick` and `pick_side`, but not from `posture`, `horizon_budget` or `intraday_closing`.
- **Posture only blocks or shrinks entries.** Exits are never blocked by research state. The only research-related rule that also blocks sells is `foreign_holding`: the bot never touches a position it did not open.

## Global Constraints

- Python `>=3.13,<3.14`. Run everything with `uv run` from the repo root (bot) or from `infra/` (infra).
- These must stay green: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest` in both the root and `infra/`.
- Tests never touch real AWS or real Schwab. Use moto, the fake Schwab server and the in-memory stores.
- **Fail closed.** Any of these means no new entries: no research read yet, research stale beyond `research.max_stale_s`, no posture for today, a pick from a run that is not `ok`, or an unwritable ledger. **Exits never depend on research.**
- **The broker is the source of truth.** Never resend an order when its outcome is in doubt.
- Every safety guard gets a test that fails when the guard is removed on purpose. Each task names the breaks to perform.
- Nothing secret goes in research data, logs, alerts or events.
- Without a research table, behaviour is exactly as before. The existing ~1050 bot tests must pass unchanged, except where a task says otherwise.
- Conventional Commits with short subjects. End each commit message with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Xh7zur22CRYEGaDoTB5nkV
  ```
- Work on branch `feat/research-universe`. It is stacked on `feat/versioned-settings`, which is PR #4. Never push to `main`.
- `README.md` and `docs/runbook.md` must stay true. `tests/unit/test_docs.py` enforces the runbook event table (every `self._event("...")` in `engine.py`), the risk-code examples and the `traider <command>` names.

## File structure

| File | Responsibility |
|---|---|
| `src/traider/config.py` | `ResearchSettings`, `Config.research`, `Config.research_table`, symbols optional with research |
| `src/traider/settings.py` | `Settings.research`, empty `pinned_symbols` allowed, `pinned_symbols` no longer restart-only (Task 8) |
| `src/traider/settings_store.py` | `LiveSettings(require_pinned=...)` |
| `src/traider/research/__init__.py` (new) | package docstring |
| `src/traider/research/models.py` (new) | `Pick`, `Posture`, `RunMeta`, enums |
| `src/traider/research/store.py` (new) | `ResearchStore`, `DayResearch`, `MemoryResearchStore`, `DynamoResearchStore` |
| `src/traider/research/source.py` (new) | `ResearchSource`, `ResearchView`, `ResearchUpdate` |
| `src/traider/state/base.py`, `memory.py`, `dynamo.py` | `LedgerEntry` and ledger methods |
| `src/traider/universe.py` (new) | `root_symbol`, `compute_universe` |
| `src/traider/strategy/base.py`, `sma_cross.py` | `on_universe`, `ctx.picks`, `ctx.posture`, lazy history, `require_pick` |
| `src/traider/risk.py` | `ResearchGate`, new codes, reduced caps, horizon budget |
| `src/traider/schwab/stream.py`, `src/traider/feed.py` | changing symbol set, resubscribe, warm-up of new symbols |
| `src/traider/engine.py` | universe, research refresh and gate, ledger, foreign holdings, intraday flatten |
| `src/traider/app.py` | wiring |
| `src/traider/cli.py` | `traider research seed / show`, `backtest --picks` |
| `src/traider/backtest.py` | picks replay |
| `infra/*` | research table, access, env, opt-in flag |
| `README.md`, `docs/runbook.md`, `infra/Pulumi.example.yaml`, spec | docs |

---

### Task 1: Research models and research settings

**Files:**
- Create: `src/traider/research/__init__.py`, `src/traider/research/models.py`
- Modify: `src/traider/config.py`, `src/traider/settings.py`
- Test: `tests/unit/test_research_models.py`, `tests/unit/test_config.py`, `tests/unit/test_settings.py`

**Interfaces:**
- Produces:
  - Enums (`StrEnum`): `PickSide` (`long`, `bearish`), `Horizon` (`intraday`, `swing`), `PostureLevel` (`trade`, `reduced`, `stand_aside`), `RunStatus` (`running`, `ok`, `partial`, `failed`).
  - `RunKind = Literal["premarket", "intraday", "weekly", "monthly", "earnings_watch", "scorecard", "manual", "backtest"]`
  - `Pick`, `Posture`, `RunMeta`: frozen pydantic models with `extra="forbid"`. Fields are as in the code below.
  - `ResearchSettings` (in `config.py`), with these fields and defaults: `poll_s=60.0`, `max_stale_s=600.0`, `min_score=60`, `max_symbols=25`, `accept_partial_runs=False`, `intraday_share=Decimal("0.5")`, `reduced_factor=Decimal("0.5")`, `intraday_flatten_min=15`, `swing_lookback_days=10`.
  - New `Config` fields:
    - `research: ResearchSettings`, from the env var `TRAIDER_RESEARCH` (JSON).
    - `research_table: str | None`.
    - `symbols` may be empty when `research_table` is set.
  - `Settings.research: ResearchSettings`. `Settings.pinned_symbols` may be empty. `Settings.from_config` copies `research`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_research_models.py
"""The shapes research writes and the bot reads. Anything else is rejected."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from traider.research.models import (
    Horizon,
    Pick,
    PickSide,
    Posture,
    PostureLevel,
    RunMeta,
    RunStatus,
)

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def pick(**overrides) -> Pick:
    fields = {
        "run_id": "premarket-20261009T120000Z-ab12",
        "rank": 1,
        "symbol": "NVDA",
        "side": "long",
        "horizon": "intraday",
        "score": 80,
        "pre_score": 70,
        "thesis": "gap up on earnings, holding VWAP",
        "invalidation": "120.50",
        "expires_at": "2026-10-09T20:00:00+00:00",
    }
    return Pick.model_validate(fields | overrides)


def test_a_valid_pick_parses_with_typed_fields():
    p = pick(earnings_date="2026-10-15", features={"gap_pct": 4.2})
    assert p.side is PickSide.LONG
    assert p.horizon is Horizon.INTRADAY
    assert p.invalidation == Decimal("120.50")
    assert p.earnings_date == date(2026, 10, 15)
    assert p.features == {"gap_pct": 4.2}


@pytest.mark.parametrize(
    "bad",
    [
        {"score": 101},
        {"score": -1},
        {"rank": 0},
        {"side": "short"},
        {"horizon": "weekly"},
        {"invalidation": "0"},
        {"symbol": "NV DA"},
        {"thesis": "x" * 2001},
        {"expires_at": "2026-10-09T20:00:00"},  # no timezone
        {"surprise": True},  # unknown field
    ],
)
def test_bad_picks_are_rejected(bad):
    with pytest.raises(ValidationError):
        pick(**bad)


def test_posture_and_run_meta_parse():
    posture = Posture.model_validate(
        {"level": "stand_aside", "reasons": ["CPI at 08:30"], "run_id": "r1", "at": T0.isoformat()}
    )
    assert posture.level is PostureLevel.STAND_ASIDE
    meta = RunMeta.model_validate(
        {
            "run_id": "r1",
            "kind": "premarket",
            "status": "ok",
            "started_at": T0.isoformat(),
            "finished_at": T0.isoformat(),
            "trading_day": "2026-10-09",
        }
    )
    assert meta.status is RunStatus.OK
    with pytest.raises(ValidationError):
        RunMeta.model_validate(meta.model_dump(mode="json") | {"kind": "hourly"})
```

Append to `tests/unit/test_config.py`:

```python
def test_research_table_makes_symbols_optional():
    cfg = Config.from_env({"TRAIDER_RESEARCH_TABLE": "traider-dev-research"})
    assert cfg.symbols == ()
    assert cfg.research_table == "traider-dev-research"


def test_without_research_symbols_are_still_required():
    with pytest.raises(ConfigError, match="TRAIDER_SYMBOLS"):
        Config.from_env({})
    with pytest.raises(ValidationError, match="at least one symbol"):
        Config(symbols=())


def test_research_settings_come_from_json():
    cfg = Config.from_env({**BASE, "TRAIDER_RESEARCH": '{"min_score": 75, "intraday_share": 0.3}'})
    assert cfg.research.min_score == 75
    assert str(cfg.research.intraday_share) == "0.3"


@pytest.mark.parametrize(
    "bad", ['{"max_symbols": 26}', '{"intraday_share": 1.5}', '{"reduced_factor": 0}', '{"x": 1}']
)
def test_bad_research_settings_are_rejected(bad):
    with pytest.raises(ConfigError):
        Config.from_env({**BASE, "TRAIDER_RESEARCH": bad})
```

(Add `from pydantic import ValidationError` to the imports of `test_config.py` if it is not there.)

Append to `tests/unit/test_settings.py`:

```python
def test_settings_carry_research_settings_and_allow_no_pinned_symbols():
    config = Config(symbols=(), research_table="r", research={"min_score": 70})
    settings = Settings.from_config(config)
    assert settings.pinned_symbols == ()
    assert settings.research.min_score == 70
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_research_models.py tests/unit/test_config.py tests/unit/test_settings.py -q`
Expected: FAIL (`ModuleNotFoundError: traider.research`, and the new config tests fail).

- [ ] **Step 3: Implement**

`src/traider/research/__init__.py`:

```python
"""Research: ranked picks and the day's posture, written by the research jobs and read
by the bot. The bot treats all of it as data: it can narrow what is traded and make the
bot more cautious, never bypass a risk limit."""
```

`src/traider/research/models.py`:

```python
"""What research writes: runs, ranked picks and the day's posture.

The models are strict (unknown fields and out-of-range values are errors) because the
writer is partly a language model and the reader trades real money.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from traider.config import check_symbols

Score = Annotated[int, Field(ge=0, le=100)]
RunKind = Literal[
    "premarket",
    "intraday",
    "weekly",
    "monthly",
    "earnings_watch",
    "scorecard",
    "manual",
    "backtest",
]


class PickSide(StrEnum):
    LONG = "long"
    BEARISH = "bearish"  # traded with long puts only


class Horizon(StrEnum):
    INTRADAY = "intraday"  # flat by the close
    SWING = "swing"  # may be held overnight


class PostureLevel(StrEnum):
    TRADE = "trade"
    REDUCED = "reduced"
    STAND_ASIDE = "stand_aside"


class RunStatus(StrEnum):
    RUNNING = "running"
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must carry a timezone")
    return value


class Pick(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, max_length=100)
    rank: int = Field(ge=1, le=999)
    symbol: str
    side: PickSide
    horizon: Horizon
    score: Score
    pre_score: Score
    thesis: str = Field(max_length=2000)
    invalidation: Decimal = Field(gt=0)
    earnings_date: date | None = None
    expires_at: datetime
    features: dict[str, float] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def _symbol_ok(cls, value: str) -> str:
        return check_symbols((value,))[0]

    @field_validator("expires_at")
    @classmethod
    def _expires_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class Posture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    level: PostureLevel
    reasons: tuple[Annotated[str, Field(max_length=500)], ...] = ()
    run_id: str = Field(min_length=1, max_length=100)
    at: datetime
    metrics: dict[str, float] = Field(default_factory=dict)

    @field_validator("at")
    @classmethod
    def _at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class RunMeta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, max_length=100)
    kind: RunKind
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None = None
    trading_day: date
    models: tuple[str, ...] = ()
    cost_usd: Decimal = Field(default=Decimal(0), ge=0)
    s3_prefix: str = ""
    error: str = Field(default="", max_length=2000)

    @field_validator("started_at")
    @classmethod
    def _started_aware(cls, value: datetime) -> datetime:
        return _aware(value)
```

`src/traider/config.py`:

- Add `ResearchSettings` after `RiskLimits`:

```python
class ResearchSettings(BaseModel):
    """How the bot uses research. Only matters when a research table is configured."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    poll_s: Annotated[float, Field(ge=10, le=600)] = 60.0
    # A failed read leaves the last snapshot in force this long; after that, no entries.
    max_stale_s: Annotated[float, Field(ge=60, le=3600)] = 600.0
    min_score: Annotated[int, Field(ge=0, le=100)] = 60
    max_symbols: Annotated[int, Field(ge=1, le=25)] = 25
    accept_partial_runs: bool = False
    # Share of max_total_exposure_usd that intraday positions may use; swing gets the rest.
    intraday_share: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.5")
    # On a "reduced" day, order and position caps are multiplied by this.
    reduced_factor: Annotated[Decimal, Field(gt=0, le=1)] = Decimal("0.5")
    # Intraday positions are sold this many minutes before the close.
    intraday_flatten_min: Annotated[int, Field(ge=1, le=120)] = 15
    # How many earlier trading days to look back for swing picks that have not expired.
    swing_lookback_days: Annotated[int, Field(ge=1, le=30)] = 10
```

- In `Config`:
  - Change the field to `symbols: tuple[str, ...] = ()`.
  - Add `research: ResearchSettings = Field(default_factory=ResearchSettings)` after `risk`.
  - Add `research_table: str | None = None` under "Where things live".
  - Make `_symbols_ok` call `check_symbols(value, allow_empty=True)`.
  - Add this model validator:

```python
    @model_validator(mode="after")
    def _something_to_trade(self) -> Self:
        if not self.symbols and not self.research_table:
            raise ValueError("at least one symbol is required unless a research table is set")
        return self
```

- In `from_env`, replace the `"symbols" not in values` check with:

```python
        if "symbols" not in values and "research_table" not in values:
            raise ConfigError(
                f"{PREFIX}SYMBOLS is required (or {PREFIX}RESEARCH_TABLE), "
                f"for example {PREFIX}SYMBOLS=SPY,QQQ"
            )
```

- Set `_JSON_FIELDS = {"strategy_params", "risk", "research"}`.

`src/traider/settings.py`:
- Import `ResearchSettings` from `traider.config`.
- Add `research: ResearchSettings = Field(default_factory=ResearchSettings)` to `Settings`.
- Change the `pinned_symbols` validator to `check_symbols(value, allow_empty=True)` and give the field the default `= ()`. Update the comment: with no research, `LiveSettings` refuses an empty list (Task 11).
- Add `research=config.research` in `from_config`.
- **Update the existing test that required a pinned symbol** (`grep -n "pinned" tests/unit/test_settings.py`). It now asserts that `Settings` accepts an empty list. Task 11 adds the guard that matters.

- [ ] **Step 4: Run tests, then the full suite**

Run: `uv run pytest tests/unit/test_research_models.py tests/unit/test_config.py tests/unit/test_settings.py -q && uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Break on purpose**
  1. Remove `_something_to_trade`. Expected: `test_without_research_symbols_are_still_required` FAILS. Restore it.
  2. In `Pick`, remove `_expires_aware`. Expected: the naive-`expires_at` case FAILS. Restore it.

- [ ] **Step 6: Lint, types, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add src/traider/research src/traider/config.py src/traider/settings.py tests/unit
git commit -m "feat(research): pick, posture and run models; research settings"
```

---

### Task 2: Research store (memory and DynamoDB)

**Files:**
- Create: `src/traider/research/store.py`
- Test: `tests/unit/test_research_store.py`

**Interfaces:**
- Consumes: Task 1 models.
- Produces:
  - `@dataclass(frozen=True) class DayResearch: day: str; picks: tuple[Pick, ...]; postures: tuple[Posture, ...]; runs: Mapping[str, RunMeta]; invalid: int`
  - `class ResearchStore(Protocol)`:
    - `async write_run(meta: RunMeta, picks: Sequence[Pick], posture: Posture | None) -> None`: writes the picks and the posture first, then the run's meta last, so a reader never sees an `ok` run without its picks. Picks and posture are stored under `DAY#<meta.trading_day>`.
    - `async day(day: str) -> DayResearch`: every pick and posture stored under that day, plus the meta of every run they name. Items that don't parse are counted in `invalid` and left out. A run whose meta is missing or invalid is absent from `runs`.
  - `MemoryResearchStore()` with test helper `put_raw(pk: str, sk: str, body: str)`.
  - `DynamoResearchStore(table)`.
  - Item layout: `pk`, `sk`, `body` (a JSON string of `model_dump(mode="json")`). Picks also carry `gsi1pk = f"SYM#{symbol}"` and `gsi1sk = f"{day}#{run_id}"`. Keys:
    - `RUN#<run_id>` / `META`
    - `DAY#<day>` / `PICK#<run_id>#<rank:03d>`
    - `DAY#<day>` / `POSTURE#<at isoformat>`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_research_store.py
"""One contract, two implementations: in-memory and DynamoDB (through moto)."""

from datetime import UTC, date, datetime

import boto3
import pytest
from moto import mock_aws

from traider.research.models import Pick, Posture, RunMeta
from traider.research.store import DynamoResearchStore, MemoryResearchStore

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
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


async def test_a_run_with_unreadable_meta_is_absent(store):
    await store.write_run(meta(), [pick()], None)
    put_raw(store, "RUN#r1", "META", '{"run_id": "r1"}')
    day = await store.day(DAY)
    assert "r1" not in day.runs
    assert day.invalid == 1


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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_research_store.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `src/traider/research/store.py`**

```python
"""Where research lives: one table, written by the research jobs, read by the bot.

    RUN#<run_id>  / META                       how the run went (status, cost, ...)
    DAY#<date>    / PICK#<run_id>#<rank:03d>   one ranked pick
    DAY#<date>    / POSTURE#<iso time>         the day's posture as of that time

A run's META is written last, so a run that says ``ok`` has all its picks in place.
Anything that does not parse is skipped and counted, never trusted.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from traider.research.models import Pick, Posture, RunMeta


@dataclass(frozen=True, slots=True)
class DayResearch:
    day: str
    picks: tuple[Pick, ...] = ()
    postures: tuple[Posture, ...] = ()
    runs: Mapping[str, RunMeta] = field(default_factory=dict)
    invalid: int = 0


class ResearchStore(Protocol):
    async def write_run(
        self, meta: RunMeta, picks: Sequence[Pick], posture: Posture | None
    ) -> None: ...

    async def day(self, day: str) -> DayResearch: ...


def _items_for(
    meta: RunMeta, picks: Sequence[Pick], posture: Posture | None
) -> list[dict[str, Any]]:
    day = meta.trading_day.isoformat()
    items: list[dict[str, Any]] = [
        {
            "pk": f"DAY#{day}",
            "sk": f"PICK#{p.run_id}#{p.rank:03d}",
            "gsi1pk": f"SYM#{p.symbol}",
            "gsi1sk": f"{day}#{p.run_id}",
            "body": json.dumps(p.model_dump(mode="json")),
        }
        for p in picks
    ]
    if posture is not None:
        items.append(
            {
                "pk": f"DAY#{day}",
                "sk": f"POSTURE#{posture.at.isoformat()}",
                "body": json.dumps(posture.model_dump(mode="json")),
            }
        )
    items.append(
        {
            "pk": f"RUN#{meta.run_id}",
            "sk": "META",
            "body": json.dumps(meta.model_dump(mode="json")),
        }
    )  # last, on purpose
    return items


def _parse_day(
    day: str,
    items: Sequence[Mapping[str, Any]],
    metas: Mapping[str, Mapping[str, Any] | None],
) -> DayResearch:
    picks: list[Pick] = []
    postures: list[Posture] = []
    invalid = 0
    for item in sorted(items, key=lambda i: str(i["sk"])):
        sk = str(item["sk"])
        try:
            body = json.loads(str(item["body"]))
            if sk.startswith("PICK#"):
                picks.append(Pick.model_validate(body))
            elif sk.startswith("POSTURE#"):
                postures.append(Posture.model_validate(body))
        except Exception:
            invalid += 1
    runs: dict[str, RunMeta] = {}
    for run_id, item in metas.items():
        if item is None:
            continue
        try:
            runs[run_id] = RunMeta.model_validate(json.loads(str(item["body"])))
        except Exception:
            invalid += 1
    return DayResearch(day, tuple(picks), tuple(postures), runs, invalid)


def _run_ids(items: Sequence[Mapping[str, Any]]) -> set[str]:
    ids = set()
    for item in items:
        sk = str(item["sk"])
        if sk.startswith("PICK#"):
            ids.add(sk.split("#")[1])
        elif sk.startswith("POSTURE#"):
            try:
                ids.add(str(json.loads(str(item["body"]))["run_id"]))
            except Exception:
                continue
    return ids


class MemoryResearchStore:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}

    def put_raw(self, pk: str, sk: str, body: str) -> None:
        self._items[(pk, sk)] = {"pk": pk, "sk": sk, "body": body}

    async def write_run(
        self, meta: RunMeta, picks: Sequence[Pick], posture: Posture | None
    ) -> None:
        for item in _items_for(meta, picks, posture):
            self._items[(item["pk"], item["sk"])] = item

    async def day(self, day: str) -> DayResearch:
        items = [item for (pk, _), item in self._items.items() if pk == f"DAY#{day}"]
        metas = {run_id: self._items.get((f"RUN#{run_id}", "META")) for run_id in _run_ids(items)}
        return _parse_day(day, items, metas)


class DynamoResearchStore:
    def __init__(self, table: Any) -> None:
        self._table = table

    async def _call[T](self, fn: Callable[..., T], **kwargs: Any) -> T:
        return await asyncio.to_thread(fn, **kwargs)

    async def write_run(
        self, meta: RunMeta, picks: Sequence[Pick], posture: Posture | None
    ) -> None:
        for item in _items_for(meta, picks, posture):
            await self._call(self._table.put_item, Item=item)

    async def day(self, day: str) -> DayResearch:
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "pk = :pk",
            "ExpressionAttributeValues": {":pk": f"DAY#{day}"},
            "ConsistentRead": True,
        }
        while True:
            response = await self._call(self._table.query, **kwargs)
            items.extend(response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        metas: dict[str, Mapping[str, Any] | None] = {}
        for run_id in _run_ids(items):
            response = await self._call(
                self._table.get_item, Key={"pk": f"RUN#{run_id}", "sk": "META"}, ConsistentRead=True
            )
            metas[run_id] = response.get("Item")
        return _parse_day(day, items, metas)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_research_store.py -q`
Expected: all pass.

- [ ] **Step 5: Break on purpose**

In `_parse_day`, change `except Exception: invalid += 1` (the pick/posture loop) to `raise`. Expected: `test_unparseable_items_are_counted_and_left_out` FAILS. Restore.

- [ ] **Step 6: Lint, types, full suite, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
git add src/traider/research/store.py tests/unit/test_research_store.py
git commit -m "feat(research): research store for memory and DynamoDB"
```

---

### Task 3: `ResearchSource`: live picks, posture, staleness

**Files:**
- Create: `src/traider/research/source.py`
- Test: `tests/unit/test_research_source.py`

**Interfaces:**
- Consumes: `ResearchStore`, `DayResearch`, models, `ResearchSettings`, `trading_date`, `previous_weekday`.
- Produces:
  - `@dataclass(frozen=True) class ResearchView`:
    - Fields: `picks: Mapping[str, Pick]` (the best live pick per symbol), `posture: Posture | None`, `as_of: datetime | None`, `stale: bool`.
    - `level -> PostureLevel`: `STAND_ASIDE` when `stale` or `posture is None`, otherwise `posture.level`.
    - `pick(symbol: str, now: datetime) -> Pick | None`: `None` once `now >= expires_at`.
    - `live_picks(now) -> dict[str, Pick]`: the unexpired subset of `picks`.
  - `@dataclass(frozen=True) class ResearchUpdate: kind: Literal["stale", "restored"]; detail: str`
  - `class ResearchSource(store: ResearchStore, settings: Callable[[], ResearchSettings])`:
    - attribute `view: ResearchView`. Before the first successful read it is empty, with `stale=False` and posture `None`, so the level is `STAND_ASIDE`.
    - `async refresh(now) -> list[ResearchUpdate]`.

**Rules (all from spec A.4):**
- Read today's partition plus the previous `swing_lookback_days` weekdays.
- A pick is live when:
  - its run is `ok`, or `partial` with `accept_partial_runs` on;
  - `now < expires_at`;
  - `score >= min_score`;
  - it is from today, or it is a swing pick.
- Best pick per symbol is chosen by highest score, then the newest `run_id` (string order), then the lower rank.
- Posture is the latest `at` among today's postures whose run qualifies (same status rule). Otherwise `None`.
- On a failed read, keep the last view while `now - last_ok <= max_stale_s`. With no success yet, count from the first refresh call instead.
- After that window, the view becomes empty with `stale=True`, and one `stale` update is returned per outage.
- The first successful read after a stale period returns a `restored` update.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_research_source.py
"""What the bot believes research says, and what happens when it cannot tell."""

from datetime import UTC, date, datetime, timedelta

from traider.config import ResearchSettings
from traider.research.models import Pick, PostureLevel, RunMeta
from traider.research.source import ResearchSource
from traider.research.store import MemoryResearchStore
from traider.research.models import Posture

NOW = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)  # Friday 11:00 New York
TODAY, YESTERDAY = "2026-10-09", "2026-10-08"


def meta(run_id, status="ok", day=TODAY) -> RunMeta:
    return RunMeta(
        run_id=run_id,
        kind="premarket",
        status=status,
        started_at=NOW,
        finished_at=NOW,
        trading_day=date.fromisoformat(day),
    )


def pick(symbol, run_id="r1", rank=1, score=80, horizon="intraday", hours=5, side="long"):
    return Pick.model_validate(
        {
            "run_id": run_id,
            "rank": rank,
            "symbol": symbol,
            "side": side,
            "horizon": horizon,
            "score": score,
            "pre_score": score,
            "thesis": "t",
            "invalidation": "10",
            "expires_at": (NOW + timedelta(hours=hours)).isoformat(),
        }
    )


def posture(level="trade", run_id="r1", minutes=0):
    return Posture(level=level, run_id=run_id, at=NOW + timedelta(minutes=minutes))


class Flaky(MemoryResearchStore):
    def __init__(self):
        super().__init__()
        self.error = None

    async def day(self, day):
        if self.error is not None:
            raise self.error
        return await super().day(day)


def source(store, **settings):
    return ResearchSource(store, lambda: ResearchSettings(**settings))


async def test_before_any_read_nothing_is_live_and_the_bot_stands_aside():
    src = source(MemoryResearchStore())
    assert src.view.picks == {}
    assert src.view.level is PostureLevel.STAND_ASIDE


async def test_live_picks_and_todays_posture():
    store = MemoryResearchStore()
    await store.write_run(meta("r1"), [pick("NVDA"), pick("AMD", rank=2)], posture("reduced"))
    src = source(store)
    assert await src.refresh(NOW) == []
    assert set(src.view.live_picks(NOW)) == {"NVDA", "AMD"}
    assert src.view.level is PostureLevel.REDUCED


async def test_no_posture_today_means_stand_aside():
    store = MemoryResearchStore()
    await store.write_run(meta("r1"), [pick("NVDA")], None)
    src = source(store)
    await src.refresh(NOW)
    assert src.view.level is PostureLevel.STAND_ASIDE


async def test_picks_from_failed_running_or_partial_runs_are_ignored():
    store = MemoryResearchStore()
    await store.write_run(meta("r1", "failed"), [pick("A")], posture(run_id="r1"))
    await store.write_run(meta("r2", "running"), [pick("B", run_id="r2")], None)
    await store.write_run(meta("r3", "partial"), [pick("C", run_id="r3")], None)
    src = source(store)
    await src.refresh(NOW)
    assert src.view.live_picks(NOW) == {}
    assert src.view.level is PostureLevel.STAND_ASIDE
    partial_ok = source(store, accept_partial_runs=True)
    await partial_ok.refresh(NOW)
    assert set(partial_ok.view.live_picks(NOW)) == {"C"}


async def test_picks_without_a_run_record_are_ignored():
    store = MemoryResearchStore()
    await store.write_run(meta("r1"), [pick("A")], None)
    store._items.pop(("RUN#r1", "META"))
    src = source(store)
    await src.refresh(NOW)
    assert src.view.live_picks(NOW) == {}


async def test_low_scores_and_expired_picks_are_not_live():
    store = MemoryResearchStore()
    await store.write_run(meta("r1"), [pick("LOW", score=40), pick("OLD", hours=-1)], None)
    src = source(store, min_score=60)
    await src.refresh(NOW)
    assert src.view.live_picks(NOW) == {}


async def test_a_pick_expires_between_polls():
    store = MemoryResearchStore()
    await store.write_run(meta("r1"), [pick("NVDA", hours=1)], None)
    src = source(store)
    await src.refresh(NOW)
    assert src.view.pick("NVDA", NOW) is not None
    assert src.view.pick("NVDA", NOW + timedelta(hours=1)) is None


async def test_swing_picks_carry_over_from_earlier_days_and_intraday_ones_do_not():
    store = MemoryResearchStore()
    await store.write_run(
        meta("r0", day=YESTERDAY),
        [pick("SWING", run_id="r0", horizon="swing", hours=72), pick("DAY", run_id="r0", rank=2)],
        posture(run_id="r0", minutes=-1440),
    )
    src = source(store)
    await src.refresh(NOW)
    assert set(src.view.live_picks(NOW)) == {"SWING"}
    assert src.view.level is PostureLevel.STAND_ASIDE  # yesterday's posture does not count


async def test_the_best_pick_per_symbol_wins():
    store = MemoryResearchStore()
    await store.write_run(meta("r1"), [pick("NVDA", score=70)], None)
    await store.write_run(meta("r2"), [pick("NVDA", run_id="r2", score=90)], None)
    src = source(store)
    await src.refresh(NOW)
    assert src.view.pick("NVDA", NOW).run_id == "r2"


async def test_the_latest_qualifying_posture_wins():
    store = MemoryResearchStore()
    await store.write_run(meta("r1"), [], posture("trade", minutes=-60))
    await store.write_run(meta("r2"), [], posture("stand_aside", run_id="r2"))
    src = source(store)
    await src.refresh(NOW)
    assert src.view.level is PostureLevel.STAND_ASIDE


async def test_a_failed_read_keeps_the_last_view_until_it_is_too_old():
    store = Flaky()
    await store.write_run(meta("r1"), [pick("NVDA")], posture())
    src = source(store, max_stale_s=600)
    await src.refresh(NOW)
    store.error = RuntimeError("throttled")
    assert await src.refresh(NOW + timedelta(seconds=599)) == []
    assert "NVDA" in src.view.picks
    updates = await src.refresh(NOW + timedelta(seconds=601))
    assert [u.kind for u in updates] == ["stale"]
    assert src.view.picks == {}
    assert src.view.stale and src.view.level is PostureLevel.STAND_ASIDE
    assert await src.refresh(NOW + timedelta(seconds=700)) == []  # once per outage
    store.error = None
    restored = await src.refresh(NOW + timedelta(seconds=760))
    assert [u.kind for u in restored] == ["restored"]
    assert not src.view.stale and "NVDA" in src.view.picks


async def test_never_read_successfully_goes_stale_from_the_first_attempt():
    store = Flaky()
    store.error = RuntimeError("no table")
    src = source(store, max_stale_s=600)
    assert await src.refresh(NOW) == []
    updates = await src.refresh(NOW + timedelta(seconds=601))
    assert [u.kind for u in updates] == ["stale"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_research_source.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `src/traider/research/source.py`**

```python
"""The bot's view of research, refreshed by polling the research table.

Fail closed: until a read succeeds, and once reads have failed for longer than
``max_stale_s``, there are no live picks and the posture is "stand aside". Exits never
depend on any of this; it only narrows and shrinks entries.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from traider.config import ResearchSettings
from traider.research.models import Horizon, Pick, Posture, PostureLevel, RunMeta, RunStatus
from traider.research.store import DayResearch, ResearchStore
from traider.timeutil import previous_weekday, trading_date

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResearchView:
    picks: Mapping[str, Pick] = field(default_factory=dict)
    posture: Posture | None = None
    as_of: datetime | None = None
    stale: bool = False

    @property
    def level(self) -> PostureLevel:
        if self.stale or self.posture is None:
            return PostureLevel.STAND_ASIDE
        return self.posture.level

    def pick(self, symbol: str, now: datetime) -> Pick | None:
        found = self.picks.get(symbol)
        return found if found is not None and now < found.expires_at else None

    def live_picks(self, now: datetime) -> dict[str, Pick]:
        return {s: p for s, p in self.picks.items() if now < p.expires_at}


@dataclass(frozen=True, slots=True)
class ResearchUpdate:
    kind: Literal["stale", "restored"]
    detail: str


def _usable(run: RunMeta | None, settings: ResearchSettings) -> bool:
    if run is None:
        return False
    return run.status is RunStatus.OK or (
        run.status is RunStatus.PARTIAL and settings.accept_partial_runs
    )


class ResearchSource:
    def __init__(self, store: ResearchStore, settings: Callable[[], ResearchSettings]) -> None:
        self._store = store
        self._settings = settings
        self.view = ResearchView()
        self._last_ok: datetime | None = None
        self._first_try: datetime | None = None
        self._stale_reported = False

    def _days(self, now: datetime) -> list[date]:
        today = trading_date(now)
        days = [today]
        for _ in range(self._settings().swing_lookback_days):
            days.append(previous_weekday(days[-1]))
        return days

    async def refresh(self, now: datetime) -> list[ResearchUpdate]:
        settings = self._settings()
        if self._first_try is None:
            self._first_try = now
        try:
            results = [await self._store.day(d.isoformat()) for d in self._days(now)]
        except Exception as exc:
            return self._failed(now, settings, f"{type(exc).__name__}: {exc}")
        updates: list[ResearchUpdate] = []
        if self._stale_reported:
            updates.append(ResearchUpdate("restored", "research table readable again"))
        self._stale_reported = False
        self._last_ok = now
        self.view = self._build(now, results, settings)
        return updates

    def _failed(
        self, now: datetime, settings: ResearchSettings, detail: str
    ) -> list[ResearchUpdate]:
        since = self._last_ok or self._first_try or now
        if (now - since).total_seconds() <= settings.max_stale_s:
            log.warning("research read failed, keeping the last view: %s", detail)
            return []
        self.view = ResearchView(as_of=self.view.as_of, stale=True)
        if self._stale_reported:
            return []
        self._stale_reported = True
        return [ResearchUpdate("stale", detail)]

    def _build(
        self, now: datetime, results: list[DayResearch], settings: ResearchSettings
    ) -> ResearchView:
        today = trading_date(now).isoformat()
        runs: dict[str, RunMeta] = {}
        for result in results:
            runs.update(result.runs)
        best: dict[str, Pick] = {}
        for result in results:
            for p in result.picks:
                if not _usable(runs.get(p.run_id), settings):
                    continue
                if now >= p.expires_at or p.score < settings.min_score:
                    continue
                if result.day != today and p.horizon is not Horizon.SWING:
                    continue
                current = best.get(p.symbol)
                key = (p.score, p.run_id, -p.rank)
                if current is None or key > (current.score, current.run_id, -current.rank):
                    best[p.symbol] = p
        postures = (
            [p for p in results[0].postures if _usable(runs.get(p.run_id), settings)]
            if results and results[0].day == today
            else []
        )
        posture = max(postures, key=lambda p: p.at) if postures else None
        return ResearchView(picks=best, posture=posture, as_of=now, stale=False)
```

`results[0]` is always today's partition, because `_days` starts with today.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_research_source.py -q`
Expected: all pass.

- [ ] **Step 5: Break on purpose**
  1. Make `_usable` return `True` whenever `run is not None`. Expected: `test_picks_from_failed_running_or_partial_runs_are_ignored` FAILS. Restore.
  2. In `level`, drop the `self.stale` check. Expected: the stale test FAILS. Restore.
  3. In `_build`, drop the `result.day != today` check. Expected: the swing carry-over test FAILS. Restore.

- [ ] **Step 6: Lint, types, full suite, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
git add src/traider/research/source.py tests/unit/test_research_source.py
git commit -m "feat(research): research source with fail-closed staleness"
```

---

### Task 4: Position ledger in the state store

**Files:**
- Modify: `src/traider/state/base.py`, `src/traider/state/memory.py`, `src/traider/state/dynamo.py`
- Test: `tests/unit/test_state.py` (append; it already runs every test against both stores)

**Interfaces:**
- Produces:
  - In `state/base.py`, `@dataclass(frozen=True, slots=True) class LedgerEntry`:
    - `symbol: str`
    - `horizon: str` (`"intraday"` or `"swing"`)
    - `side: str` (`"long"` or `"bearish"`)
    - `opened_at: datetime`
    - `pick_run_id: str = ""`
    - `pick_rank: int = 0`
  - Helpers `ledger_to_dict(entry) -> dict` and `ledger_from_dict(data) -> LedgerEntry`.
  - Three new `StateStore` protocol methods:
    - `async ledger(self) -> dict[str, LedgerEntry]`
    - `async put_ledger(self, entry: LedgerEntry) -> None`
    - `async delete_ledger(self, symbol: str) -> None`
  - DynamoDB layout: pk `POS#<ns>`, sk `<symbol>`, `body` holding the JSON. Namespaced like everything else, so paper and live never mix.
  - Plain strings keep `state` free of any import from `research`.

- [ ] **Step 1: Write the failing tests (append to `tests/unit/test_state.py`)**

```python
from traider.state.base import LedgerEntry


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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_state.py -q`
Expected: the new tests FAIL with an ImportError for `LedgerEntry`.

- [ ] **Step 3: Implement**

`state/base.py`: update the module docstring's bullet list to add "* which positions the bot opened itself (the ledger)". Then add:

```python
@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """A position the bot opened itself, and how it was meant to be held."""

    symbol: str
    horizon: str  # "intraday" or "swing"
    side: str  # "long" or "bearish"
    opened_at: datetime
    pick_run_id: str = ""
    pick_rank: int = 0


def ledger_to_dict(entry: LedgerEntry) -> dict[str, Any]:
    return {
        "symbol": entry.symbol,
        "horizon": entry.horizon,
        "side": entry.side,
        "opened_at": entry.opened_at.isoformat(),
        "pick_run_id": entry.pick_run_id,
        "pick_rank": entry.pick_rank,
    }


def ledger_from_dict(data: Mapping[str, Any]) -> LedgerEntry:
    return LedgerEntry(
        symbol=str(data["symbol"]),
        horizon=str(data["horizon"]),
        side=str(data["side"]),
        opened_at=datetime.fromisoformat(str(data["opened_at"])),
        pick_run_id=str(data.get("pick_run_id", "")),
        pick_rank=int(data.get("pick_rank", 0)),
    )
```

Add the three methods to `StateStore`.

`state/memory.py`: in `__init__`, add `self._data.setdefault("ledger", {})`. Then add:

```python
async def ledger(self) -> dict[str, LedgerEntry]:
    stored: dict[str, dict[str, Any]] = self._data["ledger"]
    return {symbol: ledger_from_dict(item) for symbol, item in stored.items()}


async def put_ledger(self, entry: LedgerEntry) -> None:
    self._data["ledger"][entry.symbol] = ledger_to_dict(entry)


async def delete_ledger(self, symbol: str) -> None:
    self._data["ledger"].pop(symbol, None)
```

`state/dynamo.py`: add `POS#ns / <symbol>   positions this bot opened (the ledger)` to the layout docstring. Then add:

```python
async def ledger(self) -> dict[str, LedgerEntry]:
    items = await self._query(f"POS#{self._ns}")
    entries = [ledger_from_dict(json.loads(item["body"])) for item in items]
    return {entry.symbol: entry for entry in entries}


async def put_ledger(self, entry: LedgerEntry) -> None:
    await self._call(
        self._table.put_item,
        Item={
            "pk": f"POS#{self._ns}",
            "sk": entry.symbol,
            "body": json.dumps(ledger_to_dict(entry)),
        },
    )


async def delete_ledger(self, symbol: str) -> None:
    await self._call(self._table.delete_item, Key={"pk": f"POS#{self._ns}", "sk": symbol})
```

Import `LedgerEntry`, `ledger_from_dict` and `ledger_to_dict` where they are used.

- [ ] **Step 4: Run tests, then the full suite**

Run: `uv run pytest tests/unit/test_state.py -q && uv run pytest -q`
Expected: all pass. `FakeBroker` and the harness use `MemoryStateStore`, so they pick the methods up.

- [ ] **Step 5: Lint, types, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add src/traider/state tests/unit/test_state.py
git commit -m "feat(state): position ledger for memory and DynamoDB"
```

---

### Task 5: Universe function and strategy API

**Files:**
- Create: `src/traider/universe.py`
- Modify: `src/traider/strategy/base.py`, `src/traider/strategy/sma_cross.py`
- Test: `tests/unit/test_universe.py`, `tests/unit/test_strategy.py` (append)

**Interfaces:**
- Produces:
  - `root_symbol(symbol: str) -> str`: an option's underlying, or the symbol itself.
  - `compute_universe(*, required: Iterable[str], pinned: Sequence[str], picks: Sequence[tuple[str, int]], cap: int) -> tuple[str, ...]`:
    - `required` (roots of held, owned or busy positions) is always included.
    - Then `pinned`, in order.
    - Then picks by score (descending, ties by symbol) until the total reaches `cap`.
    - Required symbols come first, sorted. No duplicates.
  - `Strategy.on_universe(self, symbols: Sequence[str]) -> None` (default: `self.symbols = tuple(symbols)`).
  - `StrategyContext` gains `picks: Mapping[str, Pick] = field(default_factory=dict)` and `posture: PostureLevel | None = None` (`None` means research is off). Add `pick(symbol) -> Pick | None`.
  - `SmaCross`:
    - Creates per-symbol history lazily for symbols in `self.symbols`, so a symbol added later gets history.
    - Adds param `require_pick` (default `False`). When true, it enters only with a live `long` pick, and targets 0 on a held symbol that has no live pick.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_universe.py
"""Which symbols the bot watches: held and busy first, then pinned, then the best picks."""

from traider.universe import compute_universe, root_symbol


def test_root_symbol_of_an_option_is_its_underlying():
    assert root_symbol("SPY   261016C00500000") == "SPY"
    assert root_symbol("NVDA") == "NVDA"


def test_required_then_pinned_then_picks_by_score_up_to_the_cap():
    universe = compute_universe(
        required=["ZZZ"],
        pinned=["SPY"],
        picks=[("AMD", 70), ("NVDA", 90), ("TSLA", 80)],
        cap=4,
    )
    assert universe == ("ZZZ", "SPY", "NVDA", "TSLA")


def test_held_symbols_are_never_dropped_even_over_the_cap():
    universe = compute_universe(required=["A", "B", "C"], pinned=["D"], picks=[("E", 99)], cap=2)
    assert universe == ("A", "B", "C", "D")


def test_no_duplicates_and_ties_break_by_symbol():
    universe = compute_universe(
        required=["SPY"], pinned=["SPY", "QQQ"], picks=[("QQQ", 90), ("BB", 50), ("AA", 50)], cap=10
    )
    assert universe == ("SPY", "QQQ", "AA", "BB")
```

Append to `tests/unit/test_strategy.py`. Read the file first and reuse its imports and bar helpers. If no helper exists, use `tests.unit.helpers.make_bar`.

```python
from datetime import UTC, datetime, timedelta

from tests.unit.helpers import make_bar
from traider.research.models import Pick
from traider.strategy.base import StrategyContext
from traider.strategy.sma_cross import SmaCross

NOW = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)


def long_pick(symbol: str) -> Pick:
    return Pick.model_validate(
        {
            "run_id": "r1",
            "rank": 1,
            "symbol": symbol,
            "side": "long",
            "horizon": "intraday",
            "score": 80,
            "pre_score": 80,
            "thesis": "t",
            "invalidation": "1",
            "expires_at": (NOW + timedelta(hours=5)).isoformat(),
        }
    )


def rising(strategy, symbol, ctx, n=3):
    out = []
    for i in range(n):
        out = strategy.on_bar(make_bar(symbol, close=str(100 + i), minute=i), ctx)
    return out


def test_a_symbol_added_to_the_universe_gets_its_own_history():
    strategy = SmaCross(("SPY",), {"fast": 1, "slow": 2})
    ctx = StrategyContext(now=NOW, positions={})
    assert rising(strategy, "NVDA", ctx) == []  # not in the universe yet
    strategy.on_universe(("SPY", "NVDA"))
    assert rising(strategy, "NVDA", ctx)[0].symbol == "NVDA"


def test_require_pick_enters_only_with_a_live_long_pick():
    strategy = SmaCross(("NVDA",), {"fast": 1, "slow": 2, "require_pick": True})
    without = StrategyContext(now=NOW, positions={})
    assert rising(strategy, "NVDA", without) == []
    with_pick = StrategyContext(now=NOW, positions={}, picks={"NVDA": long_pick("NVDA")})
    assert rising(strategy, "NVDA", with_pick)[0].quantity > 0


def test_require_pick_sells_a_held_symbol_whose_pick_is_gone():
    strategy = SmaCross(("NVDA",), {"fast": 1, "slow": 2, "require_pick": True})
    held = StrategyContext(now=NOW, positions={"NVDA": 5})
    targets = rising(strategy, "NVDA", held)
    assert [(t.symbol, t.quantity) for t in targets] == [("NVDA", 0)]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_universe.py tests/unit/test_strategy.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

`src/traider/universe.py`:

```python
"""The universe: the equity symbols the bot watches right now.

Held, owned or busy symbols are always in it (their exits must keep working), then the
pinned symbols, then the best live research picks up to the cap. Options are tracked
through their underlying, which is why everything here works on root symbols.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from traider.options import is_option_symbol, parse_option_symbol


def root_symbol(symbol: str) -> str:
    return parse_option_symbol(symbol).underlying if is_option_symbol(symbol) else symbol


def compute_universe(
    *,
    required: Iterable[str],
    pinned: Sequence[str],
    picks: Sequence[tuple[str, int]],
    cap: int,
) -> tuple[str, ...]:
    out: list[str] = []
    for symbol in [*sorted(set(required)), *pinned]:
        if symbol not in out:
            out.append(symbol)
    for symbol, _ in sorted(picks, key=lambda p: (-p[1], p[0])):
        if len(out) >= cap:
            break
        if symbol not in out:
            out.append(symbol)
    return tuple(out)
```

`strategy/base.py`:
- Import `Pick, PostureLevel` from `traider.research.models`.
- Add to `StrategyContext`, after `chains`:

```python
# Live research picks by symbol, and today's posture. Empty and None when research is off.
picks: Mapping[str, Pick] = field(default_factory=dict)
posture: PostureLevel | None = None


def pick(self, symbol: str) -> Pick | None:
    return self.picks.get(symbol)
```

- Add to `Strategy`:

```python
    def on_universe(self, symbols: Sequence[str]) -> None:
        """The symbols the bot now watches. Bars only arrive for these. Optional."""
        self.symbols = tuple(symbols)
```

`strategy/sma_cross.py`:
- Make `_DEFAULTS` `{"fast": 5, "slow": 20, "position_usd": 500, "require_pick": False}`.
- Store `self.require_pick = bool(merged["require_pick"])`.
- Replace the eager `_closes` dict with `self._closes: dict[str, deque[Decimal]] = {}`.
- In `on_bar`:

```python
        closes = self._closes.get(bar.symbol)
        if closes is None:
            if bar.symbol not in self.symbols:
                return ()
            closes = self._closes[bar.symbol] = deque(maxlen=self.slow)
        ...
        held = ctx.position(bar.symbol)
        if self.require_pick:
            pick = ctx.pick(bar.symbol)
            if pick is None or pick.side is not PickSide.LONG:
                return (Target(bar.symbol, 0, "no live long pick"),) if held > 0 else ()
        if fast > slow:
            quantity = held if held > 0 else int(self.position_usd // bar.close)
            ...
```

(`PickSide` is imported from `traider.research.models`.) Keep the existing return paths otherwise. Make sure `held` is computed once, before the `require_pick` block. Update the module docstring to mention `require_pick`.

- [ ] **Step 4: Run tests, then the full suite**

Run: `uv run pytest tests/unit/test_universe.py tests/unit/test_strategy.py -q && uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Break on purpose**

Make `compute_universe` stop adding `required` once `cap` is reached. Expected: `test_held_symbols_are_never_dropped_even_over_the_cap` FAILS. Restore.

- [ ] **Step 6: Lint, types, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add src/traider/universe.py src/traider/strategy tests/unit
git commit -m "feat(strategy): universe function, picks in context, lazy history"
```

---

### Task 6: Research rules in the risk manager

**Files:**
- Modify: `src/traider/risk.py`
- Test: `tests/unit/test_risk.py` (append)

**Interfaces:**
- Produces:
  - In `risk.py`, `@dataclass(frozen=True, slots=True) class ResearchGate`:

    | Field | Type | Default |
    |---|---|---|
    | `pick_side` | `str \| None` | (required) |
    | `pinned` | `bool` | `False` |
    | `posture` | `str` | `"trade"` |
    | `foreign` | `bool` | `False` |
    | `horizon` | `str` | `"swing"` |
    | `horizon_exposure_usd` | `Decimal` | `Decimal(0)` |
    | `horizon_cap_usd` | `Decimal \| None` | `None` |
    | `cap_factor` | `Decimal` | `Decimal(1)` |
    | `intraday_closing` | `bool` | `False` |

  - `RiskContext` gains `research: ResearchGate | None = None` as its last field.
  - New rejection codes:
    - `foreign_holding`: entries and exits.
    - `posture`, `no_pick`, `pick_side`, `intraday_closing`, `horizon_budget`: entries only.
  - Entry caps `max_order_usd` and `max_position_usd` are multiplied by `cap_factor`.

**Rules:**
- `foreign`: reject everything for the symbol.
- Entries:
  - `posture == "stand_aside"` → `posture`.
  - Not pinned and `pick_side is None` → `no_pick`.
  - Not pinned and `pick_side == "long"` and the order is a put → `pick_side`.
  - Not pinned and `pick_side == "bearish"` and the order is not a put → `pick_side`.
  - `intraday_closing` → `intraday_closing`.
  - When `horizon_cap_usd` is set and `horizon_exposure_usd + notional > horizon_cap_usd` → `horizon_budget`. `notional` is the same value the existing caps use.

- [ ] **Step 1: Write the failing tests (append to `tests/unit/test_risk.py`)**

```python
from traider.risk import ResearchGate

PUT = "SPY   261016P00500000"
CALL = "SPY   261016C00500000"


def option_buy(symbol, price="1.00") -> OrderRequest:
    return OrderRequest(symbol, Side.BUY, 1, OrderType.LIMIT, Decimal(price))


def option_quote(symbol, bid="0.98", ask="1.00"):
    return replace(quote(bid, ask), symbol=symbol)


def gated(gate: ResearchGate, **overrides) -> RiskContext:
    return ctx(research=gate, **overrides)


OPTIONS = RiskLimits(allow_options=True, max_option_spread_bps=Decimal(1000))


def test_without_a_gate_nothing_changes():
    assert check(ctx()).allowed


def test_a_live_long_pick_allows_shares():
    assert check(gated(ResearchGate(pick_side="long"))).allowed


def test_no_pick_blocks_entries_but_not_exits():
    decision = check(gated(ResearchGate(pick_side=None)))
    assert decision.codes == {"no_pick"}
    assert check(gated(ResearchGate(pick_side=None), order=sell(), **holding())).allowed


def test_pinned_symbols_need_no_pick():
    assert check(gated(ResearchGate(pick_side=None, pinned=True))).allowed


def test_stand_aside_blocks_entries_even_on_pinned_symbols_but_not_exits():
    gate = ResearchGate(pick_side="long", pinned=True, posture="stand_aside")
    assert check(gated(gate)).codes == {"posture"}
    assert check(gated(gate, order=sell(), **holding())).allowed


def test_bearish_picks_are_traded_with_puts_only():
    assert check(gated(ResearchGate(pick_side="bearish"))).codes == {"pick_side"}
    call = gated(
        ResearchGate(pick_side="bearish"), order=option_buy(CALL), quote=option_quote(CALL)
    )
    assert check(call, OPTIONS).codes == {"pick_side"}
    put = gated(ResearchGate(pick_side="bearish"), order=option_buy(PUT), quote=option_quote(PUT))
    assert check(put, OPTIONS).allowed


def test_a_put_on_a_long_pick_is_refused():
    put = gated(ResearchGate(pick_side="long"), order=option_buy(PUT), quote=option_quote(PUT))
    assert check(put, OPTIONS).codes == {"pick_side"}


def test_a_foreign_holding_is_never_traded_sells_included():
    gate = ResearchGate(pick_side="long", foreign=True)
    assert "foreign_holding" in check(gated(gate)).codes
    assert check(gated(gate, order=sell(), **holding())).codes == {"foreign_holding"}


def test_reduced_days_shrink_the_order_and_position_caps():
    # 5 x 100.05 = 500.25: fine at the 500 cap x 1.1, refused at x 0.5.
    roomy = RiskLimits(max_order_usd=Decimal(600))
    order = buy(qty=5)
    assert check(gated(ResearchGate(pick_side="long"), order=order), roomy).allowed
    reduced = gated(ResearchGate(pick_side="long", cap_factor=Decimal("0.5")), order=order)
    assert check(reduced, roomy).codes >= {"max_order_usd"}


def test_the_horizon_budget_bounds_entries():
    gate = ResearchGate(
        pick_side="long",
        horizon="intraday",
        horizon_exposure_usd=Decimal(900),
        horizon_cap_usd=Decimal(1000),
    )
    assert check(gated(gate)).codes == {"horizon_budget"}  # 900 + 200.10 > 1000
    roomy = replace(gate, horizon_exposure_usd=Decimal(700))
    assert check(gated(roomy)).allowed


def test_no_intraday_entries_in_the_flatten_window():
    gate = ResearchGate(pick_side="long", horizon="intraday", intraday_closing=True)
    assert check(gated(gate)).codes == {"intraday_closing"}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_risk.py -q`
Expected: the new tests FAIL with an ImportError for `ResearchGate`.

- [ ] **Step 3: Implement in `src/traider/risk.py`**

Update the module docstring with this paragraph: "With research on, a `ResearchGate` adds the research rules: an entry needs a live pick on the right side, the day's posture can stop or shrink entries, intraday and swing positions have separate budgets, and a position the bot did not open is never touched, sells included."

Add the dataclass after `SessionView`:

```python
@dataclass(frozen=True, slots=True)
class ResearchGate:
    """What research says about one order's symbol. Built by the engine; None without research."""

    pick_side: str | None  # side of the live pick on the symbol (or its underlying); None: no pick
    pinned: bool = False  # pinned symbols need no pick
    posture: str = "trade"  # "trade" | "reduced" | "stand_aside"
    foreign: bool = False  # held, but not opened by the bot: never traded
    horizon: str = "swing"  # the budget this entry counts against
    horizon_exposure_usd: Decimal = Decimal(0)  # already held in that horizon
    horizon_cap_usd: Decimal | None = None  # None: no horizon budget
    cap_factor: Decimal = Decimal(1)  # order and position caps are multiplied by this
    intraday_closing: bool = False  # inside the window where intraday positions are sold
```

Add `research: ResearchGate | None = None` as the last `RiskContext` field.

In `check`, after `self._check_control(...)`:

```python
        if ctx.research is not None:
            self._check_research(ctx, is_entry, reject)
```

Add the method:

```python
    @staticmethod
    def _check_research(ctx: RiskContext, is_entry: bool, reject: _Reject) -> None:
        gate, symbol = ctx.research, ctx.order.symbol
        assert gate is not None
        if gate.foreign:
            reject("foreign_holding", f"{symbol} is held but the bot did not open it")
        if not is_entry:
            return
        if gate.posture == "stand_aside":
            reject("posture", "research says stand aside today")
        if not gate.pinned:
            put = is_option_symbol(symbol) and parse_option_symbol(symbol).right == "P"
            if gate.pick_side is None:
                reject("no_pick", f"no live research pick for {symbol}")
            elif gate.pick_side == "long" and put:
                reject("pick_side", "a put on a long pick")
            elif gate.pick_side == "bearish" and not put:
                reject("pick_side", "bearish picks are traded with long puts only")
        if gate.intraday_closing:
            reject("intraday_closing", "intraday positions are being closed for the day")
```

In `_check_entry_caps`, after `notional = ...`:

```python
        factor = ctx.research.cap_factor if ctx.research is not None else Decimal(1)
        max_order, max_position = limits.max_order_usd * factor, limits.max_position_usd * factor
```

Use `max_order` and `max_position` in the two existing cap checks. Keep the messages but print the effective cap. Then, after the total-exposure check:

```python
        gate = ctx.research
        if gate is not None and gate.horizon_cap_usd is not None:
            if gate.horizon_exposure_usd + notional > gate.horizon_cap_usd:
                reject(
                    "horizon_budget",
                    f"{gate.horizon} positions would be "
                    f"{gate.horizon_exposure_usd + notional:.2f}, budget {gate.horizon_cap_usd:.2f}",
                )
```

- [ ] **Step 4: Run tests, then the full suite**

Run: `uv run pytest tests/unit/test_risk.py -q && uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Break on purpose (one at a time, restore after each)**
  - Remove the `foreign` check → the foreign test FAILS.
  - Make `posture` a non-entry check, so it also blocks exits → the stand-aside test FAILS.
  - Drop the `factor` multiply → the reduced test FAILS.

- [ ] **Step 6: Lint, types, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add src/traider/risk.py tests/unit/test_risk.py
git commit -m "feat(risk): research gate: pick, side, posture, budgets, foreign holdings"
```

---

### Task 7: The feed and stream follow a changing symbol set

**Files:**
- Modify: `src/traider/schwab/stream.py`, `src/traider/feed.py`
- Test: `tests/unit/test_schwab_stream.py`, `tests/unit/test_feed.py` (append)

**Interfaces:**
- Produces:
  - `SchwabStream.set_symbols(symbols: Sequence[str]) -> None` (sync, safe to call any time).
    - If the set changed and the stream is connected, the open socket is closed and the stream reconnects at once, without backoff, and subscribes to the new set.
    - With no symbols, the stream does not connect. It waits until symbols arrive.
  - `Feed.set_symbols(symbols: Sequence[str]) -> None` (sync).
    - Updates the polled symbols and the stream.
    - Queues symbols it has not warmed up yet. The next `poll_once` fetches their recent history (the same lookback as the start-up warm-up, the last `warmup_bars` closed bars) and replays it as warm-up bars. A symbol whose history fails stays queued for the next poll.
  - `Feed._warmup_bars` is set from `run(warmup_bars=...)`. It is `0` before `run`, which means no warm-up is needed.
  - Polling skips the quotes call when there are no symbols.

- [ ] **Step 1: Write the failing tests**

Read the existing stream and feed tests first. The fake Schwab server records stream requests in `schwab.stream_requests`, and `make_feed` / `schwab_connected` exist in `test_feed.py`.

Append to `tests/unit/test_feed.py`:

```python
async def test_new_symbols_are_warmed_up_from_history_on_the_next_poll(schwab, client, signed_in):
    schwab.set_quote("SPY", 100, 100.02)
    schwab.set_quote("NVDA", 120, 120.02)
    schwab.candles["NVDA"] = [candle(MINUTE - timedelta(minutes=3 - i), 120 + i) for i in range(3)]
    async with make_feed(schwab, client, signed_in) as feed:
        feed._warmup_bars = 2
        feed.set_symbols(("SPY", "NVDA"))
        await feed.poll_once()
        warm = [(b.symbol, w) for b, w in feed.market.drain_bars() if b.symbol == "NVDA"]
        assert len(warm) >= 2 and warm[0][1] is True
        assert "NVDA" in feed._symbols


async def test_a_symbol_whose_history_fails_is_retried(schwab, client, signed_in):
    async with make_feed(schwab, client, signed_in) as feed:
        feed._warmup_bars = 2
        schwab.fail_next("pricehistory")
        feed.set_symbols(("SPY", "NVDA"))
        await feed.poll_once()
        assert "NVDA" in feed._pending_warmup
        await feed.poll_once()
        assert "NVDA" not in feed._pending_warmup


async def test_no_symbols_means_no_quote_calls(schwab, client, signed_in):
    async with make_feed(schwab, client, signed_in) as feed:
        feed.set_symbols(())
        before = len(schwab.requests)
        await feed.poll_once()
        assert not any("quotes" in r for r in schwab.requests[before:])


async def test_the_stream_resubscribes_when_the_symbols_change(schwab, client, signed_in):
    schwab.set_quote("SPY", 100, 100.02)
    async with make_feed(schwab, client, signed_in, feed="stream") as feed:
        task = asyncio.create_task(feed._stream.run())
        try:
            await schwab_connected(schwab)
            feed.set_symbols(("SPY", "NVDA"))
            await until(
                lambda: any(
                    r["command"] == "SUBS" and "NVDA" in r["parameters"]["keys"]
                    for r in schwab.stream_requests
                )
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
```

The names `schwab.fail_next` and `schwab.requests` are placeholders here. **Read `tests/fakes/schwab_server.py` first** and use its real ways to fail one price-history call and to see REST requests. If the fake has no way to fail one call, add a small one, for example a `fail_once: set[str]` of path fragments that returns HTTP 500 once. That is a test-only change.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_feed.py -q`
Expected: the new tests FAIL (`set_symbols` does not exist).

- [ ] **Step 3: Implement**

`schwab/stream.py`:
- In `__init__`, add `self._changed = asyncio.Event()`.
- Add the method:

```python
    def set_symbols(self, symbols: Sequence[str]) -> None:
        """Watch these symbols instead. A live connection is closed and reopened at once
        with the new subscription; the feed polls while that happens."""
        new = tuple(symbols)
        if new == self._symbols:
            return
        self._symbols = new
        self._changed.set()
```

- In `run`, at the top of the `while True:` loop:

```python
            if not self._symbols:
                await self._changed.wait()
                self._changed.clear()
                continue
            self._changed.clear()
```

  After the `try/except/finally` block, before the backoff sleep:

```python
            if self._changed.is_set():
                backoff = self._backoff_initial_s
                continue  # the symbols changed: reconnect now, no backoff
```

- In `_connect_and_read`, right after `self._connected = True` and the log line, wrap the read loop:

```python
            closer = asyncio.create_task(self._close_on_change(ws))
            try:
                async for message in ws:
                    ...  # existing body unchanged
            finally:
                closer.cancel()
```

- Add:

```python
    async def _close_on_change(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await self._changed.wait()
        await ws.close()
```

  `_changed` is cleared at the top of the next loop iteration, so the new connection does not close itself.

`feed.py`:
- In `__init__`, add `self._pending_warmup: dict[str, None] = {}` and `self._warmup_bars = 0`.
- In `run`, add `self._warmup_bars = warmup_bars` as the first line.
- Add the method:

```python
def set_symbols(self, symbols: Sequence[str]) -> None:
    """Follow the engine's universe. New symbols are warmed up from history on the
    next poll; the stream resubscribes."""
    new = tuple(symbols)
    for symbol in new:
        if symbol not in self._symbols:
            self._pending_warmup[symbol] = None
    self._symbols = new
    if self._stream is not None:
        self._stream.set_symbols(new)


async def _warm_pending(self, now: datetime) -> None:
    if not self._pending_warmup or self._warmup_bars <= 0:
        self._pending_warmup.clear()
        return
    for symbol in list(self._pending_warmup):
        try:
            raw = await self._client.price_history(symbol, now - self.WARMUP_LOOKBACK, now)
        except SchwabError as exc:
            self._complain(now, "warm-up history for %s not available yet: %s", symbol, exc)
            continue
        closed = [bar for bar in parse_candles(raw, symbol) if bar.start + _MINUTE <= now]
        for bar in closed[-self._warmup_bars :]:
            self._market.on_bar(bar, warmup=True)
        del self._pending_warmup[symbol]
```

- In `poll_once`, make the first lines `now = ...` then `await self._warm_pending(now)`, before the session check.
- Guard the share-quote poll: `if self._symbols:` around the `quotes` call.
- Import `Sequence` from `collections.abc`.

- [ ] **Step 4: Run tests, then the full suite**

Run: `uv run pytest tests/unit/test_feed.py tests/unit/test_schwab_stream.py -q && uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Break on purpose**

In `run`, remove the `if self._changed.is_set(): ... continue` block. Expected: the resubscribe test FAILS or times out. Restore.

- [ ] **Step 6: Lint, types, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add src/traider/schwab/stream.py src/traider/feed.py tests/
git commit -m "feat(feed): follow a changing symbol set with warm-up and resubscribe"
```

---

### Task 8: Engine universe (pinned symbols apply live)

**Files:**
- Modify: `src/traider/engine.py`, `src/traider/settings.py`, `tests/unit/engine_harness.py`
- Test: `tests/unit/test_engine_universe.py`, `tests/unit/test_settings.py`, `tests/unit/test_engine_settings.py` (adjust)

**Interfaces:**
- Consumes: `compute_universe`, `root_symbol`, `Strategy.on_universe`.
- Produces:
  - `Engine.__init__(..., on_universe: Callable[[tuple[str, ...]], None] | None = None)`, a new keyword placed after `settings`.
  - `Engine.universe -> tuple[str, ...]` property. It starts as `settings.pinned_symbols`.
  - `Engine._refresh_universe(now)`:
    - Called after an applied settings update, at the end of `_on_account`, and (in Task 9) after a research refresh.
    - `required` is the root symbols of: every symbol state that is busy or has a non-zero position in the latest account snapshot, for symbols in the current universe or options on them. Task 10 adds the ledger symbols.
    - `pinned` comes from the settings. `picks` is `[]` until Task 9.
    - `cap` is `self._settings.research.max_symbols`.
  - When the universe changes:
    - Add `_SymbolState`s for the added symbols.
    - Delete the states of dropped symbols, and of options on them, that are not busy (they are flat by construction). Call `market.unwatch` for dropped option contracts.
    - Call `strategy.on_universe(new)` inside `try`. An exception is treated like any other strategy error: set `_strategy_error` and alert `strategy_error`.
    - Call `on_universe(new)`.
    - Record the event `universe_changed` with `{"added": [...], "dropped": [...], "universe": [...]}`.
  - Replace `self._settings.pinned_symbols` with `self._universe` in `_is_our_option` and `_apply_target`. In `_apply_target`, a target for a universe symbol uses `self._symbols.setdefault(symbol, _SymbolState())`.
  - `settings.RESTART_FIELDS` no longer contains `pinned_symbols`. Remove `Engine._orphaned_by`, the stranded-holdings text in `_on_settings`, and their tests: held symbols now stay in the universe, so nothing is stranded. Keep `LiveSettings.pending`; it is still used for other restart-only fields. In `tests/unit/test_settings.py`, update the `RESTART_FIELDS` and `merge_live` tests so `pinned_symbols` is treated as live.
  - The harness exposes `on_universe` calls: `Harness.create(..., on_universe=None)`. By default it records calls in `harness.universe_calls: list[tuple[str, ...]]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_engine_universe.py
"""The engine's universe follows the pinned symbols live and never drops a held one."""

from decimal import Decimal

from tests.unit.engine_harness import Harness
from traider.settings_store import MemorySettingsStore


async def pin(h: Harness, store, symbols) -> None:
    current = await store.latest()
    await store.write(
        current.settings.model_copy(update={"pinned_symbols": tuple(symbols)}),
        expected_version=current.version,
        author="test",
        note="",
        now=h.clock.now(),
    )
    await h.tick(11)


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
```

In `tests/unit/test_engine_settings.py`, delete the tests about stranded holdings (`grep -n "orphan\|no longer manage" tests/unit/test_engine_settings.py`). Replace them with:

```python
async def test_changing_pinned_symbols_is_live_not_pending(tmp_path):
    store = MemorySettingsStore()
    h = await Harness.create(tmp_path, settings_store=store)
    current = await store.latest()
    await store.write(
        current.settings.model_copy(update={"pinned_symbols": ("SPY", "QQQ")}),
        expected_version=1,
        author="test",
        note="",
        now=h.clock.now(),
    )
    await h.tick(11)
    assert await h.events("settings_pending_restart") == []
    assert h.engine.settings.pinned_symbols == ("SPY", "QQQ")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_engine_universe.py tests/unit/test_engine_settings.py -q`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement**

`settings.py`: set `RESTART_FIELDS = frozenset({"strategy", "strategy_params", "option_chain_days", "option_chain_strikes"})`. Update its comment: pinned symbols now apply live through the engine's universe.

`engine.py`:
- Imports: `from traider.universe import compute_universe, root_symbol`.
- In `__init__`: store `self._on_universe = on_universe` and set `self._universe: tuple[str, ...] = self._settings.pinned_symbols`. Build `self._symbols` from `self._universe`.
- Add the property:

```python
    @property
    def universe(self) -> tuple[str, ...]:
        return self._universe
```

- Add the methods:

```python
def _required_symbols(self) -> set[str]:
    """Roots that must stay in the universe: anything with an order working or unsettled,
    and anything the bot holds."""
    required = {root_symbol(s) for s, st in self._symbols.items() if self._busy(st)}
    account = self._account
    if account is not None:
        for symbol, held in account.positions.items():
            root = root_symbol(symbol)
            if held.quantity != 0 and root in self._universe:
                required.add(root)
    return required


def _wanted_picks(self, now: datetime) -> list[tuple[str, int]]:
    return []  # research picks arrive in Task 9


async def _refresh_universe(self, now: datetime) -> None:
    new = compute_universe(
        required=self._required_symbols(),
        pinned=self._settings.pinned_symbols,
        picks=self._wanted_picks(now),
        cap=self._settings.research.max_symbols,
    )
    if new == self._universe:
        return
    old, self._universe = self._universe, new
    added = [s for s in new if s not in old]
    dropped = [s for s in old if s not in new]
    for symbol in added:
        self._symbols.setdefault(symbol, _SymbolState())
    for symbol, st in list(self._symbols.items()):
        if root_symbol(symbol) in dropped and not self._busy(st):
            del self._symbols[symbol]
            if is_option_symbol(symbol):
                self._market.unwatch(symbol)
    await self._event(
        "universe_changed", {"added": added, "dropped": dropped, "universe": list(new)}, now
    )
    try:
        self._strategy.on_universe(new)
    except Exception as exc:
        log.exception("strategy raised in on_universe")
        if self._strategy_error is None:
            self._strategy_error = f"{type(exc).__name__}: {exc}"
        await self._alerts.send(
            "strategy_error",
            "Strategy error",
            f"{self._strategy_error}. New entries are off until the bot is restarted. "
            "Exits still work.",
        )
    if self._on_universe is not None:
        self._on_universe(new)
```

- Call `await self._refresh_universe(now)`:
  - at the end of `_on_settings` when `update.kind == "applied"`;
  - at the end of `_on_account`.
- In `_is_our_option`, use `self._universe`.
- In `_apply_target`, the first branch becomes `if target.symbol in self._universe: st = self._symbols.setdefault(target.symbol, _SymbolState())`.
- Delete `_orphaned_by` and the stranded-holdings block in the `pending_restart` branch.

Harness: add `on_universe=None` to `create`. Then:

```python
        self.universe_calls: list[tuple[str, ...]] = []
        ...
            on_universe=on_universe or self.universe_calls.append,
```

passed to `Engine(...)`.

- [ ] **Step 4: Run tests, then the full suite**

Run: `uv run pytest tests/unit/test_engine_universe.py tests/unit/test_engine_settings.py tests/unit/test_settings.py -q && uv run pytest -q`
Expected: all pass. The universe event needs a runbook row before `test_docs` passes. Add it to the event table now:

```markdown
| `universe_changed` | The symbols the bot watches changed: `added`, `dropped` and the full `universe`. Held and busy symbols are never dropped. |
```

- [ ] **Step 5: Break on purpose**

Drop the account-holdings part of `_required_symbols`. Expected: `test_unpinning_a_held_symbol_keeps_it_until_it_is_sold` FAILS. Restore.

- [ ] **Step 6: Lint, types, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add src/traider/engine.py src/traider/settings.py tests/ docs/runbook.md
git commit -m "feat(engine): live universe; pinned symbols apply without a restart"
```

---

### Task 9: Engine reads research: picks, posture, gate

**Files:**
- Modify: `src/traider/engine.py`, `tests/unit/engine_harness.py`
- Test: `tests/unit/test_engine_research.py`

**Interfaces:**
- Consumes: `ResearchSource`, `ResearchView`, `ResearchUpdate`, `ResearchGate`, `PostureLevel`, `Horizon`.
- Produces:
  - `Engine.__init__(..., research: ResearchSource | None = None)`, a new keyword placed after `on_universe`.
  - Housekeeping refreshes research every `self._settings.research.poll_s` seconds. It then calls `_refresh_universe(now)` and handles each `ResearchUpdate`:
    - `stale`: event `research_stale` `{"detail"}`, alert key `research_stale`, subject "Research is stale", with a body saying no new positions until research is readable and that exits still work.
    - `restored`: event `research_restored` `{}`, alert key `research_restored`, subject "Research readable again".
  - `_wanted_picks(now)`: `[(symbol, pick.score) for symbol, pick in view.live_picks(now).items()]` when research is on.
  - The `StrategyContext` carries `picks=view.live_picks(now)` and `posture=view.level` when research is on.
  - `_check` passes `research=self._gate(order, now)`. `_gate` returns `None` without research. Otherwise:

```python
        root = root_symbol(order.symbol)
        pick = view.pick(root, now)
        level = view.level
        factor = s.reduced_factor if level is PostureLevel.REDUCED else Decimal(1)
        return ResearchGate(
            pick_side=pick.side.value if pick else None,
            pinned=root in self._settings.pinned_symbols,
            posture=level.value,
            cap_factor=factor,
        )
```

    Horizon, foreign status and intraday closing come in Task 10.
  - `Harness.create(..., research_store=None, research_settings=None)`:
    - With a store, the harness builds `ResearchSource(store, lambda: self.engine.settings.research)`, awaits one `refresh(clock.now())`, and passes the source as `research=`.
    - `research_settings` (a dict) goes into `config` as `research=`.
    - The harness also exposes `harness.research`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_engine_research.py
"""The engine only enters what research picked, on the right side, on a day it may trade."""

from datetime import date, timedelta

from tests.unit.engine_harness import Harness
from traider.research.models import Pick, Posture, RunMeta
from traider.research.store import MemoryResearchStore


def run_meta(h, run_id="r1", status="ok"):
    now = h.clock.now()
    return RunMeta(
        run_id=run_id,
        kind="premarket",
        status=status,
        started_at=now,
        finished_at=now,
        trading_day=date(2026, 10, 8),
    )


def make_pick(h, symbol, side="long", horizon="intraday", score=80, run_id="r1", rank=1):
    return Pick.model_validate(
        {
            "run_id": run_id,
            "rank": rank,
            "symbol": symbol,
            "side": side,
            "horizon": horizon,
            "score": score,
            "pre_score": score,
            "thesis": "t",
            "invalidation": "1",
            "expires_at": (h.clock.now() + timedelta(hours=4)).isoformat(),
        }
    )


async def researched(tmp_path, picks=(), level="trade", status="ok", **kwargs) -> Harness:
    store = MemoryResearchStore()
    h = await Harness.create(tmp_path, symbols=(), research_store=store, begin=False, **kwargs)
    posture = Posture(level=level, run_id="r1", at=h.clock.now()) if level else None
    await store.write_run(run_meta(h, status=status), [make_pick(h, *p) for p in picks], posture)
    await h.research.refresh(h.clock.now())
    await h.engine.start()
    await h.engine.step()
    return h


async def test_a_live_pick_joins_the_universe_and_can_be_bought(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)])
    assert h.engine.universe == ("NVDA",)
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()
    assert h.position("NVDA") == 2


async def test_a_held_symbol_whose_pick_is_gone_cannot_be_added_to(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)])
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()
    # The pick expires; NVDA stays in the universe because it is held.
    h.clock.advance(4 * 3600 + 60)
    await h.settle(65)
    assert "NVDA" in h.engine.universe
    await h.target("NVDA", 4)
    await h.settle()
    assert h.position("NVDA") == 2
    assert "no_pick" in (await h.events("order_blocked"))[-1]["data"]["codes"]


async def test_pinned_symbols_need_no_pick_but_follow_the_posture(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)], symbols_extra=("SPY",))
    await h.target("SPY", 2)
    await h.settle()
    assert h.position("SPY") == 2
    h2 = await researched(tmp_path, picks=[], level="stand_aside", symbols_extra=("SPY",))
    await h2.target("SPY", 2)
    await h2.settle()
    assert h2.position("SPY") == 0


async def test_stand_aside_blocks_entries(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)], level="stand_aside")
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()
    assert h.position("NVDA") == 0
    blocked = await h.events("order_blocked")
    assert "posture" in blocked[-1]["data"]["codes"]


async def test_no_posture_today_blocks_entries(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)], level=None)
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()
    assert h.position("NVDA") == 0


async def test_picks_from_a_failed_run_are_not_traded(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)], status="failed")
    assert h.engine.universe == ()


async def test_shares_on_a_bearish_pick_are_refused(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA", "bearish")])
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()
    assert h.position("NVDA") == 0
    assert "pick_side" in (await h.events("order_blocked"))[-1]["data"]["codes"]


async def test_reduced_days_shrink_the_caps(tmp_path):
    h = await researched(
        tmp_path, picks=[("NVDA",)], level="reduced", research_settings={"reduced_factor": 0.5}
    )
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 8)  # ~800: fine at 1000, too big at 500
    await h.settle()
    assert h.position("NVDA") == 0
    assert "max_order_usd" in (await h.events("order_blocked"))[-1]["data"]["codes"]


async def test_stale_research_stops_entries_alerts_once_and_keeps_exits(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)], research_settings={"max_stale_s": 60})
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()

    async def broken(day):
        raise RuntimeError("no table")

    h.research._store.day = broken
    await h.run_for(130, step=10)
    assert len(await h.events("research_stale")) == 1
    assert "research_stale" in h.alert_keys()
    await h.target("NVDA", 0)  # exits still work
    await h.settle()
    assert h.position("NVDA") == 0


async def test_the_strategy_sees_picks_and_posture(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)], level="reduced")
    h.price("NVDA", "100.00", "100.02")
    h.bar("NVDA")
    await h.engine.step()
    ctx = h.strategy.contexts[-1]
    assert "NVDA" in ctx.picks and ctx.posture.value == "reduced"
```

Harness details:
- `researched(...)` passes `symbols_extra` through. Add a `symbols_extra=()` keyword to `Harness.create`. It is merged into `symbols` for pinned-symbol cases.
- `symbols=()` must be allowed when `research_store` is given. In that case the harness sets `config["research_table"] = "test-research"` so `Config` validates.
- `self.prices` must cover any symbol a test quotes. `h.price` adds it.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_engine_research.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement as specified in Interfaces**

- In `_housekeeping`, after the settings refresh:

```python
if self._research is not None and _due(self._research_at, now, self._settings.research.poll_s):
    self._research_at = now
    for update in await self._research.refresh(now):
        await self._on_research(update, now)
    await self._refresh_universe(now)
```

- In `_run_strategy`, build the context with `picks`/`posture` when research is on.
- In `_check`, add `research=self._gate(order, now)` to the `RiskContext(...)` call.

Add the runbook event rows now (the doc test needs them):

```markdown
| `research_stale` | Research could not be read for longer than `max_stale_s`. No new entries until it can. |
| `research_restored` | Research is readable again. |
```

Add alert rows to the runbook alerts table:

```markdown
| Research is stale | The research table has been unreadable for longer than `max_stale_s` (10 minutes by default). | No new positions open; exits still work. Check the research table, the task role and the research jobs. |
| Research readable again | It recovered. | Nothing. |
```

- [ ] **Step 4: Run tests, then the full suite**

Run: `uv run pytest tests/unit/test_engine_research.py -q && uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Break on purpose (restore after each)**
  - `_gate` returns `None` always → the no-pick, stand-aside, bearish and reduced tests FAIL.
  - Ignore `view.stale` in `ResearchView.level` → the stale test still blocks entries through empty picks. Instead, check that it is the alert that proves staleness, by removing the `_on_research` alert. Expected: the stale test FAILS.

- [ ] **Step 6: Lint, types, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add src/traider/engine.py tests/ docs/runbook.md
git commit -m "feat(engine): trade only live picks, follow the day's posture"
```

---

### Task 10: Engine ledger: owned positions, foreign holdings, horizons

**Files:**
- Modify: `src/traider/engine.py`
- Test: `tests/unit/test_engine_ledger.py`

**Interfaces:**
- Consumes: `LedgerEntry`, `StateStore.ledger/put_ledger/delete_ledger`, `ResearchGate` horizon fields.
- Produces (all only when research is on):
  - **Loading the ledger.** It is loaded at `start()` into `self._ledger: dict[str, LedgerEntry]`. If loading fails, `self._ledger_ok = False`, and every housekeeping pass retries until it works. While `_ledger_ok` is false, `_entries_halted` returns `"position ledger not loaded"`.
  - **Writing an entry.** Before a BUY is sent for a symbol with no ledger entry, `_place` writes one:

    ```python
    LedgerEntry(
        symbol,
        horizon=<pick horizon, or "swing">,
        side=<pick side, or "long">,
        opened_at=now,
        pick_run_id=..., pick_rank=...,
    )
    ```

    If that write fails, no order is sent. Log it throttled and set `st.hold_until = now + PRETRADE_RETRY_S`. The write happens after the order counter is incremented and before `broker.place`.
  - **Clearing entries.** In `_on_account`, each ledger symbol whose broker position is 0 and whose state is not busy is deleted from the store and from memory. A failed delete is retried on the next snapshot.
  - **Foreign holdings.** In `_on_account`, a held symbol (quantity != 0) whose root is not pinned and that has no ledger entry is foreign.
    - Keep these in `self._foreign: set[str]`.
    - The first time a symbol is seen as foreign, record the event `unknown_holding` `{"symbol", "quantity"}` and send alert key `unknown_holding:{symbol}`, subject `"{symbol} is held but the bot did not open it"`, with body "The bot will not trade it, sells included. Use an account that is the bot's alone."
    - A foreign symbol is not added to `required` in the universe.
    - Drop it from `self._foreign` once it is flat.
  - **Universe.** `_required_symbols` also includes `root_symbol(s)` for every ledger entry.
  - **Gate horizon fields.**
    - `horizon` = the ledger horizon if held, else the pick horizon, else `"swing"`.
    - `horizon_exposure_usd` = `self._exposure(account, horizon=horizon)`. Extend `_exposure` with an optional `horizon` filter: a symbol counts under the horizon of its ledger entry (`"swing"` if none).
    - `horizon_cap_usd` = `limits.max_total_exposure_usd * share`, where `share` is `research.intraday_share` for intraday and `1 - intraday_share` for swing.
    - `foreign` = whether `order.symbol` is in `self._foreign`.
    - `intraday_closing` = `horizon == "intraday"` and the session is open and minutes to close is at most `research.intraday_flatten_min`.
  - **Intraday flatten.** `_effective_target` returns 0 for a symbol whose ledger horizon is `"intraday"` when minutes to close is at most `intraday_flatten_min`, whatever the strategy says. The sell's reason is `"intraday position: flatten before close"`. Set it in `_build_order` the same way `"flatten before close"` is set today.
  - **Without research, none of this runs.** No ledger reads or writes, no foreign check. That keeps A1 behaviour exactly.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_engine_ledger.py
"""The bot only manages what it opened, and intraday positions are flat by the close."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tests.unit.engine_harness import Harness
from tests.unit.test_engine_research import researched
from traider.models import Position


async def test_a_buy_is_booked_in_the_ledger_before_it_is_sent(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA", "long", "intraday")])
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()
    ledger = await h.store.ledger()
    assert ledger["NVDA"].horizon == "intraday" and ledger["NVDA"].pick_run_id == "r1"


async def test_no_ledger_write_no_order(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)])

    async def refuse(entry):
        raise RuntimeError("throttled")

    h.store.put_ledger = refuse
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()
    assert h.position("NVDA") == 0
    assert h.broker.calls["place"] == 0


async def test_a_sold_out_position_leaves_the_ledger(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)])
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 2)
    await h.settle()
    await h.target("NVDA", 0)
    await h.settle(35)
    assert "NVDA" not in await h.store.ledger()


async def test_a_holding_the_bot_did_not_open_is_never_traded(tmp_path):
    h = await researched(tmp_path, picks=[("NVDA",)])
    h.broker._holdings["NVDA"] = Position("NVDA", 7, Decimal(90))  # bought by hand
    h.price("NVDA", "100.00", "100.02")
    await h.tick(31)
    assert (await h.events("unknown_holding"))[-1]["data"] == {"symbol": "NVDA", "quantity": 7}
    assert "unknown_holding:NVDA" in h.alert_keys()
    await h.target("NVDA", 0)
    await h.settle()
    assert h.position("NVDA") == 7
    assert "foreign_holding" in (await h.events("order_blocked"))[-1]["data"]["codes"]


async def test_intraday_positions_are_sold_before_the_close_and_swing_ones_are_not(tmp_path):
    h = await researched(
        tmp_path, picks=[("DAY", "long", "intraday"), ("SWG", "long", "swing", 70, "r1", 2)]
    )
    for s in ("DAY", "SWG"):
        h.price(s, "50.00", "50.02")
        await h.target(s, 2)
    await h.settle()
    close = datetime(2026, 10, 8, 20, 0, tzinfo=UTC)
    h.clock.set(close - timedelta(minutes=14))
    await h.settle()
    assert h.position("DAY") == 0
    assert h.position("SWG") == 2


async def test_the_intraday_budget_caps_intraday_entries(tmp_path):
    h = await researched(
        tmp_path,
        picks=[("NVDA", "long", "intraday")],
        risk={"max_total_exposure_usd": 1000, "max_position_usd": 1000},
        research_settings={"intraday_share": 0.3},
    )
    h.price("NVDA", "100.00", "100.02")
    await h.target("NVDA", 4)  # ~400 > 300 intraday budget
    await h.settle()
    assert h.position("NVDA") == 0
    assert "horizon_budget" in (await h.events("order_blocked"))[-1]["data"]["codes"]


async def test_without_research_there_is_no_ledger(tmp_path):
    h = await Harness.create(tmp_path)
    await h.target("SPY", 2)
    await h.settle()
    assert await h.store.ledger() == {}
```

Session hours: `StaticSessionProvider` closes at 16:00 New York, which is 20:00 UTC on 2026-10-08. Check `src/traider/session.py`. If it differs, use its close time. Use the real attribute for broker holdings as well: check `PaperBroker` for its holdings dict name (`_holdings` is used by `FakeBroker.true_position`).

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_engine_ledger.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement as specified.** Then add the runbook event row and alert row:

```markdown
| `unknown_holding` | The account holds a position the bot did not open (not in its ledger, not pinned). The bot will not trade it. |
```

```markdown
| X is held but the bot did not open it | With research on, the account holds X but the bot's ledger has no record of buying it, and X is not pinned. | The bot leaves X alone, sells included. Sell it yourself, or pin it if the bot should manage it. Keep the bot's account to the bot. |
```

- [ ] **Step 4: Run tests, then the full suite**

Run: `uv run pytest tests/unit/test_engine_ledger.py -q && uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Break on purpose (restore after each)**
  - Write the ledger entry after `broker.place` instead of before → `test_no_ledger_write_no_order` FAILS.
  - Skip the foreign check → the foreign test FAILS.
  - Remove the intraday branch in `_effective_target` → the intraday flatten test FAILS.

- [ ] **Step 6: Lint, types, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add src/traider/engine.py tests/ docs/runbook.md
git commit -m "feat(engine): position ledger, foreign holdings, intraday flatten"
```

---

### Task 11: App wiring and `LiveSettings(require_pinned=...)`

**Files:**
- Modify: `src/traider/app.py`, `src/traider/settings_store.py`
- Test: `tests/unit/test_settings_store.py`, `tests/integration/test_bot.py`

**Interfaces:**
- **`LiveSettings(store, fallback, *, require_pinned: bool = False)`.** When `require_pinned` is set, a stored version with no pinned symbols is rejected in both `_start` and `refresh`, with the detail: `"settings version N has no pinned symbols and research is off, so the bot would have nothing to trade"`. That version goes through the same once-only reporting and keeps the last good version in force.
- **`build_bot`:**
  - **Build order.** Construct the `Feed` before the `Engine`.
  - **With `config.research_table`:**
    - store: `DynamoResearchStore(aws.table(config.research_table))`
    - source: `ResearchSource(store, lambda: engine.settings.research)`. Because of the build order, use a small closure over a holder, or create the source after the engine and pass it through a setter. Simplest is to create the source with `settings=lambda: holder.settings.research`, where `holder` is the `LiveSettings` if present, or else a fixed `Settings`.
    - Await `source.refresh(clock.now())` once before building the engine.
    - Pass `research=source` and `on_universe=feed.set_symbols` to `Engine(...)`.
  - **`LiveSettings`:** pass `require_pinned=config.research_table is None`.
  - **`describe`:** gains `"research": config.research_table or "off"`.
  - **Startup:** before `engine.start`, call `strategy.on_universe(engine.universe)` and `feed.set_symbols(engine.universe)` with the start universe (pinned symbols plus live picks).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_settings_store.py`:

```python
async def test_with_research_off_a_version_with_no_pinned_symbols_is_rejected():
    store = MemorySettingsStore()
    live = LiveSettings(store, settings(), require_pinned=True)
    await live.start(T0)
    empty = settings().model_copy(update={"pinned_symbols": ()})
    await store.write(empty, expected_version=1, author="cli", note="", now=T0)
    updates = await live.refresh(T0)
    assert [u.kind for u in updates] == ["rejected"]
    assert "no pinned symbols" in updates[0].detail
    assert live.current.pinned_symbols == ("SPY",)


async def test_with_research_on_no_pinned_symbols_is_fine():
    store = MemorySettingsStore()
    live = LiveSettings(store, settings())
    await live.start(T0)
    empty = settings().model_copy(update={"pinned_symbols": ()})
    await store.write(empty, expected_version=1, author="cli", note="", now=T0)
    assert [u.kind for u in await live.refresh(T0)] == ["applied"]
```

Append to `tests/integration/test_bot.py`. Reuse `create_settings_table` / `settings_store`, and add a `create_research_table()` helper next to them with the same GSI as in Task 2:

```python
async def test_bot_trades_a_researched_symbol_end_to_end(world, aws):
    from datetime import date, timedelta

    from traider.research.models import Pick, Posture, RunMeta
    from traider.research.store import DynamoResearchStore

    create_research_table()
    store = DynamoResearchStore(boto3.resource("dynamodb").Table("traider-test-research"))
    now = world.clock.now()
    await store.write_run(
        RunMeta(
            run_id="r1",
            kind="manual",
            status="ok",
            started_at=now,
            finished_at=now,
            trading_day=date(2026, 10, 8),
        ),
        [
            Pick.model_validate(
                {
                    "run_id": "r1",
                    "rank": 1,
                    "symbol": "SPY",
                    "side": "long",
                    "horizon": "intraday",
                    "score": 90,
                    "pre_score": 90,
                    "thesis": "t",
                    "invalidation": "1",
                    "expires_at": (now + timedelta(hours=4)).isoformat(),
                }
            )
        ],
        Posture(level="trade", run_id="r1", at=now),
    )
    world.sign_in()
    config = world.config(symbols=(), research_table="traider-test-research")
    await world.start(config)
    await world.ready()
    assert world.bot.engine.universe == ("SPY",)
    for close in (100, 101, 102, 103):
        await world.bar(close)
    await until(lambda: world.bot.broker.true_position("SPY") > 0, what="a buy")
```

Use the integration file's real way to check the paper position (read the existing paper tests). If it isn't `true_position`, use what they use.

- [ ] **Step 2: Run them to verify they fail.**

- [ ] **Step 3: Implement as specified.**

- [ ] **Step 4: Run tests, then the full suite.**

- [ ] **Step 5: Break on purpose:** remove the `require_pinned` check, and the first new store test FAILS. Restore it.

- [ ] **Step 6: Lint, types, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add src/traider/app.py src/traider/settings_store.py tests/
git commit -m "feat(app): wire research, universe and ledger into the bot"
```

---

### Task 12: `traider research seed` and `traider research show`

**Files:**
- Create: `src/traider/research/seed.py`
- Modify: `src/traider/cli.py`
- Test: `tests/unit/test_cli_research.py`

**Interfaces:**
- **`research/seed.py`:** `build_manual_run(data: Mapping[str, Any], now: datetime) -> tuple[RunMeta, list[Pick], Posture | None]`
  - Input JSON: `{"posture": {"level": "...", "reasons": ["..."]}, "picks": [{"symbol", "side", "horizon", "score", "thesis", "invalidation", "expires_at"?, "pre_score"?, "earnings_date"?}]}`.
  - `run_id` is `f"manual-{now:%Y%m%dT%H%M%SZ}"`. Rank follows list order.
  - `pre_score` defaults to `score`.
  - `expires_at` defaults to today's 16:00 New York for intraday picks, and the 16:00 close five weekdays later for swing picks.
  - Meta: kind `manual`, status `ok`, `trading_day = trading_date(now)`.
  - Raises `ValueError` (or `ValidationError`) on bad input.
- **`cli.research_seed(store, path, out, *, now) -> int`:** validates the whole file before writing anything. Exit 0 on success, 1 on a bad file.
- **`cli.research_show(source, out, *, now) -> int`:** refreshes once, then prints the posture (level and reasons), then one line per live pick: `rank symbol side horizon score expires`. Exit 0.
- **CLI:** `traider research seed FILE` and `traider research show`. Both need `TRAIDER_RESEARCH_TABLE`; without it, exit 2 with `TRAIDER_RESEARCH_TABLE is not set`. Wire them inside `main`'s existing `try`, the same way `settings` is.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_cli_research.py
"""Hand-made research for paper testing before the research jobs exist."""

import io
import json
from datetime import UTC, datetime

from traider import cli
from traider.config import ResearchSettings
from traider.research.source import ResearchSource
from traider.research.store import MemoryResearchStore

NOW = datetime(2026, 10, 8, 13, 0, tzinfo=UTC)  # 09:00 New York


def seed_file(tmp_path, body) -> str:
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(body))
    return str(path)


GOOD = {
    "posture": {"level": "trade", "reasons": ["quiet macro calendar"]},
    "picks": [
        {
            "symbol": "NVDA",
            "side": "long",
            "horizon": "intraday",
            "score": 80,
            "thesis": "t",
            "invalidation": "100",
        },
        {
            "symbol": "AMD",
            "side": "bearish",
            "horizon": "swing",
            "score": 70,
            "thesis": "t",
            "invalidation": "200",
        },
    ],
}


async def test_seed_writes_a_manual_run_the_bot_will_read(tmp_path):
    store = MemoryResearchStore()
    out = io.StringIO()
    assert await cli.research_seed(store, seed_file(tmp_path, GOOD), out, now=NOW) == 0
    source = ResearchSource(store, ResearchSettings)
    await source.refresh(NOW)
    assert set(source.view.live_picks(NOW)) == {"NVDA", "AMD"}
    assert source.view.level.value == "trade"
    nvda = source.view.pick("NVDA", NOW)
    assert nvda.expires_at == datetime(2026, 10, 8, 20, 0, tzinfo=UTC)  # 16:00 New York
    assert source.view.pick("AMD", NOW).expires_at == datetime(2026, 10, 15, 20, 0, tzinfo=UTC)


async def test_a_bad_seed_file_writes_nothing(tmp_path):
    store = MemoryResearchStore()
    bad = {"picks": [GOOD["picks"][0], {"symbol": "X", "side": "short"}]}
    out = io.StringIO()
    assert await cli.research_seed(store, seed_file(tmp_path, bad), out, now=NOW) == 1
    assert store._items == {}


async def test_show_prints_posture_and_live_picks(tmp_path):
    store = MemoryResearchStore()
    await cli.research_seed(store, seed_file(tmp_path, GOOD), io.StringIO(), now=NOW)
    out = io.StringIO()
    assert await cli.research_show(ResearchSource(store, ResearchSettings), out, now=NOW) == 0
    text = out.getvalue()
    assert "posture trade" in text and "quiet macro calendar" in text
    assert "NVDA long intraday 80" in text and "AMD bearish swing 70" in text


def test_research_needs_a_table(monkeypatch, capsys):
    monkeypatch.setenv("TRAIDER_SYMBOLS", "SPY")
    monkeypatch.delenv("TRAIDER_RESEARCH_TABLE", raising=False)
    assert cli.main(["research", "show"]) == 2
    assert "TRAIDER_RESEARCH_TABLE is not set" in capsys.readouterr().err
```

- [ ] **Steps 2–6:** Follow the usual cycle: see the tests fail, implement, see them pass, then break on purpose (make `research_seed` write the picks before validating the posture, and confirm the bad-file test FAILS). Then lint, type-check and commit with the message `feat(cli): traider research seed and show`.

The `research/seed.py` close-time helper:

```python
def _close(day: date) -> datetime:
    return datetime.combine(day, time(16, 0), tzinfo=ET).astimezone(UTC)


def _weekdays_after(day: date, n: int) -> date:
    for _ in range(n):
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
    return day
```

---

### Task 13: Backtest with picks

**Files:**
- Modify: `src/traider/backtest.py`, `src/traider/cli.py`
- Test: `tests/unit/test_backtest.py` (append)

**Interfaces:**
- **`run_backtest(bars, config, *, ..., picks: Sequence[Mapping[str, Any]] | None = None)`**: each row is either a pick row (`{"day": "YYYY-MM-DD", <pick fields as in the seed format>}`) or a posture row (`{"day": ..., "posture": "trade" | "reduced" | "stand_aside"}`).
  - When `picks` is given:
    - Build a `MemoryResearchStore` and write one run per day. The run is `backtest-<day>`, kind `backtest`, status `ok`, with the posture from the posture row for that day (or `trade` if there is none).
    - `expires_at` defaults as in `seed.py`.
    - Pass `research=ResearchSource(store, lambda: config.research)` and an `on_universe` no-op to the `Engine`.
    - Refresh the source once before the first step. After that, the engine polls it on the simulated clock.
  - **Stranger-bar check:** with picks, a bar is acceptable when its symbol is pinned or appears in any pick row.
  - **Config:** set `research_table="backtest"` on the copied config, so empty `symbols` validates.
- **`load_picks_jsonl(path) -> list[dict]`:** reads JSON Lines. A bad line raises `BacktestError` naming the line number.
- **CLI:** `traider backtest ... --picks FILE`.

- [ ] **Step 1: Failing tests (append to `tests/unit/test_backtest.py`; read the file's helpers first)**

```python
async def test_a_backtest_with_picks_trades_only_the_picked_symbol_on_its_day(tmp_path):
    bars = rising_bars("NVDA", day="2026-10-06") + rising_bars("AMD", day="2026-10-06")
    picks = [
        {
            "day": "2026-10-06",
            "symbol": "NVDA",
            "side": "long",
            "horizon": "intraday",
            "score": 90,
            "thesis": "t",
            "invalidation": "1",
        }
    ]
    config = Config(symbols=(), research_table="x", strategy_params={"fast": 1, "slow": 2})
    result = await run_backtest(bars, config, picks=picks)
    assert {t.symbol for t in result.trades} == {"NVDA"}


async def test_a_stand_aside_day_trades_nothing(tmp_path):
    bars = rising_bars("NVDA", day="2026-10-06")
    picks = [
        {
            "day": "2026-10-06",
            "symbol": "NVDA",
            "side": "long",
            "horizon": "intraday",
            "score": 90,
            "thesis": "t",
            "invalidation": "1",
        },
        {"day": "2026-10-06", "posture": "stand_aside"},
    ]
    config = Config(symbols=(), research_table="x", strategy_params={"fast": 1, "slow": 2})
    result = await run_backtest(bars, config, picks=picks)
    assert result.trades == ()
    assert result.blocked["posture"] > 0


def test_bad_picks_lines_name_the_line(tmp_path):
    path = tmp_path / "p.jsonl"
    path.write_text('{"day": "2026-10-06", "posture": "trade"}\nnot json\n')
    with pytest.raises(BacktestError, match="line 2"):
        load_picks_jsonl(path)
```

`rising_bars(symbol, day)` is a small helper you add to the test file. It returns one-minute bars from 09:31 New York that rise by 0.1 each minute, about 30 bars. `run_backtest` replays only regular-session bars.

- [ ] **Steps 2–6:** Follow the usual cycle. The deliberate break: drop the stranger-bar extension and confirm the first test FAILS with `BacktestError`. Commit with the message `feat(backtest): replay research picks and postures`.

---

### Task 14: Infrastructure: opt-in research table

**Files:**
- Modify: `infra/settings.py`, `infra/data.py`, `infra/bot.py`, `infra/stack.py`, `infra/Pulumi.example.yaml`
- Test: `infra/tests/test_stack.py`

**Interfaces:**
- **Stack config:**
  - `traider:research` (bool, default `false`).
  - `traider:pinnedSymbols` (list). `traider:symbols` is still accepted as an alias. If both are set, the deploy fails with a clear message.
  - `traider:researchSettings` (object), which becomes `TRAIDER_RESEARCH`.
  - With research off, at least one pinned symbol is required (the existing message). With research on, pinned symbols may be empty.
  - `Settings` gains `research: bool`.
- **`Data.research_table: aws.dynamodb.Table | None`.** When `settings.research` is on, this is Pulumi name `research`, table name `{prefix}-research`, keys `pk`/`sk`, and GSI `gsi1` (`gsi1pk`/`gsi1sk`, projection ALL). Like the settings table, it has PITR and deletion protection on live stacks. Otherwise it is `None`.
- **Bot, when research is on:**
  - env `TRAIDER_RESEARCH_TABLE`
  - task policy statement `Sid: "Research"`, actions `["dynamodb:Query", "dynamodb:GetItem"]` on the table ARN only (not the index)
  - stack output `researchTable`
  - `localEnv` gains `TRAIDER_RESEARCH_TABLE`
- **Validation** uses the bot's own `Config.from_env`, with `TRAIDER_RESEARCH_TABLE` set to a placeholder when research is on. That way an empty pinned list validates exactly when the bot would accept it.

- [ ] **Step 1: Failing tests (append to `infra/tests/test_stack.py`)**

```python
def test_research_is_off_by_default(paper):
    assert [t for t in paper.of(TABLE) if t.name == "research"] == []
    assert "TRAIDER_RESEARCH_TABLE" not in environment(paper)
    assert "Research" not in {s["Sid"] for s in paper.policy("bot-task")}


def test_research_on_creates_an_indexed_table_the_bot_can_only_read():
    on = deploy({"research": True, "pinnedSymbols": []})
    table = on.one(TABLE, "research").inputs
    assert table["name"] == "traider-dev-research"
    (index,) = table["globalSecondaryIndexes"]
    assert (index["name"], index["hashKey"], index["rangeKey"]) == ("gsi1", "gsi1pk", "gsi1sk")
    assert environment(on)["TRAIDER_RESEARCH_TABLE"] == table["name"]
    (statement,) = [s for s in on.policy("bot-task") if s["Sid"] == "Research"]
    assert set(statement["Action"]) == {"dynamodb:Query", "dynamodb:GetItem"}
    assert statement["Resource"] == on.one(TABLE, "research").arn
    assert on.outputs["researchTable"] == table["name"]


def test_without_research_a_pinned_symbol_is_required():
    with pytest.raises(Exception, match="symbol"):
        deploy({"symbols": None, "pinnedSymbols": []})


def test_pinned_symbols_and_symbols_cannot_both_be_set():
    with pytest.raises(Exception, match="pinnedSymbols"):
        deploy({"pinnedSymbols": ["SPY"], "symbols": ["QQQ"]})


def test_research_settings_reach_the_bot_as_json():
    on = deploy({"research": True, "researchSettings": {"min_score": 75}})
    assert json.loads(environment(on)["TRAIDER_RESEARCH"]) == {"min_score": 75}
```

Read `infra/tests/conftest.py`'s `BASE` config. It sets `symbols`, so tests that use `pinnedSymbols` must also pass `"symbols": None` (or the conftest needs a small change, made test-only). Pick whichever matches how the conftest merges `None`. Update any existing test that breaks only because of the new flag, and say so in the report.

- [ ] **Steps 2–6:** Follow the usual cycle. Break on purpose by adding `"dynamodb:PutItem"` to the Research statement and confirming the test FAILS. Run the infra suite, lint and types, plus the root suite once. Commit with the message `feat(infra): opt-in research table with read-only bot access`.

---

### Task 15: Docs and spec

**Files:**
- Modify: `README.md`, `docs/runbook.md`, `infra/Pulumi.example.yaml` (if Task 14 did not cover it), `docs/superpowers/specs/2026-10-09-research-driven-trading-design.md`

- [ ] **Step 1: README.** Add a section `## Research and the universe` after "Configuration". Cover:
  - Research is opt-in with `traider:research: true`.
  - What the bot does with picks: universe, live-pick rule, sides, posture (stand aside, reduced), intraday vs swing budgets and the intraday flatten. Name `intraday_share`, `reduced_factor`, `intraday_flatten_min` and `min_score`.
  - Pinned symbols need no pick, but they still follow the posture.
  - Fail-closed behaviour: stale research means no entries, exits always work.
  - The position ledger, and why the account must be the bot's alone (the foreign holdings rule).
  - Seeding picks by hand with `traider research seed` for paper testing before the research jobs exist, and `traider research show`.
  - `traider backtest --picks`.
  - In "Writing a strategy", show `ctx.pick(symbol)` and `ctx.posture`, and say that `on_universe` exists.
  - Update "Limits worth knowing" if it still says the symbol list is fixed.

- [ ] **Step 2: Runbook.**
  - The event and alert rows were added in Tasks 8–10. Check they are all present: `universe_changed`, `research_stale`, `research_restored`, `unknown_holding`.
  - Add the new risk codes to the `order_blocked` row's examples, for example `no_pick` and `posture`.
  - Add a section "Seeding research by hand" with the seed JSON format and the `traider research seed | show` commands.
  - Update "Going live" with a checklist item: "With research on: research jobs are writing a posture every morning (otherwise the bot stands aside every day), and the account holds nothing the bot did not buy."
  - The `pinned_symbols` restart note from A1 changes: pinned symbols now apply live. Fix that text in both the README and the runbook (`grep -n "pinned" README.md docs/runbook.md`).

- [ ] **Step 3: Spec.** Update A.1, A.5 and A.8 with the decisions at the top of this plan: research is opt-in; pinned symbols are live; posture applies to pinned symbols; the ledger is only used with research on. In A.10, record the opt-in flag and the `pinnedSymbols` / `symbols` alias.

- [ ] **Step 4: Verify everything**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
cd infra && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q && cd ..
```

- [ ] **Step 5: Commit** with the message `docs: research, universe and ledger in README and runbook`. Do not push. The controller opens the PR after the final review.
