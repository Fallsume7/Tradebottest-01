"""Playwright adapter that drives the bundled MOCK trading site.

Purpose: prove the browser-execution architecture (login, staged order preview, confirmation,
status polling, cancel, reconcile, error handling, idempotency) against a site we control.
A future authorized MarketWatch adapter would follow the same shape with its own page objects.

Everything here is virtual-money against ``MWBOT_MOCK_SITE_URL``. The adapter refuses to navigate
to any host other than the configured mock-site origin.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import time
from typing import Any, ClassVar
from urllib.parse import urlparse

from mwbot.domain import OrderRequest, OrderStatus, OrderType, Side
from mwbot.execution.base import (
    ExecutionAdapter,
    ExecutionError,
    ExternalOrder,
    Holding,
    PreparedOrder,
    SessionExpiredError,
    SessionStatus,
)

log = logging.getLogger(__name__)

_STATUS = {
    "pending": OrderStatus.PENDING, "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED, "rejected": OrderStatus.REJECTED,
}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def find_chromium() -> str | None:
    """Locate a Chromium binary: MWBOT_CHROMIUM_PATH, then PLAYWRIGHT_BROWSERS_PATH, else None (use Playwright's own)."""
    explicit = os.environ.get("MWBOT_CHROMIUM_PATH")
    if explicit and os.path.exists(explicit):
        return explicit
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    for pattern in (f"{root}/chromium-*/chrome-linux/chrome", f"{root}/chromium*/chrome-linux*/chrome"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    return None


def _money(text: str) -> float:
    m = re.search(r"-?[\d,]+\.?\d*", text.replace("$", ""))
    if not m:
        raise ExecutionError(f"could not parse a money value from {text!r}")
    return float(m.group(0).replace(",", ""))


class MockBrowserAdapter(ExecutionAdapter):
    name: ClassVar[str] = "mock_browser"
    is_real_money: ClassVar[bool] = False

    def __init__(
        self, base_url: str, username: str, password: str, *, headless: bool = True,
        timeout_ms: int = 5000, executable_path: str | None = None, slow_mo_ms: int = 0,
    ) -> None:
        host = urlparse(base_url).hostname or ""
        if host not in LOCAL_HOSTS:
            raise ExecutionError(f"MockBrowserAdapter only talks to a local mock site, refusing host {host!r}")
        self.base_url = base_url.rstrip("/")
        self.username, self.password = username, password
        self.timeout_ms = timeout_ms
        self._headless = headless
        self._exe = executable_path or find_chromium()
        self._slow_mo = slow_mo_ms
        self._pw: Any = None
        self._browser: Any = None
        self._page: Any = None
        self._ledger: dict[str, str] = {}  # client_order_id -> external order id (idempotency)
        self._staged: dict[str, PreparedOrder] = {}
        self._before_ids: dict[str, set[str]] = {}

    # ------------------------------------------------------------------ browser plumbing
    def _ensure_page(self) -> Any:
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise ExecutionError("Playwright is not installed: pip install -e 'backend[browser]' && playwright install chromium") from exc
        self._pw = sync_playwright().start()
        kwargs: dict[str, Any] = {"headless": self._headless, "slow_mo": self._slow_mo,
                                  "args": ["--no-sandbox"] if os.environ.get("CI") or os.geteuid() == 0 else []}
        if self._exe:
            kwargs["executable_path"] = self._exe
        self._browser = self._pw.chromium.launch(**kwargs)
        ctx = self._browser.new_context()
        ctx.set_default_timeout(self.timeout_ms)
        self._page = ctx.new_page()
        return self._page

    def close(self) -> None:
        for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:  # pragma: no cover - best effort shutdown
                pass
        self._page = self._browser = self._pw = None

    def _goto(self, path: str) -> Any:
        page = self._ensure_page()
        try:
            page.goto(self.base_url + path)
        except Exception as exc:
            raise ExecutionError(f"navigation to {path} failed: {exc}") from exc
        if "/login" in page.url and path != "/login":
            raise SessionExpiredError("session expired or not logged in")
        return page

    # ------------------------------------------------------------------ session
    def login(self) -> None:
        page = self._ensure_page()
        page.goto(self.base_url + "/login")
        page.fill("#username", self.username)
        page.fill("#password", self.password)
        page.click("#login-btn")
        page.wait_for_load_state()
        if page.locator("#login-error").count():
            raise ExecutionError("login failed: " + page.locator("#login-error").inner_text())

    def ensure_logged_in(self) -> None:
        try:
            self._goto("/portfolio")
        except SessionExpiredError:
            log.info("mock site session expired; logging in again")
            self.login()

    def check_session(self) -> SessionStatus:
        try:
            self._goto("/portfolio")
            return SessionStatus(self.name, True, None, "logged in to mock site")
        except SessionExpiredError:
            return SessionStatus(self.name, False, None, "not logged in")
        except ExecutionError as exc:
            return SessionStatus(self.name, False, None, f"unreachable: {exc}")

    # ------------------------------------------------------------------ reads
    def get_buying_power(self) -> float:
        page = self._goto("/portfolio")
        return _money(page.locator("#buying-power").get_attribute("data-value") or page.locator("#buying-power").inner_text())

    def get_holdings(self) -> list[Holding]:
        page = self._goto("/portfolio")
        rows = page.eval_on_selector_all(
            "tr.holding", "els => els.map(e => ({s: e.dataset.symbol, q: e.dataset.qty, a: e.dataset.avg}))")
        return [Holding(r["s"], int(r["q"]), float(r["a"])) for r in rows]

    # ------------------------------------------------------------------ orders
    def prepare_order(self, request: OrderRequest) -> PreparedOrder:
        """Fill in the order form and reach the confirmation page WITHOUT confirming."""
        # Snapshot existing order ids first: needed to detect an order whose submission outcome is unknown.
        self._before_ids[request.client_order_id] = self._known_order_ids()
        page = self._goto(f"/trade?symbol={request.symbol}")
        if page.locator("#error-banner").count():
            raise ExecutionError(page.locator("#error-banner").inner_text())
        if not page.locator("#quote-price").count():
            raise ExecutionError(f"no quote form for {request.symbol}")
        px = float(page.locator("#quote-price").get_attribute("data-value") or 0)
        page.select_option("#side", "buy" if request.side == Side.BUY else "sell")
        page.fill("#quantity", str(request.quantity))
        page.select_option("#order-type", request.order_type.value)
        if request.order_type == OrderType.LIMIT and request.limit_price:
            page.fill("#limit-price", f"{request.limit_price:.2f}")
        page.click("#preview-btn")
        page.wait_for_load_state()
        if page.locator("#error-banner").count():
            raise ExecutionError(page.locator("#error-banner").inner_text())
        if not page.locator("#confirm-btn").count():
            raise ExecutionError("confirmation page not reached")
        est = float(page.locator("#confirm-estimate").get_attribute("data-value") or 0)
        prepared = PreparedOrder(request=request, adapter=self.name, estimated_price=px, estimated_cost=est)
        self._staged[request.client_order_id] = prepared
        return prepared

    def submit_order(self, prepared: PreparedOrder) -> ExternalOrder:
        cid = prepared.request.client_order_id
        if cid in self._ledger:  # idempotent: never click confirm twice for the same client id
            return self.get_order_status(self._ledger[cid])
        page = self._ensure_page()
        if cid not in self._staged or not page.locator("#confirm-btn").count():
            raise ExecutionError("order is not staged on the confirmation page; call prepare_order first")
        before = self._before_ids.pop(cid, set())
        try:
            page.click("#confirm-btn")
            page.wait_for_selector("#order-confirmation, #order-error, #login-error", timeout=self.timeout_ms)
        except Exception as exc:
            # Unknown outcome: the click may or may not have registered. Look for a new order before anyone retries.
            found = self._new_order_since(before, prepared.request)
            if found:
                self._ledger[cid] = found
                return self.get_order_status(found)
            raise ExecutionError(f"submission outcome unknown, no new order found: {exc}") from exc
        finally:
            self._staged.pop(cid, None)
        if "/login" in page.url:
            raise SessionExpiredError("session expired during submission")
        if page.locator("#order-error").count():
            oid = page.locator("#order-error").get_attribute("data-order-id") or ""
            msg = page.locator("#order-error").inner_text()
            self._ledger[cid] = oid
            return ExternalOrder(oid, cid, OrderStatus.REJECTED, detail=msg)
        oid = page.locator("#order-confirmation").get_attribute("data-order-id") or ""
        self._ledger[cid] = oid
        ext = self.get_order_status(oid)
        ext.client_order_id = cid
        return ext

    def _read_orders(self) -> list[dict[str, str]]:
        page = self._goto("/orders")
        return page.eval_on_selector_all(
            "tr.order", """els => els.map(e => ({id: e.dataset.orderId, status: e.dataset.status, symbol: e.dataset.symbol,
              side: e.dataset.side, qty: e.dataset.qty, filled: e.dataset.filled, price: e.dataset.price,
              note: e.children[6] ? e.children[6].innerText : ''}))""")

    def _known_order_ids(self) -> set[str]:
        try:
            ids = {o["id"] for o in self._read_orders()}
        except ExecutionError:
            ids = set()
        return ids

    def _new_order_since(self, before: set[str], req: OrderRequest) -> str | None:
        try:
            for o in self._read_orders():
                if o["id"] not in before and o["symbol"] == req.symbol and int(o["qty"]) == req.quantity and o["side"] == req.side.value:
                    return o["id"]
        except ExecutionError:
            pass
        return None

    def get_order_status(self, external_id: str) -> ExternalOrder:
        for o in self._read_orders():
            if o["id"] == external_id:
                cid = next((c for c, e in self._ledger.items() if e == external_id), "")
                return ExternalOrder(
                    external_id, cid, _STATUS.get(o["status"], OrderStatus.PENDING), int(o["filled"] or 0),
                    float(o["price"]) if o["price"] else None, o["note"])
        raise ExecutionError(f"order {external_id} not found on venue")

    def cancel_order(self, external_id: str) -> bool:
        page = self._goto("/orders")
        btn = page.locator(f"tr.order[data-order-id='{external_id}'] .cancel-btn")
        if not btn.count():
            return False
        btn.click()
        page.wait_for_load_state()
        return self.get_order_status(external_id).status == OrderStatus.CANCELED

    def wait_for_terminal(self, external_id: str, timeout_s: float = 10.0, poll_s: float = 0.25) -> ExternalOrder:
        """Poll until the venue reports a final state (used in tests / delayed-execution handling)."""
        deadline = time.monotonic() + timeout_s
        while True:
            ext = self.get_order_status(external_id)
            if ext.status not in (OrderStatus.PENDING, OrderStatus.NEW) or time.monotonic() > deadline:
                return ext
            time.sleep(poll_s)
