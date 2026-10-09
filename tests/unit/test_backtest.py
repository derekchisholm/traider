from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from traider.backtest import (
    BacktestError,
    load_bars_csv,
    load_picks_jsonl,
    max_drawdown,
    run_backtest,
)
from traider.config import Config
from traider.models import Bar, Side
from traider.timeutil import ET, trading_date

OPEN = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)  # Thursday 11:00 New York


def bars(closes, *, symbol="SPY", start=OPEN) -> list[Bar]:
    out = []
    for i, close in enumerate(closes):
        price = Decimal(str(close))
        out.append(Bar(symbol, start + timedelta(minutes=i), price, price, price, price, 1000))
    return out


def config(**overrides) -> Config:
    fields = {
        "symbols": ("SPY",),
        "strategy": "sma_cross",
        "strategy_params": {"fast": 1, "slow": 2, "position_usd": 300},
    }
    fields.update(overrides)
    return Config.model_validate(fields)


async def test_round_trip_is_traded_at_bar_closes():
    result = await run_backtest(bars([100, 101, 102, 99]), config(), spread_bps=Decimal(0))
    assert [(t.side, t.quantity, t.price) for t in result.trades] == [
        (Side.BUY, 2, Decimal(101)),
        (Side.SELL, 2, Decimal(99)),
    ]
    assert result.start_equity == Decimal(10000)
    assert result.end_equity == Decimal(9996)
    assert result.return_pct == Decimal("-0.04")


async def test_trades_carry_the_time_of_the_bar_close_that_triggered_them():
    result = await run_backtest(bars([100, 101, 102, 99]), config(), spread_bps=Decimal(0))
    assert result.trades[0].time == OPEN + timedelta(minutes=2)  # the 11:01 bar closes at 11:02


async def test_the_spread_is_paid_on_the_way_in_and_out():
    result = await run_backtest(bars([100, 101, 102, 99]), config(), spread_bps=Decimal(10))
    buy, sell = result.trades
    assert buy.price == Decimal("101.0505")  # half of 10 bps above the close
    assert sell.price == Decimal("98.9505")
    assert result.end_equity < Decimal(9996)


async def test_round_trips_and_wins_are_counted():
    closes = [100, 101, 99, 100, 104, 103]  # lose on the first trip, win on the second
    result = await run_backtest(
        bars(closes), config(risk={"order_cooldown_s": 0}), spread_bps=Decimal(0)
    )
    assert (result.round_trips, result.wins) == (2, 1)
    assert result.realized_pnl == Decimal(2) * (99 - 101) + Decimal(3) * (103 - 100)


async def test_a_position_still_open_at_the_end_is_reported_not_closed():
    result = await run_backtest(bars([100, 101, 102, 103]), config(), spread_bps=Decimal(0))
    assert [t.side for t in result.trades] == [Side.BUY]
    assert result.open_positions == {"SPY": 2}
    assert result.end_equity == Decimal(10000) + 2 * (103 - 101)


async def test_a_live_configuration_can_be_backtested_and_stays_on_paper():
    live = config(
        trading_mode="live",
        account_last4="1234",
        control_param="/traider/prod/control",
        state_table="traider-prod",
    )
    result = await run_backtest(bars([100, 101, 102, 99]), live, spread_bps=Decimal(0))
    assert len(result.trades) == 2


async def test_same_input_gives_the_same_result():
    closes = [100, 101, 99, 100, 104, 103, 105, 101]
    first = await run_backtest(bars(closes), config(), spread_bps=Decimal(2))
    second = await run_backtest(bars(closes), config(), spread_bps=Decimal(2))
    assert first == second


async def test_equity_is_recorded_after_every_bar():
    result = await run_backtest(bars([100, 101, 102, 99]), config(), spread_bps=Decimal(0))
    assert [equity for _, equity in result.equity_curve] == [
        Decimal(10000),
        Decimal(10000),  # bought 2 at 101, marked at 101
        Decimal(10002),
        Decimal(9996),
    ]


