from datetime import UTC, datetime
from decimal import Decimal

from traider.models import AccountSnapshot, OrderStatus, Position, Quote

NOW = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)


def quote(bid: str, ask: str) -> Quote:
    return Quote("SPY", Decimal(bid), Decimal(ask), Decimal(bid), ts=NOW, received_at=NOW)


def test_quote_mid_is_average_of_bid_and_ask():
    assert quote("100.00", "100.10").mid == Decimal("100.05")


def test_quote_spread_in_basis_points_of_mid():
    assert quote("99.95", "100.05").spread_bps == Decimal("10")


def test_quote_spread_is_none_when_a_side_is_missing():
    assert quote("0", "100.05").spread_bps is None


def test_only_final_statuses_are_terminal():
    terminal = {s for s in OrderStatus if s.is_terminal}
    assert terminal == {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }


def test_account_position_defaults_to_zero_for_unheld_symbols():
    snap = AccountSnapshot(
        equity=Decimal("1000"),
        cash_available=Decimal("1000"),
        positions={"SPY": Position("SPY", 3, Decimal("500"))},
        as_of=NOW,
    )
    assert snap.position("SPY") == 3
    assert snap.position("QQQ") == 0
