"""Whole-session behaviour of the bot on scripted price paths."""

from datetime import timedelta

import pytest

from mwbot.bot.engine import TradingBot
from mwbot.bot.runner import SessionRunner
from mwbot.calendar import Phase, build_schedule
from mwbot.config import AppSettings, ScheduleConfig, StrategySelection
from mwbot.data.base import DataUnavailableError, MarketDataSource
from mwbot.domain import FeedInfo, FeedKind, OrderStatus, Side
from mwbot.execution.base import (
    ExecutionAdapter,
    ExecutionError,
    ExternalOrder,
    Holding,
    PreparedOrder,
    SessionStatus,
)
from mwbot.trading.broker import PaperBroker
from mwbot.trading.risk import BotState

from .conftest import TEST_DAY, at, make_source, meta, relaxed_settings
from .scenarios import runner_day

NO_EXITS = {
    "take_profit_pct": None,
    "trailing_stop_pct": None,
    "max_hold_minutes": None,
    "stop_loss_pct": 20.0,
}


def settings_for(strategy="opening_momentum", params=None, **risk) -> AppSettings:
    s = relaxed_settings(**risk)
    s.strategies = StrategySelection(enabled=[strategy], params={strategy: params or {}})
    return s


def run(
    settings, bars_by_symbol, *, hook=None, halts=None, source_wrapper=None, mirror=None, prev_close=10.0
):
    from mwbot.calendar import session_for

    sess = session_for(TEST_DAY)
    src = make_source(
        sess, {s: (meta(s, prev_close=prev_close), b) for s, b in bars_by_symbol.items()}, halts=halts
    )
    if source_wrapper:
        src = source_wrapper(src)
    broker = PaperBroker(settings.risk.starting_capital, settings.costs)
    bot = TradingBot(settings, broker, mirror=mirror)
    bot.start()
    sched = build_schedule(TEST_DAY, settings.schedule)
    runner = SessionRunner(bot, src, sched)
    while not runner.finished:
        if hook:
            hook(runner, bot)
        runner.step()
    return bot, broker, runner


def bought(broker):
    return [o for o in broker.orders.values() if o.side == Side.BUY and o.status != OrderStatus.REJECTED]


# ------------------------------------------------------------------ entries and exits
def test_enters_after_observation_period_and_never_on_signal_bar():
    bot, broker, _ = run(settings_for(params=NO_EXITS), {"TST": strong_runner_with_tail()})
    (entry,) = bought(broker)
    sched = build_schedule(TEST_DAY, ScheduleConfig())
    assert entry.created_at >= sched.entries_start
    assert entry.history[-1][0] > entry.created_at  # filled strictly after the decision time
    assert broker.fills[0].timestamp >= entry.created_at + timedelta(minutes=1)
    assert "Open drive" in entry.reason and entry.strategy == "opening_momentum"


def strong_runner_with_tail():
    return runner_day_flat()


def runner_day_flat():
    from mwbot.calendar import session_for

    return runner_day(session_for(TEST_DAY), [(20, 11.7, 28_000), (30, 11.85, 14_000)])


def test_stop_loss_exit_after_collapse():
    from mwbot.calendar import session_for

    sess = session_for(TEST_DAY)
    day = runner_day(sess, [(20, 11.7, 28_000), (15, 11.8, 14_000), (1, 10.4, 90_000), (37, 10.4, 5_000)])
    bot, broker, _ = run(settings_for(params={"trailing_stop_pct": None}), {"TST": day})
    (t,) = broker.trades
    assert "Stop loss" in t.exit_reason and t.net_pnl < 0
    assert t.exit_price < t.entry_price and t.exit_time > t.entry_time


def test_take_profit_exit():
    from mwbot.calendar import session_for

    sess = session_for(TEST_DAY)
    day = runner_day(sess, [(20, 11.7, 28_000), (25, 13.2, 30_000), (60, 13.2, 5_000)])
    bot, broker, _ = run(
        settings_for(params={"take_profit_pct": 8.0, "trailing_stop_pct": None}), {"TST": day}
    )
    assert any("Profit target" in t.exit_reason for t in broker.trades)
    assert all(t.net_pnl > 0 for t in broker.trades if "Profit target" in t.exit_reason)


def test_end_of_day_liquidation_closes_before_close():
    bot, broker, runner = run(settings_for(params=NO_EXITS), {"TST": runner_day_flat()})
    (t,) = broker.trades
    sched = runner.schedule
    assert t.exit_reason.startswith("End-of-day liquidation")
    assert sched.liquidation_start <= t.exit_time < sched.close
    assert not broker.positions and runner.report["liquidation_complete"] is True
    assert runner.report["unresolved_positions"] == []