async def test_risk_limits_apply_in_a_backtest_and_blocks_are_counted():
    closes = [100, 101, 99, 100, 104, 103]
    limits = {"max_orders_per_day": 2, "order_cooldown_s": 0}
    result = await run_backtest(bars(closes), config(risk=limits), spread_bps=Decimal(0))
    # One buy and one sell use up the day's two orders; the second entry is refused.
    assert [t.side for t in result.trades] == [Side.BUY, Side.SELL]
    assert result.blocked["max_orders_per_day"] >= 1


async def test_cooldown_between_entries_applies_in_a_backtest():
    closes = [100, 101, 99, 100, 104, 103]
    result = await run_backtest(
        bars(closes), config(risk={"order_cooldown_s": 600}), spread_bps=Decimal(0)
    )
    assert [t.side for t in result.trades] == [Side.BUY, Side.SELL]
    assert result.blocked["cooldown"] >= 1


async def test_bars_outside_the_regular_session_are_ignored():
    saturday = datetime(2026, 10, 10, 15, 0, tzinfo=UTC)
    premarket = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    data = bars([100, 101, 102], start=saturday) + bars([100, 101, 102], start=premarket)
    result = await run_backtest(data, config(), spread_bps=Decimal(0))
    assert result.trades == ()
    assert result.bars == 0


async def test_symbols_are_replayed_together_in_time_order():
    data = bars([100, 101, 102, 99], symbol="SPY") + bars([50, 51, 52, 49], symbol="QQQ")
    result = await run_backtest(data, config(symbols=("SPY", "QQQ")), spread_bps=Decimal(0))
    assert {t.symbol for t in result.trades} == {"SPY", "QQQ"}
    assert [t.time for t in result.trades] == sorted(t.time for t in result.trades)


async def test_a_symbol_with_no_bar_this_minute_is_not_traded_on_its_old_price():
    opening = datetime(2026, 10, 8, 13, 30, tzinfo=UTC)  # 09:30 New York
    spy = bars([100] * 10, start=opening)  # flat: never a signal
    qqq = bars([100, 101], symbol="QQQ", start=opening)  # a buy signal, then silence
    both = config(symbols=("SPY", "QQQ"), risk={"entry_delay_min_after_open": 5})
    result = await run_backtest(
        sorted(spy + qqq, key=lambda bar: bar.start), both, spread_bps=Decimal(0)
    )
    # The signal came inside the opening delay. By the time buying is allowed, QQQ's
    # last price is minutes old, and the bot must not trade on it.
    assert result.trades == ()
    assert result.blocked["no_quote"] + result.blocked["quote_stale"] > 0


async def test_bars_for_symbols_not_in_the_config_are_an_error():
    with pytest.raises(BacktestError, match="IWM"):
        await run_backtest(bars([100, 101], symbol="IWM"), config(), spread_bps=Decimal(0))


async def test_no_bars_is_an_error():
    with pytest.raises(BacktestError, match="no bars"):
        await run_backtest([], config())


async def test_starting_cash_can_be_set():
    result = await run_backtest(
        bars([100, 101]), config(), spread_bps=Decimal(0), starting_cash=Decimal(2500)
    )
    assert result.start_equity == Decimal(2500)


def test_max_drawdown_is_the_worst_peak_to_trough_fall():
    curve = [Decimal(v) for v in (10000, 9990, 10010, 9980, 10005)]
    assert max_drawdown(curve) == (Decimal(10010) - Decimal(9980)) / Decimal(10010) * 100


def test_max_drawdown_of_a_rising_curve_is_zero():
    assert max_drawdown([Decimal(1), Decimal(2), Decimal(3)]) == 0


# --- CSV -------------------------------------------------------------------------------


def write(tmp_path, text):
    path = tmp_path / "bars.csv"
    path.write_text(text)
    return path


