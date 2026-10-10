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

Score = Annotated[int, Field(strict=True, ge=0, le=100)]
Finite = Annotated[float, Field(allow_inf_nan=False)]
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
    rank: int = Field(strict=True, ge=1, le=999)
    symbol: str
    side: PickSide
    horizon: Horizon
    score: Score
    pre_score: Score
    thesis: str = Field(max_length=2000)
    invalidation: Decimal = Field(gt=0)
    earnings_date: date | None = None
    expires_at: datetime
    features: dict[str, Finite] = Field(default_factory=dict)

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
    metrics: dict[str, Finite] = Field(default_factory=dict)

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
    # Added for the research jobs (C1). Defaulted, so items written before still parse.
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    notes: tuple[Annotated[str, Field(max_length=300)], ...] = ()
    counts: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)

    @field_validator("started_at")
    @classmethod
    def _started_aware(cls, value: datetime) -> datetime:
        return _aware(value)

    @field_validator("finished_at")
    @classmethod
    def _finished_aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware(value)


# --- the scorecard (C2a) ------------------------------------------------------------------

Percent = Annotated[float, Field(allow_inf_nan=False)]
Price = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class OutcomeStatus(StrEnum):
    PENDING = "pending"  # no entry price yet (the pick day's bar is missing)
    PARTIAL = "partial"  # some returns known, the 20-day one not yet
    FINAL = "final"  # the 20-day close and the expiry are known, or 30 weekdays have passed


def outcome_key(run_id: str, rank: int) -> str:
    """The partition key of a pick's outcome: ``PICK#<run_id>#<rank:03d>``."""
    return f"PICK#{run_id}#{rank:03d}"


class PickOutcome(BaseModel):
    """How one pick did, from Schwab daily bars. Every return is a percentage, signed by
    the pick's side: a long gains when the price rises, a bearish pick when it falls."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, max_length=100)
    rank: int = Field(strict=True, ge=1, le=999)
    symbol: str
    side: PickSide
    horizon: Horizon
    score: Score
    pre_score: Score
    llm_score: Score | None = None  # None for a hand-made pick
    pick_day: date
    run_status: RunStatus
    entry: Price | None = None  # the pick day's open
    price_at_pick: Price | None = None
    ret_0d: Percent | None = None  # intraday picks only: entry to the pick day's close
    ret_1d: Percent | None = None
    ret_5d: Percent | None = None
    ret_20d: Percent | None = None
    mfe_pct: Percent | None = None
    mae_pct: Percent | None = None
    hit_invalidation: bool | None = None
    expired_return: Percent | None = None
    traded: bool | None = None  # None: the bot's event log could not be read
    status: OutcomeStatus
    updated_at: datetime

    @property
    def key(self) -> str:
        return outcome_key(self.run_id, self.rank)

    @field_validator("symbol")
    @classmethod
    def _symbol_ok(cls, value: str) -> str:
        return check_symbols((value,))[0]

    @field_validator("run_status")
    @classmethod
    def _finished_run(cls, value: RunStatus) -> RunStatus:
        if value not in (RunStatus.OK, RunStatus.PARTIAL):
            raise ValueError("only picks from ok or partial runs are scored")
        return value

    @field_validator("updated_at")
    @classmethod
    def _updated_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class HorizonStats(BaseModel):
    """Picks whose return over one horizon became known today."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    matured: int = Field(default=0, ge=0)
    hits: int = Field(default=0, ge=0)  # return above zero
    mean_pct: Percent | None = None


class ScoreBucket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    low: Score
    high: Score
    count: int = Field(ge=0)
    mean_ret_1d_pct: Percent | None = None


class ScoreSummary(BaseModel):
    """One scorecard run's summary, item ``SCORE#<day>`` / ``SUMMARY``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    day: date
    run_id: str = Field(min_length=1, max_length=100)
    picks: int = Field(ge=0)
    by_kind: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    by_side: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    ret_1d: HorizonStats = Field(default_factory=HorizonStats)
    ret_5d: HorizonStats = Field(default_factory=HorizonStats)
    buckets: tuple[ScoreBucket, ...] = ()
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def _updated_aware(cls, value: datetime) -> datetime:
        return _aware(value)
