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


@pytest.mark.parametrize("symbols", [[], None])
def test_empty_pinned_symbols_are_rejected(symbols):
    body = base().model_dump(mode="json")
    if symbols is None:
        del body["pinned_symbols"]  # leaving it out is no way round the rule
    else:
        body["pinned_symbols"] = symbols
    with pytest.raises(ValidationError, match="pinned_symbols"):
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
