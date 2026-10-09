from datetime import UTC, date, datetime, timedelta

import pytest

from mwbot.calendar import (
    Phase,
    build_schedule,
    chicago_to_utc,
    is_trading_day,
    next_trading_day,
    previous_trading_day,
    session_for,
    to_chicago,
    trading_days,
)
from mwbot.config import ScheduleConfig


def test_weekend_and_holidays_are_closed():
    assert not is_trading_day(date(2026, 10, 10))  # Saturday
    assert not is_trading_day(date(2026, 11, 26))  # Thanksgiving
    assert not is_trading_day(date(2026, 12, 25))  # Christmas
    assert not is_trading_day(date(2026, 7, 3))  # Independence Day observed (Jul 4 is Saturday)
    assert is_trading_day(date(2026, 10, 9))
    assert session_for(date(2026, 11, 26)) is None


def test_regular_session_times_in_chicago():
    s = session_for(date(2026, 10, 8))
    assert to_chicago(s.open_utc).strftime("%H:%M") == "08:30"
    assert to_chicago(s.close_utc).strftime("%H:%M") == "15:00"
    assert not s.is_early_close
    assert to_chicago(s.premarket_open_utc).strftime("%H:%M") == "03:00"


def test_early_close_day_after_thanksgiving():
    s = session_for(date(2026, 11, 27))
    assert s.is_early_close
    assert to_chicago(s.close_utc).strftime("%H:%M") == "12:00"  # 1 pm ET


def test_dst_transition_moves_utc_open():
    before = session_for(date(2026, 3, 6))  # EST: 09:30 ET = 14:30Z
    after = session_for(date(2026, 3, 9))  # EDT after Mar 8 change: 09:30 ET = 13:30Z
    assert before.open_utc.hour == 14 and after.open_utc.hour == 13
    # Chicago wall-clock open is 08:30 on both sides of the change
    assert to_chicago(before.open_utc).strftime("%H:%M") == "08:30"
    assert to_chicago(after.open_utc).strftime("%H:%M") == "08:30"


def test_chicago_wallclock_to_utc_respects_dst():
    assert chicago_to_utc(date(2026, 3, 6), "08:00") == datetime(2026, 3, 6, 14, 0, tzinfo=UTC)
    assert chicago_to_utc(date(2026, 3, 10), "08:00") == datetime(2026, 3, 10, 13, 0, tzinfo=UTC)


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        to_chicago(datetime(2026, 1, 1))


def test_schedule_phases_in_order():
    sch = build_schedule(date(2026, 10, 8), ScheduleConfig())
    assert sch is not None
    open_ = sch.open
    cases = [
        (sch.scanner_start - timedelta(minutes=1), Phase.CLOSED),
        (sch.scanner_start, Phase.PREMARKET_SCAN),
        (open_ + timedelta(minutes=3), Phase.OBSERVING),
        (open_ + timedelta(minutes=10), Phase.TRADING),
        (sch.last_entry, Phase.NO_NEW_ENTRIES),
        (sch.liquidation_start, Phase.LIQUIDATING),
        (sch.final_check, Phase.FINAL_CHECK),
        (sch.close, Phase.POST_CLOSE),
    ]
    for ts, expected in cases:
        assert sch.phase_at(ts) == expected, (ts, expected)
    d = sch.to_dict()
    assert d["scanner_start"] == "08:00" and d["liquidation_start"] == "14:45" and d["close"] == "15:00"


def test_schedule_is_configurable():
    cfg = ScheduleConfig(scanner_start="07:30", observe_minutes=20, last_entry="13:00", liquidation_start="14:30")
    sch = build_schedule(date(2026, 10, 8), cfg)
    assert to_chicago(sch.scanner_start).strftime("%H:%M") == "07:30"
    assert to_chicago(sch.entries_start).strftime("%H:%M") == "08:50"
    assert to_chicago(sch.last_entry).strftime("%H:%M") == "13:00"


def test_early_close_clamps_late_milestones():
    sch = build_schedule(date(2026, 11, 27), ScheduleConfig())  # closes 12:00 CT, config liquidates at 14:45
    assert sch.liquidation_start < sch.close
    assert sch.last_entry <= sch.liquidation_start
    assert sch.liquidation_start <= sch.final_check < sch.close


def test_no_schedule_on_holiday():
    assert build_schedule(date(2026, 11, 26), ScheduleConfig()) is None


def test_bad_time_format_rejected():
    with pytest.raises(ValueError):
        ScheduleConfig(scanner_start="8am")


def test_trading_day_navigation_skips_closed_days():
    assert next_trading_day(date(2026, 11, 25)) == date(2026, 11, 27)
    assert previous_trading_day(date(2026, 11, 27)) == date(2026, 11, 25)
    assert next_trading_day(date(2026, 10, 9)) == date(2026, 10, 12)  # Fri -> Mon
    days = trading_days(date(2026, 11, 23), date(2026, 11, 30))
    assert date(2026, 11, 26) not in days and date(2026, 11, 28) not in days and len(days) == 5
