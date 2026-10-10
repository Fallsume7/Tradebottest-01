"""MarketWatch Virtual Stock Exchange adapter (PLAY money) that drives YOUR already-logged-in Chrome.

How it works
------------
* You start a dedicated Chrome profile with ``scripts\\marketwatch_chrome.ps1`` and log in yourself. The bot
  attaches to that browser over the local DevTools port (``connect_over_cdp``, localhost only). It never sees,
  asks for, or stores your password, and it never logs in: if the session is gone it stops and tells you.
* Every page it touches must be on ``marketwatch.com``; anything else is refused.
* A CAPTCHA / "are you a robot" page makes it stop (``ChallengeError``); a human resolves it. Nothing here tries
  to get around bot detection.
* Rollout stages (``MWBOT_MARKETWATCH_STAGE``): ``read_only`` (reads only; orders are skipped),
  ``prepare_only`` (fills the order form and takes a screenshot, never confirms), ``live`` (confirms orders).
* Orders are idempotent on ``client_order_id`` (persisted ledger) and a submission with an unknown outcome is
  looked up on the orders page before anything can be retried.

What is NOT here: the MarketWatch-specific page knowledge. That lives behind :class:`MarketWatchPages` and is
supplied by ``marketwatch_pages.py`` once it has been built and tested against pages YOU saved
(``docs/MARKETWATCH_AUTOMATION.md``). Until then :class:`UncalibratedPages` refuses to guess.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

from mwbot.domain import OrderRequest, OrderStatus, OrderType, Side
from mwbot.execution.base import (
    ExecutionAdapter,
    ExecutionError,
    ExternalOrder,
    Holding,
    MirrorSkipped,
    PreparedOrder,
    SessionExpiredError,
    SessionStatus,
)
from mwbot.execution.worker import on_worker

log = logging.getLogger(__name__)

STAGES = ("read_only", "prepare_only", "live")


class NotCalibratedError(ExecutionError):
    """The MarketWatch page layout has not been learned yet (needs your saved pages)."""


class ChallengeError(ExecutionError):
    """MarketWatch is asking a human to prove they are one (CAPTCHA etc.). The bot stops; it never tries to pass."""


class VenueRejected(ExecutionError):  # noqa: N818
    """MarketWatch refused the order with a message (insufficient cash, market closed, ...)."""


@dataclass(slots=True)
class VenueOrder:
    order_id: str
    symbol: str
    side: Side
    quantity: int
    status: OrderStatus
    filled_quantity: int = 0
    avg_price: float | None = None
    note: str = ""


@dataclass(slots=True)
class StagedQuote:
    price: float | None
    cost: float | None
    warnings: list[str] = field(default_factory=list)


class MarketWatchPages(ABC):
    """Everything that depends on how MarketWatch's pages look. Replace this when the site changes."""

    @abstractmethod
    def is_logged_in(self, page: Any) -> bool: ...

    @abstractmethod
    def challenge(self, page: Any) -> str | None:
        """Return a description if the page is a CAPTCHA / bot check, else None."""

    @abstractmethod
    def open_portfolio(self, page: Any, game_url: str) -> None: ...

    @abstractmethod
    def read_buying_power(self, page: Any) -> float: ...

    @abstractmethod
    def read_holdings(self, page: Any) -> list[Holding]: ...

    @abstractmethod
    def read_orders(self, page: Any, game_url: str) -> list[VenueOrder]: ...

    @abstractmethod
    def stage_order(self, page: Any, game_url: str, request: OrderRequest) -> StagedQuote:
        """Fill the trade form up to, NOT including, the final confirmation."""

    @abstractmethod
    def click_confirm(self, page: Any) -> str:
        """Press the final confirmation. Return the venue order id if the page shows one, else ''.

        Raise :class:`VenueRejected` when the site shows a refusal message.
        """

    @abstractmethod
    def cancel(self, page: Any, game_url: str, order_id: str) -> bool: ...