def test_csv_with_iso_timestamps_and_a_symbol_column(tmp_path):
    path = write(
        tmp_path,
        (
            "timestamp,open,high,low,close,volume,symbol\n"
            "2026-10-08T11:00:00-04:00,512.1,512.9,511.8,512.5,120345,SPY\n"
            "2026-10-08T15:01:00Z,512.5,513.0,512.4,512.8,98000,SPY\n"
        ),
    )
    first, second = load_bars_csv(path)
    assert (first.symbol, first.start, first.close, first.volume) == (
        "SPY",
        OPEN,
        Decimal("512.5"),
        120345,
    )
    assert second.start == OPEN + timedelta(minutes=1)


def test_csv_without_a_symbol_column_uses_the_given_symbol(tmp_path):
    path = write(
        tmp_path, "timestamp,open,high,low,close,volume\n2026-10-08T15:00:00Z,1,2,0.5,1.5,10\n"
    )
    (bar,) = load_bars_csv(path, symbol="QQQ")
    assert bar.symbol == "QQQ"


def test_csv_without_any_symbol_is_an_error(tmp_path):
    path = write(
        tmp_path, "timestamp,open,high,low,close,volume\n2026-10-08T15:00:00Z,1,2,0.5,1.5,10\n"
    )
    with pytest.raises(BacktestError, match="symbol"):
        load_bars_csv(path)


@pytest.mark.parametrize("stamp", ["1791471600", "1791471600000"])
def test_csv_accepts_epoch_seconds_and_milliseconds(tmp_path, stamp):
    path = write(tmp_path, f"timestamp,open,high,low,close,volume\n{stamp},1,2,0.5,1.5,10\n")
    (bar,) = load_bars_csv(path, symbol="SPY")
    assert bar.start == OPEN


def test_csv_rows_are_sorted_by_time(tmp_path):
    path = write(
        tmp_path,
        (
            "timestamp,open,high,low,close,volume\n"
            "2026-10-08T15:01:00Z,2,2,2,2,10\n"
            "2026-10-08T15:00:00Z,1,1,1,1,10\n"
        ),
    )
    assert [bar.close for bar in load_bars_csv(path, symbol="SPY")] == [Decimal(1), Decimal(2)]


def test_csv_timestamp_without_a_timezone_is_refused(tmp_path):
    path = write(
        tmp_path, "timestamp,open,high,low,close,volume\n2026-10-08T11:00:00,1,2,0.5,1.5,10\n"
    )
    with pytest.raises(BacktestError, match="line 2"):
        load_bars_csv(path, symbol="SPY")


def test_csv_bad_number_names_the_line(tmp_path):
    path = write(
        tmp_path,
        (
            "timestamp,open,high,low,close,volume\n"
            "2026-10-08T15:00:00Z,1,2,0.5,1.5,10\n"
            "2026-10-08T15:01:00Z,1,2,oops,1.5,10\n"
        ),
    )
    with pytest.raises(BacktestError, match="line 3"):
        load_bars_csv(path, symbol="SPY")


def test_csv_missing_column_is_explained(tmp_path):
    path = write(tmp_path, "timestamp,open,high,low,volume\n2026-10-08T15:00:00Z,1,2,0.5,10\n")
    with pytest.raises(BacktestError, match="close"):
        load_bars_csv(path, symbol="SPY")


# --- research picks and postures ----------------------------------------------------------


def rising_bars(symbol, day, *, count=30) -> list[Bar]:
    """One-minute bars from 09:31 New York on ``day`` that rise by 0.1 a minute."""
    opening = datetime.fromisoformat(f"{day}T09:31:00").replace(tzinfo=ET)
    out = []
    for i in range(count):
        price = Decimal(100) + Decimal("0.1") * i
        start = (opening + timedelta(minutes=i)).astimezone(UTC)
        out.append(Bar(symbol, start, price, price, price, price, 1000))
    return out


def pick(day, symbol="NVDA", **overrides) -> dict:
    row = {
        "day": day,
        "symbol": symbol,
        "side": "long",
        "horizon": "intraday",
        "score": 90,
        "thesis": "t",
        "invalidation": "1",
    }
    row.update(overrides)
    return row


