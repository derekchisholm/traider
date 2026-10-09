# A1: Versioned, live-reloaded settings — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move every tunable bot setting into a versioned DynamoDB store that the running bot re-reads every 10 seconds, with the environment as the bootstrap source and the CLI as the first editor.

**Architecture:** A new `Settings` pydantic model holds the tunable fields (built from `Config` when no store exists, so local runs, tests and backtests are unchanged). A `SettingsStore` keeps immutable numbered versions; the latest one is current, and writes are optimistic (`attribute_not_exists` on the next version number). `LiveSettings` wraps a store with polling, last-good fallback and restart-only fields; the engine reads tunables from it instead of `Config`.

**Tech Stack:** Python 3.13, pydantic 2, boto3 DynamoDB resource API, moto for tests, Pulumi (Python) for infra.

Spec: `docs/superpowers/specs/2026-10-09-research-driven-trading-design.md`, sections A.1, A.2, A.9 (settings commands), A.10 (settings table), A.11 (settings events), A.12 (settings tests). Research, universe, ledger and picks are plan A2.

**Deliberate deviation from the spec, recorded here and in the spec:** `Config` keeps its tunable fields as the *bootstrap* values (read from `TRAIDER_*`), instead of losing them. At runtime the engine, feed and strategy read the `Settings` built from them or loaded from the store. That avoids rewriting ~900 tests that construct `Config(...)` directly. "Current" is the highest version number (no separate `CURRENT` pointer item), which makes a write a single conditional put.

## Global Constraints

- Python `>=3.13,<3.14`; run everything with `uv run` from the repo root (bot) or `infra/` (infra).
- Must stay green: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest` in both the root and `infra/`.
- Tests never touch real AWS or real Schwab (moto + fakes only).
- Safety rule: fail closed. No settings ever loaded in this process with a store configured → no entries. Exits never depend on settings being readable.
- Every safety guard added here gets a test that fails when the guard is removed on purpose; the task says which.
- Nothing secret goes in settings, logs, alerts or events.
- Conventional Commits, short subjects. End each commit message with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Xh7zur22CRYEGaDoTB5nkV
  ```
- Work on branch `feat/versioned-settings` (it already holds the spec and this plan). Never push to `main`.
- `README.md` and `docs/runbook.md` must stay true; `tests/unit/test_docs.py` enforces the runbook's event table and command names.

## File structure

| File | Responsibility |
|---|---|
| `src/traider/settings.py` (new) | `Settings` model, `settings_diff`, restart-only field rules, `merge_live` |
| `src/traider/settings_store.py` (new) | `SettingsVersion`, errors, `MemorySettingsStore`, `DynamoSettingsStore`, `LiveSettings`, `SettingsUpdate` |
| `src/traider/config.py` | add `settings_table` |
| `src/traider/engine.py` | read tunables from `Settings`; refresh `LiveSettings`; events, alerts, fail-closed |
| `src/traider/feed.py` | take `settings` for symbols and option-chain settings |
| `src/traider/app.py` | load settings before building strategy, feed and engine; `describe` |
| `src/traider/cli.py` | `traider settings show / history / apply` |
| `infra/data.py`, `infra/bot.py`, `infra/stack.py` | settings table, IAM, env, outputs |
| `tests/unit/test_settings.py`, `tests/unit/test_settings_store.py`, `tests/unit/test_engine_settings.py`, `tests/unit/test_cli_settings.py` (new) | tests |
| `README.md`, `docs/runbook.md`, `infra/Pulumi.example.yaml` | docs |

---

### Task 1: `Settings` model and its rules

**Files:**
- Create: `src/traider/settings.py`
- Test: `tests/unit/test_settings.py`

**Interfaces:**
- Produces:
  - `class Settings(BaseModel)` (frozen, `extra="forbid"`) with fields `pinned_symbols: tuple[str, ...]`, `strategy: str`, `strategy_params: dict[str, Any]`, `risk: RiskLimits`, `order_type: Literal["LIMIT","MARKET"]`, `limit_offset_bps: Decimal`, `order_timeout_s: float`, `flatten_before_close_min: int | None`, `cancel_unknown_orders: bool`, `option_chain_days: int`, `option_chain_strikes: int`.
  - `Settings.from_config(config: Config) -> Settings`
  - `RESTART_FIELDS: frozenset[str]`
  - `restart_changes(running: Settings, new: Settings) -> list[str]` (dotted names)
  - `merge_live(running: Settings, new: Settings) -> Settings`
  - `settings_diff(old: Settings, new: Settings) -> dict[str, list[Any]]` (`{"risk.max_order_usd": ["500", "250"]}`)

- [ ] **Step 1: Check the branch**

Work happens on `feat/versioned-settings`, which already carries the spec and this plan.

```bash
git branch --show-current   # feat/versioned-settings
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/unit/test_settings.py
"""The tunable settings: how they are built, compared and partly applied."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from traider.config import Config, RiskLimits
from traider.settings import (
    RESTART_FIELDS,
    Settings,
    merge_live,
    restart_changes,
    settings_diff,
)


def base(**overrides) -> Settings:
    return Settings.from_config(Config(symbols=("SPY",))).model_copy(update=overrides)


def test_built_from_config_with_the_same_values():
    config = Config(
        symbols=("SPY", "QQQ"),
        order_type="MARKET",
        risk=RiskLimits(max_order_usd=Decimal(250)),
        strategy_params={"fast": 3, "slow": 9},
    )
    settings = Settings.from_config(config)
    assert settings.pinned_symbols == ("SPY", "QQQ")
    assert settings.order_type == "MARKET"
    assert settings.risk.max_order_usd == Decimal(250)
    assert settings.strategy_params == {"fast": 3, "slow": 9}
    assert settings.order_timeout_s == config.order_timeout_s


def test_round_trips_through_json():
    settings = base()
    again = Settings.model_validate(settings.model_dump(mode="json"))
    assert again == settings


def test_unknown_fields_are_rejected():
    body = base().model_dump(mode="json") | {"max_orderr_usd": 5}
    with pytest.raises(ValidationError):
        Settings.model_validate(body)


def test_bad_symbols_are_rejected():
    body = base().model_dump(mode="json") | {"pinned_symbols": ["SPY;rm"]}
    with pytest.raises(ValidationError):
        Settings.model_validate(body)


def test_an_unknown_strategy_is_rejected():
    body = base().model_dump(mode="json") | {"strategy": "nope"}
    with pytest.raises(ValidationError, match="unknown strategy"):
        Settings.model_validate(body)


def test_bad_strategy_parameters_are_rejected():
    body = base().model_dump(mode="json") | {"strategy_params": {"fast": 9, "slow": 3}}
    with pytest.raises(ValidationError, match="fast"):
        Settings.model_validate(body)


def test_diff_names_each_changed_leaf():
    old = base()
    new = old.model_copy(
        update={"risk": old.risk.model_copy(update={"max_order_usd": Decimal(250)})}
    )
    assert settings_diff(old, new) == {"risk.max_order_usd": ["500", "250"]}
    assert settings_diff(old, old) == {}


def test_restart_changes_lists_only_restart_fields():
    old = base()
    new = old.model_copy(
        update={
            "strategy_params": {"fast": 3, "slow": 9},
            "order_timeout_s": 5.0,
            "risk": old.risk.model_copy(update={"allow_options": True}),
        }
    )
    assert restart_changes(old, new) == ["risk.allow_options", "strategy_params"]


def test_merge_live_keeps_running_restart_fields_and_takes_the_rest():
    running = base()
    new = running.model_copy(
        update={
            "pinned_symbols": ("QQQ",),
            "order_timeout_s": 5.0,
            "risk": running.risk.model_copy(
                update={"allow_options": True, "max_order_usd": Decimal(250)}
            ),
        }
    )
    merged = merge_live(running, new)
    assert merged.pinned_symbols == ("SPY",)
    assert merged.risk.allow_options is False
    assert merged.order_timeout_s == 5.0
    assert merged.risk.max_order_usd == Decimal(250)


def test_restart_fields_are_the_ones_the_process_cannot_change_under_itself():
    assert {
        "strategy",
        "strategy_params",
        "pinned_symbols",
        "option_chain_days",
        "option_chain_strikes",
    } == RESTART_FIELDS
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_settings.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'traider.settings'`

