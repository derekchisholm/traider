"""The scorecard's maths, on fixed bar series: returns signed by side, excursions, the
invalidation, status transitions, "traded" from the event log, and the summary."""

from datetime import UTC, date, datetime, timedelta

import pytest

from traider.research.market import DailyBar
from traider.research.models import OutcomeStatus, Pick, RunStatus
from traider.research.rank import close_of
from traider.research.scorecard import (
    score_pick,
    signed_pct,
    summarize,
    summary_text,
    traded_from_logs,
)
from traider.timeutil import weekdays_after

NOW = datetime(2026, 10, 12, 20, 30, tzinfo=UTC)  # Monday 16:30 New York
MON, TUE, WED, THU, FRI = (date(2026, 10, d) for d in (5, 6, 7, 8, 9))
NEXT_MON = date(2026, 10, 12)


def bar(day, o, h, low, c) -> DailyBar:
    return DailyBar(day=day, open=o, high=h, low=low, close=c, volume=1_000_000)


# Monday 10-05 to Monday 10-12. The pick day is Monday: entry 100.
WEEK = [
    bar(MON, 100, 103, 99, 102),
    bar(TUE, 102, 104, 98, 101),
    bar(WED, 101, 106, 100, 105),
    bar(THU, 105, 107, 103, 104),
    bar(FRI, 104, 105, 94, 96),
    bar(NEXT_MON, 96, 97, 90, 91),  # after the expiry
]


def pick(side="long", horizon="swing", invalidation="95", expires=FRI, **overrides) -> Pick:
    fields = {
        "run_id": "premarket-a",
        "rank": 1,
        "symbol": "NVDA",
        "side": side,
        "horizon": horizon,
        "score": 84,
        "pre_score": 70,
        "thesis": "t",
        "invalidation": invalidation,
        "expires_at": close_of(expires).isoformat(),
        "features": {"llm_score": 82.0, "price_at_pick": 99.5},
    }
    return Pick.model_validate(fields | overrides)


def score(p, *, bars=WEEK, pick_day=MON, today=NEXT_MON, traded=False, status=RunStatus.OK):
    return score_pick(
        p, pick_day=pick_day, run_status=status, bars=bars, today=today, traded=traded, now=NOW
    )


# --- returns, excursions, invalidation --------------------------------------------------


def test_a_long_swing_pick_golden():
    scored = score(pick())
    o = scored.outcome
    assert (o.entry, o.price_at_pick, o.llm_score) == (100.0, 99.5, 82)
    assert (o.ret_0d, o.ret_1d, o.ret_5d, o.ret_20d) == (None, 2.0, -4.0, None)
    assert (o.mfe_pct, o.mae_pct) == (7.0, -6.0)  # high 107, low 94, inside the window
    assert o.hit_invalidation is True  # Friday's low 94 is under 95
    assert o.expired_return == -4.0  # Friday's close
    assert (o.status, o.traded, o.run_status) == (OutcomeStatus.PARTIAL, False, RunStatus.OK)
    assert scored.matured == frozenset()  # nothing matured on Monday the 12th


def test_a_bearish_pick_is_signed_the_other_way():
    o = score(pick(side="bearish", invalidation="108")).outcome
    assert (o.ret_1d, o.ret_5d) == (-2.0, 4.0)
    assert (o.mfe_pct, o.mae_pct) == (6.0, -7.0)  # the fall to 94 is favourable
    assert o.hit_invalidation is False  # the highest high, 107, stayed under 108
    assert o.expired_return == 4.0


def test_a_bearish_invalidation_is_hit_by_a_high_at_or_above_it():
    assert score(pick(side="bearish", invalidation="107")).outcome.hit_invalidation is True


def test_the_excursions_stop_at_the_expiry():
    # Monday the 12th's low of 90 is after Friday's expiry: not counted.
    o = score(pick(expires=THU)).outcome
    assert (o.mfe_pct, o.mae_pct) == (7.0, -2.0)
    assert o.hit_invalidation is False
    assert o.expired_return == 4.0  # Thursday's close, 104


def test_an_intraday_pick_has_a_same_day_return():
    o = score(pick(horizon="intraday", expires=WED), pick_day=WED).outcome
    assert o.entry == 101.0
    assert o.ret_0d == o.ret_1d == pytest.approx(3.9604)
    assert o.ret_5d is None  # Wednesday to Monday is four bars
    assert (o.mfe_pct, o.mae_pct) == (pytest.approx(4.9505), pytest.approx(-0.9901))
    assert o.expired_return == pytest.approx(3.9604)
    assert o.status is OutcomeStatus.PARTIAL


def test_signed_pct_rounds_to_four_places():
    assert signed_pct(3.0, 4.0, pick().side) == 33.3333
    assert signed_pct(3.0, 4.0, pick(side="bearish", invalidation="108").side) == -33.3333