def test_unresolved_liquidation_is_reported_honestly():
    sched = build_schedule(TEST_DAY, ScheduleConfig())
    halts = {"TST": [(sched.liquidation_start - timedelta(minutes=2), sched.close + timedelta(hours=2))]}
    bot, broker, runner = run(settings_for(params=NO_EXITS), {"TST": runner_day_flat()}, halts=halts)
    rep = runner.report
    assert rep["liquidation_complete"] is False
    assert [u["symbol"] for u in rep["unresolved_positions"]] == ["TST"]
    codes = {a["code"] for a in bot.alerts}
    assert {"unresolved_positions", "overnight_risk"} <= codes
    sells = [o for o in broker.orders.values() if o.side == Side.SELL]
    assert sells and all(o.status == OrderStatus.EXPIRED for o in sells)  # never marked as filled
    assert "TST" in broker.positions


def test_no_new_entries_after_cutoff_time():
    from mwbot.calendar import session_for

    sess = session_for(TEST_DAY)
    # quiet for 60 min after the open, then a strong run: the signal only appears ~09:30 CT (60 min in)
    late_runner = runner_day(sess, [(60, 10.95, 3_000), (20, 12.2, 40_000), (30, 12.4, 20_000)])
    wide = {**NO_EXITS, "end_minute": 150}
    control = settings_for(params=wide)
    _, broker_ok, _ = run(control, {"TST": late_runner})
    assert len(bought(broker_ok)) == 1, "scenario must produce an entry when entries are allowed"
    entry_time = bought(broker_ok)[0].created_at

    cut = settings_for(params=wide)
    cut.schedule = ScheduleConfig(
        last_entry="09:15", liquidation_start="14:45"
    )  # cutoff before the signal appears
    assert entry_time > at(sess, 45)
    bot, broker, _ = run(cut, {"TST": late_runner})
    assert bought(broker) == []  # signal existed (control run) but arrived after the configured cutoff


def test_max_positions_is_enforced_while_holding():
    from mwbot.calendar import session_for

    sess = session_for(TEST_DAY)
    days = {
        f"S{i}": runner_day(sess, [(20, 11.7, 28_000), (30, 11.85, 14_000)], seed=i + 1) for i in range(4)
    }
    peak = {"n": 0}

    def hook(runner, bot):
        peak["n"] = max(
            peak["n"],
            len(bot.broker.positions) + len(bot.broker.pending_buy_symbols() - set(bot.broker.positions)),
        )

    bot, broker, _ = run(settings_for(params=NO_EXITS, max_positions=2), days, hook=hook)
    assert peak["n"] == 2 and len(broker.trades) == 2


def test_position_size_respects_max_allocation():
    bot, broker, _ = run(
        settings_for(params=NO_EXITS, max_position_pct=5.0, risk_per_trade_pct=5.0),
        {"TST": runner_day_flat()},
    )
    (t,) = broker.trades
    assert t.quantity * t.entry_price <= 100_000 * 0.05 * 1.01


# ------------------------------------------------------------------ risk controls
def test_daily_loss_limit_flattens_and_blocks_new_entries():
    from mwbot.calendar import session_for

    sess = session_for(TEST_DAY)
    crash = runner_day(sess, [(20, 11.7, 28_000), (12, 8.5, 90_000), (60, 8.5, 5_000)])
    other = runner_day(
        sess, [(150, 10.95, 3_000), (20, 12.5, 60_000), (60, 12.6, 20_000)], seed=7
    )  # would signal AFTER the breach
    st = settings_for(
        params={"stop_loss_pct": 20.0, "trailing_stop_pct": None},
        max_daily_loss_pct=0.3,
        max_position_pct=50,
        risk_per_trade_pct=5,
    )
    bot, broker, runner = run(st, {"CRASH": crash, "LATE": other})
    assert any(a["code"] == "daily_loss_limit" for a in bot.alerts)
    assert bot.daily_halted
    assert {t.symbol for t in broker.trades} == {"CRASH"}
    assert "Daily loss limit" in broker.trades[0].exit_reason or "Stop" in broker.trades[0].exit_reason
    assert not [o for o in bought(broker) if o.symbol == "LATE"]