- [ ] **Step 4: Implement `src/traider/settings.py`**

```python
"""The bot's tunable settings.

``Config`` says where the bot runs (account, tables, secrets); ``Settings`` says how
it trades. Without a settings table they come from the same ``TRAIDER_*`` variables
as ever. With one, they are versioned in DynamoDB and the running bot picks up a new
version within seconds. A few fields cannot change under a running process; those
wait for the next restart (``RESTART_FIELDS``).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from traider.config import (
    Config,
    PositiveFloat,
    RiskLimits,
    check_symbols,
)

#: Fields a running bot keeps until it restarts: the strategy is built once, and the
#: feed subscribes to its symbols and option chains at start-up.
RESTART_FIELDS = frozenset(
    {"strategy", "strategy_params", "pinned_symbols", "option_chain_days", "option_chain_strikes"}
)
#: Risk limits that are also fixed at start-up (the feed decides then whether to load chains).
_RESTART_RISK_FIELDS = ("allow_options",)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pinned_symbols: tuple[str, ...] = ()
    strategy: str = "sma_cross"
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    order_type: Literal["LIMIT", "MARKET"] = "LIMIT"
    limit_offset_bps: Annotated[Decimal, Field(ge=0, le=100)] = Decimal(5)
    order_timeout_s: PositiveFloat = 20.0
    flatten_before_close_min: Annotated[int, Field(ge=1)] | None = None
    cancel_unknown_orders: bool = False
    option_chain_days: Annotated[int, Field(ge=1, le=365)] = 45
    option_chain_strikes: Annotated[int, Field(ge=1, le=100)] = 20

    @field_validator("pinned_symbols")
    @classmethod
    def _symbols_ok(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return check_symbols(value, allow_empty=True)

    @model_validator(mode="after")
    def _strategy_builds(self) -> Self:
        # Imported here: the strategy registry is not needed to read a Settings object
        # and importing it at module level would tie config loading to every strategy.
        from traider.strategy import create_strategy

        create_strategy(self.strategy, self.pinned_symbols, self.strategy_params)
        return self

    @classmethod
    def from_config(cls, config: Config) -> Settings:
        return cls(
            pinned_symbols=config.symbols,
            strategy=config.strategy,
            strategy_params=config.strategy_params,
            risk=config.risk,
            order_type=config.order_type,
            limit_offset_bps=config.limit_offset_bps,
            order_timeout_s=config.order_timeout_s,
            flatten_before_close_min=config.flatten_before_close_min,
            cancel_unknown_orders=config.cancel_unknown_orders,
            option_chain_days=config.option_chain_days,
            option_chain_strikes=config.option_chain_strikes,
        )


def _flat(data: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for key, value in data.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict) and key != "strategy_params":
                out.update(_flat(value, name))
            else:
                out[name] = value
        return out
    return {prefix: data}


def settings_diff(old: Settings, new: Settings) -> dict[str, list[Any]]:
    """Each changed field as ``{"risk.max_order_usd": [old, new]}``, in JSON form."""
    before, after = _flat(old.model_dump(mode="json")), _flat(new.model_dump(mode="json"))
    return {
        key: [before.get(key), after.get(key)]
        for key in sorted(before.keys() | after.keys())
        if before.get(key) != after.get(key)
    }


def restart_changes(running: Settings, new: Settings) -> list[str]:
    """The restart-only fields that differ, sorted, as dotted names."""
    changed = [name for name in RESTART_FIELDS if getattr(running, name) != getattr(new, name)]
    changed += [
        f"risk.{name}"
        for name in _RESTART_RISK_FIELDS
        if getattr(running.risk, name) != getattr(new.risk, name)
    ]
    return sorted(changed)


def merge_live(running: Settings, new: Settings) -> Settings:
    """``new``, except that restart-only fields keep the values the process runs on."""
    keep = {name: getattr(running, name) for name in RESTART_FIELDS}
    risk = new.risk.model_copy(
        update={name: getattr(running.risk, name) for name in _RESTART_RISK_FIELDS}
    )
    return new.model_copy(update={**keep, "risk": risk})
```

- [ ] **Step 5: Factor the symbol check out of `Config` so both models share it**

In `src/traider/config.py`, add this function just above `class Config` and make `Config._symbols_ok` call it:

```python
def check_symbols(value: tuple[str, ...], *, allow_empty: bool = False) -> tuple[str, ...]:
    """The rules every list of equity symbols follows."""
    if not value and not allow_empty:
        raise ValueError("at least one symbol is required")
    if len(value) > 25:
        raise ValueError("at most 25 symbols")
    for symbol in value:
        if not _SYMBOL.match(symbol):
            raise ValueError(f"not a valid equity symbol: {symbol!r}")
    if len(set(value)) != len(value):
        raise ValueError("duplicate symbols")
    return value
```

```python
    @field_validator("symbols")
    @classmethod
    def _symbols_ok(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return check_symbols(value)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_settings.py tests/unit/test_config.py -q`
Expected: all pass.

- [ ] **Step 7: Break a guard on purpose**

Comment out the body of `_strategy_builds` (leave `return self`). Run `uv run pytest tests/unit/test_settings.py -q`. Expected: `test_an_unknown_strategy_is_rejected` and `test_bad_strategy_parameters_are_rejected` FAIL. Restore it and re-run: all pass.

