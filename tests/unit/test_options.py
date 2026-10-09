from datetime import date
from decimal import Decimal

import pytest

from traider.options import (
    OptionContract,
    OptionQuote,
    contract_size,
    is_option_symbol,
    parse_option_symbol,
)

SPY_CALL = "SPY   261016C00500000"


def test_schwab_option_symbol_is_read_into_its_parts():
    contract = parse_option_symbol(SPY_CALL)
    assert contract == OptionContract("SPY", date(2026, 10, 16), "C", Decimal(500))


def test_fractional_strikes_and_puts_are_read():
    contract = parse_option_symbol("AAPL  240517P00192500")
    assert (contract.right, contract.strike) == ("P", Decimal("192.5"))


def test_a_contract_writes_itself_back_as_the_same_symbol():
    assert parse_option_symbol(SPY_CALL).symbol == SPY_CALL
    assert OptionContract("F", date(2027, 1, 15), "P", Decimal("12.5")).symbol == (
        "F     270115P00012500"
    )


@pytest.mark.parametrize(
    "text",
    ["SPY", "SPY261016C00500000", "SPY   261016X00500000", "SPY   261316C00500000", "", "BRK.B"],
)
def test_anything_else_is_not_an_option_symbol(text):
    assert is_option_symbol(text) is False
    with pytest.raises(ValueError, match="option symbol"):
        parse_option_symbol(text)


def test_one_contract_is_a_hundred_shares_and_a_share_is_one():
    assert contract_size(SPY_CALL) == 100
    assert contract_size("SPY") == 1


def test_days_to_expiry_counts_calendar_days():
    contract = parse_option_symbol(SPY_CALL)
    assert contract.days_to_expiry(date(2026, 10, 9)) == 7
    assert contract.days_to_expiry(date(2026, 10, 16)) == 0
    assert contract.days_to_expiry(date(2026, 10, 17)) == -1


def test_an_option_quote_knows_its_contract():
    quote = OptionQuote(SPY_CALL, Decimal("4.10"), Decimal("4.20"), Decimal("0.45"), 7)
    assert quote.contract.strike == Decimal(500)
    assert quote.mid == Decimal("4.15")