def test_emergency_stop_cancels_buys_flattens_and_blocks_everything():
    state = {}

    def hook(runner, bot):
        if runner.now == at(runner.schedule.session, 25) and "done" not in state:
            state["positions_before"] = list(bot.broker.positions)
            bot.emergency_stop(runner.now)
            state["done"] = True

    bot, broker, runner = run(settings_for(params=NO_EXITS), {"TST": runner_day_flat()}, hook=hook)
    assert state["positions_before"] == ["TST"]
    (t,) = broker.trades
    assert t.exit_reason.startswith("Emergency stop") and t.exit_time < at(runner.schedule.session, 40)
    assert bot.state == BotState.EMERGENCY_STOP and len(bought(broker)) == 1
    with pytest.raises(RuntimeError):
        bot.start()
    bot.reset_emergency()
    assert bot.state == BotState.STOPPED


def test_pause_blocks_entries_but_still_manages_exits():
    from mwbot.calendar import session_for

    sess = session_for(TEST_DAY)
    day = runner_day(sess, [(20, 11.7, 28_000), (15, 11.8, 14_000), (1, 10.4, 90_000), (37, 10.4, 5_000)])

    def hook(runner, bot):
        if runner.now == at(runner.schedule.session, 30):
            bot.pause()

    bot, broker, _ = run(settings_for(params={"trailing_stop_pct": None}), {"TST": day}, hook=hook)
    assert (
        bot.state == BotState.PAUSED
        and len(broker.trades) == 1
        and "Stop loss" in broker.trades[0].exit_reason
    )

    def hook_early(runner, bot):
        if runner.now == at(runner.schedule.session, 5):
            bot.pause()

    _, broker2, _ = run(settings_for(), {"TST": day}, hook=hook_early)
    assert bought(broker2) == []


def test_duplicate_orders_are_prevented_when_a_tick_repeats():
    from mwbot.calendar import session_for

    sess = session_for(TEST_DAY)
    st = settings_for(params=NO_EXITS)
    src = make_source(sess, {"TST": (meta(), runner_day_flat())})
    broker = PaperBroker(st.risk.starting_capital, st.costs)
    bot = TradingBot(st, broker)
    bot.start()
    sched = build_schedule(TEST_DAY, st.schedule)
    runner = SessionRunner(bot, src, sched)
    while runner.now < at(sess, 14):
        runner.step()
    t = runner.now
    for _ in range(3):
        bot.tick(src, sched, t)  # same instant, three times
    assert len(bought(broker)) == 1
    assert len(broker.orders) == len({o.client_order_id for o in broker.orders.values()})


# ------------------------------------------------------------------ data problems
class Wrapped(MarketDataSource):
    def __init__(self, inner, *, age=0.0, kind=None, fail=False):
        self.inner, self.age, self.kind, self.fail = inner, age, kind, fail
        self.session = inner.session

    def feed_info(self):
        fi = self.inner.feed_info()
        return FeedInfo(
            self.kind or fi.kind,
            fi.provider,
            fi.label,
            delay_seconds=900 if self.kind == FeedKind.DELAYED else 0,
        )

    def symbols(self):
        return self.inner.symbols()

    def contexts_at(self, as_of):
        if self.fail:
            raise DataUnavailableError("provider down")
        return self.inner.contexts_at(as_of)

    def context_for(self, symbol, as_of):
        return self.inner.context_for(symbol, as_of)

    def feed_age_seconds(self, as_of):
        return self.age

    def bar_at(self, symbol, label):
        return self.inner.bar_at(symbol, label)

    def is_halted(self, symbol, ts):
        return self.inner.is_halted(symbol, ts)

    def spread_bps(self, symbol):
        return self.inner.spread_bps(symbol)


def test_stale_feed_blocks_entries_and_alerts():
    bot, broker, _ = run(
        settings_for(params=NO_EXITS),
        {"TST": runner_day_flat()},
        source_wrapper=lambda s: Wrapped(s, age=900.0),
    )
    assert bought(broker) == [] and any(a["code"] == "stale_feed" for a in bot.alerts)


def test_stale_feed_mid_session_still_allows_risk_reducing_exits():
    flag = {"stale": False}

    class Flaky(Wrapped):
        def feed_age_seconds(self, as_of):
            return 900.0 if flag["stale"] else 0.0

    def hook(runner, bot):
        if runner.now == at(runner.schedule.session, 30):
            flag["stale"] = True

    bot, broker, runner = run(
        settings_for(params=NO_EXITS), {"TST": runner_day_flat()}, hook=hook, source_wrapper=Flaky
    )
    assert len(broker.trades) == 1 and broker.trades[0].exit_reason.startswith("End-of-day")