- [ ] **Step 8: Lint, types, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add src/traider/settings.py src/traider/config.py tests/unit/test_settings.py
git commit -m "feat(settings): add Settings model with restart-only fields"
```

---

### Task 2: Settings stores (memory and DynamoDB)

**Files:**
- Create: `src/traider/settings_store.py`
- Test: `tests/unit/test_settings_store.py`

**Interfaces:**
- Consumes: `Settings`, `settings_diff` (Task 1).
- Produces:
  - `@dataclass(frozen=True) class SettingsVersion: version: int; settings: Settings; author: str; at: datetime; note: str; diff: dict[str, list[Any]]`
  - `class SettingsConflict(Exception)` — another version was written first.
  - `class SettingsInvalid(Exception)` with attribute `version: int` — the stored body does not validate.
  - `class SettingsStore(Protocol)`: `async latest() -> SettingsVersion | None` (raises `SettingsInvalid`), `async write(settings, *, expected_version: int, author: str, note: str, now: datetime) -> SettingsVersion` (raises `SettingsConflict`), `async history(limit: int = 20) -> list[SettingsVersion]` (newest first; invalid versions skipped).
  - `MemorySettingsStore()` with test helper `put_raw(version: int, body: dict, *, author="test", at=...)`.
  - `DynamoSettingsStore(table)` — `table` is a boto3 DynamoDB `Table` resource.
  - Item layout: `pk="SETTINGS"`, `sk=f"V#{version:09d}"`, attributes `version` (number), `author`, `at` (ISO), `note`, `body` (JSON string), `diff` (JSON string).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_settings_store.py
"""One contract, two implementations: in-memory and DynamoDB (through moto)."""

import json
from datetime import UTC, datetime
from decimal import Decimal

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


def put_invalid(store, version: int) -> None:
    body = settings().model_dump(mode="json") | {"strategy": "nope"}
    if isinstance(store, MemorySettingsStore):
        store.put_raw(version, body)
    else:
        store._table.put_item(
            Item={
                "pk": "SETTINGS",
                "sk": f"V#{version:09d}",
                "version": version,
                "author": "test",
                "at": T0.isoformat(),
                "note": "",
                "body": json.dumps(body),
                "diff": "{}",
            }
        )


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
        settings(max_order_usd=Decimal(250)), expected_version=1, author="cli", note="smaller", now=T0
    )
    assert second.version == 2
    assert second.diff == {"risk.max_order_usd": ["500", "250"]}
    assert (await store.latest()).note == "smaller"


async def test_a_stale_writer_gets_a_conflict_and_nothing_is_written(store):
    await store.write(settings(), expected_version=0, author="a", note="", now=T0)
    await store.write(settings(max_order_usd=Decimal(250)), expected_version=1, author="a", note="", now=T0)
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


async def test_history_is_newest_first_and_skips_invalid_versions(store):
    await store.write(settings(), expected_version=0, author="a", note="", now=T0)
    await store.write(settings(max_order_usd=Decimal(250)), expected_version=1, author="b", note="", now=T0)
    put_invalid(store, 3)
    assert [v.version for v in await store.history()] == [2, 1]
    assert [v.version for v in await store.history(limit=1)] == [2]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_settings_store.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'traider.settings_store'`

- [ ] **Step 3: Implement `src/traider/settings_store.py` (stores only; `LiveSettings` comes in Task 3)**

```python
"""Where settings versions live, and the bot's live view of them.

Every change is a new, immutable, numbered version; the highest number is current.
A write names the version it was based on and is refused if anything was written
since (``SettingsConflict``), so two editors cannot overwrite each other unseen.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from botocore.exceptions import ClientError
from pydantic import ValidationError

from traider.settings import Settings, settings_diff

PK = "SETTINGS"


def _sk(version: int) -> str:
    return f"V#{version:09d}"


@dataclass(frozen=True, slots=True)
class SettingsVersion:
    version: int
    settings: Settings
    author: str
    at: datetime
    note: str = ""
    diff: dict[str, list[Any]] = field(default_factory=dict)


class SettingsConflict(Exception):
    """Another version was written since the one this write was based on."""


class SettingsInvalid(Exception):
    """A stored version does not describe valid settings."""

    def __init__(self, version: int, problem: str) -> None:
        super().__init__(f"settings version {version} is invalid: {problem}")
        self.version = version


class SettingsStore(Protocol):
    async def latest(self) -> SettingsVersion | None: ...

    async def write(
        self, settings: Settings, *, expected_version: int, author: str, note: str, now: datetime
    ) -> SettingsVersion: ...

    async def history(self, limit: int = 20) -> list[SettingsVersion]: ...


def _parse(item: dict[str, Any]) -> SettingsVersion:
    version = int(item["version"])
    try:
        settings = Settings.model_validate(json.loads(item["body"]))
    except (ValidationError, ValueError) as exc:
        raise SettingsInvalid(version, str(exc).splitlines()[0]) from None
    return SettingsVersion(
        version=version,
        settings=settings,
        author=str(item.get("author", "")),
        at=datetime.fromisoformat(str(item["at"])),
        note=str(item.get("note", "")),
        diff=json.loads(item.get("diff") or "{}"),
    )


def _item(
    settings: Settings, version: int, author: str, note: str, now: datetime, diff: dict[str, Any]
) -> dict[str, Any]:
    return {
        "pk": PK,
        "sk": _sk(version),
        "version": version,
        "author": author,
        "at": now.isoformat(),
        "note": note,
        "body": json.dumps(settings.model_dump(mode="json")),
        "diff": json.dumps(diff),
    }


class MemorySettingsStore:
    def __init__(self) -> None:
        self._items: dict[int, dict[str, Any]] = {}

    def put_raw(
        self, version: int, body: dict[str, Any], *, author: str = "test", at: str = ""
    ) -> None:
        """Store a body without checking it, the way a buggy writer might."""
        self._items[version] = {
            "version": version,
            "author": author,
            "at": at or "2026-10-09T13:00:00+00:00",
            "note": "",
            "body": json.dumps(body),
            "diff": "{}",
        }

    async def latest(self) -> SettingsVersion | None:
        if not self._items:
            return None
        return _parse(self._items[max(self._items)])

    async def write(
        self, settings: Settings, *, expected_version: int, author: str, note: str, now: datetime
    ) -> SettingsVersion:
        version = expected_version + 1
        if version in self._items or (self._items and max(self._items) > expected_version):
            raise SettingsConflict(f"version {version} already exists")
        previous = self._items.get(expected_version)
        diff = settings_diff(_parse(previous).settings, settings) if previous else {}
        self._items[version] = _item(settings, version, author, note, now, diff)
        return _parse(self._items[version])

    async def history(self, limit: int = 20) -> list[SettingsVersion]:
        out: list[SettingsVersion] = []
        for version in sorted(self._items, reverse=True):
            try:
                out.append(_parse(self._items[version]))
            except SettingsInvalid:
                continue
            if len(out) >= limit:
                break
        return out


class DynamoSettingsStore:
    def __init__(self, table: Any) -> None:
        self._table = table

    async def _call[T](self, fn: Callable[..., T], **kwargs: Any) -> T:
        return await asyncio.to_thread(fn, **kwargs)

    async def _newest(self, limit: int) -> list[dict[str, Any]]:
        response = await self._call(
            self._table.query,
            KeyConditionExpression="pk = :pk AND begins_with(sk, :v)",
            ExpressionAttributeValues={":pk": PK, ":v": "V#"},
            ScanIndexForward=False,
            Limit=limit,
            ConsistentRead=True,
        )
        items: list[dict[str, Any]] = response.get("Items", [])
        return items

    async def latest(self) -> SettingsVersion | None:
        items = await self._newest(1)
        return _parse(items[0]) if items else None

    async def write(
        self, settings: Settings, *, expected_version: int, author: str, note: str, now: datetime
    ) -> SettingsVersion:
        version = expected_version + 1
        diff: dict[str, Any] = {}
        if expected_version > 0:
            response = await self._call(
                self._table.get_item,
                Key={"pk": PK, "sk": _sk(expected_version)},
                ConsistentRead=True,
            )
            previous = response.get("Item")
            if previous is not None:
                try:
                    diff = settings_diff(_parse(previous).settings, settings)
                except SettingsInvalid:
                    diff = {}
        item = _item(settings, version, author, note, now, diff)
        try:
            await self._call(
                self._table.put_item,
                Item=item,
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise SettingsConflict(f"version {version} already exists") from None
            raise
        latest = await self.latest()
        if latest is None or latest.version != version:
            # Someone wrote a later version between our put and this read. Ours stands in
            # history, but it is not current: tell the caller.
            raise SettingsConflict(f"version {version} was superseded at once")
        return latest

    async def history(self, limit: int = 20) -> list[SettingsVersion]:
        out: list[SettingsVersion] = []
        for item in await self._newest(limit + 10):
            try:
                out.append(_parse(item))
            except SettingsInvalid:
                continue
            if len(out) >= limit:
                break
        return out
```

