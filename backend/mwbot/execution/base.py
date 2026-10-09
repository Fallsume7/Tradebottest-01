"""Execution adapter interface.

An adapter is the *only* thing that turns an order request into an order somewhere. The
bot's decisions are venue-agnostic; adapters are swappable:

* ``PaperBroker``      - local virtual engine (default, fully functional)
* ``MockBrowserAdapter`` - Playwright against the bundled mock trading site (testing)
* ``MarketWatchAdapter`` - skeleton for a future, *authorized* integration (does nothing yet)

Safety invariant: every adapter in this repository is virtual. ``is_real_money`` is False and the
factory refuses anything else (see ``mwbot.execution.factory``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar

from mwbot.domain import OrderRequest, OrderStatus, utcnow


class ExecutionError(Exception):
    """Order could not be prepared/submitted (venue error, validation failure, timeout...)."""


class SessionExpiredError(ExecutionError):
    """The external session is no longer authenticated."""


@dataclass(slots=True)
class SessionStatus:
    adapter: str
    logged_in: bool
    market_open: bool | None
    detail: str = ""
    is_real_money: bool = False


@dataclass(slots=True)
class Holding:
    symbol: str
    quantity: int
    avg_cost: float | None = None
    last_price: float | None = None


@dataclass(slots=True)
class PreparedOrder:
    """An order that has been validated and (for browser venues) filled into a form, but not sent."""

    request: OrderRequest
    adapter: str
    estimated_price: float | None
    estimated_cost: float | None
    warnings: list[str] = field(default_factory=list)
    prepared_at: datetime = field(default_factory=utcnow)


@dataclass(slots=True)
class ExternalOrder:
    external_id: str
    client_order_id: str
    status: OrderStatus
    filled_quantity: int = 0
    avg_fill_price: float | None = None
    detail: str = ""


@dataclass(slots=True)
class ReconciliationReport:
    ok: bool
    matched: list[str] = field(default_factory=list)
    missing_externally: list[str] = field(default_factory=list)  # we think we hold it; venue doesn't
    missing_internally: list[str] = field(default_factory=list)  # venue holds it; we don't know
    quantity_mismatches: list[tuple[str, int, int]] = field(
        default_factory=list
    )  # symbol, internal, external

    def summary(self) -> str:
        if self.ok:
            return f"reconciled: {len(self.matched)} positions match"
        parts = []
        if self.missing_externally:
            parts.append(f"missing at venue: {self.missing_externally}")
        if self.missing_internally:
            parts.append(f"unknown to bot: {self.missing_internally}")
        if self.quantity_mismatches:
            parts.append(f"qty mismatches: {self.quantity_mismatches}")
        return "; ".join(parts)


def reconcile_holdings(internal: list[Holding], external: list[Holding]) -> ReconciliationReport:
    i = {h.symbol: h.quantity for h in internal if h.quantity}
    e = {h.symbol: h.quantity for h in external if h.quantity}
    rep = ReconciliationReport(ok=True)
    for sym in sorted(set(i) | set(e)):
        if sym in i and sym not in e:
            rep.missing_externally.append(sym)
        elif sym in e and sym not in i:
            rep.missing_internally.append(sym)
        elif i[sym] != e[sym]:
            rep.quantity_mismatches.append((sym, i[sym], e[sym]))
        else:
            rep.matched.append(sym)
    rep.ok = not (rep.missing_externally or rep.missing_internally or rep.quantity_mismatches)
    return rep


class ExecutionAdapter(ABC):
    name: ClassVar[str]
    is_real_money: ClassVar[bool] = False

    @abstractmethod
    def check_session(self) -> SessionStatus: ...

    @abstractmethod
    def get_buying_power(self) -> float: ...

    @abstractmethod
    def get_holdings(self) -> list[Holding]: ...

    @abstractmethod
    def prepare_order(self, request: OrderRequest) -> PreparedOrder:
        """Validate and stage an order WITHOUT sending it."""

    @abstractmethod
    def submit_order(self, prepared: PreparedOrder) -> ExternalOrder:
        """Send a prepared order. Must be idempotent on ``request.client_order_id``."""

    @abstractmethod
    def get_order_status(self, external_id: str) -> ExternalOrder: ...

    @abstractmethod
    def cancel_order(self, external_id: str) -> bool:
        """Cancel a pending order. Returns False if it is no longer cancellable."""

    def reconcile(self, internal: list[Holding]) -> ReconciliationReport:
        return reconcile_holdings(internal, self.get_holdings())

    def close(self) -> None:  # noqa: B027 - optional hook
        pass
