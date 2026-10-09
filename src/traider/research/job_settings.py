"""How the research jobs run: thresholds, limits, models and budgets.

Part of the versioned settings (``Settings.research_jobs``), so the web app can tune it
like everything else. Each research run reads the current version once, when it starts.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from traider.config import check_symbols

DEFAULT_MODEL = "anthropic.claude-sonnet-5-5"

Weight = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Usd = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Price = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class _Group(BaseModel):
    # Not pydantic's strict mode: versions round-trip through JSON in the settings table,
    # where dates and decimals are strings.
    model_config = ConfigDict(extra="forbid", frozen=True)


class CollectSettings(_Group):
    max_candidates: Annotated[int, Field(ge=1, le=500)] = 150
    earnings_lookahead_days: Annotated[int, Field(ge=1, le=30)] = 10
    market_news_count: Annotated[int, Field(ge=0, le=100)] = 30


class PostureSettings(_Group):
    vix_reduced: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 25.0
    vix_stand_aside: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 35.0
    gap_reduced_pct: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 1.5
    gap_stand_aside_pct: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 3.0
    reduce_below_sma50: bool = True
    reduced_days: tuple[date, ...] = ()
    stand_aside_days: tuple[date, ...] = ()

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.vix_reduced >= self.vix_stand_aside:
            raise ValueError("vix_reduced must be below vix_stand_aside")
        if self.gap_reduced_pct >= self.gap_stand_aside_pct:
            raise ValueError("gap_reduced_pct must be below gap_stand_aside_pct")
        return self


class ScreenWeights(_Group):
    move: Weight = 0.35
    participation: Weight = 0.25
    liquidity: Weight = 0.15
    catalyst: Weight = 0.15
    alignment: Weight = 0.10

    @model_validator(mode="after")
    def _sum_to_one(self) -> Self:
        total = self.move + self.participation + self.liquidity + self.catalyst + self.alignment
        if not math.isclose(total, 1.0, rel_tol=0, abs_tol=1e-9):
            raise ValueError(f"screen weights must sum to 1, not {total}")
        return self


class ScreenSettings(_Group):
    min_price: Usd = Decimal(5)
    max_price: Usd = Decimal(1000)
    min_dollar_volume: Usd = Decimal(20_000_000)
    allow_etfs: bool = False
    deep_dive_count: Annotated[int, Field(ge=1, le=30)] = 12
    weights: ScreenWeights = Field(default_factory=ScreenWeights)

    @model_validator(mode="after")
    def _price_range(self) -> Self:
        if self.min_price >= self.max_price:
            raise ValueError("min_price must be below max_price")
        return self


class DiveSettings(_Group):
    model: Annotated[str, Field(min_length=1, max_length=200)] = DEFAULT_MODEL
    posture_model: Annotated[str, Field(min_length=1, max_length=200)] = DEFAULT_MODEL
    max_tool_calls: Annotated[int, Field(ge=0, le=20)] = 6
    max_turns: Annotated[int, Field(ge=1, le=20)] = 8
    max_tokens: Annotated[int, Field(ge=256, le=8000)] = 2000
    max_dive_input_tokens: Annotated[int, Field(ge=1000, le=500_000)] = 60_000
    dive_timeout_s: Annotated[float, Field(ge=10, le=900)] = 180.0
    dive_concurrency: Annotated[int, Field(ge=1, le=8)] = 4
    tool_result_max_chars: Annotated[int, Field(ge=500, le=50_000)] = 6000


class RankSettings(_Group):
    llm_weight: Weight = 0.7
    min_stop_atr: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 0.3
    max_stop_atr: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 3.0
    max_put_spread_pct: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] = 10.0
    min_put_oi: Annotated[int, Field(ge=0)] = 100
    max_per_sector: Annotated[int, Field(ge=1, le=25)] = 3
    max_picks: Annotated[int, Field(ge=1, le=25)] = 10

    @model_validator(mode="after")
    def _stop_range(self) -> Self:
        if self.min_stop_atr >= self.max_stop_atr:
            raise ValueError("min_stop_atr must be below max_stop_atr")
        return self


class ModelPrice(_Group):
    """Dollars per million tokens."""

    in_per_mtok: Price
    out_per_mtok: Price


def _default_prices() -> dict[str, ModelPrice]:
    # From a third-party listing. Check against AWS's Bedrock pricing page.
    return {DEFAULT_MODEL: ModelPrice(in_per_mtok=Decimal(2), out_per_mtok=Decimal(10))}


class BudgetSettings(_Group):
    run_usd: Usd = Decimal("3.00")
    day_usd: Usd = Decimal("8.00")
    prices: dict[str, ModelPrice] = Field(default_factory=_default_prices)

    @model_validator(mode="after")
    def _run_within_day(self) -> Self:
        if self.run_usd > self.day_usd:
            raise ValueError("run_usd cannot exceed day_usd")
        return self


class ResearchJobSettings(_Group):
    enabled: bool = True
    # Names the owner wants looked at. Optional: research finds its own candidates.
    watchlist: tuple[str, ...] = ()
    max_run_s: Annotated[float, Field(ge=60, le=3600)] = 1200.0
    collect: CollectSettings = Field(default_factory=CollectSettings)
    posture: PostureSettings = Field(default_factory=PostureSettings)
    screen: ScreenSettings = Field(default_factory=ScreenSettings)
    dive: DiveSettings = Field(default_factory=DiveSettings)
    rank: RankSettings = Field(default_factory=RankSettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)

    @field_validator("watchlist")
    @classmethod
    def _watchlist_ok(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 50:
            raise ValueError("at most 50 watchlist symbols")
        for symbol in value:
            check_symbols((symbol,))
        if len(set(value)) != len(value):
            raise ValueError("duplicate watchlist symbols")
        return value

    @model_validator(mode="after")
    def _models_have_prices(self) -> Self:
        # Without a price the cost of a call cannot be bounded.
        for model in (self.dive.model, self.dive.posture_model):
            if model not in self.budget.prices:
                raise ValueError(f"model {model!r} has no price in budget.prices")
        return self
