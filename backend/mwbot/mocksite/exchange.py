"""In-memory fake virtual-trading venue used ONLY to test the browser adapter.

It imitates a *generic* virtual-stock-game site (login, balance, ticker search, buy/sell form,
confirmation step, portfolio, pending/completed orders). It is NOT MarketWatch and copies none of
its markup. Failure injection (delays, rejections, expired session, market closed) lets tests
exercise unhappy paths deterministically.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class MockOrder:
    id: str
    symbol: str
    side: str
    quantity: int
    order_type: str
    limit_price: float | None
    created: float
    status: str = "pending"  # pending | filled | canceled | rejected
    fill_price: float | None = None
    filled_quantity: int = 0
    message: str = ""


@dataclass
class MockConfig:
    exec_delay_seconds: float = 0.0
    fail_next_order: bool = False
    market_open: bool = True
    reject_symbols: set[str] = field(default_factory=set)
    expire_session_after_orders: int | None = None  # simulate session timeout after N orders


class MockExchange:
    USERS = {"demo": "demo"}

    def __init__(self, clock: Callable[[], float] = time.monotonic, starting_cash: float = 100_000.0) -> None:
        self.clock = clock
        self.lock = threading.RLock()
        self.starting_cash = starting_cash
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.cash = self.starting_cash
            self.holdings: dict[str, dict[str, float]] = {}  # symbol -> {qty, cost}
            self.prices: dict[str, float] = {
                "ZNTR": 5.00,
                "QBLT": 12.40,
                "VYRO": 2.15,
                "KLMX": 0.85,
                "PRXL": 48.20,
            }
            self.orders: dict[str, MockOrder] = {}
            self.sessions: set[str] = set()
            self.config = MockConfig()
            self._seq = 1000
            self._orders_placed = 0

    # ------------------------------------------------------------------ auth
    def login(self, user: str, password: str) -> str | None:
        if self.USERS.get(user) == password:
            token = f"tok-{len(self.sessions) + 1}-{int(self.clock() * 1000)}"
            with self.lock:
                self.sessions.add(token)
            return token
        return None

    def valid(self, token: str | None) -> bool:
        return bool(token and token in self.sessions)

    def logout(self, token: str | None) -> None:
        with self.lock:
            self.sessions.discard(token or "")

    def expire_all_sessions(self) -> None:
        with self.lock:
            self.sessions.clear()

    # ------------------------------------------------------------------ market
    def quote(self, symbol: str) -> float | None:
        return self.prices.get(symbol.upper())

    def set_prices(self, prices: dict[str, float]) -> None:
        with self.lock:
            self.prices.update({k.upper(): float(v) for k, v in prices.items()})
            self._match()

    # ------------------------------------------------------------------ orders
    def validate(self, symbol: str, side: str, qty: int, order_type: str, limit: float | None) -> str | None:
        symbol = symbol.upper()
        if not self.config.market_open:
            return "Market is closed. Orders cannot be placed right now."
        if symbol not in self.prices:
            return f"Unknown ticker {symbol!r}."
        if symbol in self.config.reject_symbols:
            return f"{symbol} is not available for trading in this game."
        if qty < 1:
            return "Quantity must be at least 1 share."
        if order_type == "limit" and (limit is None or limit <= 0):
            return "Limit orders need a positive limit price."
        px = limit if order_type == "limit" and limit else self.prices[symbol]
        if side == "buy" and qty * px > self.available_cash() + 1e-9:
            return "Insufficient buying power for this order."
        if side == "sell" and qty > self.holdings.get(symbol, {}).get("qty", 0) - self._pending_sell_qty(
            symbol
        ):
            return "You do not own enough shares to sell this quantity."
        return None

    def _pending_sell_qty(self, symbol: str) -> int:
        return sum(
            o.quantity
            for o in self.orders.values()
            if o.status == "pending" and o.side == "sell" and o.symbol == symbol
        )

    def available_cash(self) -> float:
        reserved = sum(
            o.quantity * (o.limit_price or self.prices.get(o.symbol, 0))
            for o in self.orders.values()
            if o.status == "pending" and o.side == "buy"
        )
        return self.cash - reserved

    def place(self, symbol: str, side: str, qty: int, order_type: str, limit: float | None) -> MockOrder:
        with self.lock:
            err = self.validate(symbol, side, qty, order_type, limit)
            self._seq += 1
            o = MockOrder(f"MW-{self._seq}", symbol.upper(), side, qty, order_type, limit, self.clock())
            if err is None and self.config.fail_next_order:
                self.config.fail_next_order = False
                err = "Order service temporarily unavailable. Please try again."
            if err:
                o.status, o.message = "rejected", err
            self.orders[o.id] = o
            self._orders_placed += 1
            if (
                self.config.expire_session_after_orders is not None
                and self._orders_placed >= self.config.expire_session_after_orders
            ):
                self.expire_all_sessions()
            self._match()
            return o

    def cancel(self, order_id: str) -> bool:
        with self.lock:
            self._match()
            o = self.orders.get(order_id)
            if o and o.status == "pending":
                o.status, o.message = "canceled", "Canceled by user"
                return True
            return False

    def _match(self) -> None:
        now = self.clock()
        for o in self.orders.values():
            if o.status != "pending" or now - o.created < self.config.exec_delay_seconds:
                continue
            px = self.prices[o.symbol]
            if o.order_type == "limit" and o.limit_price is not None:
                if (o.side == "buy" and px > o.limit_price) or (o.side == "sell" and px < o.limit_price):
                    continue
                px = o.limit_price
            if o.side == "buy":
                if o.quantity * px > self.cash + 1e-9:
                    o.status, o.message = "rejected", "Insufficient funds at execution"
                    continue
                self.cash -= o.quantity * px
                h = self.holdings.setdefault(o.symbol, {"qty": 0, "cost": 0.0})
                h["qty"] += o.quantity
                h["cost"] += o.quantity * px
            else:
                held = self.holdings.get(o.symbol)
                if not held or held["qty"] < o.quantity:
                    o.status, o.message = "rejected", "Insufficient shares at execution"
                    continue
                avg = held["cost"] / held["qty"]
                held["qty"] -= o.quantity
                held["cost"] -= avg * o.quantity
                self.cash += o.quantity * px
                if held["qty"] == 0:
                    del self.holdings[o.symbol]
            o.status, o.fill_price, o.filled_quantity = "filled", px, o.quantity

    def snapshot(self) -> dict[str, float]:
        with self.lock:
            self._match()
            value = sum(h["qty"] * self.prices[s] for s, h in self.holdings.items())
            return {
                "cash": self.cash,
                "holdings_value": value,
                "total": self.cash + value,
                "buying_power": self.available_cash(),
            }
