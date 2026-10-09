"""Configuration, read from ``TRAIDER_*`` environment variables and validated.

Bad configuration must stop the process at start-up with a clear message. A
trading bot that guesses at a misspelled limit is worse than one that refuses
to start.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

PREFIX = "TRAIDER_"
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9./-]{0,9}$")

PositiveDecimal = Annotated[Decimal, Field(gt=0)]
PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]


class ConfigError(ValueError):
    """Raised when the environment does not describe a valid configuration."""


class RiskLimits(BaseModel):
    """Hard limits applied to every order. Defaults are deliberately small."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Size caps. Entries (buys) must fit inside all three.
    max_order_usd: PositiveDecimal = Decimal(500)
    max_position_usd: PositiveDecimal = Decimal(1000)
    max_total_exposure_usd: PositiveDecimal = Decimal(2000)
    max_shares_per_order: PositiveInt = 500
    # Buys must be covered by cash the broker reports as available. No margin.
    require_cash: bool = True
    # ... and not by what the bot sold today: that money settles the next business
    # day, and in a cash account trading on it is a good-faith violation. Margin
    # accounts do not have that problem and can turn this off.
    settled_cash_only: bool = True

    # Options. Off unless switched on. Only ever long calls and long puts, bought to
    # open and sold to close, so the most a position can lose is what was paid for it.
    # The dollar caps above apply to the premium (price x 100 x contracts).
    allow_options: bool = False
    max_contracts_per_order: PositiveInt = 5
    min_option_price: PositiveDecimal = Decimal("0.05")
    max_option_spread_bps: PositiveDecimal = Decimal(1000)
    # Do not buy an option with fewer calendar days than this left. 1 means never on
    # its last day.
    min_days_to_expiry: Annotated[int, Field(ge=1)] = 1
    # On an option's last day, sell it this many minutes before the close whatever the
    # strategy says: one left to expire in the money is exercised into shares.
    option_expiry_exit_min: Annotated[int, Field(ge=1)] = 60

    # Activity caps.
    max_orders_per_day: PositiveInt = 20
    order_cooldown_s: Annotated[float, Field(ge=0)] = 30.0
    max_daily_loss_usd: PositiveDecimal = Decimal(100)

    # Market-data sanity.
    min_price: PositiveDecimal = Decimal(5)
    max_spread_bps: PositiveDecimal = Decimal(20)
    max_quote_age_s: PositiveFloat = 15.0
    # ...and how far behind the market's own timestamp on the quote may be. Catches a
    # frozen or halted quote that keeps arriving. Quiet symbols may need this raised.
    max_quote_lag_s: PositiveFloat = 120.0
    max_feed_silence_s: PositiveFloat = 30.0
    max_limit_deviation_bps: PositiveDecimal = Decimal(100)

    # Session window for entries.
    entry_delay_min_after_open: Annotated[int, Field(ge=0)] = 1
    entry_cutoff_min_before_close: Annotated[int, Field(ge=0)] = 5

    # No new entries when the Schwab refresh token is this close to expiring.
    min_token_hours_for_entry: Annotated[float, Field(ge=0)] = 2.0

    @model_validator(mode="after")
    def _caps_are_ordered(self) -> Self:
        if self.max_order_usd > self.max_position_usd:
            raise ValueError("max_order_usd cannot exceed max_position_usd")
        if self.max_position_usd > self.max_total_exposure_usd:
            raise ValueError("max_position_usd cannot exceed max_total_exposure_usd")
        return self


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # What to trade and how.
    trading_mode: Literal["paper", "live"] = "paper"
    symbols: tuple[str, ...]
    strategy: str = "sma_cross"
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    order_type: Literal["LIMIT", "MARKET"] = "LIMIT"
    limit_offset_bps: Annotated[Decimal, Field(ge=0, le=100)] = Decimal(5)
    order_timeout_s: PositiveFloat = 20.0
    flatten_before_close_min: Annotated[int, Field(ge=1)] | None = None
    # Cancel open orders on our symbols that this process did not place. Only
    # sensible in an account nothing else trades in.
    cancel_unknown_orders: bool = False

    # Market data.
    feed: Literal["stream", "poll"] = "stream"
    poll_interval_s: Annotated[float, Field(ge=1)] = 5.0
    # The option chain a strategy gets to choose from (only loaded when risk.allow_options
    # is on): expiries up to this many days out, this many strikes around the money.
    option_chain_days: Annotated[int, Field(ge=1, le=365)] = 45
    option_chain_strikes: Annotated[int, Field(ge=1, le=100)] = 20

    # Paper trading.
    paper_starting_cash: PositiveDecimal = Decimal(10000)

    # Which Schwab account.
    account_hash: str | None = None
    account_last4: str | None = None

    # Where things live. Unset means "local": memory state, static control.
    aws_region: str | None = None
    schwab_app_secret_id: str | None = None
    schwab_token_secret_id: str | None = None
    control_param: str | None = None
    control: str = "paper"  # used only when control_param is unset
    state_table: str | None = None
    alert_topic_arn: str | None = None
    reauth_url: str | None = None

    # Local-development alternatives to Secrets Manager.
    schwab_app_key: str | None = None
    schwab_app_secret: str | None = None
    schwab_token_file: str | None = None
    schwab_callback_url: str | None = None

    heartbeat_file: str = "/tmp/traider-heartbeat"  # noqa: S108
    log_level: str = "INFO"

    @field_validator("symbols")
    @classmethod
    def _symbols_ok(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("at least one symbol is required")
        if len(value) > 25:
            raise ValueError("at most 25 symbols")
        for symbol in value:
            if not _SYMBOL.match(symbol):
                raise ValueError(f"not a valid equity symbol: {symbol!r}")
        if len(set(value)) != len(value):
            raise ValueError("duplicate symbols")
        return value

    @field_validator("account_last4")
    @classmethod
    def _last4_ok(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"\d{4}", value):
            raise ValueError("account_last4 must be the last four digits of the account number")
        return value

    @model_validator(mode="after")
    def _live_needs_everything(self) -> Self:
        if self.trading_mode != "live":
            return self
        if not (self.account_hash or self.account_last4):
            raise ValueError("live mode needs an explicit account (account_last4 or account_hash)")
        if not self.control_param:
            raise ValueError("live mode needs control_param so the kill switch works")
        if not self.state_table:
            raise ValueError("live mode needs state_table so limits survive a restart")
        return self

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        values: dict[str, Any] = {}
        for key, raw in env.items():
            if not key.startswith(PREFIX):
                continue
            if raw.strip() == "":
                continue  # blank means unset
            name = key[len(PREFIX) :].lower()
            values[_ENV_ALIASES.get(name, name)] = _convert(key, name, raw)
        if "symbols" not in values:
            raise ConfigError(f"{PREFIX}SYMBOLS is required, for example {PREFIX}SYMBOLS=SPY,QQQ")
        if "aws_region" not in values and env.get("AWS_REGION"):
            values["aws_region"] = env["AWS_REGION"]
        try:
            return cls.model_validate(values)
        except ValidationError as exc:
            raise ConfigError(_describe(exc)) from exc


_ENV_ALIASES = {
    "schwab_account_hash": "account_hash",
    "schwab_account_last4": "account_last4",
}
_JSON_FIELDS = {"strategy_params", "risk"}


def _convert(key: str, name: str, raw: str) -> Any:
    if name == "symbols":
        return tuple(part.strip().upper() for part in raw.split(",") if part.strip())
    if name in _JSON_FIELDS:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{key} is not valid JSON: {exc}") from exc
    return raw.strip()


def _describe(exc: ValidationError) -> str:
    problems = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error["loc"]) or "config"
        problems.append(f"{where}: {error['msg']}")
    return "invalid configuration: " + "; ".join(problems)
