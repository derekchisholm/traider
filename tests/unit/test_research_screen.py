"""The code screen: candidates, filters, features and pre_score, with exact numbers."""

from datetime import date

import pytest

from tests.fakes.research import TODAY, flat_bars, quote
from traider.research.events import EarningsEvent
from traider.research.job_settings import ScreenSettings, ScreenWeights
from traider.research.market import DailyBar
from traider.research.screen import (
    Features,
    ScreenRow,
    atr,
    build_candidates,
    compute_features,
    earnings_candidates,
    earnings_days,
    earnings_near,
    history_filter,
    percentile_ranks,
    quote_filter,
    score_rows,
    top_k,
    with_news,
)

SETTINGS = ScreenSettings()
YESTERDAY = date(2026, 10, 8)


def event(symbol, day, hour="unknown") -> EarningsEvent:
    return EarningsEvent(symbol=symbol, day=day, hour=hour)


# --- candidates ---------------------------------------------------------------------


def test_candidates_come_in_priority_order_without_pinned_symbols_and_capped():
    found = build_candidates(
        watchlist=["MSFT"],
        earnings_names=["AMD", "MSFT"],
        movers=["NVDA", "AMD", "SPY", "PLTR"],
        pinned=["SPY"],
        cap=4,
    )
    assert [(c.symbol, c.sources) for c in found] == [
        ("MSFT", ("watchlist", "earnings")),
        ("AMD", ("earnings", "movers")),
        ("NVDA", ("movers",)),
        ("PLTR", ("movers",)),
    ]
    assert (
        len(build_candidates(watchlist=[], earnings_names=[], movers=["A", "B"], pinned=[], cap=1))
        == 1
    )


def test_earnings_candidates_reported_yesterday_after_the_close_or_today_before_the_open():
    events = [
        event("AMD", YESTERDAY, "amc"),
        event("JPM", TODAY, "bmo"),
        event("IBM", YESTERDAY, "bmo"),  # already traded on it yesterday
        event("NFLX", TODAY, "amc"),  # reports tonight
        event("XOM", TODAY, "unknown"),
    ]
    assert earnings_candidates(events, TODAY) == ["AMD", "JPM"]
    monday = date(2026, 10, 12)
    assert earnings_candidates([event("AMD", TODAY, "amc")], monday) == ["AMD"]


# --- filters ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("symbol", "q", "reason"),
    [
        ("NV DA", quote("NV DA", 100, 99), "symbol"),
        ("$VIX", quote("$VIX", 20, 19, asset_type="INDEX"), "symbol"),
        ("NVDA", None, "no_quote"),
        ("NVDA", quote("NVDA", None, 99), "no_quote"),
        ("SPY", quote("SPY", 500, 499, asset_type="COLLECTIVE_INVESTMENT"), "asset_type"),
        ("TQQQ", quote("TQQQ", 50, 49, sub_type="ETF"), "asset_type"),
        ("TVIX", quote("TVIX", 50, 49, sub_type="ETN"), "asset_type"),
        ("BOND", quote("BOND", 50, 49, asset_type="MUTUAL_FUND"), "asset_type"),
        ("OTCX", quote("OTCX", 50, 49, exchange="OTC Markets"), "otc"),
        ("PINK", quote("PINK", 50, 49, exchange="Pink Sheet"), "otc"),
        ("TINY", quote("TINY", 4.99, 5), "price"),
        ("BRKA", quote("BRKA", 1000.01, 1000), "price"),
        ("NVDA", quote("NVDA", 5, 5), None),
        ("NVDA", quote("NVDA", 1000, 999), None),
    ],
)
def test_quote_filters_in_order(symbol, q, reason):
    assert quote_filter(symbol, q, SETTINGS) == reason


def test_etfs_pass_when_allowed():
    allowed = ScreenSettings(allow_etfs=True)
    assert quote_filter("SPY", quote("SPY", 500, 499, sub_type="ETF"), allowed) is None
    assert (
        quote_filter("SPY", quote("SPY", 500, 499, asset_type="COLLECTIVE_INVESTMENT"), allowed)
        is None
    )


def test_history_filter_needs_60_bars_and_enough_dollar_volume():
    q = quote("NVDA", 100, 100, avg_volume=200_000)  # $20M a day: just enough
    assert history_filter(q, flat_bars(100, 1, n=60), SETTINGS) is None
    assert history_filter(q, flat_bars(100, 1, n=59), SETTINGS) == "history"
    thin = quote("NVDA", 100, 100, avg_volume=199_999)
    assert history_filter(thin, flat_bars(100, 1, n=60), SETTINGS) == "dollar_volume"


