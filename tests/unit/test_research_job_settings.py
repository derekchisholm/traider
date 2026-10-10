"""The research jobs' settings: defaults from the spec, and the rules between fields."""

from datetime import date, time
from decimal import Decimal

import pytest
from pydantic import ValidationError

from traider.research.job_settings import DEFAULT_MODEL, ResearchJobSettings
from traider.settings import Settings


def jobs(**fields) -> ResearchJobSettings:
    return ResearchJobSettings.model_validate(fields)


def test_defaults_are_the_specs():
    s = ResearchJobSettings()
    assert (s.enabled, s.watchlist, s.max_run_s) == (True, (), 1200.0)
    assert (s.collect.max_candidates, s.collect.earnings_lookahead_days) == (150, 10)
    assert s.collect.market_news_count == 30
    assert (s.posture.vix_reduced, s.posture.vix_stand_aside) == (25.0, 35.0)
    assert (s.posture.gap_reduced_pct, s.posture.gap_stand_aside_pct) == (1.5, 3.0)
    assert s.posture.reduce_below_sma50 is True
    assert (s.screen.min_price, s.screen.max_price) == (Decimal(5), Decimal(1000))
    assert s.screen.min_dollar_volume == Decimal(20_000_000)
    assert (s.screen.allow_etfs, s.screen.deep_dive_count) == (False, 12)
    weights = s.screen.weights
    assert (weights.move, weights.participation, weights.liquidity) == (0.35, 0.25, 0.15)
    assert (weights.catalyst, weights.alignment) == (0.15, 0.10)
    assert s.dive.model == s.dive.posture_model == DEFAULT_MODEL == "anthropic.claude-sonnet-5-5"
    assert (s.dive.max_tool_calls, s.dive.max_turns, s.dive.max_tokens) == (6, 8, 2000)
    assert (s.dive.max_dive_input_tokens, s.dive.dive_timeout_s) == (60_000, 180.0)
    assert (s.dive.dive_concurrency, s.dive.tool_result_max_chars) == (4, 6000)
    assert (s.rank.llm_weight, s.rank.min_stop_atr, s.rank.max_stop_atr) == (0.7, 0.3, 3.0)
    assert (s.rank.max_put_spread_pct, s.rank.min_put_oi) == (10.0, 100)
    assert (s.rank.max_per_sector, s.rank.max_picks) == (3, 10)
    assert (s.budget.run_usd, s.budget.day_usd) == (Decimal("3.00"), Decimal("8.00"))
    price = s.budget.prices[DEFAULT_MODEL]
    assert (price.in_per_mtok, price.out_per_mtok) == (Decimal(2), Decimal(10))


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"posture": {"vix_reduced": 35, "vix_stand_aside": 35}}, "vix_reduced"),
        ({"posture": {"gap_reduced_pct": 3.0}}, "gap_reduced_pct"),
        ({"screen": {"weights": {"move": 0.5}}}, "sum to 1"),
        ({"rank": {"min_stop_atr": 3.0}}, "min_stop_atr"),
        ({"budget": {"run_usd": "9"}}, "run_usd"),
        ({"dive": {"model": "anthropic.claude-opus-5"}}, "no price"),
        ({"dive": {"posture_model": "other"}}, "no price"),
        ({"screen": {"min_price": 1000}}, "min_price"),
        ({"rank": {"max_picks": 26}}, "max_picks"),
        ({"watchlist": ["NV DA"]}, "symbol"),
        ({"watchlist": ["NVDA", "NVDA"]}, "duplicate"),
        ({"watchlist": [f"A{i}" for i in range(51)]}, "50"),
        ({"surprise": 1}, "surprise"),
        (
            {"budget": {"prices": {DEFAULT_MODEL: {"in_per_mtok": 0, "out_per_mtok": 10}}}},
            "in_per_mtok",
        ),
        (
            {"budget": {"prices": {DEFAULT_MODEL: {"in_per_mtok": 2, "out_per_mtok": 0}}}},
            "out_per_mtok",
        ),
    ],
)
def test_inconsistent_settings_are_rejected(fields, message):
    with pytest.raises(ValidationError, match=message):
        jobs(**fields)


