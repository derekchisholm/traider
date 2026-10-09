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
    "optionChainDays": "TRAIDER_OPTION_CHAIN_DAYS",
    "optionChainStrikes": "TRAIDER_OPTION_CHAIN_STRIKES",
    "paperStartingCash": "TRAIDER_PAPER_STARTING_CASH",
    "accountLast4": "TRAIDER_SCHWAB_ACCOUNT_LAST4",
    "accountHash": "TRAIDER_SCHWAB_ACCOUNT_HASH",
    "logLevel": "TRAIDER_LOG_LEVEL",
}
_JSON = {
    "strategyParams": "TRAIDER_STRATEGY_PARAMS",
    "risk": "TRAIDER_RISK",
    "researchSettings": "TRAIDER_RESEARCH",
}
_BOOL = {"cancelUnknownOrders": "TRAIDER_CANCEL_UNKNOWN_ORDERS"}


@dataclass(frozen=True)
class Settings:
    prefix: str  # e.g. traider-dev: unique per stack
    trading_mode: str
    symbols: tuple[str, ...]  # the pinned symbols; may be empty when research is on
    research: bool
    research_jobs: bool  # the scheduled research runs; needs research
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


def trail_bucket_name(prefix: str, account: str) -> str:
    """The research trail bucket. It carries the account id because bucket names are global."""
    return f"{prefix}-research-trail-{account}"


def _check_trail_bucket_name(prefix: str) -> None:
    """Fail at load, not halfway through a deploy, when the stack's prefix cannot make a
    valid bucket name: 3 to 63 lowercase letters, digits, dots and hyphens, starting and
    ending with a letter or digit. The account id is always 12 digits."""
    name = trail_bucket_name(prefix, "0" * 12)
    valid = re.fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]", name) and ".." not in name
    if not (3 <= len(name) <= 63) or not valid:
        raise ValueError(
            "traider:researchJobs: the research trail bucket would be named "
            f"{trail_bucket_name(prefix, '<account id>')!r}, which S3 does not allow. "
            "Bucket names are 3 to 63 lowercase letters, digits, dots and hyphens, so the "
            f"project and stack name ({prefix!r}) must be lowercase and at most "
            f"{63 - len(trail_bucket_name('', '0' * 12))} characters"
        )


def _symbols(config: pulumi.Config, *, research: bool) -> tuple[str, ...]:
    """The pinned symbols. ``symbols`` is the older name for ``pinnedSymbols``."""
    pinned: Any = config.get_object("pinnedSymbols")
    alias: Any = config.get_object("symbols")
    if pinned is not None and alias is not None:
        raise ValueError(
            "traider:pinnedSymbols and traider:symbols are the same setting: set only "
            "traider:pinnedSymbols"
        )
    raw = pinned if pinned is not None else alias
    if isinstance(raw, str):
        raw = raw.split(",")
    symbols = tuple(str(item).strip().upper() for item in raw or () if str(item).strip())
    if not symbols and not research:
        raise ValueError(
            "traider:pinnedSymbols (or traider:symbols) needs at least one symbol unless "
            "traider:research is on, for example: pulumi config set "
            "--path 'traider:pinnedSymbols[0]' SPY"
        )
    return symbols


def load() -> Settings:
    config = pulumi.Config()
    prefix = f"{pulumi.get_project()}-{pulumi.get_stack()}"
    research = bool(config.get_bool("research"))
    research_jobs = bool(config.get_bool("researchJobs"))
    if research_jobs and not research:
        raise ValueError(
            "traider:researchJobs needs traider:research: true: the research jobs write to "
            "the research table, which only exists with research on"
        )
    if research_jobs:
        _check_trail_bucket_name(prefix)
    symbols = _symbols(config, research=research)
    mode = config.get("tradingMode") or "paper"

    env: dict[str, str] = {"TRAIDER_TRADING_MODE": mode}
    if symbols:  # with research on and nothing pinned, the variable is left out
        env["TRAIDER_SYMBOLS"] = ",".join(symbols)
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

    # Check it the way the bot will. The placeholders stand in for resources this stack
    # creates: the bot requires two in live mode, and the research table is what lets it
    # run with no pinned symbols.
    placeholders = {"TRAIDER_CONTROL_PARAM": "/placeholder", "TRAIDER_STATE_TABLE": "placeholder"}
    if research:
        placeholders["TRAIDER_RESEARCH_TABLE"] = "placeholder"
    try:
        checked = Config.from_env({**env, **placeholders})
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
        research=research,
        research_jobs=research_jobs,
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