def test_without_a_quoted_average_volume_the_bars_give_it():
    q = quote("NVDA", 100, 100, avg_volume=None)
    assert history_filter(q, flat_bars(100, 200_000, n=60), SETTINGS) is None
    assert history_filter(q, flat_bars(100, 199_999, n=60), SETTINGS) == "dollar_volume"


# --- features -----------------------------------------------------------------------


def test_features_of_a_flat_stock_gapping_up_on_heavy_volume():
    bars = flat_bars(100.0, 1_000_000, last_volume=3_000_000)
    q = quote("NVDA", 104.0, 100.0, avg_volume=1_000_000, high_52w=130.0)
    features, range_ = compute_features(
        q, bars, today=TODAY, events=[], earnings_ok=True, lookahead=10, news_3d=5
    )
    assert range_ == pytest.approx(2.0)
    assert features.as_dict() == pytest.approx(
        {
            "gap_pct": 4.0,
            "rvol": 3.0,
            "atr_pct": 2.0 / 104 * 100,
            "trend20_pct": 4.0,
            "trend50_pct": 4.0,
            "ret5_pct": 0.0,
            "off_high_pct": (1 - 104 / 130) * 100,
            "dollar_vol_m": 104.0,
            "earnings_days": -1.0,
            "news_3d": 5.0,
            "bias": 1.0,
        }
    )


def test_trend_return_and_52_week_high_come_from_the_bars_when_needed():
    bars = [
        DailyBar(day=d.day, open=c, high=c, low=c, close=c, volume=1000)
        for d, c in zip(flat_bars(1, 1, n=60), [float(i + 1) for i in range(60)], strict=True)
    ]
    q = quote("UP", 60.0, None, high_52w=None, avg_volume=None)
    features, range_ = compute_features(
        q, bars, today=TODAY, events=[], earnings_ok=True, lookahead=10
    )
    assert features.gap_pct == pytest.approx(0.0)  # no previous close: the last bar's
    assert features.trend20_pct == pytest.approx((60 / 50.5 - 1) * 100)
    assert features.trend50_pct == pytest.approx((60 / 35.5 - 1) * 100)
    assert features.ret5_pct == pytest.approx((60 / 55 - 1) * 100)
    assert features.off_high_pct == pytest.approx(0.0)
    assert range_ == pytest.approx(1.0)  # each bar's true range is the step from the last
    assert features.rvol == pytest.approx(1.0)
    assert features.bias == 1.0  # no gap down, and above its 20-day average


def test_a_gap_up_below_the_average_is_mixed():
    q = quote("MIX", 49.5, 48.0)  # up 3.1% on the day, still under the 50.0 average
    features, _ = compute_features(
        q, flat_bars(50.0, 1_000_000), today=TODAY, events=[], earnings_ok=True, lookahead=10
    )
    assert features.bias == 0.0


def test_a_stock_gapping_down_below_its_average_leans_bearish():
    q = quote("AMD", 48.0, 50.0)
    features, _ = compute_features(
        q, flat_bars(50.0, 1_000_000), today=TODAY, events=[], earnings_ok=True, lookahead=10
    )
    assert (features.gap_pct, features.bias) == (pytest.approx(-4.0), -1.0)


def test_atr_is_the_mean_true_range_of_the_last_14_bars():
    bars = flat_bars(100.0, 1, n=20)
    gapped = bars[-1].model_copy(update={"high": 112.0, "low": 108.0, "close": 110.0})
    assert atr([*bars[:-1], gapped]) == pytest.approx((13 * 2 + 12) / 14)
    assert atr([]) == 0.0


@pytest.mark.parametrize(
    ("events", "ok", "expected"),
    [
        ([], True, -1),
        ([], False, -2),
        ([event("X", TODAY, "amc")], True, 0),
        ([event("X", date(2026, 10, 12))], True, 1),
        ([event("X", date(2026, 10, 23))], True, 10),
        ([event("X", date(2026, 10, 26))], True, -1),  # 11 weekdays out
        ([event("X", YESTERDAY, "amc")], True, -1),  # past: no next date
    ],
)
def test_weekdays_to_the_next_earnings(events, ok, expected):
    assert earnings_days(events, TODAY, earnings_ok=ok, lookahead=10) == expected


