"""Rank and validate: each rule in order, expiry and the earnings clamp, score, caps."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tests.fakes.research import TODAY, FakeMarketData, quote
from traider.research.dive import Assessment
from traider.research.events import EarningsEvent
from traider.research.job_settings import RankSettings
from traider.research.market import PutContract
from traider.research.rank import (
    RankInput,
    blended_score,
    close_of,
    rank_and_validate,
    swing_expiry_day,
    validate_and_rank,
)
from traider.timeutil import weekdays_after

CLOSE = datetime(2026, 10, 9, 20, 0, tzinfo=UTC)
RUN = "premarket-20261009T120000Z-ab12"
SETTINGS = RankSettings()
CALENDAR_END = weekdays_after(TODAY, 10)  # the default earnings lookahead
LIQUID = [PutContract(symbol="P", strike=48, days=14, bid=1.0, ask=1.05, open_interest=500)]


def assessment(**overrides) -> Assessment:
    fields = {
        "side": "long",
        "horizon": "intraday",
        "score": 80,
        "thesis": "t",
        "invalidation": 101.0,
        "risks": [],
    }
    return Assessment.model_validate(fields | overrides)


def item(
    symbol="NVDA", *, pre=70, atr=2.0, sector="Semis", earnings=(), confirmed=True, **a
) -> RankInput:
    return RankInput(
        symbol=symbol,
        assessment=assessment(**a),
        pre_score=pre,
        features={"gap_pct": 4.0},
        atr=atr,
        sector=sector,
        earnings=tuple(earnings),
        earnings_confirmed=confirmed,
    )


def rank(
    inputs, *, fresh=None, puts=None, earnings_ok=True, settings=SETTINGS, calendar_end=CALENDAR_END
):
    fresh = (
        fresh if fresh is not None else {i.symbol: quote(i.symbol, 104.0, 100.0) for i in inputs}
    )
    return validate_and_rank(
        inputs,
        fresh=fresh,
        puts=puts or {},
        run_id=RUN,
        today=TODAY,
        close=CLOSE,
        earnings_ok=earnings_ok,
        calendar_end=calendar_end,
        settings=settings,
    )


def reasons(result) -> dict[str, str]:
    return {r.symbol: r.reason for r in result.rejected}


def test_a_good_long_intraday_assessment_becomes_a_pick():
    result = rank([item(score=80, pre=70, thesis="gap and go", risks=["fade"])])
    (pick,) = result.picks
    assert (pick.rank, pick.symbol, pick.side.value, pick.horizon.value) == (
        1,
        "NVDA",
        "long",
        "intraday",
    )
    assert (pick.score, pick.pre_score) == (77, 70)  # .7 x 80 + .3 x 70 = 77
    assert pick.expires_at == CLOSE
    assert pick.invalidation == Decimal("101.0")
    assert pick.thesis == "gap and go\nRisks: fade"
    assert pick.features == {"gap_pct": 4.0, "llm_score": 80.0, "atr": 2.0, "price_at_pick": 104.0}
    assert pick.run_id == RUN and pick.earnings_date is None


def test_rule_1_a_pass_is_recorded_as_passed():
    assert reasons(rank([item(side="pass")])) == {"NVDA": "passed"}


def test_rule_2_needs_a_fresh_quote_that_is_trading():
    assert reasons(rank([item()], fresh={})) == {"NVDA": "no_quote"}
    assert reasons(rank([item()], fresh={"NVDA": quote("NVDA", None, 100)})) == {"NVDA": "no_quote"}
    halted = {"NVDA": quote("NVDA", 104, 100, halted=True)}
    assert reasons(rank([item()], fresh=halted)) == {"NVDA": "halted"}


@pytest.mark.parametrize("last", [0.0, float("inf"), float("nan"), -5.0])
def test_rule_2_a_last_price_that_is_not_a_positive_finite_number_is_no_quote(last):
    # MarketQuote normalises these itself; the rule must hold even if a quote slips through.
    bad = quote("NVDA", 104, 100).model_copy(update={"last": last})
    assert reasons(rank([item()], fresh={"NVDA": bad})) == {"NVDA": "no_quote"}


@pytest.mark.parametrize(
    ("side", "invalidation", "ok"),
    [
        ("long", 103.4, True),  # 0.3 ATR below 104
        ("long", 103.5, False),  # 0.25 ATR: too tight
        ("long", 98.0, True),  # 3.0 ATR
        ("long", 97.9, False),  # 3.05 ATR: too wide
        ("long", 105.0, False),  # on the wrong side
        ("bearish", 106.0, True),  # 1 ATR above
        ("bearish", 103.0, False),  # below the price: wrong side for a put
    ],
)
def test_rule_3_invalidation_must_be_0_3_to_3_atr_away_on_the_right_side(side, invalidation, ok):
    result = rank([item(side=side, invalidation=invalidation)], puts={"NVDA": LIQUID})
    assert (reasons(result) == {}) is ok
    if not ok:
        assert reasons(result) == {"NVDA": "bad_invalidation"}


@pytest.mark.parametrize("atr", [0.0, -1.0, float("nan"), float("inf")])
def test_rule_3_an_unknown_atr_drops_the_name(atr):
    assert reasons(rank([item(atr=atr)])) == {"NVDA": "bad_invalidation"}


def test_rule_4_bearish_needs_a_liquid_put():
    bearish = item(side="bearish", invalidation=106.0)
    assert reasons(rank([bearish], puts={"NVDA": LIQUID})) == {}
    for chain in (
        None,  # the chain could not be read
        [],
        [LIQUID[0].model_copy(update={"bid": 0.0})],
        [LIQUID[0].model_copy(update={"ask": 1.2})],  # 18% spread
        [LIQUID[0].model_copy(update={"open_interest": 99})],
    ):
        assert reasons(rank([bearish], puts={"NVDA": chain})) == {"NVDA": "illiquid_puts"}
    # No entry at all for the name is unknown liquidity too.
    assert reasons(rank([bearish], puts={})) == {"NVDA": "illiquid_puts"}


def test_rule_5_swing_expires_at_the_close_n_weekdays_out():
    (pick,) = rank([item(horizon="swing", swing_days=5)]).picks
    assert pick.expires_at == datetime(2026, 10, 16, 20, 0, tzinfo=UTC)
    assert pick.horizon.value == "swing"


def test_rule_5_swing_needs_the_earnings_calendar():
    result = rank([item(horizon="swing", swing_days=5)], earnings_ok=False)
    assert reasons(result) == {"NVDA": "earnings_unknown"}
    # Intraday picks do not depend on it.
    assert rank([item()], earnings_ok=False).picks


def test_rule_5_swing_expiry_is_clamped_before_earnings():
    wednesday = EarningsEvent(symbol="NVDA", day=date(2026, 10, 14), hour="amc")
    (pick,) = rank([item(horizon="swing", swing_days=5, earnings=[wednesday])]).picks
    assert pick.expires_at == datetime(2026, 10, 13, 20, 0, tzinfo=UTC)  # Tuesday's close
    assert pick.earnings_date == date(2026, 10, 14)


@pytest.mark.parametrize(
    ("day", "hour", "expiry"),
    [
        (date(2026, 10, 12), "bmo", None),  # Monday: the last weekday before is today
        (TODAY, "amc", None),
        (TODAY, "unknown", None),
        (TODAY, "bmo", date(2026, 10, 16)),  # already out before the open
        (date(2026, 10, 19), "bmo", date(2026, 10, 16)),  # after the expiry: no clamp
        (date(2026, 10, 8), "amc", date(2026, 10, 16)),  # in the past
    ],
)
def test_swing_expiry_day(day, hour, expiry):
    event = EarningsEvent(symbol="X", day=day, hour=hour)
    assert swing_expiry_day(TODAY, 5, [event], CALENDAR_END) == expiry


def test_swing_expiry_day_earnings_on_the_expiry_day_after_the_close_clamps_to_the_day_before():
    friday = EarningsEvent(symbol="X", day=date(2026, 10, 16), hour="amc")
    assert swing_expiry_day(TODAY, 5, [friday], CALENDAR_END) == date(2026, 10, 15)


def test_swing_expiry_day_earnings_on_a_weekend_clamps_to_the_friday_before():
    saturday = EarningsEvent(symbol="X", day=date(2026, 10, 17), hour="unknown")
    assert swing_expiry_day(TODAY, 10, [saturday], CALENDAR_END) == date(2026, 10, 16)
    sunday = EarningsEvent(symbol="X", day=date(2026, 10, 18), hour="amc")
    assert swing_expiry_day(TODAY, 10, [sunday], CALENDAR_END) == date(2026, 10, 16)


def test_swing_expiry_day_with_several_events_the_earliest_wins():
    late = EarningsEvent(symbol="X", day=date(2026, 10, 15), hour="amc")
    early = EarningsEvent(symbol="X", day=date(2026, 10, 13), hour="amc")
    middle = EarningsEvent(symbol="X", day=date(2026, 10, 14), hour="bmo")
    assert swing_expiry_day(TODAY, 10, [late, early, middle], CALENDAR_END) == date(2026, 10, 12)
    assert swing_expiry_day(TODAY, 10, [early, middle, late], CALENDAR_END) == date(2026, 10, 12)


def test_rule_5_a_swing_pick_with_earnings_next_trading_day_is_too_close():
    monday = EarningsEvent(symbol="NVDA", day=date(2026, 10, 12), hour="bmo")
    result = rank([item(horizon="swing", swing_days=5, earnings=[monday])])
    assert reasons(result) == {"NVDA": "earnings_too_close"}


def test_rule_5_intraday_is_refused_only_for_earnings_today_at_an_unknown_hour():
    unknown = EarningsEvent(symbol="NVDA", day=TODAY, hour="unknown")
    tonight = EarningsEvent(symbol="NVDA", day=TODAY, hour="amc")
    assert reasons(rank([item(earnings=[unknown])])) == {"NVDA": "earnings_too_close"}
    assert rank([item(earnings=[tonight])]).picks


def test_rule_5_intraday_earnings_today_at_an_unknown_hour_is_refused_without_a_calendar_flag():
    unknown = EarningsEvent(symbol="NVDA", day=TODAY, hour="unknown")
    result = rank([item(earnings=[unknown])], earnings_ok=False)
    assert reasons(result) == {"NVDA": "earnings_too_close"}


def test_rule_6_blended_score():
    assert blended_score(82, 89, 0.7) == 84  # 57.4 + 26.7 = 84.1
    assert blended_score(65, 35, 0.7) == 56
    assert blended_score(50, 51, 0.5) == 51  # 50.5 rounds up
    assert blended_score(100, 100, 1.0) == 100


def test_rule_6_blend_rounds_exact_halves_up_despite_float_error():
    # 0.7 x 96 + 0.3 x 51 = 82.5 and 0.7 x 96 + 0.3 x 41 = 79.5 exactly, but not in floats.
    assert blended_score(96, 51, 0.7) == 83
    assert blended_score(96, 41, 0.7) == 80


def test_rule_7_ranked_by_score_with_a_sector_cap_and_a_cut():
    settings = RankSettings(max_per_sector=2, max_picks=4)
    inputs = [
        item("A", score=90, sector="Semis"),
        item("B", score=80, sector="Semis"),
        item("C", score=70, sector="Semis"),  # third in its sector
        item("D", score=60, sector=None),
        item("E", score=50, sector="Banks"),
        item("F", score=40, sector="Banks"),  # fifth that passes
    ]
    result = rank(inputs, settings=settings)
    assert [(p.rank, p.symbol) for p in result.picks] == [(1, "A"), (2, "B"), (3, "D"), (4, "E")]
    assert reasons(result) == {"C": "sector_cap", "F": "below_cut"}


def test_an_unknown_sector_is_one_bucket():
    settings = RankSettings(max_per_sector=1)
    result = rank(
        [item("A", score=90, sector=None), item("B", score=80, sector=None)], settings=settings
    )
    assert [p.symbol for p in result.picks] == ["A"]
    assert reasons(result) == {"B": "sector_cap"}


def test_a_real_sector_named_unknown_is_not_the_missing_sector_bucket():
    settings = RankSettings(max_per_sector=1)
    inputs = [
        item("A", score=90, sector="unknown"),
        item("B", score=80, sector=None),
        item("C", score=70, sector=""),
    ]
    result = rank(inputs, settings=settings)
    assert [p.symbol for p in result.picks] == ["A", "B"]
    assert reasons(result) == {"C": "sector_cap"}  # "" and None are one bucket


def test_ties_go_to_the_higher_pre_score_then_the_symbol():
    inputs = [item("B", score=80, pre=80), item("A", score=80, pre=80), item("C", score=80, pre=90)]
    assert [p.symbol for p in rank(inputs).picks] == ["C", "A", "B"]


def test_the_close_of_a_day_is_four_pm_new_york():
    assert close_of(date(2026, 12, 1)) == datetime(2026, 12, 1, 21, 0, tzinfo=UTC)


async def test_ranking_fetches_one_quote_batch_and_puts_for_bearish_names_only():
    market = FakeMarketData()
    market.quote_map = {"NVDA": quote("NVDA", 104, 100), "AMD": quote("AMD", 48, 50)}
    market.put_chains["AMD"] = LIQUID
    inputs = [
        item("NVDA"),
        item("AMD", side="bearish", invalidation=49.0, atr=1.0, score=70),
        item("MSFT", side="pass"),
    ]
    result = await rank_and_validate(
        inputs,
        market=market,
        run_id=RUN,
        today=TODAY,
        close=CLOSE,
        earnings_ok=True,
        calendar_end=CALENDAR_END,
        settings=SETTINGS,
    )
    assert [p.symbol for p in result.picks] == ["NVDA", "AMD"]
    assert market.called("quotes") == [("NVDA", "AMD")]
    assert market.called("puts") == ["AMD"]


async def test_a_chain_that_cannot_be_read_means_illiquid():
    market = FakeMarketData()
    market.quote_map = {"AMD": quote("AMD", 48, 50)}
    market.failures["puts"] = RuntimeError("chain down")
    result = await rank_and_validate(
        [item("AMD", side="bearish", invalidation=49.0, atr=1.0)],
        market=market,
        run_id=RUN,
        today=TODAY,
        close=CLOSE,
        earnings_ok=True,
        calendar_end=CALENDAR_END,
        settings=SETTINGS,
    )
    assert reasons(result) == {"AMD": "illiquid_puts"}
    assert result.chain_failures == 1


async def test_a_failed_chain_read_is_logged_by_symbol_and_type_only(caplog):
    market = FakeMarketData()
    market.quote_map = {"AMD": quote("AMD", 48, 50)}
    market.failures["puts"] = RuntimeError("secret-vendor-text")
    with caplog.at_level("WARNING", logger="traider.research.rank"):
        await rank_and_validate(
            [item("AMD", side="bearish", invalidation=49.0, atr=1.0)],
            market=market,
            run_id=RUN,
            today=TODAY,
            close=CLOSE,
            earnings_ok=True,
            calendar_end=CALENDAR_END,
            settings=SETTINGS,
        )
    assert "AMD" in caplog.text and "RuntimeError" in caplog.text
    assert "secret-vendor-text" not in caplog.text


async def test_a_halted_name_is_dropped_and_its_chain_is_not_read():
    market = FakeMarketData()
    market.quote_map = {"AMD": quote("AMD", 48, 50, halted=True)}
    market.put_chains["AMD"] = LIQUID
    result = await rank_and_validate(
        [item("AMD", side="bearish", invalidation=49.0, atr=1.0)],
        market=market,
        run_id=RUN,
        today=TODAY,
        close=CLOSE,
        earnings_ok=True,
        calendar_end=CALENDAR_END,
        settings=SETTINGS,
    )
    assert reasons(result) == {"AMD": "halted"}
    assert market.called("puts") == []


async def test_a_failed_quote_batch_raises():
    market = FakeMarketData()
    market.failures["quotes"] = RuntimeError("quotes down")
    with pytest.raises(RuntimeError):
        await rank_and_validate(
            [item("NVDA")],
            market=market,
            run_id=RUN,
            today=TODAY,
            close=CLOSE,
            earnings_ok=True,
            calendar_end=CALENDAR_END,
            settings=SETTINGS,
        )


# --- the swing expiry stays inside the earnings calendar ------------------------------
# The calendar covers trading days through L = weekdays_after(today, lookahead): an
# earnings date after L is unknown, so a swing pick never outlives L's close.

LOOKAHEAD_5 = weekdays_after(TODAY, 5)  # Friday 2026-10-16


def test_a_swing_pick_longer_than_the_calendar_is_clamped_to_its_last_day():
    (pick,) = rank([item(horizon="swing", swing_days=20)], calendar_end=LOOKAHEAD_5).picks
    assert pick.expires_at == close_of(LOOKAHEAD_5)
    assert pick.horizon.value == "swing"


def test_earnings_just_past_the_calendar_cannot_be_held_through():
    # Earnings on L+3 are outside the calendar, so the run never saw them. Unclamped, a
    # 20-day swing would run straight through them; clamped, it is out at L's close.
    l_plus_3 = weekdays_after(TODAY, 8)
    assert swing_expiry_day(TODAY, 20, [], LOOKAHEAD_5) == LOOKAHEAD_5
    unseen = EarningsEvent(symbol="NVDA", day=l_plus_3, hour="bmo")
    assert swing_expiry_day(TODAY, 20, [unseen], LOOKAHEAD_5) == LOOKAHEAD_5
    (pick,) = rank([item(horizon="swing", swing_days=20)], calendar_end=LOOKAHEAD_5).picks
    assert pick.expires_at == close_of(LOOKAHEAD_5) < close_of(l_plus_3)


def test_earnings_inside_the_calendar_still_clamp_earlier():
    wednesday = EarningsEvent(symbol="X", day=date(2026, 10, 14), hour="amc")
    assert swing_expiry_day(TODAY, 20, [wednesday], LOOKAHEAD_5) == date(2026, 10, 13)


def test_a_shorter_swing_is_not_stretched_to_the_calendar():
    assert swing_expiry_day(TODAY, 2, [], LOOKAHEAD_5) == date(2026, 10, 13)


@pytest.mark.parametrize("calendar_end", [TODAY, date(2026, 10, 8)])
def test_a_calendar_that_ends_today_or_earlier_leaves_no_swing(calendar_end):
    assert swing_expiry_day(TODAY, 20, [], calendar_end) is None
    result = rank([item(horizon="swing", swing_days=20)], calendar_end=calendar_end)
    assert reasons(result) == {"NVDA": "earnings_too_close"}


def test_the_calendar_does_not_limit_intraday_picks():
    (pick,) = rank([item()], calendar_end=TODAY).picks
    assert pick.expires_at == CLOSE


# --- a swing pick needs this symbol's earnings confirmed --------------------------------


def test_an_unconfirmed_swing_pick_is_refused_as_earnings_unknown():
    result = rank([item(horizon="swing", swing_days=5, confirmed=False)])
    assert reasons(result) == {"NVDA": "earnings_unknown"}
    # Intraday picks do not need it.
    assert rank([item(confirmed=False)]).picks


@pytest.mark.parametrize("symbol", ["BRK/B", "BRK.B", "BF.A"])
def test_a_share_class_symbol_is_never_a_swing_pick(symbol):
    # Vendors spell share classes differently, so its earnings may not match by symbol.
    result = rank([item(symbol, horizon="swing", swing_days=5)])
    assert reasons(result) == {symbol: "earnings_unknown"}
    assert rank([item(symbol)]).picks  # intraday is fine