def posture(day, level) -> dict:
    return {"day": day, "posture": level}


def research_config(**overrides) -> Config:
    fields = {"symbols": (), "research_table": "x", "strategy_params": {"fast": 1, "slow": 2}}
    fields.update(overrides)
    return Config.model_validate(fields)


def buy_days(result) -> set[str]:
    """The days new positions were opened. Exits do not depend on research."""
    return {trading_date(t.time).isoformat() for t in result.trades if t.side is Side.BUY}


async def test_a_backtest_with_picks_trades_only_the_picked_symbol_on_its_day(tmp_path):
    bars = rising_bars("NVDA", "2026-10-06") + rising_bars("AMD", "2026-10-06")
    # AMD is only picked for the next day, so it has bars to replay but no pick on this one.
    picks = [pick("2026-10-06"), pick("2026-10-07", "AMD")]
    config = Config(symbols=(), research_table="x", strategy_params={"fast": 1, "slow": 2})
    result = await run_backtest(bars, config, picks=picks)
    assert {t.symbol for t in result.trades} == {"NVDA"}


async def test_a_stand_aside_day_trades_nothing(tmp_path):
    bars = rising_bars("NVDA", "2026-10-06")
    picks = [pick("2026-10-06"), posture("2026-10-06", "stand_aside")]
    config = Config(symbols=(), research_table="x", strategy_params={"fast": 1, "slow": 2})
    result = await run_backtest(bars, config, picks=picks)
    assert result.trades == ()
    assert result.blocked["posture"] > 0


async def test_without_a_posture_row_the_day_is_a_trade_day():
    result = await run_backtest(
        rising_bars("NVDA", "2026-10-06"), research_config(), picks=[pick("2026-10-06")]
    )
    assert result.trades


async def test_a_day_with_no_research_rows_trades_nothing():
    bars = rising_bars("NVDA", "2026-10-06") + rising_bars("NVDA", "2026-10-07")
    result = await run_backtest(bars, research_config(), picks=[pick("2026-10-06")])
    assert buy_days(result) == {"2026-10-06"}


async def test_a_trade_day_then_a_stand_aside_day_trades_only_the_first():
    bars = rising_bars("NVDA", "2026-10-06") + rising_bars("NVDA", "2026-10-07")
    picks = [
        pick("2026-10-06"),
        pick("2026-10-07"),
        posture("2026-10-06", "trade"),
        posture("2026-10-07", "stand_aside"),
    ]
    # poll_s at its largest: the day change must still be seen at the new day's first bars.
    config = research_config(research={"poll_s": 600})
    result = await run_backtest(bars, config, picks=picks)
    assert buy_days(result) == {"2026-10-06"}
    assert result.blocked["posture"] > 0


async def test_a_stand_aside_day_then_a_trade_day_trades_only_the_second():
    bars = rising_bars("NVDA", "2026-10-06") + rising_bars("NVDA", "2026-10-07")
    picks = [
        pick("2026-10-06"),
        pick("2026-10-07"),
        posture("2026-10-06", "stand_aside"),
        posture("2026-10-07", "trade"),
    ]
    result = await run_backtest(bars, research_config(research={"poll_s": 600}), picks=picks)
    assert buy_days(result) == {"2026-10-07"}


async def test_a_swing_pick_carries_to_the_next_day_and_an_intraday_pick_does_not():
    bars = (
        rising_bars("NVDA", "2026-10-06")
        + rising_bars("AMD", "2026-10-06")
        + rising_bars("NVDA", "2026-10-07")
        + rising_bars("AMD", "2026-10-07")
    )
    picks = [
        pick("2026-10-06", "NVDA", horizon="swing"),
        pick("2026-10-06", "AMD", horizon="intraday"),
        posture("2026-10-07", "trade"),
    ]
    result = await run_backtest(bars, research_config(), picks=picks)
    bought = {
        t.symbol
        for t in result.trades
        if t.side is Side.BUY and trading_date(t.time).isoformat() == "2026-10-07"
    }
    assert bought == {"NVDA"}