def test_returns_matured_today_are_flagged():
    assert score(pick(), today=MON).matured == {"ret_1d"}
    assert score(pick(), today=FRI).matured == {"ret_5d"}


# --- status ---------------------------------------------------------------------------------


def test_without_the_pick_days_bar_the_outcome_is_pending():
    o = score(pick(), bars=WEEK[1:]).outcome
    assert (o.status, o.entry, o.ret_1d, o.mfe_pct) == (OutcomeStatus.PENDING, None, None, None)
    assert (o.price_at_pick, o.traded) == (99.5, False)


def test_no_bars_at_all_is_pending_too():
    assert score(pick(), bars=[]).outcome.status is OutcomeStatus.PENDING


def trading_bars(start: date, n: int) -> list[DailyBar]:
    days = [start] + [weekdays_after(start, i) for i in range(1, n)]
    return [bar(d, 100, 101, 99, 100 + i) for i, d in enumerate(days)]


def test_the_twenty_day_close_and_the_expiry_make_it_final():
    expires = weekdays_after(MON, 19)  # the 20th bar's day
    bars = trading_bars(MON, 20)
    o = score(pick(expires=expires), bars=bars, today=expires).outcome
    assert o.ret_20d == 19.0
    assert o.status is OutcomeStatus.FINAL


def test_the_twenty_day_close_alone_is_not_final_while_the_pick_is_live():
    expires = weekdays_after(MON, 20)  # a swing of 20 weekdays outlives the 20th bar
    bars = trading_bars(MON, 20)
    o = score(pick(expires=expires), bars=bars, today=bars[-1].day).outcome
    assert (o.ret_20d, o.expired_return) == (19.0, None)
    assert o.status is OutcomeStatus.PARTIAL


def test_a_pick_thirty_weekdays_old_is_final_even_without_bars():
    today = weekdays_after(MON, 30)
    assert score(pick(), bars=[], today=today).outcome.status is OutcomeStatus.FINAL
    almost = weekdays_after(MON, 29)
    assert score(pick(), bars=[], today=almost).outcome.status is OutcomeStatus.PENDING


@pytest.mark.parametrize("junk", [float("nan"), float("inf"), 0.0, -1.0])
def test_an_unusable_pick_day_bar_is_pending_not_a_crash_or_a_guess(junk):
    bars = [bar(MON, 100, junk, 99, 102), *WEEK[1:]]
    o = score(pick(), bars=bars).outcome
    assert (o.status, o.entry, o.ret_1d, o.mfe_pct) == (OutcomeStatus.PENDING, None, None, None)


@pytest.mark.parametrize("junk", [float("nan"), float("inf"), 0.0, -1.0])
def test_a_bad_later_bar_is_not_guessed_and_nothing_after_it_is_used(junk):
    # Wednesday's bar is unusable: Friday would be the 5th bar, but its position is no
    # longer certain, so ret_5d and the expiry stay unknown. Monday and Tuesday still count.
    bars = [*WEEK[:2], bar(WED, 101, junk, 100, 105), *WEEK[3:]]
    o = score(pick(), bars=bars).outcome
    assert (o.ret_1d, o.ret_5d, o.expired_return) == (2.0, None, None)
    assert (o.mfe_pct, o.mae_pct) == (4.0, -2.0)
    assert o.hit_invalidation is False  # only seen bars count
    assert o.status is OutcomeStatus.PARTIAL


def test_an_expiry_before_the_pick_day_means_the_pick_day_itself():
    o = score(pick(expires=date(2026, 10, 2))).outcome
    assert (o.mfe_pct, o.mae_pct) == (3.0, -1.0)  # Monday's bar alone
    assert o.expired_return == 2.0  # Monday's close
    assert o.hit_invalidation is False


def test_a_long_invalidation_equal_to_the_low_is_hit():
    assert score(pick(invalidation="94")).outcome.hit_invalidation is True
    assert score(pick(invalidation="93.99")).outcome.hit_invalidation is False


def test_a_gap_of_missing_bars_ends_the_usable_data():
    # Wednesday to Friday are missing: Tuesday to the 12th is a six-day gap. The 12th's bar
    # must not become the "5th" bar, and the expiry (Friday) is not known.
    bars = [WEEK[0], WEEK[1], WEEK[5]]
    o = score(pick(), bars=bars).outcome
    assert (o.ret_1d, o.ret_5d, o.expired_return) == (2.0, None, None)
    assert (o.mfe_pct, o.mae_pct) == (4.0, -2.0)
    assert o.status is OutcomeStatus.PARTIAL


def test_a_weekend_and_a_holiday_are_not_a_gap():
    # Friday to Tuesday (a Monday holiday) is four days: the bars still count.
    bars = [WEEK[0], bar(date(2026, 10, 9), 100, 101, 99, 103), bar(date(2026, 10, 13), 1, 2, 1, 2)]
    o = score(pick(expires=date(2026, 10, 13)), bars=bars, today=date(2026, 10, 13)).outcome
    assert o.ret_5d is None and o.expired_return == -98.0  # three bars: the 3rd is the 13th