Note the memory store's conflict rule: a write based on version `n` is refused if any version above `n` exists, which matches DynamoDB because versions are written in order and are never deleted.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_settings_store.py -q`
Expected: 14 passed (7 tests × 2 stores).

- [ ] **Step 5: Break a guard on purpose**

In `DynamoSettingsStore.write`, delete the `ConditionExpression="attribute_not_exists(pk)",` line. Run the tests. Expected: `test_a_stale_writer_gets_a_conflict...[dynamo]` and `test_two_writers_racing...[dynamo]` FAIL. Restore it; all pass.

- [ ] **Step 6: Lint, types, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add src/traider/settings_store.py tests/unit/test_settings_store.py
git commit -m "feat(settings): versioned settings store for memory and DynamoDB"
```

---

### Task 3: `LiveSettings`: bootstrap, refresh, last good, restart-only

**Files:**
- Modify: `src/traider/settings_store.py` (append)
- Test: `tests/unit/test_settings_store.py` (append)

**Interfaces:**
- Consumes: Task 1 and Task 2.
- Produces:
  - `@dataclass(frozen=True) class SettingsUpdate: kind: Literal["applied", "pending_restart", "rejected", "unreadable"]; version: int | None; detail: str; diff: dict[str, list[Any]]`
  - `class LiveSettings`:
    - `__init__(store: SettingsStore, fallback: Settings)`
    - attributes `current: Settings`, `version: int | None`, `loaded: bool`
    - `async start(now: datetime) -> list[SettingsUpdate]` — load or bootstrap; on success `loaded=True` and `current` is the stored version **in full** (nothing has been built yet, so restart-only fields apply too).
    - `async refresh(now: datetime) -> list[SettingsUpdate]` — apply a newer version's live fields; report restart-only differences; keep the last good on errors.

- [ ] **Step 1: Write the failing tests (append to `tests/unit/test_settings_store.py`)**

```python
from traider.settings_store import LiveSettings


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
    await store.write(settings(max_order_usd=Decimal(250)), expected_version=1, author="cli", note="", now=T0)
    updates = await live.refresh(T0)
    assert [(u.kind, u.version) for u in updates] == [("applied", 2)]
    assert updates[0].diff == {"risk.max_order_usd": ["500", "250"]}
    assert live.current.risk.max_order_usd == Decimal(250)
    assert await live.refresh(T0) == []  # nothing new


async def test_refresh_keeps_restart_fields_and_says_a_restart_is_needed():
    store = MemorySettingsStore()
    live = LiveSettings(store, settings())
    await live.start(T0)
    changed = settings(max_order_usd=Decimal(250)).model_copy(update={"pinned_symbols": ("QQQ",)})
    await store.write(changed, expected_version=1, author="cli", note="", now=T0)
    updates = await live.refresh(T0)
    assert [u.kind for u in updates] == ["applied", "pending_restart"]
    assert updates[1].detail == "pinned_symbols"
    assert live.current.pinned_symbols == ("SPY",)
    assert live.current.risk.max_order_usd == Decimal(250)


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_settings_store.py -q`
Expected: FAIL with `ImportError: cannot import name 'LiveSettings'`

- [ ] **Step 3: Implement (append to `src/traider/settings_store.py`)**

Add `Literal` to the `typing` import and `merge_live, restart_changes` to the `traider.settings` import, then append:

```python
@dataclass(frozen=True, slots=True)
class SettingsUpdate:
    kind: Literal["applied", "pending_restart", "rejected", "unreadable"]
    version: int | None
    detail: str
    diff: dict[str, list[Any]] = field(default_factory=dict)


class LiveSettings:
    """The settings a running bot uses, kept in step with the store.

    ``loaded`` is False until a version has been read successfully in this process;
    until then the engine allows no entries. After that, a read error or an invalid
    version leaves the last good settings in force.
    """

    def __init__(self, store: SettingsStore, fallback: Settings) -> None:
        self._store = store
        self.current = fallback
        self.version: int | None = None
        self.loaded = False
        self._rejected: set[int] = set()

    async def _bootstrap(self, now: datetime) -> SettingsVersion | None:
        """Write the fallback as version 1 of an empty store. Losing the race to another
        writer is fine: theirs is read instead."""
        try:
            return await self._store.write(
                self.current,
                expected_version=0,
                author="bootstrap",
                note="seeded from the environment",
                now=now,
            )
        except SettingsConflict:
            return await self._store.latest()

    async def start(self, now: datetime) -> list[SettingsUpdate]:
        try:
            latest = await self._store.latest()
            if latest is None:
                latest = await self._bootstrap(now)
        except SettingsInvalid as exc:
            self._rejected.add(exc.version)
            return [SettingsUpdate("rejected", exc.version, str(exc))]
        except Exception as exc:
            return [SettingsUpdate("unreadable", None, f"{type(exc).__name__}: {exc}")]
        if latest is None:
            return [SettingsUpdate("unreadable", None, "no settings version after bootstrap")]
        self.current, self.version, self.loaded = latest.settings, latest.version, True
        return []

    async def refresh(self, now: datetime) -> list[SettingsUpdate]:
        try:
            latest = await self._store.latest()
            if latest is None and not self.loaded:
                # The store could not be read at start-up and is empty now: seed it.
                latest = await self._bootstrap(now)
        except SettingsInvalid as exc:
            if exc.version in self._rejected:
                return []
            self._rejected.add(exc.version)
            return [SettingsUpdate("rejected", exc.version, str(exc))]
        except Exception as exc:
            return [SettingsUpdate("unreadable", self.version, f"{type(exc).__name__}: {exc}")]
        if latest is None or latest.version == self.version:
            return []
        merged = merge_live(self.current, latest.settings)
        restart = restart_changes(self.current, latest.settings)
        diff = settings_diff(self.current, merged)
        self.current, self.version, self.loaded = merged, latest.version, True
        out = [SettingsUpdate("applied", latest.version, latest.author, diff)]
        if restart:
            out.append(SettingsUpdate("pending_restart", latest.version, ", ".join(restart)))
        return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_settings_store.py -q`
Expected: all pass.

- [ ] **Step 5: Break a guard on purpose**

In `refresh`, replace `merged = merge_live(self.current, latest.settings)` with `merged = latest.settings`. Run the tests. Expected: `test_refresh_keeps_restart_fields_and_says_a_restart_is_needed` FAILS. Restore; all pass.

- [ ] **Step 6: Lint, types, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add src/traider/settings_store.py tests/unit/test_settings_store.py
git commit -m "feat(settings): LiveSettings with bootstrap and last-good fallback"
```

---

### Task 4: Engine reads `Settings` and applies new versions live

**Files:**
- Modify: `src/traider/engine.py`
- Modify: `tests/unit/engine_harness.py` (new `settings_store` option)
- Test: `tests/unit/test_engine_settings.py`

**Interfaces:**
- Consumes: `Settings`, `LiveSettings`, `SettingsUpdate`, `MemorySettingsStore`.
- Produces:
  - `Engine.__init__(..., settings: LiveSettings | None = None)` (new keyword, last). With `None`, the engine uses `Settings.from_config(config)` and behaves exactly as before.
  - `Engine.settings -> Settings` (read-only property).
  - `Engine.SETTINGS_REFRESH_S = 10.0`.
  - Engine events: `settings_applied` `{version, author, diff}`, `settings_pending_restart` `{version, fields}`, `settings_rejected` `{version, detail}`.
  - Alerts: key `settings_applied:{version}` subject `Settings version {n} applied`; `settings_restart:{version}` subject `Settings version {n} needs a restart`; `settings_rejected:{version}` subject `Settings version {n} rejected`.
  - `_entries_halted` returns `"settings not loaded"` while a store is configured and nothing has loaded.
  - `Harness.create(..., settings_store=None)`: when given, builds `LiveSettings(store, Settings.from_config(config))`, awaits `start(clock.now())`, and passes it to the engine. Exposes `harness.live_settings`.

