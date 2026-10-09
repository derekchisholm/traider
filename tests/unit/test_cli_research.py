"""Hand-made research for paper testing before the research jobs exist."""

import io
import json
from datetime import UTC, datetime

import pytest
from moto import mock_aws

from tests.unit.test_research_store import TABLE, make_table
from traider import cli
from traider.config import ResearchSettings
from traider.research.seed import build_manual_run
from traider.research.source import ResearchSource
from traider.research.store import MemoryResearchStore

NOW = datetime(2026, 10, 8, 13, 0, tzinfo=UTC)  # 09:00 New York (EDT, UTC-4)


def seed_file(tmp_path, body) -> str:
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(body))
    return str(path)


GOOD = {
    "posture": {"level": "trade", "reasons": ["quiet macro calendar"]},
    "picks": [
        {
            "symbol": "NVDA",
            "side": "long",
            "horizon": "intraday",
            "score": 80,
            "thesis": "t",
            "invalidation": "100",
        },
        {
            "symbol": "AMD",
            "side": "bearish",
            "horizon": "swing",
            "score": 70,
            "thesis": "t",
            "invalidation": "200",
        },
    ],
}


def pick(**overrides) -> dict:
    return {**GOOD["picks"][0], **overrides}


async def test_seed_writes_a_manual_run_the_bot_will_read(tmp_path):
    store = MemoryResearchStore()
    out = io.StringIO()
    assert await cli.research_seed(store, seed_file(tmp_path, GOOD), out, now=NOW) == 0
    source = ResearchSource(store, ResearchSettings)
    await source.refresh(NOW)
    assert set(source.view.live_picks(NOW)) == {"NVDA", "AMD"}
    assert source.view.level.value == "trade"
    nvda = source.view.pick("NVDA", NOW)
    assert nvda.expires_at == datetime(2026, 10, 8, 20, 0, tzinfo=UTC)  # 16:00 New York
    assert source.view.pick("AMD", NOW).expires_at == datetime(2026, 10, 15, 20, 0, tzinfo=UTC)
    assert "manual-20261008T130000Z" in out.getvalue()


async def test_a_bad_seed_file_writes_nothing(tmp_path):
    store = MemoryResearchStore()
    bad = {"picks": [GOOD["picks"][0], {"symbol": "X", "side": "short"}]}
    out = io.StringIO()
    assert await cli.research_seed(store, seed_file(tmp_path, bad), out, now=NOW) == 1
    assert store._items == {}
    assert "invalid seed file" in out.getvalue()


async def test_a_bad_posture_after_good_picks_writes_nothing(tmp_path):
    store = MemoryResearchStore()
    bad = {**GOOD, "posture": {"level": "yolo", "reasons": []}}
    assert await cli.research_seed(store, seed_file(tmp_path, bad), io.StringIO(), now=NOW) == 1
    assert store._items == {}


@pytest.mark.parametrize("content", ["not json", "[]", "{}", '{"picks": []}'])
async def test_files_that_are_not_a_seed_are_refused(tmp_path, content):
    path = tmp_path / "seed.json"
    path.write_text(content)
    store = MemoryResearchStore()
    assert await cli.research_seed(store, str(path), io.StringIO(), now=NOW) == 1
    assert store._items == {}


async def test_a_missing_seed_file_is_refused(tmp_path):
    store = MemoryResearchStore()
    out = io.StringIO()
    assert await cli.research_seed(store, str(tmp_path / "nope.json"), out, now=NOW) == 1
    assert "invalid seed file" in out.getvalue()
    assert store._items == {}


async def test_show_prints_posture_and_live_picks(tmp_path):
    store = MemoryResearchStore()
    await cli.research_seed(store, seed_file(tmp_path, GOOD), io.StringIO(), now=NOW)
    out = io.StringIO()
    assert await cli.research_show(ResearchSource(store, ResearchSettings), out, now=NOW) == 0
    text = out.getvalue()
    assert "posture trade" in text and "quiet macro calendar" in text
    assert "NVDA long intraday 80" in text and "AMD bearish swing 70" in text


async def test_show_says_so_when_there_is_nothing():
    out = io.StringIO()
    source = ResearchSource(MemoryResearchStore(), ResearchSettings)
    assert await cli.research_show(source, out, now=NOW) == 0
    assert "posture stand_aside" in out.getvalue()
    assert "no live picks" in out.getvalue()


async def test_show_does_not_pass_off_an_unreadable_table_as_no_research():
    class Broken(MemoryResearchStore):
        async def day(self, day):
            raise RuntimeError("table unreadable")

    out = io.StringIO()
    assert await cli.research_show(ResearchSource(Broken(), ResearchSettings), out, now=NOW) == 1
    assert "could not be read" in out.getvalue()
    assert "posture" not in out.getvalue()


def test_research_needs_a_table(monkeypatch, capsys):
    monkeypatch.setenv("TRAIDER_SYMBOLS", "SPY")
    monkeypatch.delenv("TRAIDER_RESEARCH_TABLE", raising=False)
    assert cli.main(["research", "show"]) == 2
    assert "TRAIDER_RESEARCH_TABLE is not set" in capsys.readouterr().err
    assert cli.main(["research", "seed", "x.json"]) == 2