@pytest.mark.parametrize(
    ("day", "near"),
    [
        (YESTERDAY, True),
        (TODAY, True),
        (date(2026, 10, 12), True),
        (date(2026, 10, 7), False),
        (date(2026, 10, 13), False),
    ],
)
def test_earnings_within_one_weekday_either_side_count_as_a_catalyst(day, near):
    assert earnings_near([event("X", day)], TODAY) is near


# --- pre_score ----------------------------------------------------------------------


def test_percentile_ranks_average_ties():
    assert percentile_ranks([4.0, 4.0, 0.25, 2.0]) == pytest.approx([2.5 / 3, 2.5 / 3, 0, 1 / 3])
    assert percentile_ranks([7.0]) == [1.0]
    assert percentile_ranks([1.0, 1.0]) == [0.5, 0.5]


def row(symbol, gap, rvol, dollars, news, bias, near=False) -> ScreenRow:
    features = Features(
        gap_pct=gap,
        rvol=rvol,
        atr_pct=2,
        trend20_pct=0,
        trend50_pct=0,
        ret5_pct=0,
        off_high_pct=0,
        dollar_vol_m=dollars,
        earnings_days=-1,
        news_3d=news,
        bias=bias,
    )
    return ScreenRow(symbol=symbol, price=100, atr=2, features=features, earnings_near=near)


GOLDEN_ROWS = [
    row("NVDA", 4.0, 3.0, 104.0, 5, 1),
    row("AMD", -4.0, 2.0, 96.0, 2, -1, near=True),
    row("MSFT", 0.25, 1.0, 401.0, 0, 1),
    row("PLTR", 2.0, 1.5, 61.2, 1, 1),
]


def test_pre_score_is_the_weighted_sum_of_ranks():
    scored = score_rows(GOLDEN_ROWS, ScreenWeights())
    # NVDA: .35 x 2.5/3 + .25 x 1 + .15 x 2/3 + .15 x 1 + .10 = .8917
    # AMD:  .35 x 2.5/3 + .25 x 2/3 + .15 x 1/3 + .15 x 1 (earnings) + .10 = .7583
    # MSFT: 0 + 0 + .15 x 1 + .15 x 0 + .10 = .25
    # PLTR: .35 x 1/3 + .25 x 1/3 + 0 + .15 x 1/3 + .10 = .35
    assert [(r.symbol, r.pre_score) for r in scored] == [
        ("NVDA", 89),
        ("AMD", 76),
        ("MSFT", 25),
        ("PLTR", 35),
    ]


def test_weights_change_the_score():
    only_liquidity = ScreenWeights(move=0, participation=0, liquidity=1, catalyst=0, alignment=0)
    scored = score_rows(GOLDEN_ROWS, only_liquidity)
    assert [r.pre_score for r in scored] == [67, 33, 100, 0]


def test_no_bias_scores_no_alignment():
    flat = score_rows([row("A", 0, 1, 1, 0, 0)], ScreenWeights())
    assert flat[0].pre_score == 90  # every rank is 1 with one name, alignment is 0


def test_news_counts_can_be_filled_in_after_the_first_score():
    first = row("A", 1, 1, 1, 0, 1)
    assert with_news(first, 7).features.news_3d == 7.0
    assert score_rows([], ScreenWeights()) == []


def test_top_k_is_by_score_then_symbol():
    scored = score_rows(GOLDEN_ROWS, ScreenWeights())
    assert [r.symbol for r in top_k(scored, 3)] == ["NVDA", "AMD", "PLTR"]
    tied = [row("B", 1, 1, 1, 0, 1), row("A", 1, 1, 1, 0, 1)]
    assert [r.symbol for r in top_k(score_rows(tied, ScreenWeights()), 2)] == ["A", "B"]


def test_a_missing_price_is_dropped_by_the_history_filter_and_refused_by_features():
    no_price = quote("NVDA", None, 99)
    assert history_filter(no_price, flat_bars(100, 1_000_000), SETTINGS) == "no_quote"
    with pytest.raises(ValueError, match="run the filters first"):
        compute_features(
            no_price,
            flat_bars(100, 1_000_000),
            today=TODAY,
            events=[],
            earnings_ok=True,
            lookahead=10,
        )
    with pytest.raises(ValueError, match="run the filters first"):
        compute_features(
            quote("NVDA", 100, 99),
            flat_bars(100, 1_000_000, n=59),
            today=TODAY,
            events=[],
            earnings_ok=True,
            lookahead=10,
        )
