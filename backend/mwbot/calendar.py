"""Exchange calendar + configurable session schedule (America/Chicago display).

Uses ``exchange_calendars`` (XNYS) so weekends, holidays, DST changes and early closes
(day after Thanksgiving, Christmas Eve, ...) come from a real calendar rather than assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from mwbot.config import DISPLAY_TZ, EXCHANGE_CALENDAR, ScheduleConfig
from mwbot.domain import SessionInfo

CHICAGO = ZoneInfo(DISPLAY_TZ)
PREMARKET_MINUTES_BEFORE_OPEN = 330  # 04:00 ET for a 09:30 ET open


@lru_cache(maxsize=1)
def _calendar() -> xcals.ExchangeCalendar:
    # exchange_calendars limits the default window to ~1 year ahead; widen deliberately.
    start = pd.Timestamp("2015-01-02")
    end = pd.Timestamp.now().normalize() + pd.DateOffset(years=2)
    return xcals.get_calendar(EXCHANGE_CALENDAR, start=start, end=end)


def is_trading_day(d: date) -> bool:
    cal = _calendar()
    ts = pd.Timestamp(d)
    if ts < cal.first_session or ts > cal.last_session:
        raise ValueError(f"{d} is outside the calendar range {cal.first_session.date()}..{cal.last_session.date()}")
    return bool(cal.is_session(ts))


def session_for(d: date) -> SessionInfo | None:
    """Return the session for date ``d`` or ``None`` for weekends/holidays."""
    if not is_trading_day(d):
        return None
    cal = _calendar()
    ts = pd.Timestamp(d)
    open_utc = cal.session_open(ts).to_pydatetime().astimezone(UTC)
    close_utc = cal.session_close(ts).to_pydatetime().astimezone(UTC)
    regular_minutes = (close_utc - open_utc).total_seconds() / 60
    return SessionInfo(
        date=d.isoformat(),
        open_utc=open_utc,
        close_utc=close_utc,
        premarket_open_utc=open_utc - timedelta(minutes=PREMARKET_MINUTES_BEFORE_OPEN),
        is_early_close=regular_minutes < 389,
    )


def next_trading_day(d: date, *, include_today: bool = False) -> date:
    cur = d if include_today else d + timedelta(days=1)
    for _ in range(20):
        if is_trading_day(cur):
            return cur
        cur += timedelta(days=1)
    raise RuntimeError("no trading day found within 20 days")  # pragma: no cover


def previous_trading_day(d: date) -> date:
    cur = d - timedelta(days=1)
    for _ in range(20):
        if is_trading_day(cur):
            return cur
        cur -= timedelta(days=1)
    raise RuntimeError("no trading day found within 20 days")  # pragma: no cover


def trading_days(start: date, end: date) -> list[date]:
    cal = _calendar()
    sessions = cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    return [s.date() for s in sessions]


def to_chicago(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("naive datetime")
    return ts.astimezone(CHICAGO)


def chicago_to_utc(d: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, time(h, m), tzinfo=CHICAGO).astimezone(UTC)


class Phase(StrEnum):
    CLOSED = "closed"
    PREMARKET_SCAN = "premarket_scan"
    OBSERVING = "observing_open"
    TRADING = "trading"
    NO_NEW_ENTRIES = "no_new_entries"
    LIQUIDATING = "liquidating"
    FINAL_CHECK = "final_check"
    POST_CLOSE = "post_close"


@dataclass(slots=True)
class SessionSchedule:
    """Resolved schedule for one trading date (all UTC). Early closes pull late milestones earlier."""

    session: SessionInfo
    scanner_start: datetime
    entries_start: datetime
    last_entry: datetime
    liquidation_start: datetime
    final_check: datetime
    report_at: datetime

    @property
    def open(self) -> datetime:
        return self.session.open_utc

    @property
    def close(self) -> datetime:
        return self.session.close_utc

    def phase_at(self, ts: datetime) -> Phase:
        if ts < self.scanner_start or ts >= self.close + timedelta(days=1):
            return Phase.CLOSED
        if ts < self.open:
            return Phase.PREMARKET_SCAN
        if ts < self.entries_start:
            return Phase.OBSERVING
        if ts < self.last_entry:
            return Phase.TRADING
        if ts < self.liquidation_start:
            return Phase.NO_NEW_ENTRIES
        if ts < self.final_check:
            return Phase.LIQUIDATING
        if ts < self.close:
            return Phase.FINAL_CHECK
        return Phase.POST_CLOSE

    def to_dict(self) -> dict[str, str | bool]:
        def fmt(ts: datetime) -> str:
            return to_chicago(ts).strftime("%H:%M")

        return {
            "date": self.session.date,
            "early_close": self.session.is_early_close,
            "scanner_start": fmt(self.scanner_start),
            "open": fmt(self.open),
            "entries_start": fmt(self.entries_start),
            "last_entry": fmt(self.last_entry),
            "liquidation_start": fmt(self.liquidation_start),
            "final_check": fmt(self.final_check),
            "close": fmt(self.close),
            "timezone": DISPLAY_TZ,
        }


def build_schedule(d: date, cfg: ScheduleConfig) -> SessionSchedule | None:
    sess = session_for(d)
    if sess is None:
        return None
    close = sess.close_utc
    final_check = close - timedelta(minutes=cfg.final_check_minutes_before_close)
    # Early close: configured wall-clock milestones may land after the close; clamp them.
    liquidation = min(chicago_to_utc(d, cfg.liquidation_start), close - timedelta(minutes=cfg.final_check_minutes_before_close + 15))
    last_entry = min(chicago_to_utc(d, cfg.last_entry), liquidation)
    entries_start = sess.open_utc + timedelta(minutes=cfg.observe_minutes)
    entries_start = min(entries_start, last_entry)
    return SessionSchedule(
        session=sess,
        scanner_start=min(chicago_to_utc(d, cfg.scanner_start), sess.open_utc),
        entries_start=entries_start,
        last_entry=last_entry,
        liquidation_start=liquidation,
        final_check=final_check,
        report_at=close + timedelta(minutes=cfg.report_delay_minutes),
    )
