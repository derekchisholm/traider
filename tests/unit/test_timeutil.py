from datetime import UTC, date, datetime, timedelta

import pytest

from traider.timeutil import (
    ET,
    ManualClock,
    SystemClock,
    trades_without_settling,
    trading_date,
    weekdays_from,
)


def test_trading_date_uses_new_york_calendar_not_utc():
    # 03:00 UTC on Oct 9 is still the evening of Oct 8 in New York.
    assert trading_date(datetime(2026, 10, 9, 3, 0, tzinfo=UTC)) == date(2026, 10, 8)


def test_trading_date_during_the_session():
    assert trading_date(datetime(2026, 10, 8, 15, 0, tzinfo=UTC)) == date(2026, 10, 8)


def test_manual_clock_advances_by_seconds():
    clock = ManualClock(datetime(2026, 10, 8, 14, 0, tzinfo=UTC))
    clock.advance(90)
    assert clock.now() == datetime(2026, 10, 8, 14, 1, 30, tzinfo=UTC)


def test_manual_clock_can_be_set_and_rejects_naive_times():
    clock = ManualClock(datetime(2026, 10, 8, 14, 0, tzinfo=UTC))
    clock.set(datetime(2026, 10, 8, 10, 0, tzinfo=ET))
    assert clock.now() == datetime(2026, 10, 8, 14, 0, tzinfo=UTC)
    try:
        clock.set(datetime(2026, 10, 8, 10, 0))
    except ValueError:
        pass
    else:
        raise AssertionError("naive datetime accepted")


def test_system_clock_returns_aware_utc_now():
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert abs(now - datetime.now(UTC)) < timedelta(seconds=5)


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 10, 12), True),  # Columbus Day: second Monday of October
        (date(2026, 10, 5), False),  # the first Monday is an ordinary day
        (date(2026, 10, 13), False),
        (date(2026, 11, 11), True),  # Veterans Day, a Wednesday
        (date(2027, 11, 11), True),  # a Thursday
        (date(2028, 11, 10), False),  # 2028-11-11 is a Saturday: banks open on the Friday
        (date(2029, 11, 12), True),  # 2029-11-11 is a Sunday: banks closed on the Monday
        (date(2029, 11, 11), False),
        (date(2026, 7, 3), False),  # markets are closed anyway; nothing to carry
    ],
)
def test_days_the_market_trades_but_nothing_settles(day, expected):
    assert trades_without_settling(day) is expected


def test_weekday_steps_skip_weekends():
    from datetime import date

    from traider.timeutil import next_weekday, weekdays_after, weekdays_between

    friday, monday = date(2026, 10, 9), date(2026, 10, 12)
    assert next_weekday(friday) == monday
    assert next_weekday(date(2026, 10, 10)) == monday  # from a Saturday
    assert weekdays_after(friday, 5) == date(2026, 10, 16)
    assert weekdays_after(friday, 0) == friday
    assert weekdays_between(friday, friday) == 0
    assert weekdays_between(friday, monday) == 1
    assert weekdays_between(friday, date(2026, 10, 23)) == 10
    assert weekdays_between(monday, friday) == -1
    assert weekdays_between(date(2026, 10, 8), friday) == 1


def test_weekdays_after_refuses_a_negative_count():
    from datetime import date

    import pytest

    from traider.timeutil import weekdays_after

    with pytest.raises(ValueError, match="negative"):
        weekdays_after(date(2026, 10, 9), -1)


def test_weekdays_from_includes_both_ends_and_skips_weekends():
    friday, tuesday = date(2026, 10, 9), date(2026, 10, 13)
    assert weekdays_from(friday, tuesday) == [friday, date(2026, 10, 12), tuesday]
    assert weekdays_from(date(2026, 10, 10), date(2026, 10, 11)) == []
    assert weekdays_from(tuesday, friday) == []
