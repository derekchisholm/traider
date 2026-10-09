from datetime import timedelta

from tests.unit.helpers import T0, make_bar, make_quote
from traider.marketdata import MarketData


def test_unknown_symbol_has_no_quote():
    assert MarketData().quote("SPY") is None


def test_latest_quote_replaces_the_previous_one():
    market = MarketData()
    market.on_quote(make_quote(bid="100.00"))
    market.on_quote(make_quote(bid="100.50", ask="100.52", at=T0 + timedelta(seconds=1)))
    assert str(market.quote("SPY").bid) == "100.50"


def test_quote_proves_the_feed_is_alive():
    market = MarketData()
    assert market.feed_alive_at is None
    market.on_quote(make_quote(at=T0))
    assert market.feed_alive_at == T0


def test_heartbeat_proves_the_feed_is_alive_without_a_quote():
    market = MarketData()
    market.mark_alive(T0)
    assert market.feed_alive_at == T0


def test_feed_liveness_never_moves_backwards():
    market = MarketData()
    market.mark_alive(T0)
    market.mark_alive(T0 - timedelta(seconds=30))
    assert market.feed_alive_at == T0


def test_symbols_with_new_quotes_are_reported_once():
    market = MarketData()
    market.on_quote(make_quote("SPY"))
    market.on_quote(make_quote("QQQ"))
    market.on_quote(make_quote("SPY", at=T0 + timedelta(seconds=1)))
    assert market.drain_dirty() == ["SPY", "QQQ"]
    assert market.drain_dirty() == []


def test_bars_are_handed_over_in_arrival_order_then_cleared():
    market = MarketData()
    market.on_bar(make_bar(minute=0))
    market.on_bar(make_bar(minute=1))
    assert [bar.start for bar, _ in market.drain_bars()] == [T0, T0 + timedelta(minutes=1)]
    assert market.drain_bars() == []


def test_a_bar_already_seen_is_ignored():
    # The stream and the REST fallback can both deliver the same minute.
    market = MarketData()
    market.on_bar(make_bar(minute=1, close="100"))
    market.on_bar(make_bar(minute=1, close="999"))
    market.on_bar(make_bar(minute=0, close="999"))
    (only,) = market.drain_bars()
    assert str(only[0].close) == "100"


def test_bar_deduplication_is_per_symbol():
    market = MarketData()
    market.on_bar(make_bar("SPY", minute=1))
    market.on_bar(make_bar("QQQ", minute=1))
    assert len(market.drain_bars()) == 2


def test_warmup_flag_travels_with_the_bar():
    market = MarketData()
    market.on_bar(make_bar(minute=0), warmup=True)
    market.on_bar(make_bar(minute=1))
    assert [warm for _, warm in market.drain_bars()] == [True, False]


def test_waker_is_called_for_quotes_and_bars():
    market = MarketData()
    calls = []
    market.set_waker(lambda: calls.append(1))
    market.on_quote(make_quote())
    market.on_bar(make_bar())
    assert len(calls) == 2