def test_the_commands_work_against_the_research_table(monkeypatch, tmp_path, capsys):
    """Through main: the real DynamoDB store (moto), config from the environment."""
    monkeypatch.setenv("TRAIDER_RESEARCH_TABLE", TABLE)
    monkeypatch.delenv("TRAIDER_SYMBOLS", raising=False)
    swing_only = {**GOOD, "picks": [GOOD["picks"][1]]}  # never expires mid-test
    with mock_aws():
        make_table()
        assert cli.main(["research", "seed", seed_file(tmp_path, swing_only)]) == 0
        assert "wrote run manual-" in capsys.readouterr().out
        assert cli.main(["research", "show"]) == 0
        text = capsys.readouterr().out
    assert "posture trade: quiet macro calendar" in text
    assert "AMD bearish swing 70" in text


def test_a_bad_seed_file_through_main_exits_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TRAIDER_RESEARCH_TABLE", TABLE)
    path = tmp_path / "seed.json"
    path.write_text("{not json")
    with mock_aws():
        make_table()
        assert cli.main(["research", "seed", str(path)]) == 1
    assert "invalid seed file" in capsys.readouterr().out


# --- build_manual_run -----------------------------------------------------------------


def test_build_names_the_run_after_the_time_and_ranks_in_list_order():
    meta, picks, posture = build_manual_run(GOOD, NOW)
    assert meta.run_id == "manual-20261008T130000Z"
    assert (meta.kind, meta.status.value) == ("manual", "ok")
    assert meta.trading_day.isoformat() == "2026-10-08"
    assert [(p.rank, p.symbol, p.run_id) for p in picks] == [
        (1, "NVDA", meta.run_id),
        (2, "AMD", meta.run_id),
    ]
    assert posture is not None and posture.run_id == meta.run_id and posture.at == NOW


def test_pre_score_defaults_to_score_and_can_be_set():
    _, picks, _ = build_manual_run({"picks": [pick(), pick(symbol="AMD", pre_score=55)]}, NOW)
    assert [p.pre_score for p in picks] == [80, 55]


def test_no_posture_means_none():
    _, _, posture = build_manual_run({"picks": [pick()]}, NOW)
    assert posture is None


def test_expiry_follows_new_york_time_through_the_clock_change():
    # 2026-10-08 is EDT (UTC-4): 16:00 is 20:00 UTC. 2026-11-02 is EST (UTC-5): 21:00 UTC.
    _, picks, _ = build_manual_run(GOOD, NOW)
    assert picks[0].expires_at == datetime(2026, 10, 8, 20, 0, tzinfo=UTC)
    winter = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
    _, picks, _ = build_manual_run(GOOD, winter)
    assert picks[0].expires_at == datetime(2026, 11, 2, 21, 0, tzinfo=UTC)
    # Five weekdays after Thursday 2026-10-29 is Thursday 2026-11-05, after the clocks went back.
    late = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
    _, picks, _ = build_manual_run(GOOD, late)
    assert picks[1].expires_at == datetime(2026, 11, 5, 21, 0, tzinfo=UTC)


def test_swing_expiry_skips_weekends():
    friday = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)
    _, picks, _ = build_manual_run(GOOD, friday)
    assert picks[1].expires_at == datetime(2026, 10, 16, 20, 0, tzinfo=UTC)


def test_the_trading_day_is_the_new_york_day():
    late = datetime(2026, 10, 9, 2, 0, tzinfo=UTC)  # 22:00 on the 8th in New York
    meta, picks, _ = build_manual_run(GOOD, late)
    assert meta.trading_day.isoformat() == "2026-10-08"
    assert picks[0].expires_at == datetime(2026, 10, 8, 20, 0, tzinfo=UTC)


def test_an_explicit_expiry_wins():
    expiry = "2026-10-08T18:30:00+00:00"
    _, picks, _ = build_manual_run({"picks": [pick(expires_at=expiry)]}, NOW)
    assert picks[0].expires_at == datetime(2026, 10, 8, 18, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"picks": "NVDA"},
        {"picks": [pick()], "extra": 1},
        {"picks": [pick(colour="red")]},
        {"picks": [pick(score="80")]},
        {"picks": [pick(score=101)]},
        {"picks": [pick(side="short")]},
        {"picks": [pick(invalidation="0")]},
        {"picks": [pick(expires_at="2026-10-08T18:30:00")]},  # no timezone
        {"picks": [pick(expires_at=1_700_000_000)]},
        {"picks": [pick(symbol="not a symbol")]},
        {"picks": [pick(), pick()]},  # the same symbol twice
        {"picks": [{**pick(), "symbol": "amd"}]},
        {"picks": [{k: v for k, v in pick().items() if k != "thesis"}]},
    ],
)
def test_bad_input_is_a_value_error(data):
    with pytest.raises(ValueError):
        build_manual_run(data, NOW)


def test_a_naive_clock_is_refused():
    with pytest.raises(ValueError):
        build_manual_run(GOOD, datetime(2026, 10, 8, 13, 0))


def test_a_posture_alone_is_a_valid_seed():
    meta, picks, posture = build_manual_run({"posture": {"level": "stand_aside"}}, NOW)
    assert picks == [] and posture is not None and posture.reasons == ()
    assert meta.run_id.startswith("manual-")
