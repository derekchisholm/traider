import json
from decimal import Decimal

import pytest

from traider.config import Config, ConfigError

BASE = {"TRAIDER_SYMBOLS": "spy, qqq"}
LIVE = {
    **BASE,
    "TRAIDER_TRADING_MODE": "live",
    "TRAIDER_SCHWAB_ACCOUNT_LAST4": "1234",
    "TRAIDER_CONTROL_PARAM": "/traider/dev/control",
    "TRAIDER_STATE_TABLE": "traider-dev",
}


def test_minimal_env_gives_paper_mode_with_safe_defaults():
    cfg = Config.from_env(BASE)
    assert cfg.trading_mode == "paper"
    assert cfg.symbols == ("SPY", "QQQ")
    assert cfg.order_type == "LIMIT"
    assert cfg.strategy == "sma_cross"


def test_symbols_are_required():
    with pytest.raises(ConfigError, match="TRAIDER_SYMBOLS"):
        Config.from_env({})


def test_duplicate_symbols_are_rejected():
    with pytest.raises(ConfigError, match="duplicate"):
        Config.from_env({"TRAIDER_SYMBOLS": "SPY,spy"})


@pytest.mark.parametrize("bad", ["SPY;rm", "S PY", "$SPX", "TOOLONGSYMBOLX"])
def test_malformed_symbols_are_rejected(bad):
    with pytest.raises(ConfigError):
        Config.from_env({"TRAIDER_SYMBOLS": bad})


def test_unknown_trading_mode_is_rejected():
    with pytest.raises(ConfigError, match="trading_mode"):
        Config.from_env({**BASE, "TRAIDER_TRADING_MODE": "real"})


def test_live_mode_accepts_a_complete_setup():
    assert Config.from_env(LIVE).trading_mode == "live"


@pytest.mark.parametrize(
    ("missing", "hint"),
    [
        ("TRAIDER_SCHWAB_ACCOUNT_LAST4", "account"),
        ("TRAIDER_CONTROL_PARAM", "control"),
        ("TRAIDER_STATE_TABLE", "state"),
    ],
)
def test_live_mode_requires_account_kill_switch_and_durable_state(missing, hint):
    env = {k: v for k, v in LIVE.items() if k != missing}
    with pytest.raises(ConfigError, match=hint):
        Config.from_env(env)


def test_live_mode_accepts_an_account_hash_instead_of_last4():
    env = {k: v for k, v in LIVE.items() if k != "TRAIDER_SCHWAB_ACCOUNT_LAST4"}
    env["TRAIDER_SCHWAB_ACCOUNT_HASH"] = "ABCDEF0123"
    assert Config.from_env(env).account_hash == "ABCDEF0123"


def test_account_last4_must_be_four_digits():
    with pytest.raises(ConfigError, match="account_last4"):
        Config.from_env({**BASE, "TRAIDER_SCHWAB_ACCOUNT_LAST4": "12a4"})


def test_risk_json_overrides_individual_limits_and_keeps_other_defaults():
    cfg = Config.from_env({**BASE, "TRAIDER_RISK": json.dumps({"max_order_usd": "250.50"})})
    assert cfg.risk.max_order_usd == Decimal("250.50")
    assert cfg.risk.max_orders_per_day == Config.from_env(BASE).risk.max_orders_per_day


def test_misspelled_risk_limit_is_an_error_not_silently_ignored():
    with pytest.raises(ConfigError, match="max_order_usdd"):
        Config.from_env({**BASE, "TRAIDER_RISK": json.dumps({"max_order_usdd": 1})})


@pytest.mark.parametrize(
    "risk",
    [{"max_order_usd": -1}, {"max_orders_per_day": 0}, {"max_daily_loss_usd": 0}],
)
def test_non_positive_risk_limits_are_rejected(risk):
    with pytest.raises(ConfigError):
        Config.from_env({**BASE, "TRAIDER_RISK": json.dumps(risk)})


def test_order_cap_cannot_exceed_position_cap():
    risk = {"max_order_usd": 5000, "max_position_usd": 1000, "max_total_exposure_usd": 9000}
    with pytest.raises(ConfigError, match="max_order_usd"):
        Config.from_env({**BASE, "TRAIDER_RISK": json.dumps(risk)})


def test_strategy_params_are_parsed_from_json():
    cfg = Config.from_env({**BASE, "TRAIDER_STRATEGY_PARAMS": '{"fast": 3, "slow": 8}'})
    assert cfg.strategy_params == {"fast": 3, "slow": 8}


def test_invalid_json_names_the_offending_variable():
    with pytest.raises(ConfigError, match="TRAIDER_STRATEGY_PARAMS"):
        Config.from_env({**BASE, "TRAIDER_STRATEGY_PARAMS": "{nope"})


def test_blank_optional_values_are_treated_as_unset():
    cfg = Config.from_env(
        {**BASE, "TRAIDER_STATE_TABLE": "", "TRAIDER_FLATTEN_BEFORE_CLOSE_MIN": ""}
    )
    assert cfg.state_table is None
    assert cfg.flatten_before_close_min is None


def test_settings_table_is_read_from_the_environment():
    cfg = Config.from_env({**BASE, "TRAIDER_SETTINGS_TABLE": "traider-dev-settings"})
    assert cfg.settings_table == "traider-dev-settings"
