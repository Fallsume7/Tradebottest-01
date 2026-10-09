"""Core domain types shared by every layer (orders, positions, trades, market data).

Conventions
-----------
* All datetimes are timezone-aware. Internally everything is UTC; the dashboard
  converts to America/Chicago for display.
* A bar is labelled by its *open* time. A bar labelled ``t`` is only complete (and
  therefore visible to strategies) at ``t + 1 minute``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import pandas as pd

BAR_COLUMNS = ["open", "high", "low", "close", "volume"]


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(StrEnum):
    DAY = "day"
    IOC = "ioc"  # immediate-or-cancel: whatever cannot fill on the first eligible bar is canceled


class OrderStatus(StrEnum):
    NEW = "new"
    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


TERMINAL_STATUSES = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED}
)

_ALLOWED: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.NEW: frozenset({OrderStatus.PENDING, OrderStatus.REJECTED}),
    OrderStatus.PENDING: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.REJECTED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
        }
    ),
}


class InvalidTransition(Exception):
    """Raised when an order is moved through an illegal state transition."""


class FeedKind(StrEnum):
    """What kind of data a feed carries. Never present anything but REALTIME as real-time."""

    SYNTHETIC = "synthetic"
    HISTORICAL = "historical"
    REALTIME = "realtime"
    DELAYED = "delayed"


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(ts: datetime | pd.Timestamp) -> datetime:
    """Return a tz-aware UTC datetime; naive inputs are rejected (timezone bugs are silent killers)."""
    if ts.tzinfo is None:
        raise ValueError("Naive datetime is not allowed; attach a timezone explicitly")
    if isinstance(ts, pd.Timestamp):
        return ts.tz_convert("UTC").to_pydatetime()
    return ts.astimezone(UTC)


@dataclass(slots=True)
class OrderRequest:
    """What a strategy / user asks the broker to do. ``client_order_id`` makes submission idempotent."""

    client_order_id: str
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    strategy: str = "manual"
    reason: str = ""
    # Protective-exit parameters applied to the resulting position on fill
    # (stop_loss_pct, take_profit_pct, trailing_stop_pct, max_hold_minutes, reference).
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Fill:
    order_id: str
    symbol: str
    side: Side
    quantity: int
    price: float
    commission: float
    timestamp: datetime
    note: str = ""


@dataclass(slots=True)
class Order:
    id: str
    client_order_id: str
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType
    limit_price: float | None
    time_in_force: TimeInForce
    strategy: str
    reason: str
    created_at: datetime
    status: OrderStatus = OrderStatus.NEW
    filled_quantity: int = 0
    avg_fill_price: float = 0.0
    commission: float = 0.0
    updated_at: datetime | None = None
    reject_reason: str = ""
    # Orders only become eligible to fill on bars opening at/after this time (models latency).
    eligible_at: datetime | None = None
    expires_at: datetime | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    history: list[tuple[datetime, str, str]] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return self.quantity - self.filled_quantity

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def transition(self, new: OrderStatus, when: datetime, note: str = "") -> None:
        allowed = _ALLOWED.get(self.status, frozenset())
        if new not in allowed:
            raise InvalidTransition(f"{self.id}: {self.status} -> {new} is not allowed")
        self.status = new
        self.updated_at = when
        self.history.append((when, str(new), note))

    def apply_fill(self, qty: int, price: float, commission: float) -> None:
        total = self.avg_fill_price * self.filled_quantity + price * qty
        self.filled_quantity += qty
        self.avg_fill_price = total / self.filled_quantity
        self.commission += commission


@dataclass(slots=True)
class Position:
    symbol: str
    quantity: int
    avg_cost: float
    opened_at: datetime
    strategy: str = "manual"
    entry_reason: str = ""
    stop_price: float | None = None
    take_profit_price: float | None = None
    trailing_stop_pct: float | None = None
    high_water_mark: float = 0.0
    max_hold_minutes: int | None = None
    # Round-trip accounting (supports partial exits and scale-in)
    total_bought_qty: int = 0
    total_bought_cost: float = 0.0
    total_sold_qty: int = 0
    total_sold_proceeds: float = 0.0
    commissions: float = 0.0
    pending_exit_reason: str = ""
    reference: dict[str, float] = field(default_factory=dict)

    def market_value(self, mark: float) -> float:
        return self.quantity * mark

    def unrealized(self, mark: float) -> float:
        return self.quantity * (mark - self.avg_cost)


@dataclass(slots=True)
class Trade:
    """A completed round trip (entry fully closed)."""

    id: str
    symbol: str
    strategy: str
    quantity: int
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    gross_pnl: float
    commissions: float
    net_pnl: float
    return_pct: float
    hold_minutes: float
    entry_reason: str
    exit_reason: str
    session_date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "strategy": self.strategy,
            "quantity": self.quantity,
            "entry_time": self.entry_time.isoformat(),
            "entry_price": round(self.entry_price, 4),
            "exit_time": self.exit_time.isoformat(),
            "exit_price": round(self.exit_price, 4),
            "gross_pnl": round(self.gross_pnl, 2),
            "commissions": round(self.commissions, 2),
            "net_pnl": round(self.net_pnl, 2),
            "return_pct": round(self.return_pct, 4),
            "hold_minutes": round(self.hold_minutes, 1),
            "entry_reason": self.entry_reason,
            "exit_reason": self.exit_reason,
            "session_date": self.session_date,
        }


# --------------------------------------------------------------------------- market data


@dataclass(slots=True)
class StockMeta:
    """Static-ish per-symbol facts. Any ``None`` means *unavailable* (never guessed)."""

    symbol: str
    name: str = ""
    prev_close: float | None = None
    avg_daily_volume: float | None = None
    market_cap: float | None = None
    float_shares: float | None = None
    # Typical quoted spread in basis points. Only synthetic/live-quote providers set this.
    typical_spread_bps: float | None = None


@dataclass(slots=True)
class Quote:
    bid: float
    ask: float
    timestamp: datetime

    @property
    def spread_pct(self) -> float:
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 100 if mid > 0 else float("inf")


@dataclass(slots=True)
class SessionInfo:
    """One trading session on the exchange calendar (all UTC)."""

    date: str  # YYYY-MM-DD in the exchange's local date
    open_utc: datetime
    close_utc: datetime
    premarket_open_utc: datetime
    is_early_close: bool = False


@dataclass(slots=True)
class FeedInfo:
    kind: FeedKind
    provider: str
    label: str
    delay_seconds: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "provider": self.provider,
            "label": self.label,
            "delay_seconds": self.delay_seconds,
            "warnings": list(self.warnings),
            "is_realtime": self.kind == FeedKind.REALTIME,
        }


@dataclass(slots=True)
class StockContext:
    """Everything known about one symbol at ``as_of`` - and nothing from the future.

    ``bars`` contains only *complete* 1-minute bars (label + 1min <= as_of), UTC-indexed,
    covering premarket and regular hours for the session.
    """

    meta: StockMeta
    bars: pd.DataFrame
    as_of: datetime
    session: SessionInfo
    quote: Quote | None = None
    halted: bool = False
    # Seconds between ``as_of`` and the newest data point we actually have for this symbol.
    data_age_seconds: float = 0.0
    feed: FeedInfo | None = None
    # Optional precomputed numpy mirror of the *full* underlying frame (prefix of length len(bars) is visible).
    arrays: Any = None

    @property
    def symbol(self) -> str:
        return self.meta.symbol

    @property
    def last_price(self) -> float | None:
        n = len(self.bars)
        if n == 0:
            return None
        if self.arrays is not None:
            return float(self.arrays.c[n - 1])
        return float(self.bars["close"].iloc[-1])
