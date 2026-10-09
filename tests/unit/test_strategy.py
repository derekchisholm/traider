from decimal import Decimal

import pytest

from tests.unit.helpers import T0, make_bar, make_quote
from traider.strategy import available_strategies, create_strategy
from traider.strategy.base import StrategyContext
from traider.strategy.sma_cross import SmaCross

PARAMS = {"fast": 2, "slow": 4, "position_usd": 500}


def ctx(**positions) -> StrategyContext:
    return StrategyContext(now=T0, positions=positions)


def feed(strategy, closes, *, symbol="SPY", context=None):
    """Feed closes as consecutive bars and return the targets from the last bar."""
    targets = []
    for i, close in enumerate(closes):
        targets = list(strategy.on_bar(make_bar(symbol, close, minute=i), context or ctx()))
    return targets


def test_no_opinion_until_the_slow_window_is_full():
    strategy = SmaCross(["SPY"], PARAMS)
    assert feed(strategy, [100, 101, 102]) == []


def test_rising_prices_give_a_long_target_sized_by_dollars():
    strategy = SmaCross(["SPY"], PARAMS)
    (target,) = feed(strategy, [100, 101, 102, 104])
    # 500 dollars at a 104 close buys 4 whole shares.
    assert (target.symbol, target.quantity) == ("SPY", 4)


def test_falling_prices_give_a_flat_target():
    strategy = SmaCross(["SPY"], PARAMS)
    (target,) = feed(strategy, [104, 102, 101, 100])
    assert target.quantity == 0


def test_an_existing_position_is_held_not_resized_every_bar():
    strategy = SmaCross(["SPY"], PARAMS)
    (target,) = feed(strategy, [100, 101, 102, 104], context=ctx(SPY=3))
    assert target.quantity == 3


def test_equal_averages_give_no_new_target():
    strategy = SmaCross(["SPY"], PARAMS)
    assert feed(strategy, [100, 100, 100, 100]) == []


def test_a_share_that_costs_more_than_the_budget_is_not_bought():
    strategy = SmaCross(["SPY"], {**PARAMS, "position_usd": 50})
    (target,) = feed(strategy, [100, 101, 102, 104])
    assert target.quantity == 0


def test_symbols_are_tracked_independently():
    strategy = SmaCross(["SPY", "QQQ"], PARAMS)
    feed(strategy, [100, 101, 102, 104], symbol="SPY")
    assert feed(strategy, [50, 49, 48], symbol="QQQ") == []


def test_bars_for_symbols_it_was_not_given_are_ignored():
    strategy = SmaCross(["SPY"], PARAMS)
    assert feed(strategy, [100, 101, 102, 104], symbol="IWM") == []


def test_only_the_most_recent_window_matters():
    strategy = SmaCross(["SPY"], PARAMS)
    (target,) = feed(strategy, [100, 101, 102, 104, 103, 100, 96, 92])
    assert target.quantity == 0


def test_same_bars_always_give_the_same_targets():
    closes = [100, 101, 102, 104, 103, 105]
    first = feed(SmaCross(["SPY"], PARAMS), closes)
    second = feed(SmaCross(["SPY"], PARAMS), closes)
    assert first == second


def test_warmup_asks_for_enough_history_to_fill_the_slow_window():
    assert SmaCross(["SPY"], PARAMS).warmup_bars == 4


def test_quotes_are_ignored_by_default():
    strategy = SmaCross(["SPY"], PARAMS)
    assert list(strategy.on_quote(make_quote(), ctx())) == []


@pytest.mark.parametrize(
    "params",
    [
        {"fast": 4, "slow": 4},
        {"fast": 5, "slow": 4},
        {"fast": 0, "slow": 4},
        {"position_usd": 0},
        {"position_usd": -5},
        {"fastt": 2},
    ],
)
def test_bad_parameters_are_rejected_at_start_up(params):
    with pytest.raises(ValueError):
        SmaCross(["SPY"], params)


def test_defaults_are_usable_without_any_parameters():
    strategy = SmaCross(["SPY"], {})
    assert strategy.warmup_bars == 20
    assert strategy.position_usd == Decimal(500)


def test_strategy_is_created_by_name():
    assert isinstance(create_strategy("sma_cross", ["SPY"], PARAMS), SmaCross)


def test_unknown_strategy_name_lists_what_is_available():
    with pytest.raises(ValueError, match="sma_cross"):
        create_strategy("nope", ["SPY"], {})
    assert "sma_cross" in available_strategies()
