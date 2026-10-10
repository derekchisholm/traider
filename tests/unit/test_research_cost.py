"""Cost maths and budget stops, in Decimal."""

from decimal import Decimal

import pytest

from traider.research.cost import CostMeter
from traider.research.job_settings import ModelPrice
from traider.research.llm import Usage

MODEL = "anthropic.claude-sonnet-5-5"
PRICES = {MODEL: ModelPrice(in_per_mtok=Decimal(2), out_per_mtok=Decimal(10))}


def meter(run="3.00", day="8.00") -> CostMeter:
    return CostMeter(PRICES, run_usd=Decimal(run), day_remaining_usd=Decimal(day))


def test_cost_is_tokens_times_price_per_million():
    m = meter()
    assert m.cost(MODEL, 1000, 200) == Decimal("0.004")
    m.record(MODEL, Usage(1000, 200))
    m.record(MODEL, Usage(123_457, 0))
    assert m.spent == Decimal("0.250914")
    assert m.spent_usd == Decimal("0.2509")
    assert (m.tokens_in, m.tokens_out, m.models) == (124_457, 200, {MODEL})


def test_rounding_to_a_hundredth_of_a_cent_is_half_up():
    m = meter()
    m.record(MODEL, Usage(25, 0))  # 0.00005
    assert m.spent_usd == Decimal("0.0001")


def test_a_call_that_might_break_the_run_budget_is_refused():
    m = meter(run="0.03")
    # Worst case of one call: 1,000 in and 2,000 out = 0.002 + 0.02 = 0.022.
    assert not m.would_exceed(MODEL, 1000, 2000)
    m.record(MODEL, Usage(1000, 1000))  # 0.012
    assert m.would_exceed(MODEL, 1000, 2000)  # 0.012 + 0.022 > 0.03
    assert m.reserve(MODEL, 1000, 2000) is None
    assert m.exhausted


def test_the_day_budget_counts_when_less_is_left_of_it():
    m = meter(run="3.00", day="0.01")
    assert m.limit == Decimal("0.01")
    assert m.would_exceed(MODEL, 1000, 2000)
    assert meter(day="-0.50").would_exceed(MODEL, 0, 1)  # already over for the day


def test_reservations_bound_calls_in_flight():
    m = meter(run="0.05")
    first = m.reserve(MODEL, 1000, 2000)
    second = m.reserve(MODEL, 1000, 2000)
    assert first == second == Decimal("0.022")
    assert m.reserve(MODEL, 1000, 2000) is None  # 0.066 would be over 0.05
    m.settle(MODEL, first, Usage(1000, 200))
    assert (m.spent, m.reserved) == (Decimal("0.004"), Decimal("0.022"))


def test_a_failed_call_is_charged_at_its_reservation():
    m = meter()
    held = m.reserve(MODEL, 1000, 2000)
    m.settle(MODEL, held, None)
    assert (m.spent, m.reserved, m.tokens_in) == (Decimal("0.022"), Decimal(0), 0)


def test_an_unpriced_model_is_never_called():
    m = meter()
    assert m.would_exceed("anthropic.claude-opus-5", 1, 1)
    assert m.reserve("anthropic.claude-opus-5", 1, 1) is None


def test_an_overrun_is_flagged_and_stops_further_spending():
    m = meter(run="0.05")
    first = m.reserve(MODEL, 1000, 2000)
    second = m.reserve(MODEL, 1000, 2000)
    assert first is not None and second is not None
    assert not m.exhausted and not m.overrun
    # Each call really cost 0.06, far over its 0.022 reservation.
    m.settle(MODEL, first, Usage(20_000, 2000))
    m.settle(MODEL, second, Usage(20_000, 2000))
    assert m.spent == Decimal("0.12")
    assert m.exhausted and m.overrun
    assert m.reserve(MODEL, 0, 1) is None


def test_spending_past_the_limit_exhausts_the_meter_even_within_a_reservation():
    m = meter(run="0.01")
    m.record(MODEL, Usage(0, 2000))  # 0.02, no reservation involved
    assert m.exhausted and not m.overrun


def test_negative_token_counts_are_refused():
    m = meter()
    with pytest.raises(ValueError, match="negative"):
        m.reserve(MODEL, -1, 10)
    with pytest.raises(ValueError, match="negative"):
        m.reserve(MODEL, 10, -1)


def test_a_double_settle_is_refused():
    m = meter()
    held = m.reserve(MODEL, 1000, 2000)
    assert held is not None
    m.settle(MODEL, held, Usage(1000, 200))
    with pytest.raises(ValueError, match="more than is reserved"):
        m.settle(MODEL, held, Usage(1000, 200))
    assert (m.spent, m.reserved) == (Decimal("0.004"), Decimal(0))


def test_an_exhausted_meter_reserves_nothing_even_after_an_overrun_within_the_limit():
    m = meter(run="3.00")
    held = m.reserve(MODEL, 1000, 2000)  # 0.022
    assert held is not None
    m.settle(MODEL, held, Usage(10_000, 2000))  # 0.04: over the reservation, far under 3.00
    assert m.overrun and m.exhausted and m.spent < m.limit
    assert m.reserve(MODEL, 1, 1) is None


def test_negative_usage_is_refused_by_record_and_settle():
    m = meter()
    with pytest.raises(ValueError, match="negative"):
        m.record(MODEL, Usage(-1, 0))
    with pytest.raises(ValueError, match="negative"):
        m.record(MODEL, Usage(0, -1))
    held = m.reserve(MODEL, 1000, 2000)
    assert held is not None
    with pytest.raises(ValueError, match="negative"):
        m.settle(MODEL, held, Usage(-1, 0))
    assert (m.spent, m.reserved) == (Decimal(0), held)  # refused before anything changed
