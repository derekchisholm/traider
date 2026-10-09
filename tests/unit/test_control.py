from datetime import UTC, datetime, timedelta

import pytest

from traider.control import (
    ControlMode,
    ControlState,
    StaticControl,
    effective_permissions,
    parse_control,
)

T0 = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("halt", ControlMode.HALT),
        ("close_only", ControlMode.CLOSE_ONLY),
        ("paper", ControlMode.PAPER),
        ("live", ControlMode.LIVE),
        ("  LIVE \n", ControlMode.LIVE),
    ],
)
def test_parse_control_accepts_known_values(raw, expected):
    assert parse_control(raw) is expected


@pytest.mark.parametrize("raw", [None, "", "run", "on", "true", "liv"])
def test_parse_control_treats_anything_unknown_as_halt(raw):
    assert parse_control(raw) is ControlMode.HALT


def test_paper_deploy_with_paper_control_trades_on_paper():
    p = effective_permissions("paper", ControlMode.PAPER)
    assert (p.allow_entries, p.allow_exits, p.live) == (True, True, False)


def test_live_deploy_with_live_control_trades_live():
    p = effective_permissions("live", ControlMode.LIVE)
    assert (p.allow_entries, p.allow_exits, p.live) == (True, True, True)


def test_live_deploy_stays_halted_until_control_says_live():
    p = effective_permissions("live", ControlMode.PAPER)
    assert (p.allow_entries, p.allow_exits, p.live) == (False, False, False)
    assert "live" in p.reason and "paper" in p.reason


def test_live_control_cannot_arm_a_paper_deploy():
    p = effective_permissions("paper", ControlMode.LIVE)
    assert (p.allow_entries, p.allow_exits, p.live) == (False, False, False)


@pytest.mark.parametrize(("mode", "live"), [("paper", False), ("live", True)])
def test_close_only_allows_exits_but_no_entries(mode, live):
    p = effective_permissions(mode, ControlMode.CLOSE_ONLY)
    assert (p.allow_entries, p.allow_exits, p.live) == (False, True, live)


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_halt_blocks_everything(mode):
    p = effective_permissions(mode, ControlMode.HALT)
    assert (p.allow_entries, p.allow_exits) == (False, False)


class FlakySource:
    def __init__(self, values):
        self.values = list(values)

    async def read(self):
        v = self.values.pop(0)
        if isinstance(v, Exception):
            raise v
        return v


async def test_control_state_is_halt_before_the_first_successful_read():
    state = ControlState(StaticControl("live"))
    assert state.mode(T0) is ControlMode.HALT


async def test_control_state_reports_the_value_it_read():
    state = ControlState(StaticControl("paper"))
    await state.refresh(T0)
    assert state.mode(T0) is ControlMode.PAPER


async def test_control_state_keeps_last_value_through_a_brief_read_failure():
    state = ControlState(FlakySource(["live", RuntimeError("ssm down")]), max_stale_s=60)
    await state.refresh(T0)
    await state.refresh(T0 + timedelta(seconds=10))
    assert state.mode(T0 + timedelta(seconds=30)) is ControlMode.LIVE
    assert "ssm down" in (state.last_error or "")


async def test_control_state_fails_closed_when_reads_keep_failing():
    state = ControlState(FlakySource(["live", RuntimeError("ssm down")]), max_stale_s=60)
    await state.refresh(T0)
    await state.refresh(T0 + timedelta(seconds=10))
    assert state.mode(T0 + timedelta(seconds=61)) is ControlMode.HALT


async def test_control_state_picks_up_a_changed_value():
    state = ControlState(FlakySource(["live", "halt"]))
    await state.refresh(T0)
    await state.refresh(T0 + timedelta(seconds=10))
    assert state.mode(T0 + timedelta(seconds=10)) is ControlMode.HALT