- [ ] **Step 1: Extend the harness**

In `tests/unit/engine_harness.py`, add imports:

```python
from traider.settings import Settings
from traider.settings_store import LiveSettings
```

Add `settings_store=None,` to the `create` keyword arguments (after `restart_of`). Just before `self.engine = Engine(`, add:

```python
        self.live_settings = None
        if settings_store is not None:
            self.live_settings = LiveSettings(settings_store, Settings.from_config(self.config))
            await self.live_settings.start(self.clock.now())
```

and pass `settings=self.live_settings,` as the last argument to `Engine(...)`.

- [ ] **Step 2: Write the failing tests**

```python
# tests/unit/test_engine_settings.py
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_engine_settings.py -q`
Expected: FAIL (`Engine.__init__() got an unexpected keyword argument 'settings'`).

- [ ] **Step 4: Implement in `src/traider/engine.py`**

Imports (add):

```python
from traider.settings import Settings
from traider.settings_store import LiveSettings, SettingsUpdate
```

Class constant (next to `CONTROL_REFRESH_S`):

```python
    SETTINGS_REFRESH_S = 10.0
```

`__init__` signature gains `settings: LiveSettings | None = None,` after `auth_seconds_left`. In the body, right after `self._auth_seconds_left = ...`:

```python
        self._live_settings = settings
        self._settings: Settings = (
            settings.current if settings is not None else Settings.from_config(config)
        )
        self._risk.limits = self._settings.risk
        self._settings_at: datetime | None = None
```

and change the symbol table line to:

```python
        self._symbols: dict[str, _SymbolState] = {
            s: _SymbolState() for s in self._settings.pinned_symbols
        }
```

Public property (next to `is_leader`):

```python
    @property
    def settings(self) -> Settings:
        return self._settings
```

In `_housekeeping`, before the control refresh:

```python
        if self._live_settings is not None and _due(
            self._settings_at, now, self.SETTINGS_REFRESH_S
        ):
            self._settings_at = now
            for update in await self._live_settings.refresh(now):
                await self._on_settings(update, now)
```

New method (put it after `_update_permissions`):

```python
    async def _on_settings(self, update: SettingsUpdate, now: datetime) -> None:
        live = self._live_settings
        assert live is not None
        version = update.version
        if update.kind == "applied":
            self._settings = live.current
            self._risk.limits = self._settings.risk
            await self._event(
                "settings_applied",
                {"version": version, "author": update.detail, "diff": update.diff},
                now,
            )
            changes = "\n".join(f"{k}: {old} -> {new}" for k, (old, new) in update.diff.items())
            await self._alerts.send(
                f"settings_applied:{version}",
                f"Settings version {version} applied",
                changes or "No change to the settings in force.",
            )
        elif update.kind == "pending_restart":
            await self._event(
                "settings_pending_restart", {"version": version, "fields": update.detail}, now
            )
            await self._alerts.send(
                f"settings_restart:{version}",
                f"Settings version {version} needs a restart",
                f"These fields only change when the bot restarts: {update.detail}. "
                "Everything else in the version is in force now.",
            )
        elif update.kind == "rejected":
            await self._event(
                "settings_rejected", {"version": version, "detail": update.detail}, now
            )
            await self._alerts.send(
                f"settings_rejected:{version}",
                f"Settings version {version} rejected",
                f"{update.detail}. The bot keeps the settings it was running with.",
            )
        else:
            self._log_throttled("settings", now, "settings unreadable: %s", update.detail)
```

In `_entries_halted`, as the first check:

```python
        if self._live_settings is not None and not self._live_settings.loaded:
            return "settings not loaded"
```

Replace every tunable read from `self._config` with `self._settings`:

| Where | Old | New |
|---|---|---|
| `_is_our_option` (2 places) | `self._config.symbols` | `self._settings.pinned_symbols` |
| `_manage_working_orders` | `self._config.order_timeout_s` | `self._settings.order_timeout_s` |
| `_apply_target` | `self._config.symbols` | `self._settings.pinned_symbols` |
| `_flatten_now` | `self._config.flatten_before_close_min` | `self._settings.flatten_before_close_min` |
| `_build_order` | `self._config.order_type`, `self._config.limit_offset_bps` | `self._settings.order_type`, `self._settings.limit_offset_bps` |
| `_handle_unknown_orders` | `self._config.cancel_unknown_orders`, `self._config.order_timeout_s` | `self._settings.cancel_unknown_orders`, `self._settings.order_timeout_s` |

`self._config.trading_mode` and `self._config.heartbeat_file` stay on `Config`.

- [ ] **Step 5: Confirm no tunable is still read from `Config` in the engine**

Run: `grep -n "self._config\." src/traider/engine.py`
Expected: only `trading_mode` and `heartbeat_file` lines.

- [ ] **Step 6: Explain the new events in the runbook**

`tests/unit/test_docs.py` requires the runbook's event table to list exactly the events the engine records. In `docs/runbook.md`, add these rows to the `| Event | Meaning |` table, after the `entries_halted` row:

```markdown
| `settings_applied` | A new settings version is in force. `diff` lists what changed. |
| `settings_pending_restart` | A new version changes fields that only apply after a restart. `fields` names them. |
| `settings_rejected` | The newest version does not validate. The bot kept the settings it had. |
```

Run: `uv run pytest tests/unit/test_engine_settings.py -q && uv run pytest -q`
Expected: all pass (the whole suite stays green: without a store nothing changes).

- [ ] **Step 7: Break guards on purpose**

1. Remove the `"settings not loaded"` check from `_entries_halted`. Expected: `test_no_entries_until_settings_have_loaded_once` FAILS. Restore.
2. In `_on_settings`, delete `self._risk.limits = self._settings.risk`. Expected: `test_a_new_risk_limit_applies_within_one_refresh` FAILS. Restore.

- [ ] **Step 8: Lint, types, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add src/traider/engine.py tests/unit/engine_harness.py tests/unit/test_engine_settings.py docs/runbook.md
git commit -m "feat(engine): apply settings versions live and fail closed until loaded"
```

---

### Task 5: Wire settings into the app and the feed

**Files:**
- Modify: `src/traider/config.py` (add `settings_table`)
- Modify: `src/traider/feed.py`
- Modify: `src/traider/app.py`
- Test: `tests/unit/test_config.py`, `tests/unit/test_feed.py`, `tests/integration/test_bot.py` (append)

**Interfaces:**
- Consumes: `Settings`, `LiveSettings`, `DynamoSettingsStore`.
- Produces:
  - `Config.settings_table: str | None = None` (env `TRAIDER_SETTINGS_TABLE`).
  - `Feed.__init__(..., settings: Settings | None = None)` — symbols, `allow_options`, chain span and strikes come from `settings` (default `Settings.from_config(config)`).
  - `app.describe(config: Config, settings: Settings | None = None) -> dict` — reports the settings in force.
  - `app.build_bot` loads settings (bootstrap if empty) **before** building the strategy, the feed and the engine, and passes `LiveSettings` to the engine. `Bot.settings: Settings` field (the settings it started with).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_config.py`:

