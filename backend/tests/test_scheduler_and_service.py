"""Wall-clock scheduler loop (fake clock), APScheduler wiring and service-level mirror mode."""

from datetime import UTC, datetime, timedelta

import pytest

from mwbot.calendar import build_schedule
from mwbot.config import ScheduleConfig
from mwbot.data.synthetic import SYMBOL_POOL
from mwbot.service import AppService

from .conftest import TEST_DAY


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture()
def svc(tmp_env):
    s = AppService(tmp_env)
    yield s
    s.close()


def patch_clock(monkeypatch, start):
    clock = FakeClock(start)
    monkeypatch.setattr("mwbot.service.utcnow", clock)
    return clock


def test_live_loop_ignores_weekends_holidays_and_pre_scanner_hours(svc, monkeypatch):
    for when in (
        datetime(2026, 10, 10, 15, 0, tzinfo=UTC),  # Saturday
        datetime(2026, 11, 26, 15, 0, tzinfo=UTC),  # Thanksgiving
        datetime(2026, 12, 25, 15, 0, tzinfo=UTC),  # Christmas
        datetime(2026, 10, 8, 9, 0, tzinfo=UTC),
    ):  # trading day, 04:00 CT: before scanner start
        patch_clock(monkeypatch, when)
        svc._live_tick()
        assert svc.runner is None and svc.broker.orders == {}, when


def test_live_loop_runs_a_whole_day_on_the_wall_clock(svc, monkeypatch):
    sched = build_schedule(TEST_DAY, svc.settings.schedule)
    clock = patch_clock(monkeypatch, sched.scanner_start)
    svc.settings.auto_schedule = True
    phases = set()
    while clock.now <= sched.report_at:
        svc._live_tick()
        if svc.runner:
            phases.add(str(svc.bot.phase))
        clock.now += timedelta(minutes=1)
    assert {"premarket_scan", "observing_open", "trading", "liquidating", "final_check"} <= phases
    assert svc.session_status == "finished"
    rep = svc.store.reports()[-1]
    assert rep["date"] == "2026-10-08" and rep["liquidation_complete"] in (True, False)
    assert not svc.broker.positions or rep["unresolved_positions"]  # never silently left open
    assert svc.store.equity_history()
    # extra ticks after the report window are no-ops
    before = len(svc.broker.orders)
    clock.now += timedelta(hours=3)
    svc._live_tick()
    assert len(svc.broker.orders) == before


def test_scheduler_job_follows_the_auto_schedule_setting(svc):
    svc.start_scheduler()
    assert svc.scheduler is not None and svc.scheduler.get_job("live_tick") is None
    svc.update_settings({"auto_schedule": True})
    assert svc.scheduler.get_job("live_tick") is not None and svc.status()["scheduler_enabled"] is True
    svc.update_settings({"auto_schedule": False})
    assert svc.scheduler.get_job("live_tick") is None


def test_schedule_times_are_user_configurable_and_validated(svc):
    svc.update_settings(
        {"schedule": {"scanner_start": "07:30", "liquidation_start": "14:30", "last_entry": "14:00"}}
    )
    s = build_schedule(TEST_DAY, svc.settings.schedule)
    assert s.to_dict()["scanner_start"] == "07:30" and s.to_dict()["liquidation_start"] == "14:30"
    assert isinstance(svc.settings.schedule, ScheduleConfig)


def test_scanner_and_risk_edits_are_allowed_live_but_capital_and_costs_are_not(svc):
    svc.start_session(speed=1)
    try:
        svc.update_settings({"scanner": {"filters": {"min_pct_change": 9.0}}, "risk": {"max_positions": 2}})
        assert (
            svc.settings.scanner.filters.min_pct_change == 9.0
            and svc.bot.scanner.config.filters.min_pct_change == 9.0
        )
        assert svc.bot.risk.cfg.max_positions == 2
        from mwbot.service import ServiceError

        with pytest.raises(ServiceError, match="starting_capital"):
            svc.update_settings({"risk": {"starting_capital": 5}})
        with pytest.raises(ServiceError, match="costs"):
            svc.update_settings({"costs": {"slippage_bps": 99}})
        with pytest.raises(ServiceError, match="data_mode"):
            svc.update_settings({"data_mode": "historical"})
    finally:
        svc.stop_session()


@pytest.mark.browser
def test_service_mirrors_demo_orders_to_the_mock_venue(tmp_env, mock_site, monkeypatch):
    url, ex = mock_site
    ex.starting_cash = ex.cash = 1e9
    ex.set_prices({sym: 10.0 for sym, _ in SYMBOL_POOL})
    monkeypatch.setenv("MWBOT_EXECUTION_MODE", "mock_browser")
    monkeypatch.setenv("MWBOT_MOCK_SITE_URL", url)
    env = type(tmp_env)(_env_file=None)
    s = AppService(env)
    try:
        assert (
            s.mirror is not None
            and s.status()["execution_mode"] == "mock_browser"
            and s.status()["real_money"] is False
        )
        s.start_session(instant=True)
        s.wait_for_session(300)
        n_orders = len([o for o in s.broker.orders.values() if o.meta.get("external_id")])
        assert n_orders >= 2 and len(ex.orders) == n_orders
        assert s.last_report["reconciliation"]["ok"] in (True, False)
    finally:
        s.close()


def test_mock_browser_mode_degrades_cleanly_if_the_site_is_down(tmp_env, monkeypatch):
    monkeypatch.setenv("MWBOT_EXECUTION_MODE", "mock_browser")
    monkeypatch.setenv("MWBOT_MOCK_SITE_URL", "http://127.0.0.1:9")  # nothing listening
    env = type(tmp_env)(_env_file=None)
    s = AppService(env)
    try:
        assert s.mirror is None and "mock browser adapter unavailable" in s.status()["provider_error"]
    finally:
        s.close()
