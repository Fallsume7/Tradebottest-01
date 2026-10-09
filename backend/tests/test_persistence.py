from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from mwbot.bot.engine import TradingBot
from mwbot.bot.runner import SessionRunner
from mwbot.calendar import build_schedule
from mwbot.config import AppSettings
from mwbot.db.models import UTCDateTime
from mwbot.db.store import SqlStore, make_engine
from mwbot.domain import OrderRequest, OrderStatus, Side
from mwbot.trading.broker import PaperBroker

from .conftest import TEST_DAY, at, make_source, meta
from .scenarios import runner_day
from .test_bot_sessions import NO_EXITS, settings_for


@pytest.fixture()
def store(tmp_path):
    return SqlStore(make_engine(f"sqlite:///{tmp_path / 'p.db'}"))


def test_settings_round_trip(store):
    assert store.load_settings() == AppSettings()
    s = AppSettings()
    s.risk.starting_capital = 250_000
    s.scanner.filters.max_price = 33.0
    s.strategies.enabled = ["volume_surge"]
    store.save_settings(s)
    again = SqlStore(store.engine).load_settings()
    assert again.risk.starting_capital == 250_000 and again.scanner.filters.max_price == 33.0 and again.strategies.enabled == ["volume_surge"]


def test_datetimes_are_utc_aware_after_round_trip_and_naive_refused(store):
    store.add_equity(datetime(2026, 10, 8, 14, 0, tzinfo=UTC), 100.0, "2026-10-08")
    assert store.equity_history()[0]["ts"].endswith("+00:00")
    with pytest.raises(Exception):
        store.add_equity(datetime(2026, 10, 8, 14, 0), 100.0, "2026-10-08")
    assert UTCDateTime().process_result_value(None, None) is None


def midday_state(store):
    """Run a scripted session to just after entry and return (broker, bot, runner, source)."""
    from mwbot.calendar import session_for
    sess = session_for(TEST_DAY)
    st = settings_for(params=NO_EXITS)
    src = make_source(sess, {"TST": (meta(), runner_day(sess, [(20, 11.7, 28_000), (30, 11.85, 14_000)]))})
    broker = PaperBroker(st.risk.starting_capital, st.costs, store=store)
    bot = TradingBot(st, broker)
    bot.start()
    runner = SessionRunner(bot, src, build_schedule(TEST_DAY, st.schedule))
    while runner.now < at(sess, 25):
        runner.step()
    return broker, bot, runner, src


def test_broker_state_survives_restart(store):
    broker, bot, runner, src = midday_state(store)
    assert "TST" in broker.positions
    pos = broker.positions["TST"]

    fresh_store = SqlStore(store.engine)            # a brand-new process only has the database
    revived = PaperBroker(1.0, store=fresh_store)
    assert fresh_store.restore_broker(revived) is True
    assert revived.cash == pytest.approx(broker.cash) and revived.starting_cash == 100_000
    assert set(revived.positions) == {"TST"}
    rp = revived.positions["TST"]
    assert (rp.quantity, rp.avg_cost, rp.stop_price, rp.strategy) == (pos.quantity, pos.avg_cost, pos.stop_price, pos.strategy)
    assert rp.opened_at == pos.opened_at and rp.opened_at.tzinfo is not None
    assert {o.id: o.status for o in revived.orders.values()} == {o.id: o.status for o in broker.orders.values()}
    assert len(revived.fills) == len(broker.fills)
    assert revived.equity() == pytest.approx(broker.equity())  # marks are persisted too


def test_restored_broker_is_idempotent_and_continues_numbering(store):
    broker, *_ = midday_state(store)
    existing = next(o for o in broker.orders.values() if o.side == Side.BUY)
    revived = PaperBroker(1.0, store=SqlStore(store.engine))
    revived.store.restore_broker(revived)
    again = revived.submit(OrderRequest(existing.client_order_id, "TST", Side.BUY, 5), at_time())
    assert again.id == existing.id and len(revived.orders) == len(broker.orders)
    revived.mark("TST", 11.8)
    new = revived.submit(OrderRequest("another", "TST", Side.SELL, 1), at_time())
    assert int(new.id.split("-")[1]) == len(broker.orders) + 1


def at_time():
    from mwbot.calendar import session_for
    return at(session_for(TEST_DAY), 26)


def test_pending_orders_resume_filling_after_restart(store):
    from mwbot.calendar import session_for
    sess = session_for(TEST_DAY)
    src = make_source(sess, {"TST": (meta(), runner_day(sess, []))})
    b = PaperBroker(100_000, store=store)
    b.mark("TST", 10.95)
    t = at(sess, 5)
    o = b.submit(OrderRequest("c", "TST", Side.BUY, 50), t, expires_at=sess.close_utc)
    assert o.status == OrderStatus.PENDING
    revived = PaperBroker(1.0, store=SqlStore(store.engine))
    revived.store.restore_broker(revived)
    revived.process(src, t + timedelta(minutes=3))
    assert revived.orders[o.id].status == OrderStatus.FILLED and revived.positions["TST"].quantity == 50


def test_reset_clears_everything(store):
    broker, *_ = midday_state(store)
    store.reset_account()
    empty = PaperBroker(5.0, store=SqlStore(store.engine))
    assert empty.store.restore_broker(empty) is False and store.recent_decisions() == []


def test_decisions_reports_backtests_persist(store):
    store.add_decision({"ts": "2026-10-08T14:00:00+00:00", "kind": "entry", "symbol": "TST", "strategy": "s", "message": "bought", "details": {"x": 1}})
    assert store.recent_decisions()[0]["message"] == "bought"
    store.save_report({"date": "2026-10-08", "pnl": 1.5, "end_equity": 100_001.5, "pnl_pct": 0.0015})
    store.save_report({"date": "2026-10-08", "pnl": 2.5, "end_equity": 100_002.5, "pnl_pct": 0.0025})
    assert [r["pnl"] for r in store.reports()] == [2.5]
    store.save_backtest("abc", "bt", {"a": 1}, {"total_return_pct": 1.0}, {"trades": []})
    assert store.list_backtests()[0]["id"] == "abc" and store.get_backtest("abc")["summary"]["total_return_pct"] == 1.0
    assert store.get_backtest("nope") is None