def test_weights_that_sum_to_one_within_rounding_are_accepted():
    weights = {"move": 0.1, "participation": 0.2, "liquidity": 0.3, "catalyst": 0.3}
    assert jobs(screen={"weights": {**weights, "alignment": 0.1}}).screen.weights.move == 0.1


def test_a_model_with_a_price_can_be_chosen():
    s = jobs(
        dive={"model": "anthropic.claude-sonnet-5"},
        budget={
            "prices": {
                "anthropic.claude-sonnet-5": {"in_per_mtok": "3", "out_per_mtok": "15"},
                DEFAULT_MODEL: {"in_per_mtok": "2", "out_per_mtok": "10"},
            }
        },
    )
    assert s.dive.model == "anthropic.claude-sonnet-5"


def test_a_watchlist_may_be_longer_than_the_bots_universe():
    assert len(jobs(watchlist=[f"A{i}" for i in range(50)]).watchlist) == 50


def test_research_jobs_round_trip_through_the_settings_json():
    settings = Settings(
        research_jobs={"posture": {"reduced_days": ["2026-10-28"]}, "watchlist": ["NVDA"]}
    )
    again = Settings.model_validate_json(settings.model_dump_json())
    assert again == settings
    assert again.research_jobs.posture.reduced_days == (date(2026, 10, 28),)


def test_settings_written_before_research_jobs_existed_still_load():
    body = Settings().model_dump(mode="json")
    del body["research_jobs"]
    assert Settings.model_validate(body).research_jobs == ResearchJobSettings()


# --- C2a: intraday runs and the scorecard ------------------------------------------------


def test_the_c2a_defaults_are_the_specs():
    s = ResearchJobSettings()
    assert s.dive.intraday_model == DEFAULT_MODEL
    assert s.budget.intraday_run_usd == Decimal("0.75")
    i = s.intraday
    assert (i.enabled, i.last_start, i.max_candidates) == (True, time(15, 0), 30)
    assert (i.deep_dive_count, i.max_run_s) == (3, 600.0)
    assert (s.scorecard.enabled, s.scorecard.lookback_days) == (True, 30)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"budget": {"intraday_run_usd": "9"}}, "intraday_run_usd"),
        ({"dive": {"intraday_model": "anthropic.claude-haiku-5"}}, "no price"),
        ({"intraday": {"last_start": "09:30"}}, "inside the session"),
        ({"intraday": {"last_start": "16:00"}}, "inside the session"),
        ({"intraday": {"last_start": "15:00+00:00"}}, "without a timezone"),
        ({"intraday": {"deep_dive_count": 11}}, "deep_dive_count"),
        ({"intraday": {"max_run_s": 1201}}, "max_run_s"),
        ({"scorecard": {"lookback_days": 4}}, "lookback_days"),
        ({"scorecard": {"lookback_days": 61}}, "lookback_days"),
        ({"scorecard": {"surprise": 1}}, "surprise"),
    ],
)
def test_inconsistent_c2a_settings_are_rejected(fields, message):
    with pytest.raises(ValidationError, match=message):
        jobs(**fields)


def test_a_cheaper_intraday_model_needs_its_price():
    haiku = "anthropic.claude-haiku-5"
    s = jobs(
        dive={"intraday_model": haiku},
        budget={
            "prices": {
                haiku: {"in_per_mtok": "1", "out_per_mtok": "5"},
                DEFAULT_MODEL: {"in_per_mtok": "2", "out_per_mtok": "10"},
            }
        },
    )
    assert s.dive.intraday_model == haiku


def test_c2a_settings_round_trip_and_older_versions_still_load():
    settings = Settings(research_jobs={"intraday": {"last_start": "14:30"}})
    again = Settings.model_validate_json(settings.model_dump_json())
    assert again.research_jobs.intraday.last_start == time(14, 30)
    body = Settings().model_dump(mode="json")
    for name in ("intraday", "scorecard"):
        del body["research_jobs"][name]
    del body["research_jobs"]["dive"]["intraday_model"]
    del body["research_jobs"]["budget"]["intraday_run_usd"]
    assert Settings.model_validate(body).research_jobs == ResearchJobSettings()