def test_a_hand_made_pick_without_features_still_scores():
    o = score(pick(features={})).outcome
    assert (o.llm_score, o.price_at_pick, o.ret_1d) == (None, None, 2.0)


# --- traded, from the bot's event log -------------------------------------------------------

START = datetime(2026, 10, 5, 12, 30, tzinfo=UTC)
END = close_of(TUE)


def submitted(symbol, side="BUY", at="2026-10-05T14:00:00+00:00", kind="order_submitted"):
    return {"kind": kind, "at": at, "data": {"symbol": symbol, "side": side}}


@pytest.mark.parametrize(
    "event",
    [
        submitted("NVDA"),
        submitted("NVDA  261016P00100000"),  # a put on it
        submitted("NVDA", at="2026-10-06T19:59:00+00:00"),
    ],
)
def test_a_buy_while_the_pick_was_live_counts(event):
    logs = {MON: [event], TUE: []}
    assert traded_from_logs(logs, "NVDA", START, END) is True


@pytest.mark.parametrize(
    "event",
    [
        submitted("NVDA", side="SELL"),
        submitted("NVDAX"),
        submitted("AMD"),
        submitted("NVDA", kind="order_blocked"),
        submitted("NVDA", at="2026-10-05T12:00:00+00:00"),  # before the pick
        submitted("NVDA", at="2026-10-05T14:00:00"),  # no timezone
        submitted("NVDA", at="soon"),
        {"kind": "order_submitted", "at": "2026-10-05T14:00:00+00:00", "data": "NVDA"},
    ],
)
def test_anything_else_does_not(event):
    assert traded_from_logs({MON: [event], TUE: []}, "NVDA", START, END) is False


def test_an_end_before_the_start_is_the_start():
    event = submitted("NVDA", at=START.isoformat())
    logs = {MON: [event], TUE: []}
    assert traded_from_logs(logs, "NVDA", START, START - timedelta(days=1)) is True


def test_an_unreadable_day_makes_it_unknown_unless_a_buy_was_found():
    assert traded_from_logs({MON: [], TUE: None}, "NVDA", START, END) is None
    assert traded_from_logs({MON: []}, "NVDA", START, END) is None  # TUE never read
    assert traded_from_logs({MON: [submitted("NVDA")], TUE: None}, "NVDA", START, END) is True


# --- the summary ----------------------------------------------------------------------------


def test_the_summary_counts_hits_means_and_buckets():
    items = [
        score(pick(rank=1, score=84), today=MON),  # +2.0 matured today
        score(pick(rank=2, side="bearish", invalidation="108", score=72), today=MON),  # -2.0
        score(pick(rank=3, score=91, run_id="intraday-b"), today=MON),  # +2.0
        score(pick(rank=4, score=55), bars=WEEK[1:], today=MON),  # pending
    ]
    summary = summarize(
        items,
        day=MON,
        run_id="scorecard-x",
        kinds={"premarket-a": "premarket", "intraday-b": "intraday"},
        now=NOW,
    )
    assert summary.picks == 4
    assert summary.by_kind == {"intraday": 1, "premarket": 3}
    assert summary.by_side == {"bearish": 1, "long": 3}
    assert (summary.ret_1d.matured, summary.ret_1d.hits) == (3, 2)
    assert summary.ret_1d.mean_pct == pytest.approx(0.6667)
    assert (summary.ret_5d.matured, summary.ret_5d.mean_pct) == (0, None)
    buckets = {(b.low, b.high): (b.count, b.mean_ret_1d_pct) for b in summary.buckets}
    assert buckets == {
        (60, 69): (0, None),
        (70, 79): (1, -2.0),
        (80, 89): (1, 2.0),
        (90, 100): (1, 2.0),
    }
    assert summary_text(summary) == (
        "traider scorecard 2026-10-05: 3 picks matured 1d, hit 2/3, mean +0.7%; "
        "no picks matured 5d; 4 picks in the window"
    )


def test_the_alert_says_one_pick_in_the_singular():
    summary = summarize(
        [score(pick(), today=MON)], day=MON, run_id="scorecard-x", kinds={}, now=NOW
    )
    assert summary_text(summary) == (
        "traider scorecard 2026-10-05: 1 pick matured 1d, hit 1/1, mean +2.0%; "
        "no picks matured 5d; 1 pick in the window"
    )


def test_an_empty_summary_reads_plainly():
    summary = summarize([], day=MON, run_id="scorecard-x", kinds={}, now=NOW)
    assert summary.picks == 0 and all(b.count == 0 for b in summary.buckets)
    assert summary_text(summary) == (
        "traider scorecard 2026-10-05: no picks matured 1d; no picks matured 5d; "
        "0 picks in the window"
    )
