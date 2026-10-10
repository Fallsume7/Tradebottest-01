"""The MarketWatch adapter's orchestration, tested with a scripted page layer (no browser, no MarketWatch)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mwbot.config import EnvSettings
from mwbot.domain import OrderRequest, OrderStatus, OrderType, Side
from mwbot.execution.base import ExecutionError, Holding, MirrorSkipped, SessionExpiredError
from mwbot.execution.factory import create_adapter
from mwbot.execution.marketwatch import (
    ChallengeError,
    MarketWatchAdapter,
    MarketWatchPages,
    NotCalibratedError,
    StagedQuote,
    VenueOrder,
    VenueRejected,
    is_marketwatch_url,
)

GAME = "https://www.marketwatch.com/games/test-game"


class FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.visited: list[str] = []

    def goto(self, url: str) -> None:
        self.url = url
        self.visited.append(url)

    def screenshot(self, path: str) -> None:
        Path(path).write_bytes(b"png")


class FakePages(MarketWatchPages):
    """A scripted stand-in for the real page knowledge."""

    def __init__(self) -> None:
        self.logged_in = True
        self.challenge_text: str | None = None
        self.orders: list[VenueOrder] = []
        self.confirm_clicks = 0
        self.staged: list[OrderRequest] = []
        self.confirm_mode = "ok"  # ok | reject | timeout_placed | timeout_not_placed | no_id
        self.cash = 50_000.0
        self.holdings = [Holding("AAA", 10, 5.0)]

    def is_logged_in(self, page: Any) -> bool:
        return self.logged_in

    def challenge(self, page: Any) -> str | None:
        return self.challenge_text

    def open_portfolio(self, page: Any, game_url: str) -> None:
        pass

    def read_buying_power(self, page: Any) -> float:
        return self.cash

    def read_holdings(self, page: Any) -> list[Holding]:
        return list(self.holdings)

    def read_orders(self, page: Any, game_url: str) -> list[VenueOrder]:
        return list(self.orders)

    def stage_order(self, page: Any, game_url: str, request: OrderRequest) -> StagedQuote:
        self.staged.append(request)
        return StagedQuote(price=10.0, cost=10.0 * request.quantity, warnings=["test quote"])

    def _place(self, n: int = 1) -> str:
        oid = f"MW{len(self.orders) + n}"
        self.orders.append(
            VenueOrder(
                oid,
                self.staged[-1].symbol,
                self.staged[-1].side,
                self.staged[-1].quantity,
                OrderStatus.PENDING,
            )
        )
        return oid

    def click_confirm(self, page: Any) -> str:
        self.confirm_clicks += 1
        if self.confirm_mode == "reject":
            raise VenueRejected("Insufficient buying power")
        if self.confirm_mode == "timeout_placed":
            self._place()
            raise TimeoutError("page did not respond")
        if self.confirm_mode == "timeout_not_placed":
            raise TimeoutError("page did not respond")
        oid = self._place()
        return "" if self.confirm_mode == "no_id" else oid

    def cancel(self, page: Any, game_url: str, order_id: str) -> bool:
        for i, o in enumerate(self.orders):
            if o.order_id == order_id:
                self.orders[i] = VenueOrder(o.order_id, o.symbol, o.side, o.quantity, OrderStatus.CANCELED)
                return True
        return False


def req(cid: str = "c1", symbol: str = "AAA", qty: int = 5, side: Side = Side.BUY, **kw: Any) -> OrderRequest:
    return OrderRequest(client_order_id=cid, symbol=symbol, side=side, quantity=qty, **kw)


@pytest.fixture
def parts(tmp_path: Path):
    pages, page = FakePages(), FakePage()

    def make(stage: str = "live", **kw: Any) -> MarketWatchAdapter:
        return MarketWatchAdapter(
            GAME,
            stage=stage,
            pages=pages,
            ledger_path=tmp_path / "ledger.json",
            shots_dir=tmp_path / "shots",
            min_action_gap_s=0,
            page_provider=lambda: page,
            **kw,
        )

    return pages, page, make, tmp_path


# ------------------------------------------------------------------ safety of the connection itself
def test_only_marketwatch_https_addresses_are_accepted():
    assert is_marketwatch_url("https://www.marketwatch.com/games/x")
    assert is_marketwatch_url("https://marketwatch.com/")
    for bad in (
        "http://www.marketwatch.com/",
        "https://marketwatch.com.evil.example/",
        "https://evil.example/marketwatch.com",
    ):
        assert not is_marketwatch_url(bad)
    with pytest.raises(ExecutionError):
        MarketWatchAdapter("https://evil.example/games/x")
    with pytest.raises(ExecutionError):
        MarketWatchAdapter(GAME, cdp_url="http://192.168.1.50:9222")
    with pytest.raises(ExecutionError):
        MarketWatchAdapter(GAME, stage="yolo")


def test_env_settings_enforce_the_same_rules():
    with pytest.raises(ValueError):
        EnvSettings(marketwatch_game_url="https://evil.example/x")
    with pytest.raises(ValueError):
        EnvSettings(marketwatch_cdp_url="http://10.0.0.5:9222")
    assert EnvSettings().marketwatch_stage == "read_only"  # the safe default


def test_adapter_never_navigates_away_from_marketwatch(parts):
    _pages, page, make, _ = parts
    ad = make()
    with pytest.raises(ExecutionError, match="non-MarketWatch"):
        ad._goto("https://example.com/")
    assert page.visited == []
    ad.close()


def test_is_play_money_and_needs_a_real_feed(parts):
    ad = parts[2]()
    assert ad.is_real_money is False and ad.requires_real_feed is True
    ad.close()


def test_factory_builds_the_adapter_without_touching_chrome(tmp_path):
    env = EnvSettings(execution_mode="marketwatch", marketwatch_game_url=GAME, data_dir=str(tmp_path))
    ad = create_adapter(env)
    assert isinstance(ad, MarketWatchAdapter) and ad.stage == "read_only"
    ad.close()
    with pytest.raises(ExecutionError, match="GAME_URL"):
        create_adapter(EnvSettings(execution_mode="marketwatch"))


# ------------------------------------------------------------------ reads and session handling
def test_reads_cash_and_holdings(parts):
    pages, _page, make, _ = parts
    ad = make("read_only")
    assert ad.get_buying_power() == 50_000.0
    assert ad.get_holdings() == [Holding("AAA", 10, 5.0)]
    assert ad.check_session().logged_in
    assert ad.reconcile([Holding("AAA", 10, 5.0)]).ok
    assert not ad.reconcile([Holding("AAA", 7, 5.0)]).ok
    ad.close()


def test_logged_out_means_stop_never_log_in(parts):
    pages, _page, make, _ = parts
    pages.logged_in = False
    ad = make()
    with pytest.raises(SessionExpiredError):
        ad.get_buying_power()
    st = ad.check_session()
    assert not st.logged_in and "log in again" in st.detail
    ad.close()


def test_a_captcha_stops_everything(parts):
    pages, _page, make, _ = parts
    pages.challenge_text = "captcha"
    ad = make()
    with pytest.raises(ChallengeError):
        ad.get_holdings()
    with pytest.raises(ChallengeError):
        ad.prepare_order(req())
    assert pages.staged == [] and pages.confirm_clicks == 0
    ad.close()


def test_uncalibrated_pages_refuse_to_guess(tmp_path):
    ad = MarketWatchAdapter(GAME, page_provider=FakePage, min_action_gap_s=0)
    st = ad.check_session()
    assert not st.logged_in and "not calibrated" in st.detail
    with pytest.raises(NotCalibratedError):
        ad.get_buying_power()
    ad.close()


# ------------------------------------------------------------------ rollout stages
def test_read_only_stage_never_touches_the_order_form(parts):
    pages, _page, make, _ = parts
    ad = make("read_only")
    with pytest.raises(MirrorSkipped):
        ad.prepare_order(req())
    assert pages.staged == [] and pages.confirm_clicks == 0
    ad.close()


def test_prepare_only_fills_the_form_and_photographs_it_but_never_confirms(parts):
    pages, _page, make, tmp = parts
    ad = make("prepare_only")
    prepared = ad.prepare_order(req())
    assert prepared.estimated_cost == 50.0 and any("screenshot" in w for w in prepared.warnings)
    with pytest.raises(MirrorSkipped):
        ad.submit_order(prepared)
    assert pages.staged and pages.confirm_clicks == 0 and pages.orders == []
    assert list((tmp / "shots").glob("*prepared*.png"))
    ad.close()


def test_live_stage_submits_and_reads_the_order_back(parts):
    pages, _page, make, _ = parts
    ad = make("live")
    ext = ad.submit_order(ad.prepare_order(req("c-live")))
    assert pages.confirm_clicks == 1
    assert ext.client_order_id == "c-live" and ext.status == OrderStatus.PENDING and ext.external_id == "MW1"
    ad.close()


def test_limit_order_needs_a_price_and_zero_quantity_is_refused(parts):
    _pages, _page, make, _ = parts
    ad = make("live")
    with pytest.raises(ExecutionError):
        ad.prepare_order(req(order_type=OrderType.LIMIT))
    with pytest.raises(ExecutionError):
        ad.prepare_order(req(qty=0))
    ad.close()


# ------------------------------------------------------------------ never double-submit
def test_same_client_id_is_never_confirmed_twice_even_after_a_restart(parts):
    pages, _page, make, _ = parts
    a = make("live")
    first = a.submit_order(a.prepare_order(req("dup")))
    again = a.submit_order(a.prepare_order(req("dup")))
    assert again.external_id == first.external_id and pages.confirm_clicks == 1
    a.close()

    b = make("live")  # new process, same ledger file on disk
    third = b.submit_order(b.prepare_order(req("dup")))
    assert third.external_id == first.external_id and pages.confirm_clicks == 1
    b.close()


def test_a_timeout_after_the_click_is_resolved_by_looking_not_by_retrying(parts):
    pages, _page, make, _ = parts
    pages.confirm_mode = "timeout_placed"
    ad = make("live")
    ext = ad.submit_order(ad.prepare_order(req("t1")))
    assert ext.external_id == "MW1" and pages.confirm_clicks == 1 and len(pages.orders) == 1
    ad.close()


def test_a_timeout_with_no_order_found_raises_and_never_resends(parts):
    pages, _page, make, _ = parts
    pages.confirm_mode = "timeout_not_placed"
    ad = make("live")
    with pytest.raises(ExecutionError, match="UNKNOWN"):
        ad.submit_order(ad.prepare_order(req("t2")))
    assert pages.confirm_clicks == 1 and pages.orders == []
    ad.close()


def test_order_id_is_found_on_the_orders_page_when_the_confirmation_shows_none(parts):
    pages, _page, make, _ = parts
    pages.confirm_mode = "no_id"
    ad = make("live")
    ext = ad.submit_order(ad.prepare_order(req("t3")))
    assert ext.external_id == "MW1"
    ad.close()


def test_a_refusal_from_marketwatch_is_reported_not_raised_and_not_retried(parts):
    pages, _page, make, _ = parts
    pages.confirm_mode = "reject"
    ad = make("live")
    ext = ad.submit_order(ad.prepare_order(req("r1")))
    assert ext.status == OrderStatus.REJECTED and "Insufficient" in ext.detail
    again = ad.submit_order(ad.prepare_order(req("r1")))
    assert again.status == OrderStatus.REJECTED and pages.confirm_clicks == 1
    ad.close()


def test_cancel_only_in_live_stage(parts):
    pages, _page, make, _ = parts
    live = make("live")
    ext = live.submit_order(live.prepare_order(req("x1")))
    assert live.cancel_order(ext.external_id) is True
    live.close()
    ro = make("read_only")
    with pytest.raises(MirrorSkipped):
        ro.cancel_order(ext.external_id)
    ro.close()


def test_unreadable_ledger_stops_trading_instead_of_starting_empty(tmp_path):
    bad = tmp_path / "ledger.json"
    bad.write_text("{not json", "utf-8")
    with pytest.raises(ExecutionError, match="unreadable"):
        MarketWatchAdapter(GAME, ledger_path=bad, page_provider=FakePage)


# ------------------------------------------------------------------ real DevTools attach (no MarketWatch contact)
@pytest.mark.browser
def test_attaches_to_a_running_chrome_over_localhost_and_leaves_it_running(tmp_path):
    import socket

    from playwright.sync_api import sync_playwright

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=[f"--remote-debugging-port={port}"])
        try:
            ctx = browser.new_context()
            ctx.new_page().set_content("<h1>the user's own window</h1>")
            ad = MarketWatchAdapter(GAME, cdp_url=f"http://127.0.0.1:{port}", min_action_gap_s=0)
            page = ad._pool.submit(ad._ensure_page).result(timeout=30)  # attaches on the adapter's own thread
            assert page is not None
            ad.close()  # disconnects only
            assert browser.is_connected()
            assert ctx.pages and "own window" in ctx.pages[0].inner_text("h1")
        finally:
            browser.close()


def test_unreachable_chrome_gives_a_helpful_error(tmp_path):
    ad = MarketWatchAdapter(GAME, cdp_url="http://127.0.0.1:1", min_action_gap_s=0, timeout_ms=2000)
    with pytest.raises(ExecutionError, match="marketwatch_chrome.ps1"):
        ad._pool.submit(ad._ensure_page).result(timeout=30)
    ad.close()
