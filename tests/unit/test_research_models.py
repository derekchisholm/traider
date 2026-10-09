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
