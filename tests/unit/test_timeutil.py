from datetime import UTC, date, datetime, timedelta

from traider.timeutil import ET, ManualClock, SystemClock, trading_date


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
