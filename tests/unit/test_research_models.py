"""The shapes research writes and the bot reads. Anything else is rejected."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from traider.research.models import (
    Horizon,
    Pick,
    PickSide,
    Posture,
    PostureLevel,
    RunMeta,
    RunStatus,
)

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def pick(**overrides) -> Pick:
    fields = {
        "run_id": "premarket-20261009T120000Z-ab12",
        "rank": 1,
        "symbol": "NVDA",
        "side": "long",
        "horizon": "intraday",
        "score": 80,
        "pre_score": 70,
        "thesis": "gap up on earnings, holding VWAP",
        "invalidation": "120.50",
        "expires_at": "2026-10-09T20:00:00+00:00",
    }
    return Pick.model_validate(fields | overrides)


def test_a_valid_pick_parses_with_typed_fields():
    p = pick(earnings_date="2026-10-15", features={"gap_pct": 4.2})
    assert p.side is PickSide.LONG
    assert p.horizon is Horizon.INTRADAY
    assert p.invalidation == Decimal("120.50")
    assert p.earnings_date == date(2026, 10, 15)
    assert p.features == {"gap_pct": 4.2}


@pytest.mark.parametrize(
    "bad",
    [
        {"score": 101},
        {"score": -1},
        {"rank": 0},
        {"side": "short"},
        {"horizon": "weekly"},
        {"invalidation": "0"},
        {"symbol": "NV DA"},
        {"thesis": "x" * 2001},
        {"expires_at": "2026-10-09T20:00:00"},  # no timezone
        {"surprise": True},  # unknown field
    ],
)
def test_bad_picks_are_rejected(bad):
    with pytest.raises(ValidationError):
        pick(**bad)


def run_meta(**overrides) -> RunMeta:
    fields = {
        "run_id": "r1",
        "kind": "premarket",
        "status": "ok",
        "started_at": T0.isoformat(),
        "trading_day": "2026-10-09",
    }
    return RunMeta.model_validate(fields | overrides)


def posture(**overrides) -> Posture:
    fields = {"level": "trade", "run_id": "r1", "at": T0.isoformat()}
    return Posture.model_validate(fields | overrides)


@pytest.mark.parametrize(
    "bad",
    [
        {"rank": True},
        {"rank": 1.0},
        {"rank": "1"},
        {"score": True},
        {"score": 80.0},
        {"score": "80"},
        {"pre_score": 70.0},
        {"pre_score": "70"},
    ],
)
def test_pick_numbers_are_strict_ints(bad):
    with pytest.raises(ValidationError):
        pick(**bad)


def test_pick_numbers_still_parse_from_json():
    assert Pick.model_validate_json(pick().model_dump_json()) == pick()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_pick_features_reject_nan_and_inf(bad):
    with pytest.raises(ValidationError):
        pick(features={"gap_pct": bad})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_posture_metrics_reject_nan_and_inf(bad):
    with pytest.raises(ValidationError):
        posture(metrics={"vix": bad})


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_run_cost_rejects_nan_and_infinity(bad):
    with pytest.raises(ValidationError):
        run_meta(cost_usd=bad)


def test_naive_posture_time_is_rejected():
    with pytest.raises(ValidationError, match="timezone"):
        posture(at="2026-10-09T12:00:00")


def test_naive_run_start_is_rejected():
    with pytest.raises(ValidationError, match="timezone"):
        run_meta(started_at="2026-10-09T12:00:00")


def test_naive_run_finish_is_rejected():
    with pytest.raises(ValidationError, match="timezone"):
        run_meta(finished_at="2026-10-09T12:00:00")


def test_aware_run_finish_is_accepted_and_may_be_absent():
    assert run_meta(finished_at=T0.isoformat()).finished_at == T0
    assert run_meta().finished_at is None


def test_posture_and_run_meta_parse():
    posture = Posture.model_validate(
        {"level": "stand_aside", "reasons": ["CPI at 08:30"], "run_id": "r1", "at": T0.isoformat()}
    )
    assert posture.level is PostureLevel.STAND_ASIDE
    meta = RunMeta.model_validate(
        {
            "run_id": "r1",
            "kind": "premarket",
            "status": "ok",
            "started_at": T0.isoformat(),
            "finished_at": T0.isoformat(),
            "trading_day": "2026-10-09",
        }
    )
    assert meta.status is RunStatus.OK
    with pytest.raises(ValidationError):
        RunMeta.model_validate(meta.model_dump(mode="json") | {"kind": "hourly"})


def test_run_meta_carries_tokens_notes_and_counts():
    meta = RunMeta.model_validate(
        {
            "run_id": "premarket-20261009T120000Z-ab12",
            "kind": "premarket",
            "status": "partial",
            "started_at": T0.isoformat(),
            "trading_day": "2026-10-09",
            "tokens_in": 7000,
            "tokens_out": 1400,
            "notes": ["deadline passed: 2 deep-dives not started"],
            "counts": {"candidates": 8, "picks": 3},
        }
    )
    assert (meta.tokens_in, meta.tokens_out) == (7000, 1400)
    assert meta.notes == ("deadline passed: 2 deep-dives not started",)
    assert meta.counts == {"candidates": 8, "picks": 3}


def test_run_meta_written_before_c1_still_parses_with_defaults():
    old = {
        "run_id": "r1",
        "kind": "manual",
        "status": "ok",
        "started_at": T0.isoformat(),
        "trading_day": "2026-10-09",
    }
    meta = RunMeta.model_validate(old)
    assert (meta.tokens_in, meta.tokens_out, meta.notes, meta.counts) == (0, 0, (), {})


@pytest.mark.parametrize(
    "bad", [{"tokens_in": -1}, {"notes": ["x" * 301]}, {"counts": {"picks": -1}}]
)
def test_bad_run_accounting_is_rejected(bad):
    base = {
        "run_id": "r1",
        "kind": "premarket",
        "status": "ok",
        "started_at": T0.isoformat(),
        "trading_day": "2026-10-09",
    }
    with pytest.raises(ValidationError):
        RunMeta.model_validate(base | bad)
