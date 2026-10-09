"""Stack configuration: read it, turn it into the bot's environment, and validate it.

Validation uses the bot's own configuration code, so a misspelled limit or a bad
strategy parameter fails ``pulumi preview`` rather than crash-looping the task.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import pulumi
from traider.config import Config, ConfigError
from traider.strategy import create_strategy

# Stack config key -> bot environment variable, for plain values.
_PASS_THROUGH = {
    "strategy": "TRAIDER_STRATEGY",
    "orderType": "TRAIDER_ORDER_TYPE",
    "limitOffsetBps": "TRAIDER_LIMIT_OFFSET_BPS",
    "orderTimeoutS": "TRAIDER_ORDER_TIMEOUT_S",
    "flattenBeforeCloseMin": "TRAIDER_FLATTEN_BEFORE_CLOSE_MIN",
    "feed": "TRAIDER_FEED",
    "pollIntervalS": "TRAIDER_POLL_INTERVAL_S",
    "paperStartingCash": "TRAIDER_PAPER_STARTING_CASH",
    "accountLast4": "TRAIDER_SCHWAB_ACCOUNT_LAST4",
    "accountHash": "TRAIDER_SCHWAB_ACCOUNT_HASH",
    "logLevel": "TRAIDER_LOG_LEVEL",
}
_JSON = {"strategyParams": "TRAIDER_STRATEGY_PARAMS", "risk": "TRAIDER_RISK"}
_BOOL = {"cancelUnknownOrders": "TRAIDER_CANCEL_UNKNOWN_ORDERS"}


@dataclass(frozen=True)
class Settings:
    prefix: str  # e.g. traider-dev: unique per stack
    trading_mode: str
    symbols: tuple[str, ...]
    bot_env: dict[str, str]  # everything the bot needs that is known before deploy
    alert_email: str | None
    callback_url: str | None  # override; None means "the hosted callback"
    always_on: bool
    start_time: tuple[int, int]  # New York time, weekdays
    stop_time: tuple[int, int]
    cpu_architecture: str
    image: str | None
    log_retention_days: int
    reauth_warn_hours: int
    watchdog_schedule: str
    tags: dict[str, str]


def _time(config: pulumi.Config, key: str, default: str) -> tuple[int, int]:
    raw = config.get(key) or default
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise ValueError(f"traider:{key} must be a time like 09:00 (New York time), got {raw!r}")
    return int(match[1]), int(match[2])


def _symbols(config: pulumi.Config) -> tuple[str, ...]:
    raw: Any = config.get_object("symbols")
    if raw is None:
        raise ValueError(
            "traider:symbols is required, for example: pulumi config set "
            "--path 'traider:symbols[0]' SPY"
        )
    if isinstance(raw, str):
        raw = raw.split(",")
    return tuple(str(item).strip().upper() for item in raw if str(item).strip())


def load() -> Settings:
    config = pulumi.Config()
    prefix = f"{pulumi.get_project()}-{pulumi.get_stack()}"
    symbols = _symbols(config)
    mode = config.get("tradingMode") or "paper"

    env: dict[str, str] = {"TRAIDER_TRADING_MODE": mode, "TRAIDER_SYMBOLS": ",".join(symbols)}
    for key, name in _PASS_THROUGH.items():
        value = config.get(key)
        if value is not None and value != "":
            env[name] = str(value)
    for key, name in _JSON.items():
        value = config.get_object(key)
        if value is not None:
            env[name] = json.dumps(value)
    for key, name in _BOOL.items():
        flag = config.get_bool(key)
        if flag is not None:
            env[name] = "true" if flag else "false"

    # Check it the way the bot will. The two placeholders stand in for resources
    # this stack creates, which the bot requires in live mode.
    try:
        checked = Config.from_env(
            {**env, "TRAIDER_CONTROL_PARAM": "/placeholder", "TRAIDER_STATE_TABLE": "placeholder"}
        )
        create_strategy(checked.strategy, checked.symbols, checked.strategy_params)
    except (ConfigError, ValueError) as exc:
        raise ValueError(f"the bot would reject this configuration: {exc}") from None

    alert_email = config.get("alertEmail")
    if mode == "live" and not alert_email:
        raise ValueError(
            "traider:alertEmail is required for live trading: the bot must be able to "
            "tell you when the Schwab sign-in lapses or an order needs a look"
        )

    architecture = (config.get("cpuArchitecture") or "ARM64").upper()
    if architecture not in ("ARM64", "X86_64"):
        raise ValueError("traider:cpuArchitecture must be ARM64 or X86_64")

    start_time = _time(config, "startTime", "09:00")
    stop_time = _time(config, "stopTime", "16:30")
    if stop_time <= start_time:
        raise ValueError("traider:stopTime must be later in the day than traider:startTime")

    return Settings(
        prefix=prefix,
        trading_mode=mode,
        symbols=symbols,
        bot_env=env,
        alert_email=alert_email,
        callback_url=config.get("schwabCallbackUrl"),
        always_on=bool(config.get_bool("alwaysOn")),
        start_time=start_time,
        stop_time=stop_time,
        cpu_architecture=architecture,
        image=config.get("image"),
        log_retention_days=config.get_int("logRetentionDays") or 30,
        reauth_warn_hours=config.get_int("reauthWarnHours") or 48,
        watchdog_schedule=config.get("watchdogSchedule") or "cron(0 16 * * ? *)",
        tags={"project": pulumi.get_project(), "stack": pulumi.get_stack()},
    )