```python
def test_settings_table_is_read_from_the_environment():
    cfg = Config.from_env({**BASE, "TRAIDER_SETTINGS_TABLE": "traider-dev-settings"})
    assert cfg.settings_table == "traider-dev-settings"
```

In `tests/unit/test_feed.py`, give the `make_feed` helper a `settings=None` keyword and pass `settings=settings` to `Feed(...)`. Then append:

```python
async def test_feed_takes_its_symbols_from_the_settings_it_is_given(schwab, client, signed_in):
    from traider.settings import Settings

    settings = Settings.from_config(Config(symbols=("SPY",))).model_copy(
        update={"pinned_symbols": ("QQQ",)}
    )
    async with make_feed(schwab, client, signed_in, settings=settings) as feed:
        assert feed._symbols == ("QQQ",)
```

Append to `tests/integration/test_bot.py` (the `world` and `aws` fixtures are already there):

```python
async def test_bot_seeds_the_settings_table_and_starts_on_its_values(world, aws):
    from traider.settings_store import DynamoSettingsStore

    boto3.client("dynamodb").create_table(
        TableName="traider-test-settings",
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
    world.sign_in()
    config = world.config(settings_table="traider-test-settings")
    await world.start(config)
    store = DynamoSettingsStore(boto3.resource("dynamodb").Table("traider-test-settings"))
    latest = await store.latest()
    assert latest is not None and latest.author == "bootstrap"
    assert world.bot.settings == latest.settings
    assert app.describe(config, world.bot.settings)["settings"] == "traider-test-settings"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_feed.py tests/integration/test_bot.py -q`
Expected: the three new tests FAIL.

- [ ] **Step 3: Implement**

`src/traider/config.py`, in `Config` under "Where things live":

```python
    settings_table: str | None = None
```

`src/traider/feed.py`:

```python
from traider.settings import Settings
```

`Feed.__init__` gains `settings: Settings | None = None,` (after `session`). Replace the uses of `config.symbols`, `config.risk.allow_options`, `config.option_chain_days` and `config.option_chain_strikes`:

```python
        chosen = settings if settings is not None else Settings.from_config(config)
        self._symbols = chosen.pinned_symbols
        ...
                http, client, tokens, sink=self, clock=clock, symbols=chosen.pinned_symbols, sleep=sleep
        ...
        self._chains_wanted = chosen.risk.allow_options
        self._chain_span = timedelta(days=chosen.option_chain_days)
        self._chain_strikes = chosen.option_chain_strikes
```

`src/traider/app.py`:

```python
from traider.settings import Settings
from traider.settings_store import DynamoSettingsStore, LiveSettings
```

Replace `describe` with:

```python
def describe(config: Config, settings: Settings | None = None) -> dict[str, Any]:
    """What the bot is set up to do, safe to log: no keys, no secrets, no sign-in link."""
    s = settings if settings is not None else Settings.from_config(config)
    return {
        "trading_mode": config.trading_mode,
        "symbols": list(s.pinned_symbols),
        "strategy": s.strategy,
        "strategy_params": s.strategy_params,
        "order_type": s.order_type,
        "limit_offset_bps": str(s.limit_offset_bps),
        "order_timeout_s": s.order_timeout_s,
        "flatten_before_close_min": s.flatten_before_close_min,
        "cancel_unknown_orders": s.cancel_unknown_orders,
        "feed": config.feed,
        "account": f"...{config.account_last4}" if config.account_last4 else "by hash or only one",
        "risk": {name: str(value) for name, value in s.risk.model_dump().items()},
        "control": config.control_param or f"static:{config.control}",
        "state": config.state_table or "memory",
        "settings": config.settings_table or "environment",
        "alerts": "sns" if config.alert_topic_arn else "log only",
        "token_store": (
            "secrets manager"
            if config.schwab_token_secret_id
            else "file"
            if config.schwab_token_file
            else "none"
        ),
    }
```

Add `settings: Settings` to the `Bot` dataclass (after `config`). In `Bot._announce` use `describe(self.config, self.settings)` and replace `self.config.symbols`, `self.config.strategy`, `self.config.strategy_params` with `self.settings.pinned_symbols`, `self.settings.strategy`, `self.settings.strategy_params`.

In `build_bot`, after `session = SessionTracker(...)` and before the strategy is created:

```python
    settings = Settings.from_config(config)
    live_settings: LiveSettings | None = None
    if config.settings_table:
        live_settings = LiveSettings(DynamoSettingsStore(aws.table(config.settings_table)), settings)
        for update in await live_settings.start(clock.now()):
            log.warning("settings at start-up: %s %s", update.kind, update.detail)
        settings = live_settings.current
    strategy = create_strategy(settings.strategy, settings.pinned_symbols, settings.strategy_params)
```

(delete the old `strategy = create_strategy(config.strategy, ...)` line). Pass `risk=RiskManager(settings.risk)`, `settings=live_settings` to `Engine(...)`, `settings=settings` to `Feed(...)`, and `settings=settings` to `Bot(...)`.

If `start` could not load, the engine refuses entries until a refresh succeeds (Task 4), and any restart-only field that differs is then announced.

- [ ] **Step 4: Run all bot tests**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Lint, types, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add src/traider/config.py src/traider/feed.py src/traider/app.py tests/
git commit -m "feat(app): load versioned settings before building the bot"
```

---

### Task 6: `traider settings` commands

**Files:**
- Modify: `src/traider/cli.py`
- Test: `tests/unit/test_cli_settings.py`

**Interfaces:**
- Consumes: `MemorySettingsStore`, `DynamoSettingsStore`, `Settings`, `SettingsConflict`.
- Produces:
  - `async settings_show(store, out: TextIO) -> int`
  - `async settings_history(store, out: TextIO, *, limit: int = 20) -> int`
  - `async settings_apply(store, path: str, out: TextIO, *, note: str, now: datetime) -> int` — exit 0 written, 1 invalid file or conflict, with the reason.
  - CLI: `traider settings show`, `traider settings history [--limit N]`, `traider settings apply FILE [--note TEXT]`. Needs `TRAIDER_SETTINGS_TABLE`; otherwise exits 2 with `TRAIDER_SETTINGS_TABLE is not set`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_cli_settings.py
"""`traider settings`: read and change the versioned settings from a terminal."""

import io
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from traider import cli
from traider.config import Config
from traider.settings import Settings
from traider.settings_store import MemorySettingsStore

T0 = datetime(2026, 10, 9, 13, 0, tzinfo=UTC)


async def seeded() -> MemorySettingsStore:
    store = MemorySettingsStore()
    await store.write(
        Settings.from_config(Config(symbols=("SPY",))),
        expected_version=0,
        author="bootstrap",
        note="",
        now=T0,
    )
    return store


async def test_show_prints_the_current_version_as_json():
    out = io.StringIO()
    assert await cli.settings_show(await seeded(), out) == 0
    text = out.getvalue()
    assert text.startswith("version 1 by bootstrap")
    body = json.loads(text.split("\n", 1)[1])
    assert body["pinned_symbols"] == ["SPY"]


async def test_show_on_an_empty_table_says_so():
    out = io.StringIO()
    assert await cli.settings_show(MemorySettingsStore(), out) == 1
    assert "no settings" in out.getvalue()


async def test_apply_writes_a_new_version_and_prints_the_diff(tmp_path):
    store = await seeded()
    body = (await store.latest()).settings.model_dump(mode="json")
    body["risk"]["max_order_usd"] = "250"
    path = tmp_path / "s.json"
    path.write_text(json.dumps(body))
    out = io.StringIO()
    assert await cli.settings_apply(store, str(path), out, note="smaller", now=T0) == 0
    latest = await store.latest()
    assert latest.version == 2 and latest.author == "cli" and latest.note == "smaller"
    assert latest.settings.risk.max_order_usd == Decimal(250)
    assert "risk.max_order_usd: 500 -> 250" in out.getvalue()


async def test_apply_refuses_an_invalid_file_and_writes_nothing(tmp_path):
    store = await seeded()
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"strategy": "nope"}))
    out = io.StringIO()
    assert await cli.settings_apply(store, str(path), out, note="", now=T0) == 1
    assert (await store.latest()).version == 1
    assert "invalid" in out.getvalue()


async def test_history_lists_versions_newest_first():
    store = await seeded()
    current = await store.latest()
    await store.write(current.settings, expected_version=1, author="cli", note="again", now=T0)
    out = io.StringIO()
    assert await cli.settings_history(store, out) == 0
    lines = out.getvalue().splitlines()
    assert lines[0].startswith("2 ") and "cli" in lines[0] and "again" in lines[0]
    assert lines[1].startswith("1 ")


def test_settings_needs_a_table(monkeypatch, capsys):
    monkeypatch.setenv("TRAIDER_SYMBOLS", "SPY")
    monkeypatch.delenv("TRAIDER_SETTINGS_TABLE", raising=False)
    assert cli.main(["settings", "show"]) == 2
    assert "TRAIDER_SETTINGS_TABLE is not set" in capsys.readouterr().err


def test_settings_help_works():
    with pytest.raises(SystemExit) as exit_:
        cli.main(["settings", "--help"])
    assert exit_.value.code == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_cli_settings.py -q`
