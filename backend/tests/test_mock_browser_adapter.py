"""End-to-end browser tests: the Playwright adapter against the local mock trading site."""

import pytest

from mwbot.domain import OrderRequest, OrderStatus, OrderType, Side
from mwbot.execution.base import ExecutionError, Holding, PreparedOrder, SessionExpiredError
from mwbot.execution.mock_browser import MockBrowserAdapter

pytestmark = pytest.mark.browser


@pytest.fixture()
def adapter(mock_site):
    url, ex = mock_site
    ad = MockBrowserAdapter(url, "demo", "demo", timeout_ms=4000)
    yield ad, ex
    ad.close()


def buy(sym="ZNTR", qty=100, cid="c1", **kw):
    return OrderRequest(cid, sym, Side.BUY, qty, **kw)


def test_login_session_status_and_balances(adapter):
    ad, ex = adapter
    assert ad.check_session().logged_in is False  # not logged in yet
    ad.login()
    st = ad.check_session()
    assert st.logged_in and st.adapter == "mock_browser" and ad.is_real_money is False
    assert ad.get_buying_power() == pytest.approx(100_000)
    assert ad.get_holdings() == []


def test_wrong_password_raises_clear_error(mock_site):
    url, _ = mock_site
    ad = MockBrowserAdapter(url, "demo", "wrong")
    try:
        with pytest.raises(ExecutionError, match="login failed"):
            ad.login()
    finally:
        ad.close()


def test_prepare_does_not_place_an_order_then_submit_buys(adapter):
    ad, ex = adapter
    ad.login()
    p = ad.prepare_order(buy())
    assert p.estimated_price == 5.0 and p.estimated_cost == pytest.approx(500) and not ex.orders
    ext = ad.submit_order(p)
    assert ext.status == OrderStatus.FILLED and ext.filled_quantity == 100 and ext.avg_fill_price == 5.0
    assert ad.get_buying_power() == pytest.approx(99_500)
    assert ad.get_holdings() == [Holding("ZNTR", 100, 5.0)]
    assert len(ex.orders) == 1


def test_sell_round_trip_with_profit(adapter):
    ad, ex = adapter
    ad.login()
    ad.submit_order(ad.prepare_order(buy(qty=200)))
    ex.set_prices({"ZNTR": 5.5})
    sell = ad.submit_order(ad.prepare_order(OrderRequest("s1", "ZNTR", Side.SELL, 200)))
    assert sell.status == OrderStatus.FILLED and sell.avg_fill_price == 5.5
    assert ad.get_holdings() == [] and ad.get_buying_power() == pytest.approx(100_000 + 100)


def test_submit_is_idempotent_for_same_client_order_id(adapter):
    ad, ex = adapter
    ad.login()
    p = ad.prepare_order(buy(qty=10, cid="dup"))
    first = ad.submit_order(p)
    second = ad.submit_order(p)
    assert first.external_id == second.external_id and len(ex.orders) == 1
    with pytest.raises(ExecutionError, match="not staged"):
        ad.submit_order(PreparedOrder(buy(qty=1, cid="never-prepared"), "mock_browser", None, None))


@pytest.mark.parametrize(
    "req,needle",
    [
        (OrderRequest("a", "NOPE", Side.BUY, 1), "Unknown ticker"),
        (OrderRequest("b", "ZNTR", Side.BUY, 10_000_000), "Insufficient buying power"),
        (OrderRequest("c", "ZNTR", Side.SELL, 1), "do not own enough"),
    ],
)
def test_unsuccessful_orders_are_reported_not_hidden(adapter, req, needle):
    ad, ex = adapter
    ad.login()
    with pytest.raises(ExecutionError, match=needle):
        ad.prepare_order(req)
    assert not ex.orders


def test_venue_rejection_is_returned_as_rejected_order(adapter):
    ad, ex = adapter
    ad.login()
    p = ad.prepare_order(buy(qty=5, cid="r1"))
    ex.config.fail_next_order = True  # venue glitch between preview and confirm
    ext = ad.submit_order(p)
    assert ext.status == OrderStatus.REJECTED and "temporarily unavailable" in ext.detail
    assert ad.get_holdings() == []


def test_market_closed_blocks_preparation(adapter):
    ad, ex = adapter
    ad.login()
    ex.config.market_open = False
    with pytest.raises(ExecutionError, match="Market is closed"):
        ad.prepare_order(buy())


def test_delayed_execution_pending_then_filled(adapter):
    ad, ex = adapter
    ad.login()
    ex.config.exec_delay_seconds = 1.0
    ext = ad.submit_order(ad.prepare_order(buy(qty=10, cid="slow")))
    assert ext.status == OrderStatus.PENDING and ad.get_holdings() == []
    done = ad.wait_for_terminal(ext.external_id, timeout_s=8)
    assert done.status == OrderStatus.FILLED and ad.get_holdings()[0].quantity == 10


def test_cancel_pending_limit_order(adapter):
    ad, ex = adapter
    ad.login()
    p = ad.prepare_order(buy(qty=10, cid="lim", order_type=OrderType.LIMIT, limit_price=1.0))
    ext = ad.submit_order(p)
    assert ext.status == OrderStatus.PENDING
    assert ad.cancel_order(ext.external_id) is True
    assert ad.get_order_status(ext.external_id).status == OrderStatus.CANCELED
    assert ad.cancel_order(ext.external_id) is False  # no longer cancellable
    assert ad.get_buying_power() == pytest.approx(100_000)


def test_limit_order_fills_when_price_crosses(adapter):
    ad, ex = adapter
    ad.login()
    ext = ad.submit_order(
        ad.prepare_order(buy(qty=10, cid="lim2", order_type=OrderType.LIMIT, limit_price=4.0))
    )
    assert ext.status == OrderStatus.PENDING
    ex.set_prices({"ZNTR": 3.9})
    assert ad.get_order_status(ext.external_id).status == OrderStatus.FILLED