def test_delayed_feed_blocks_automated_entries_unless_explicitly_allowed():
    st = settings_for(params=NO_EXITS)
    _, broker, _ = run(
        st, {"TST": runner_day_flat()}, source_wrapper=lambda s: Wrapped(s, kind=FeedKind.DELAYED)
    )
    assert bought(broker) == []
    st2 = settings_for(params=NO_EXITS)
    st2.allow_delayed_feed_trading = True
    _, broker2, _ = run(
        st2, {"TST": runner_day_flat()}, source_wrapper=lambda s: Wrapped(s, kind=FeedKind.DELAYED)
    )
    assert len(bought(broker2)) == 1


def test_provider_outage_does_not_crash_or_trade():
    bot, broker, runner = run(
        settings_for(), {"TST": runner_day_flat()}, source_wrapper=lambda s: Wrapped(s, fail=True)
    )
    assert (
        runner.finished and broker.orders == {} and any(a["code"] == "data_unavailable" for a in bot.alerts)
    )


# ------------------------------------------------------------------ accounting invariants
def test_full_synthetic_day_invariants():
    from mwbot.data.synthetic import SyntheticProvider

    st = AppSettings()
    broker = PaperBroker(st.risk.starting_capital, st.costs)
    bot = TradingBot(st, broker)
    bot.start()
    src = SyntheticProvider(42).open_session(TEST_DAY)
    runner = SessionRunner.for_date(bot, src, TEST_DAY)
    min_cash = float("inf")
    while not runner.finished:
        runner.step()
        min_cash = min(min_cash, broker.cash)
    rep = runner.report
    assert min_cash >= 0
    assert all(o.is_terminal for o in broker.orders.values())
    assert (
        rep["end_equity"] == pytest.approx(100_000 + sum(t.net_pnl for t in broker.trades), abs=0.05)
        or rep["unresolved_positions"]
    )
    assert (
        broker.cash == pytest.approx(100_000 + sum(t.net_pnl for t in broker.trades), abs=0.05)
        or broker.positions
    )
    for t in broker.trades:
        assert t.exit_time > t.entry_time and t.quantity > 0
        assert t.net_pnl == pytest.approx(t.gross_pnl - t.commissions)
        assert t.gross_pnl == pytest.approx((t.exit_price - t.entry_price) * t.quantity, rel=1e-6, abs=0.01)
    for o in broker.orders.values():
        if o.status == OrderStatus.FILLED:
            assert o.filled_quantity == o.quantity
    assert runner.schedule.phase_at(runner.now) == Phase.POST_CLOSE or runner.now >= runner.schedule.close
    assert rep["disclaimer"]


# ------------------------------------------------------------------ mirror adapter integration
class FakeMirror(ExecutionAdapter):
    name = "fake"

    def __init__(self, fail_symbols=(), holdings=None):
        self.submitted, self.fail, self._holdings = [], set(fail_symbols), holdings

    def check_session(self):
        return SessionStatus("fake", True, True)

    def get_buying_power(self):
        return 1.0

    def get_holdings(self):
        return self._holdings or []

    def prepare_order(self, request):
        return PreparedOrder(request, "fake", None, None)

    def submit_order(self, prepared):
        if prepared.request.symbol in self.fail:
            raise ExecutionError("venue rejected")
        self.submitted.append(prepared.request)
        return ExternalOrder(f"X{len(self.submitted)}", prepared.request.client_order_id, OrderStatus.PENDING)

    def get_order_status(self, external_id):
        raise NotImplementedError

    def cancel_order(self, external_id):
        return False


def test_orders_are_mirrored_to_external_adapter_and_ids_recorded():
    m = FakeMirror(holdings=[])
    bot, broker, runner = run(settings_for(params=NO_EXITS), {"TST": runner_day_flat()}, mirror=m)
    assert [r.side for r in m.submitted] == [Side.BUY, Side.SELL]
    assert all(o.meta.get("external_id") for o in broker.orders.values())
    assert runner.report["reconciliation"]["ok"] is True  # flat internally, nothing held externally


def test_mirror_failure_raises_alert_and_pauses_bot():
    m = FakeMirror(fail_symbols={"TST"})
    bot, broker, _ = run(settings_for(params=NO_EXITS), {"TST": runner_day_flat()}, mirror=m)
    assert any(a["code"] == "mirror_failed" for a in bot.alerts) and bot.state == BotState.PAUSED


def test_reconciliation_mismatch_is_flagged():
    m = FakeMirror(holdings=[Holding("GHOST", 10)])
    bot, _, runner = run(settings_for(params=NO_EXITS), {"TST": runner_day_flat()}, mirror=m)
    assert runner.report["reconciliation"]["ok"] is False
    assert any(a["code"] == "reconcile_mismatch" for a in bot.alerts)