Expected: FAIL (`AttributeError: module 'traider.cli' has no attribute 'settings_show'`).

- [ ] **Step 3: Implement in `src/traider/cli.py`**

Update the module docstring's first line to ``Command line: ``traider run | check | login | backtest | settings``.``

Imports:

```python
import json
from pydantic import ValidationError
from traider.settings import Settings
from traider.settings_store import DynamoSettingsStore, SettingsConflict, SettingsStore
```

(`datetime` is already imported.) Add a section before `# ---- main`:

```python
# ------------------------------------------------------------------------- settings


async def settings_show(store: SettingsStore, out: TextIO) -> int:
    latest = await store.latest()
    if latest is None:
        out.write("no settings stored yet; the bot writes version 1 when it first starts\n")
        return 1
    out.write(f"version {latest.version} by {latest.author} at {latest.at.isoformat()}\n")
    out.write(json.dumps(latest.settings.model_dump(mode="json"), indent=2) + "\n")
    return 0


async def settings_history(store: SettingsStore, out: TextIO, *, limit: int = 20) -> int:
    for version in await store.history(limit):
        changed = ", ".join(version.diff) or "-"
        out.write(
            f"{version.version} {version.at.isoformat()} {version.author} "
            f"[{changed}] {version.note}\n"
        )
    return 0


async def settings_apply(
    store: SettingsStore, path: str, out: TextIO, *, note: str, now: datetime
) -> int:
    try:
        with open(path, encoding="utf-8") as handle:
            settings = Settings.model_validate(json.load(handle))
    except (OSError, ValueError, ValidationError) as exc:
        out.write(f"invalid settings file: {exc}\n")
        return 1
    latest = await store.latest()
    expected = latest.version if latest is not None else 0
    try:
        written = await store.write(
            settings, expected_version=expected, author="cli", note=note, now=now
        )
    except SettingsConflict as exc:
        out.write(f"not written: {exc}. Run `traider settings show` and try again.\n")
        return 1
    out.write(f"wrote version {written.version}\n")
    for key, (old, new) in written.diff.items():
        out.write(f"  {key}: {old} -> {new}\n")
    return 0


def _settings_store(config: Config) -> SettingsStore:
    assert config.settings_table is not None
    return DynamoSettingsStore(app.Aws(config.aws_region).table(config.settings_table))
```

In `_parser`, after the backtest parser:

```python
    settings = commands.add_parser("settings", help="read or change the bot's versioned settings")
    actions = settings.add_subparsers(dest="action", required=True)
    actions.add_parser("show", help="print the settings in force")
    history = actions.add_parser("history", help="list earlier versions")
    history.add_argument("--limit", type=int, default=20)
    apply = actions.add_parser("apply", help="write a JSON file as the next version")
    apply.add_argument("file")
    apply.add_argument("--note", default="", help="why, kept with the version")
```

In `main`, after `logging.basicConfig(...)` and before the `try:`:

```python
    if args.command == "settings":
        if not config.settings_table:
            print("TRAIDER_SETTINGS_TABLE is not set", file=sys.stderr)
            return 2
        store = _settings_store(config)
        if args.action == "show":
            return asyncio.run(settings_show(store, sys.stdout))
        if args.action == "history":
            return asyncio.run(settings_history(store, sys.stdout, limit=args.limit))
        return asyncio.run(
            settings_apply(
                store, args.file, sys.stdout, note=args.note, now=SystemClock().now()
            )
        )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_cli_settings.py tests/unit/test_cli.py tests/unit/test_docs.py -q`
Expected: all pass.

- [ ] **Step 5: Lint, types, commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
git add src/traider/cli.py tests/unit/test_cli_settings.py
git commit -m "feat(cli): traider settings show, history and apply"
```

---

### Task 7: Infrastructure: settings table, permissions, environment

**Files:**
- Modify: `infra/data.py`, `infra/bot.py`, `infra/stack.py`
- Test: `infra/tests/test_stack.py`

**Interfaces:**
- Consumes: `Config.settings_table` (env `TRAIDER_SETTINGS_TABLE`).
- Produces: `Data.settings_table: aws.dynamodb.Table` (Pulumi name `settings`, table name `f"{prefix}-settings"`); bot env `TRAIDER_SETTINGS_TABLE`; bot task policy statement `Sid: "Settings"` with actions `["dynamodb:Query", "dynamodb:GetItem", "dynamodb:PutItem"]` on the settings table ARN; stack output `settingsTable`; `localEnv` gains `TRAIDER_SETTINGS_TABLE`.

- [ ] **Step 1: Update existing tests that assume one table**

In `infra/tests/test_stack.py`, every `paper.one("aws:dynamodb/table:Table")`, `live.one("aws:dynamodb/table:Table")` and `paper.one(TABLE)` becomes `...one(TABLE, "state")`. Check: `grep -n 'dynamodb/table:Table")\|one(TABLE)' infra/tests/test_stack.py` returns nothing afterwards.

In `test_each_bot_permission_names_exactly_the_resource_it_is_for`, add to the expected dict:

```python
        "Settings": paper.one(TABLE, "settings").arn,
```

- [ ] **Step 2: Write the new failing tests (append to `infra/tests/test_stack.py`)**

```python
def test_settings_table_is_on_demand_with_point_in_time_recovery(paper):
    table = paper.one(TABLE, "settings").inputs
    assert table["name"] == "traider-dev-settings"
    assert table["billingMode"] == "PAY_PER_REQUEST"
    assert (table["hashKey"], table["rangeKey"]) == ("pk", "sk")
    assert table["pointInTimeRecovery"] == {"enabled": True}


def test_live_settings_table_is_protected_from_deletion(live, paper):
    assert live.one(TABLE, "settings").inputs["deletionProtectionEnabled"] is True
    assert not paper.one(TABLE, "settings").inputs.get("deletionProtectionEnabled")


