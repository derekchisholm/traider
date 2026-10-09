"""Small builders shared by the unit tests."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from traider.models import Bar, Quote

T0 = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)  # Thursday, 11:00 in New York


def make_quote(symbol="SPY", bid="100.00", ask="100.02", *, at=T0, delayed=False) -> Quote:
    return Quote(
        symbol, Decimal(bid), Decimal(ask), Decimal(bid), ts=at, received_at=at, delayed=delayed
    )


def make_bar(symbol="SPY", close="100", *, minute=0, start=T0, volume=1000) -> Bar:
    price = Decimal(str(close))
    return Bar(symbol, start + timedelta(minutes=minute), price, price, price, price, volume)
