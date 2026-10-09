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
    ResearchSettings,
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

    # May be empty when research chooses the symbols. With no research, LiveSettings
    # refuses an empty list (Task 11).
    pinned_symbols: tuple[str, ...] = ()
    strategy: str = "sma_cross"
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    research: ResearchSettings = Field(default_factory=ResearchSettings)
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
        from traider.strategy import create_strategy  # noqa: PLC0415 - see the comment above

        create_strategy(self.strategy, self.pinned_symbols, self.strategy_params)
        return self

    @classmethod
    def from_config(cls, config: Config) -> Settings:
        return cls(
            pinned_symbols=config.symbols,
            strategy=config.strategy,
            strategy_params=config.strategy_params,
            risk=config.risk,
            research=config.research,
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
