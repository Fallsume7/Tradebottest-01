"""Session runner: drives ``TradingBot.tick`` minute by minute through one trading day.

Used for (a) the dashboard demo (throttled to look like a live stream), (b) instant full-day
runs, (c) every backtest day - so backtests exercise exactly the code path the live loop uses.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from mwbot.bot.engine import TickReport, TradingBot
from mwbot.calendar import SessionSchedule, build_schedule
from mwbot.data.base import MarketDataSource

ONE_MIN = timedelta(minutes=1)


class SessionRunner:
    def __init__(self, bot: TradingBot, source: MarketDataSource, schedule: SessionSchedule) -> None:
        self.bot = bot
        self.source = source
        self.schedule = schedule
        self.now = schedule.scanner_start
        self.finished = False
        self.report: dict[str, Any] | None = None
        self.bot.begin_session(schedule)

    @classmethod
    def for_date(cls, bot: TradingBot, source: MarketDataSource, d: date) -> SessionRunner:
        sched = build_schedule(d, bot.settings.schedule)
        if sched is None:
            raise ValueError(f"{d} is not a trading day")
        return cls(bot, source, sched)

    @property
    def progress(self) -> float:
        total = (self.schedule.close - self.schedule.scanner_start).total_seconds()
        return min(1.0, max(0.0, (self.now - self.schedule.scanner_start).total_seconds() / total))

    def step(self) -> TickReport | None:
        """Advance one minute. Returns None once the session has been finalised."""
        if self.finished:
            return None
        rep = self.bot.tick(self.source, self.schedule, self.now)
        if self.now >= self.schedule.close:
            self.report = self.bot.finish_session(self.schedule, self.now)
            self.finished = True
        else:
            self.now = min(self.now + ONE_MIN, self.schedule.close)
        return rep

    def run(
        self,
        *,
        seconds_per_step: float = 0.0,
        stop: threading.Event | None = None,
        on_step: Callable[[TickReport], None] | None = None,
    ) -> dict[str, Any] | None:
        while not self.finished:
            if stop is not None and stop.is_set():
                return None
            rep = self.step()
            if rep is not None and on_step:
                on_step(rep)
            if seconds_per_step > 0:
                if stop is not None:
                    if stop.wait(seconds_per_step):
                        return None
                else:
                    time.sleep(seconds_per_step)
        return self.report