class UncalibratedPages(MarketWatchPages):
    _MSG = (
        "MarketWatch page layout not calibrated yet. Save your game's pages into marketwatch_snapshots\\ "
        "and see docs/MARKETWATCH_AUTOMATION.md - the bot will not guess at a page it has never seen."
    )

    def _no(self) -> NotCalibratedError:
        return NotCalibratedError(self._MSG)

    def is_logged_in(self, page: Any) -> bool:
        raise self._no()

    def challenge(self, page: Any) -> str | None:
        raise self._no()

    def open_portfolio(self, page: Any, game_url: str) -> None:
        raise self._no()

    def read_buying_power(self, page: Any) -> float:
        raise self._no()

    def read_holdings(self, page: Any) -> list[Holding]:
        raise self._no()

    def read_orders(self, page: Any, game_url: str) -> list[VenueOrder]:
        raise self._no()

    def stage_order(self, page: Any, game_url: str, request: OrderRequest) -> StagedQuote:
        raise self._no()

    def click_confirm(self, page: Any) -> str:
        raise self._no()

    def cancel(self, page: Any, game_url: str, order_id: str) -> bool:
        raise self._no()


class OrderLedger:
    """client_order_id -> venue order id, persisted so a restart can never re-send an order."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}
        if path is not None and path.exists():
            try:
                self._data = {str(k): str(v) for k, v in json.loads(path.read_text("utf-8")).items()}
            except (OSError, ValueError) as exc:
                # An unreadable ledger must stop live trading, not silently start empty.
                raise ExecutionError(f"order ledger {path} is unreadable ({exc}); fix or move it") from exc

    def get(self, client_id: str) -> str | None:
        with self._lock:
            return self._data.get(client_id)

    def items(self) -> dict[str, str]:
        with self._lock:
            return dict(self._data)

    def set(self, client_id: str, external_id: str) -> None:
        with self._lock:
            self._data[client_id] = external_id
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._data, indent=1), "utf-8")
                os.replace(tmp, self.path)


def is_marketwatch_url(url: str) -> bool:
    u = urlparse(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and (host == "marketwatch.com" or host.endswith(".marketwatch.com"))


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)[:60]


class MarketWatchAdapter(ExecutionAdapter):
    name: ClassVar[str] = "marketwatch"
    is_real_money: ClassVar[bool] = False  # MarketWatch Virtual Stock Exchange is a play-money game
    requires_real_feed: ClassVar[bool] = True  # orders must be based on real prices, never synthetic ones

    def __init__(
        self,
        game_url: str,
        *,
        cdp_url: str = "http://127.0.0.1:9222",
        stage: str = "read_only",
        pages: MarketWatchPages | None = None,
        ledger_path: Path | None = None,
        shots_dir: Path | None = None,
        min_action_gap_s: float = 2.0,
        timeout_ms: int = 15000,
        page_provider: Callable[[], Any] | None = None,
    ) -> None:
        if stage not in STAGES:
            raise ExecutionError(f"unknown stage {stage!r}; use one of {STAGES}")
        if not is_marketwatch_url(game_url):
            raise ExecutionError("MWBOT_MARKETWATCH_GAME_URL must be an https://...marketwatch.com address")
        if (urlparse(cdp_url).hostname or "") not in {"127.0.0.1", "localhost", "::1"}:
            raise ExecutionError("the Chrome debugging endpoint must be on this computer")
        self.game_url = game_url
        self.cdp_url = cdp_url
        self.stage = stage
        self.pages = pages or UncalibratedPages()
        self.ledger = OrderLedger(ledger_path)
        self.shots_dir = shots_dir
        self.min_action_gap_s = max(min_action_gap_s, 0.0)
        self.timeout_ms = timeout_ms
        self._page_provider = page_provider
        self._pw: Any = None
        self._page: Any = None
        self._last_action = 0.0
        self._staged: dict[str, PreparedOrder] = {}
        self._before_ids: dict[str, set[str]] = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="marketwatch")
        self._worker_ident: int = self._pool.submit(threading.get_ident).result()

    # ------------------------------------------------------------------ plumbing
    def _pace(self) -> None:
        wait = self.min_action_gap_s - (time.monotonic() - self._last_action)
        if wait > 0:
            time.sleep(wait)
        self._last_action = time.monotonic()

    def _ensure_page(self) -> Any:
        if self._page is not None:
            return self._page
        if self._page_provider is not None:
            self._page = self._page_provider()
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise ExecutionError("Playwright is not installed: pip install -e 'backend[dev]'") from exc
        self._pw = sync_playwright().start()
        try:
            browser = self._pw.chromium.connect_over_cdp(self.cdp_url, timeout=self.timeout_ms)
        except Exception as exc:
            self._pw.stop()
            self._pw = None
            raise ExecutionError(
                f"cannot reach your MarketWatch Chrome at {self.cdp_url}: start it with "
                f"scripts\\marketwatch_chrome.ps1 and log in ({exc})"
            ) from exc
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        ctx.set_default_timeout(self.timeout_ms)
        page = next((p for p in ctx.pages if is_marketwatch_url(p.url)), None)
        self._page = page or ctx.new_page()
        return self._page

    def close(self) -> None:
        """Disconnect from Chrome. The browser itself (and your login) is left running."""
        if threading.get_ident() != self._worker_ident:
            try:
                self._pool.submit(self._disconnect).result(timeout=15)
            finally:
                self._pool.shutdown(wait=False)
            return
        self._disconnect()

    def _disconnect(self) -> None:
        try:
            if self._pw is not None:
                self._pw.stop()  # drops the DevTools connection; does not close Chrome
        except Exception:  # pragma: no cover - best effort
            pass
        self._page = self._pw = None

    def _goto(self, url: str) -> Any:
        if not is_marketwatch_url(url):
            raise ExecutionError(f"refusing to open a non-MarketWatch address: {url}")
        page = self._ensure_page()
        self._pace()
        try:
            page.goto(url)
        except Exception as exc:
            raise ExecutionError(f"navigation failed: {exc}") from exc
        return self._checked(page)

    def _checked(self, page: Any) -> Any:
        if not is_marketwatch_url(page.url or ""):
            raise ExecutionError(f"browser is not on MarketWatch ({page.url!r}); not acting")
        why = self.pages.challenge(page)
        if why:
            self._shot("challenge")
            raise ChallengeError(
                f"MarketWatch is asking for a human check ({why}). Solve it in Chrome, then resume."
            )
        if not self.pages.is_logged_in(page):
            self._shot("logged_out")
            raise SessionExpiredError(
                "not logged in to MarketWatch - log in again in the bot's Chrome window"
            )
        return page

    def _shot(self, label: str) -> str | None:
        if self.shots_dir is None or self._page is None:
            return None
        try:
            self.shots_dir.mkdir(parents=True, exist_ok=True)
            path = self.shots_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(label)}.png"
            self._page.screenshot(path=str(path))
            return str(path)
        except Exception:  # pragma: no cover - a screenshot must never break trading logic
            return None

    # ------------------------------------------------------------------ session + reads
    @on_worker
    def check_session(self) -> SessionStatus:
        try:
            self._goto(self.game_url)
            return SessionStatus(self.name, True, None, f"logged in; stage={self.stage}")
        except SessionExpiredError as exc:
            return SessionStatus(self.name, False, None, str(exc))
        except ExecutionError as exc:
            return SessionStatus(self.name, False, None, str(exc))

    @on_worker
    def get_buying_power(self) -> float:
        page = self._goto(self.game_url)
        self.pages.open_portfolio(page, self.game_url)
        return float(self.pages.read_buying_power(self._checked(page)))

    @on_worker
    def get_holdings(self) -> list[Holding]:
        page = self._goto(self.game_url)
        self.pages.open_portfolio(page, self.game_url)
        return list(self.pages.read_holdings(self._checked(page)))

    def _read_orders(self) -> list[VenueOrder]:
        page = self._goto(self.game_url)
        return list(self.pages.read_orders(page, self.game_url))

    def _known_order_ids(self) -> set[str]:
        return {o.order_id for o in self._read_orders()}

    # ------------------------------------------------------------------ orders
    @on_worker
    def prepare_order(self, request: OrderRequest) -> PreparedOrder:
        if self.stage == "read_only":
            raise MirrorSkipped("read-only stage: order was not sent to MarketWatch")
        self._validate(request)
        cid = request.client_order_id
        if self.ledger.get(cid) is None:
            # Needed to find an order whose submission outcome is later unknown
            self._before_ids[cid] = self._known_order_ids()
        page = self._goto(self.game_url)
        quote = self.pages.stage_order(page, self.game_url, request)
        shot = self._shot(f"prepared-{request.side.value}-{request.symbol}")
        warnings = list(quote.warnings) + ([f"screenshot: {shot}"] if shot else [])
        prepared = PreparedOrder(request, self.name, quote.price, quote.cost, warnings)
        self._staged[cid] = prepared
        return prepared

    @staticmethod
    def _validate(req: OrderRequest) -> None:
        if req.quantity < 1:
            raise ExecutionError("quantity must be at least 1")
        if req.side not in (Side.BUY, Side.SELL):
            raise ExecutionError(f"unsupported side {req.side!r} (long-only)")
        if req.order_type == OrderType.LIMIT and not req.limit_price:
            raise ExecutionError("limit order without a limit price")

    @on_worker
    def submit_order(self, prepared: PreparedOrder) -> ExternalOrder:
        req = prepared.request
        cid = req.client_order_id
        known = self.ledger.get(cid)
        if known is not None:  # idempotent: never confirm the same client id twice, even after a restart
            return self.get_order_status(known)
        if self.stage != "live":
            self._staged.pop(cid, None)
            raise MirrorSkipped("prepare-only stage: form filled and photographed, NOT submitted")
        if cid not in self._staged:
            raise ExecutionError("order is not staged; call prepare_order first")
        page = self._checked(self._ensure_page())
        before = self._before_ids.pop(cid, set())
        self._pace()
        try:
            oid = self.pages.click_confirm(page)
        except VenueRejected as exc:
            self._shot(f"rejected-{req.symbol}")
            self.ledger.set(cid, f"rejected:{cid}")
            return ExternalOrder(f"rejected:{cid}", cid, OrderStatus.REJECTED, detail=str(exc))
        except (SessionExpiredError, ChallengeError):
            raise
        except Exception as exc:
            # Unknown outcome: the click may or may not have registered. Look before anyone retries.
            found = self._find_new_order(before, req)
            if found:
                self.ledger.set(cid, found)
                return self.get_order_status(found)
            self._shot(f"unknown-{req.symbol}")
            raise ExecutionError(
                f"submission outcome UNKNOWN and no new order found on MarketWatch ({exc}). "
                "Check your game by hand before the bot places anything else."
            ) from exc
        finally:
            self._staged.pop(cid, None)
        if not oid:
            oid = self._find_new_order(before, req) or ""
        if not oid:
            raise ExecutionError(
                "order confirmed but its id could not be read back; check MarketWatch before continuing"
            )
        self.ledger.set(cid, oid)
        ext = self.get_order_status(oid)
        ext.client_order_id = cid
        return ext

    def _find_new_order(self, before: set[str], req: OrderRequest) -> str | None:
        try:
            for o in self._read_orders():
                if (
                    o.order_id not in before
                    and o.symbol == req.symbol
                    and o.quantity == req.quantity
                    and o.side == req.side
                ):
                    return o.order_id
        except ExecutionError:
            pass
        return None

    @on_worker
    def get_order_status(self, external_id: str) -> ExternalOrder:
        if external_id.startswith("rejected:"):
            return ExternalOrder(external_id, external_id.split(":", 1)[1], OrderStatus.REJECTED)
        for o in self._read_orders():
            if o.order_id == external_id:
                cid = next((c for c, e in self.ledger.items().items() if e == external_id), "")
                return ExternalOrder(external_id, cid, o.status, o.filled_quantity, o.avg_price, o.note)
        raise ExecutionError(f"order {external_id} not found on MarketWatch")

    @on_worker
    def cancel_order(self, external_id: str) -> bool:
        if self.stage != "live":
            raise MirrorSkipped(f"{self.stage} stage: cancel not sent")
        page = self._goto(self.game_url)
        ok = bool(self.pages.cancel(page, self.game_url, external_id))
        return ok and self.get_order_status(external_id).status == OrderStatus.CANCELED