async def test_a_pick_below_the_minimum_score_is_not_traded():
    result = await run_backtest(
        rising_bars("NVDA", "2026-10-06"),
        research_config(),
        picks=[pick("2026-10-06", score=10)],
    )
    assert result.trades == ()


async def test_a_pinned_symbol_is_replayed_alongside_the_picks():
    bars = rising_bars("SPY", "2026-10-06") + rising_bars("NVDA", "2026-10-06")
    config = research_config(symbols=("SPY",))
    result = await run_backtest(bars, config, picks=[pick("2026-10-06")])
    assert {t.symbol for t in result.trades} == {"SPY", "NVDA"}


async def test_bars_for_a_symbol_neither_pinned_nor_picked_are_an_error():
    bars = rising_bars("NVDA", "2026-10-06") + rising_bars("AMD", "2026-10-06")
    with pytest.raises(BacktestError, match="AMD"):
        await run_backtest(bars, research_config(), picks=[pick("2026-10-06")])


async def test_picks_work_with_a_configuration_that_has_no_research_table():
    config = research_config(symbols=("SPY",), research_table=None)
    bars = rising_bars("SPY", "2026-10-06") + rising_bars("NVDA", "2026-10-06")
    result = await run_backtest(bars, config, picks=[pick("2026-10-06")])
    assert {t.symbol for t in result.trades} == {"SPY", "NVDA"}


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([{"symbol": "NVDA"}], "row 1.*day"),
        ([{"day": "10/06/2026", "posture": "trade"}], "row 1.*day"),
        ([{"day": "2026-10-06", "posture": "maybe"}], "2026-10-06.*posture"),
        ([{"day": "2026-10-06", "posture": "trade", "symbol": "NVDA"}], "row 1"),
        ([posture("2026-10-06", "trade"), posture("2026-10-06", "reduced")], "row 2.*posture"),
        ([pick("2026-10-06", side="sideways")], "2026-10-06"),
        ([pick("2026-10-06", colour="red")], "colour"),
        ([pick("2026-10-06"), pick("2026-10-06")], "2026-10-06.*twice"),
        (["not a row"], "row 1"),
    ],
)
async def test_bad_pick_rows_are_refused_and_named(rows, message):
    with pytest.raises(BacktestError, match=message):
        await run_backtest(rising_bars("NVDA", "2026-10-06"), research_config(), picks=rows)


async def test_picks_given_but_empty_means_research_says_nothing_so_pinned_symbols_wait():
    config = research_config(symbols=("SPY",))
    result = await run_backtest(rising_bars("SPY", "2026-10-06"), config, picks=[])
    assert result.trades == ()
    assert result.blocked["posture"] > 0


async def test_without_picks_nothing_changes_for_a_research_only_config():
    with pytest.raises(BacktestError, match="NVDA"):
        await run_backtest(rising_bars("NVDA", "2026-10-06"), research_config())


def test_load_picks_reads_json_lines_and_skips_blank_lines(tmp_path):
    path = tmp_path / "p.jsonl"
    path.write_text('{"day": "2026-10-06", "posture": "trade"}\n\n{"day": "2026-10-06"}\n')
    assert load_picks_jsonl(path) == [
        {"day": "2026-10-06", "posture": "trade"},
        {"day": "2026-10-06"},
    ]


def test_bad_picks_lines_name_the_line(tmp_path):
    path = tmp_path / "p.jsonl"
    path.write_text('{"day": "2026-10-06", "posture": "trade"}\nnot json\n')
    with pytest.raises(BacktestError, match="line 2"):
        load_picks_jsonl(path)


def test_a_picks_line_that_is_not_an_object_names_the_line(tmp_path):
    path = tmp_path / "p.jsonl"
    path.write_text("[1, 2]\n")
    with pytest.raises(BacktestError, match="line 1"):
        load_picks_jsonl(path)
