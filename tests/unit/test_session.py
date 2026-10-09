from datetime import UTC, date, datetime, timedelta

from traider.session import Session, SessionTracker, StaticSessionProvider

OPEN = datetime(2026, 10, 8, 13, 30, tzinfo=UTC)  # 09:30 New York (EDT)
CLOSE = datetime(2026, 10, 8, 20, 0, tzinfo=UTC)  # 16:00 New York
THURSDAY = Session(date(2026, 10, 8), OPEN, CLOSE)


def test_session_is_closed_before_the_open():
    view = THURSDAY.view(OPEN - timedelta(seconds=1))
    assert view.is_open is False


def test_session_opens_exactly_at_the_open():
    view = THURSDAY.view(OPEN)
    assert (view.is_open, view.minutes_since_open, view.minutes_to_close) == (True, 0, 390)


def test_session_reports_minutes_either_side():
    view = THURSDAY.view(OPEN + timedelta(minutes=90))
    assert (view.minutes_since_open, view.minutes_to_close) == (90, 300)


def test_session_is_closed_from_the_closing_time_onwards():
    assert THURSDAY.view(CLOSE).is_open is False


def test_a_holiday_has_no_session():
    assert Session(date(2026, 12, 25), None, None).view(OPEN).is_open is False


async def test_static_calendar_weekday_hours_in_summer_time():
    session = await StaticSessionProvider().session_for(date(2026, 10, 8))
    assert (session.open, session.close) == (OPEN, CLOSE)


async def test_static_calendar_weekday_hours_in_winter_time():
    session = await StaticSessionProvider().session_for(date(2026, 12, 3))
    assert session.open == datetime(2026, 12, 3, 14, 30, tzinfo=UTC)
    assert session.close == datetime(2026, 12, 3, 21, 0, tzinfo=UTC)


async def test_static_calendar_is_closed_at_weekends():
    session = await StaticSessionProvider().session_for(date(2026, 10, 10))
    assert session.open is None


class ScriptedProvider:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def session_for(self, day):
        self.calls.append(day)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


MID = OPEN + timedelta(hours=1)


async def test_tracker_treats_an_unknown_session_as_closed():
    tracker = SessionTracker(ScriptedProvider([]))
    assert tracker.view(MID).is_open is False


async def test_tracker_reports_the_session_once_known():
    tracker = SessionTracker(ScriptedProvider([THURSDAY]))
    await tracker.refresh(MID)
    assert tracker.view(MID).is_open is True


async def test_tracker_fetches_a_known_session_only_once():
    provider = ScriptedProvider([THURSDAY])
    tracker = SessionTracker(provider)
    await tracker.refresh(MID)
    await tracker.refresh(MID + timedelta(minutes=5))
    assert len(provider.calls) == 1


async def test_tracker_stays_closed_when_the_calendar_lookup_fails():
    tracker = SessionTracker(ScriptedProvider([RuntimeError("503")]))
    await tracker.refresh(MID)
    assert tracker.view(MID).is_open is False


async def test_tracker_stays_closed_when_the_calendar_has_no_answer():
    tracker = SessionTracker(ScriptedProvider([None]))
    await tracker.refresh(MID)
    assert tracker.view(MID).is_open is False


async def test_tracker_retries_a_failed_lookup_after_the_retry_interval():
    provider = ScriptedProvider([RuntimeError("503"), THURSDAY])
    tracker = SessionTracker(provider, retry_s=60)
    await tracker.refresh(MID)
    await tracker.refresh(MID + timedelta(seconds=30))  # too soon, no call
    assert len(provider.calls) == 1
    await tracker.refresh(MID + timedelta(seconds=61))
    assert tracker.view(MID + timedelta(seconds=61)).is_open is True


async def test_tracker_looks_up_the_next_day_after_midnight_new_york():
    friday = Session(date(2026, 10, 9), OPEN + timedelta(days=1), CLOSE + timedelta(days=1))
    provider = ScriptedProvider([THURSDAY, friday])
    tracker = SessionTracker(provider)
    await tracker.refresh(MID)
    next_day = MID + timedelta(days=1)
    await tracker.refresh(next_day)
    assert provider.calls == [date(2026, 10, 8), date(2026, 10, 9)]
    assert tracker.view(next_day).is_open is True


async def test_tracker_does_not_apply_yesterdays_hours_to_today():
    tracker = SessionTracker(ScriptedProvider([THURSDAY, RuntimeError("503")]))
    await tracker.refresh(MID)
    next_day = MID + timedelta(days=1)
    await tracker.refresh(next_day)
    assert tracker.view(next_day).is_open is False
