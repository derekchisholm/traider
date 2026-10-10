"""Which symbols the bot watches: held and busy first, then pinned, then the best picks."""

from traider.universe import compute_universe, root_symbol


def test_root_symbol_of_an_option_is_its_underlying():
    assert root_symbol("SPY   261016C00500000") == "SPY"
    assert root_symbol("NVDA") == "NVDA"


def test_required_then_pinned_then_picks_by_score_up_to_the_cap():
    universe = compute_universe(
        required=["ZZZ"],
        pinned=["SPY"],
        picks=[("AMD", 70), ("NVDA", 90), ("TSLA", 80)],
        cap=4,
    )
    assert universe == ("ZZZ", "SPY", "NVDA", "TSLA")


def test_held_symbols_are_never_dropped_even_over_the_cap():
    universe = compute_universe(required=["A", "B", "C"], pinned=["D"], picks=[("E", 99)], cap=2)
    assert universe == ("A", "B", "C", "D")


def test_no_duplicates_and_ties_break_by_symbol():
    universe = compute_universe(
        required=["SPY"], pinned=["SPY", "QQQ"], picks=[("QQQ", 90), ("BB", 50), ("AA", 50)], cap=10
    )
    assert universe == ("SPY", "QQQ", "AA", "BB")