def test_bot_is_told_where_its_settings_live(paper):
    assert environment(paper)["TRAIDER_SETTINGS_TABLE"] == paper.one(TABLE, "settings").inputs["name"]
    assert paper.outputs["settingsTable"] == paper.one(TABLE, "settings").inputs["name"]


def test_bot_can_read_and_add_settings_versions_but_not_delete_or_rewrite_them(paper):
    (statement,) = [s for s in paper.policy("bot-task") if s["Sid"] == "Settings"]
    assert set(statement["Action"]) == {"dynamodb:Query", "dynamodb:GetItem", "dynamodb:PutItem"}


def test_local_env_lets_the_cli_edit_settings(paper):
    assert local_env(paper)["TRAIDER_SETTINGS_TABLE"] == paper.one(TABLE, "settings").inputs["name"]
```

- [ ] **Step 3: Run them to verify they fail**

Run (from `infra/`): `uv run pytest -q`
Expected: new tests FAIL; the edited existing ones pass.

- [ ] **Step 4: Implement**

`infra/data.py`: add `settings_table: aws.dynamodb.Table` to `Data` (after `table`), and after the state table:

```python
    # Versioned bot settings. Each change is a new item and nothing is ever rewritten,
    # so the history is the audit trail.
    settings_table = aws.dynamodb.Table(
        "settings",
        name=f"{prefix}-settings",
        billing_mode="PAY_PER_REQUEST",
        hash_key="pk",
        range_key="sk",
        attributes=[
            aws.dynamodb.TableAttributeArgs(name="pk", type="S"),
            aws.dynamodb.TableAttributeArgs(name="sk", type="S"),
        ],
        point_in_time_recovery=aws.dynamodb.TablePointInTimeRecoveryArgs(enabled=True),
        deletion_protection_enabled=settings.trading_mode == "live",
        tags=tags,
    )
```

and return `Data(app_secret, token_secret, control, table, settings_table, topic)`.

`infra/bot.py`: in the task policy statements (next to the `"State"` statement), add:

```python
                    {
                        "Sid": "Settings",
                        "Effect": "Allow",
                        "Action": ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:PutItem"],
                        "Resource": data.settings_table.arn,
                    },
```

and in `environment`: `"TRAIDER_SETTINGS_TABLE": data.settings_table.name,`.

(The bot writes only version 1, through a conditional put. `UpdateItem` and `DeleteItem` are not granted, so it cannot rewrite history.)

`infra/stack.py`: in `local`, add `"TRAIDER_SETTINGS_TABLE": store.settings_table.name,`; in outputs add `"settingsTable": store.settings_table.name,`. Update the comment above `local` to say the settings table is included so `traider settings` works locally, and that settings cannot place orders.

- [ ] **Step 5: Run infra tests, lint, types**

Run (from `infra/`): `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add infra/
git commit -m "feat(infra): settings table, bot access and local env"
```

---

### Task 8: Docs, final verification, PR

**Files:**
- Modify: `README.md`, `docs/runbook.md`, `infra/Pulumi.example.yaml`, `docs/superpowers/specs/2026-10-09-research-driven-trading-design.md` (record the two deviations)

- [ ] **Step 1: Runbook event table**

Already done in Task 4. Check the three `settings_*` rows are there.

- [ ] **Step 2: Runbook alerts**

Add rows to the alerts table, after "Engine error":

```markdown
| Settings version N applied | A new version of the settings is in force; the alert lists each change. | Nothing, if you made it. If you did not, set `halt` and look at `traider settings history`. |
| Settings version N needs a restart | The version changes the strategy, its parameters, the pinned symbols, the option-chain span or whether options are allowed. Those wait for a restart; the rest applies now. | Restart when convenient (see below), or tomorrow's 09:00 start does it. |
| Settings version N rejected | The newest version does not validate. | Fix it with `traider settings apply`. The bot runs on the last good version meanwhile. |
```

- [ ] **Step 3: Runbook "Restarting, changing settings, tearing down"**

Replace the `**Change a setting or the code:**` paragraph with:

```markdown
**Change a setting:** settings are versioned in the settings table, and the running
bot picks up a new version within about 10 seconds. With the `localEnv` output
loaded:

```sh
uv run --env-file .env traider settings show > settings.json
# edit settings.json
uv run --env-file .env traider settings apply settings.json --note "why"
uv run --env-file .env traider settings history
```

Limits, order settings and flattening apply at once. The strategy, its parameters,
the pinned symbols, the option-chain span and `allow_options` wait for a restart.
To go back, apply an older version's body as a new version. Stack settings in
Pulumi only seed version 1 when the table is empty; after that the table wins.

**Change the code:** edit, then `pulumi up`. Same stop-then-start.
```

(Nested code fences: the outer block above is just this plan's formatting; in the runbook, write the `sh` block normally.)

Also add the settings table to the deletion-protection paragraph under **Remove everything**: "A live stack's state and settings tables are protected against deletion; lift that first:" and show the same `update-table` command for `"$(pulumi stack output settingsTable)"`.

- [ ] **Step 4: README**

In `README.md` "Configuration", after the first paragraph, add:

```markdown
**Settings are versioned.** The first time a deployed bot starts it copies the stack's
settings into its settings table as version 1. From then on that table is the source:
every change is a new numbered version, the running bot applies it within about 10
seconds, and an alert lists what changed. `traider settings show | history | apply`
reads and writes it (see the [runbook](docs/runbook.md#restarting-changing-settings-tearing-down)).
A few fields (strategy, its parameters, pinned symbols, option-chain span,
`allow_options`) wait for the next restart. Until the bot has read a valid version
it opens no new positions; after that, an unreadable table or a bad version leaves
the last good settings in force.
```

Check the anchor exists: the heading is `## Restarting, changing settings, tearing down` → `restarting-changing-settings-tearing-down`.

- [ ] **Step 5: Pulumi example**

In `infra/Pulumi.example.yaml`, in the header comment, add: "These settings seed the bot's versioned settings table the first time it starts. After that, change them with `traider settings apply`: a `pulumi up` does not overwrite a table that already has versions."

- [ ] **Step 6: Spec deviations**

In the spec, section A.1, add a paragraph: "Implementation note: `Config` keeps its tunable fields as the bootstrap source; the engine, feed and strategy read `Settings` at runtime." In A.2, replace the `CURRENT` row and the transaction sentence with: "The current version is the highest-numbered `V#` item. A write is one conditional put of `V#n+1` (`attribute_not_exists`), so two editors cannot both write version n+1."

- [ ] **Step 7: Full verification**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
cd infra && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q && cd ..
```

Expected: everything passes. Paste the summary lines into the PR description.

- [ ] **Step 8: Commit, push, open the PR**

```bash
git add README.md docs/ infra/Pulumi.example.yaml
git commit -m "docs: versioned settings in README and runbook"
git fetch origin main && git rebase origin/main
git push -u origin feat/versioned-settings
gh pr create --title "feat: versioned, live-reloaded settings" --body-file - <<'EOF'
Sub-project A1 from docs/superpowers/specs/2026-10-09-research-driven-trading-design.md.

- Settings move to a versioned DynamoDB table; the bot seeds version 1 from the environment.
- The running bot applies new versions within ~10 s; strategy, pinned symbols and option-chain settings wait for a restart (alerted).
- Fail closed: no entries until settings have loaded once; a bad or unreadable version keeps the last good one.
- `traider settings show | history | apply`.
- Infra: settings table, least-privilege access (no update/delete), localEnv.

Verified: (paste test summaries)
Not verified: anything against real AWS (`pulumi preview` not run).

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01Xh7zur22CRYEGaDoTB5nkV
EOF
```