def test_expired_session_is_detected_and_recovered(adapter):
    ad, ex = adapter
    ad.login()
    ex.expire_all_sessions()
    with pytest.raises(SessionExpiredError):
        ad.get_buying_power()
    assert ad.check_session().logged_in is False
    ad.ensure_logged_in()
    assert ad.get_buying_power() == pytest.approx(100_000)


def test_submission_with_unknown_outcome_does_not_double_order(adapter, monkeypatch):
    """The click registers but the confirmation never reaches us (timeout). We must find the order, not resubmit."""
    ad, ex = adapter
    ad.login()
    p = ad.prepare_order(buy(qty=7, cid="flaky"))
    page = ad._page
    real_wait = page.wait_for_selector

    def flaky_wait(*a, **k):
        page.goto(ad.base_url + "/portfolio")  # lose the confirmation page
        raise TimeoutError("lost the response")

    monkeypatch.setattr(page, "wait_for_selector", flaky_wait)
    ext = ad.submit_order(p)
    monkeypatch.setattr(page, "wait_for_selector", real_wait)
    assert ext.status == OrderStatus.FILLED and len(ex.orders) == 1
    assert ad.submit_order(p).external_id == ext.external_id and len(ex.orders) == 1


def test_unknown_outcome_with_nothing_found_raises(adapter, monkeypatch):
    ad, ex = adapter
    ad.login()
    p = ad.prepare_order(buy(qty=7, cid="never-clicked"))
    page = ad._page
    monkeypatch.setattr(page, "click", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("click failed")))
    with pytest.raises(ExecutionError, match="outcome unknown"):
        ad.submit_order(p)
    assert not ex.orders


def test_reconciliation_detects_drift_between_bot_and_venue(adapter):
    ad, ex = adapter
    ad.login()
    ad.submit_order(ad.prepare_order(buy(qty=100, cid="rec")))
    ok = ad.reconcile([Holding("ZNTR", 100)])
    assert ok.ok and ok.matched == ["ZNTR"]
    bad = ad.reconcile([Holding("ZNTR", 90), Holding("QBLT", 5)])
    assert (
        not bad.ok and bad.quantity_mismatches == [("ZNTR", 90, 100)] and bad.missing_externally == ["QBLT"]
    )
    # something placed by hand on the venue that the bot never knew about
    ex.holdings["KLMX"] = {"qty": 3, "cost": 3.0}
    assert "KLMX" in ad.reconcile([Holding("ZNTR", 100)]).missing_internally


def test_bot_mirrors_a_whole_session_to_the_browser_venue(mock_site):
    """Vertical slice: paper bot decides -> orders are replayed on the mock website -> reconciled at the close."""
    from mwbot.bot.engine import TradingBot
    from mwbot.bot.runner import SessionRunner
    from mwbot.calendar import build_schedule, session_for
    from mwbot.trading.broker import PaperBroker

    from .conftest import TEST_DAY, make_source, meta
    from .scenarios import runner_day
    from .test_bot_sessions import NO_EXITS, settings_for

    url, ex = mock_site
    ex.starting_cash = ex.cash = 10_000_000.0  # the venue must afford what the bot sized
    sess = session_for(TEST_DAY)
    st = settings_for(params=NO_EXITS)
    src = make_source(
        sess, {"ZNTR": (meta("ZNTR"), runner_day(sess, [(20, 11.7, 28_000), (30, 11.85, 14_000)]))}
    )
    ad = MockBrowserAdapter(url, "demo", "demo", timeout_ms=4000)
    try:
        ad.login()
        broker = PaperBroker(st.risk.starting_capital, st.costs)
        bot = TradingBot(st, broker, mirror=ad)
        bot.start()
        runner = SessionRunner(bot, src, build_schedule(TEST_DAY, st.schedule))
        report = runner.run()
        assert broker.trades and not broker.positions
        sides = sorted(o.side for o in ex.orders.values())
        assert sides == ["buy", "sell"] and all(o.status == "filled" for o in ex.orders.values())
        assert report["reconciliation"]["ok"] is True and not ex.holdings
        assert all(o.meta.get("external_id", "").startswith("MW-") for o in broker.orders.values())
    finally:
        ad.close()


def test_bot_pauses_when_venue_rejects_a_mirrored_order(mock_site):
    from mwbot.bot.engine import TradingBot
    from mwbot.bot.runner import SessionRunner
    from mwbot.calendar import build_schedule, session_for
    from mwbot.trading.broker import PaperBroker
    from mwbot.trading.risk import BotState

    from .conftest import TEST_DAY, make_source, meta
    from .scenarios import runner_day
    from .test_bot_sessions import NO_EXITS, settings_for

    url, ex = mock_site
    ex.config.reject_symbols = {"ZNTR"}
    sess = session_for(TEST_DAY)
    st = settings_for(params=NO_EXITS)
    src = make_source(
        sess, {"ZNTR": (meta("ZNTR"), runner_day(sess, [(20, 11.7, 28_000), (30, 11.85, 14_000)]))}
    )
    ad = MockBrowserAdapter(url, "demo", "demo", timeout_ms=4000)
    try:
        ad.login()
        bot = TradingBot(st, PaperBroker(st.risk.starting_capital, st.costs), mirror=ad)
        bot.start()
        SessionRunner(bot, src, build_schedule(TEST_DAY, st.schedule)).run()
        assert any(a["code"] == "mirror_failed" for a in bot.alerts) and bot.state == BotState.PAUSED
        assert not ex.orders or all(o.status == "rejected" for o in ex.orders.values())
    finally:
        ad.close()
