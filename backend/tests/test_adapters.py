"""Adapter safety, the MarketWatch skeleton, and the mock trading site's HTTP behaviour (no browser)."""

import pytest
from fastapi.testclient import TestClient

from mwbot.config import EnvSettings
from mwbot.domain import OrderRequest, Side
from mwbot.execution.base import ExecutionAdapter, ExecutionError
from mwbot.execution.factory import assert_virtual_only, create_adapter
from mwbot.execution.marketwatch import MarketWatchAdapter, NotConfiguredError
from mwbot.execution.mock_browser import MockBrowserAdapter
from mwbot.mocksite.app import create_mock_app
from mwbot.mocksite.exchange import MockExchange


def test_marketwatch_skeleton_does_nothing_and_says_so():
    a = MarketWatchAdapter()
    st = a.check_session()
    assert st.logged_in is False and "not configured" in st.detail
    for call in (a.get_buying_power, a.get_holdings, lambda: a.prepare_order(OrderRequest("x", "A", Side.BUY, 1)),
                 lambda: a.cancel_order("1"), lambda: a.get_order_status("1")):
        with pytest.raises(NotConfiguredError, match="MARKETWATCH_INTEGRATION"):
            call()


def test_factory_refuses_real_money_adapters():
    class Real(ExecutionAdapter):
        name = "real"
        is_real_money = True

        def check_session(self): ...
        def get_buying_power(self): ...
        def get_holdings(self): ...
        def prepare_order(self, r): ...
        def submit_order(self, p): ...
        def get_order_status(self, e): ...
        def cancel_order(self, e): ...

    with pytest.raises(ExecutionError, match="forbidden"):
        assert_virtual_only(Real())
    for ad in (MockBrowserAdapter, MarketWatchAdapter):
        assert ad.is_real_money is False


def test_factory_paper_mode_has_no_external_adapter():
    with pytest.raises(ExecutionError, match="internal broker"):
        create_adapter(EnvSettings(_env_file=None, execution_mode="paper"))
    with pytest.raises(Exception):
        EnvSettings(_env_file=None, execution_mode="marketwatch")  # not a selectable mode


def test_browser_adapter_only_talks_to_local_hosts():
    with pytest.raises(ExecutionError, match="local mock site"):
        MockBrowserAdapter("https://www.marketwatch.com/game", "u", "p")
    MockBrowserAdapter("http://127.0.0.1:1", "u", "p")  # construction alone launches nothing


# ----------------------------------------------------------------- mock site over plain HTTP
@pytest.fixture()
def site():
    clock = {"t": 0.0}
    ex = MockExchange(clock=lambda: clock["t"])
    app = create_mock_app(ex)
    c = TestClient(app, follow_redirects=False)
    c.clock = clock  # tests advance simulated venue time through this dict
    yield c, ex
    c.close()


def login(c):
    r = c.post("/login", data={"username": "demo", "password": "demo"})
    assert r.status_code == 303 and "session" in r.cookies
    return r


def test_site_requires_login_and_rejects_bad_credentials(site):
    c, _ = site
    r = c.get("/portfolio")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    bad = c.post("/login", data={"username": "demo", "password": "nope"})
    assert "error=" in bad.headers["location"] and "session" not in bad.cookies
    login(c)
    assert "cash-balance" in c.get("/portfolio").text and "$100,000.00" in c.get("/portfolio").text


def test_site_buy_sell_flow_and_balances(site):
    c, ex = site
    login(c)
    prev = c.post("/trade/preview", data={"symbol": "ZNTR", "side": "buy", "quantity": "100", "order_type": "market", "limit_price": ""})
    assert prev.status_code == 200 and "confirm-btn" in prev.text and not ex.orders          # preview never places an order
    done = c.post("/trade/confirm", data={"symbol": "ZNTR", "side": "buy", "quantity": "100", "order_type": "market", "limit_price": ""})
    oid = done.headers["location"].split("placed=")[1]
    assert ex.orders[oid].status == "filled" and ex.cash == pytest.approx(99_500) and ex.holdings["ZNTR"]["qty"] == 100
    c.post("/trade/confirm", data={"symbol": "ZNTR", "side": "sell", "quantity": "40", "order_type": "market", "limit_price": ""})
    assert ex.holdings["ZNTR"]["qty"] == 60 and ex.cash == pytest.approx(99_700)


@pytest.mark.parametrize(
    "form,message",
    [
        ({"symbol": "NOPE", "side": "buy", "quantity": "1"}, "Unknown ticker"),
        ({"symbol": "ZNTR", "side": "buy", "quantity": "999999"}, "Insufficient buying power"),
        ({"symbol": "ZNTR", "side": "sell", "quantity": "1"}, "do not own enough"),
        ({"symbol": "ZNTR", "side": "buy", "quantity": "0"}, "at least 1"),
        ({"symbol": "ZNTR", "side": "buy", "quantity": "1", "order_type": "limit"}, "positive limit"),
    ],
)
def test_site_validation_errors_surface_to_the_user(site, form, message):
    c, ex = site
    login(c)
    r = c.post("/trade/preview", data={"order_type": "market", "limit_price": "", **form}, follow_redirects=True)
    assert message in r.text and not ex.orders


def test_site_failure_injection_and_delayed_execution(site):
    c, ex = site
    login(c)
    form = {"symbol": "ZNTR", "side": "buy", "quantity": "10", "order_type": "market", "limit_price": ""}
    ex.config.fail_next_order = True
    r = c.post("/trade/confirm", data=form, follow_redirects=True)
    assert "order-error" in r.text and "temporarily unavailable" in r.text
    ex.config.exec_delay_seconds = 5
    r = c.post("/trade/confirm", data=form, follow_redirects=True)
    assert "data-status='pending'" in r.text
    c.clock["t"] = 6.0
    r = c.get("/orders")
    assert "data-status='filled'" in r.text
    ex.config.market_open = False
    r = c.post("/trade/preview", data=form, follow_redirects=True)
    assert "Market is closed" in r.text


def test_site_cancel_pending_limit_order_and_session_expiry(site):
    c, ex = site
    login(c)
    r = c.post("/trade/confirm", data={"symbol": "ZNTR", "side": "buy", "quantity": "10", "order_type": "limit", "limit_price": "1.00"})
    oid = r.headers["location"].split("placed=")[1]
    assert ex.orders[oid].status == "pending"
    c.post(f"/orders/{oid}/cancel")
    assert ex.orders[oid].status == "canceled"
    ex.expire_all_sessions()
    assert c.get("/orders").headers["location"].startswith("/login?expired=1")
