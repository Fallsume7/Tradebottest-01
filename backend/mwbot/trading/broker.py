"""Local paper-trading engine: orders, fills, positions, cash, round-trip trade ledger.

This is a *virtual* broker. It can never reach a real brokerage: it has no network code and
``is_real_money`` is False. It implements the ``ExecutionAdapter`` interface so the rest of the
bot treats it like any other venue.

Accounting is a long-only cash account (no shorting, no margin):
    equity        = cash + sum(qty * mark)
    buying_power  = cash - reserved cash for pending buy orders
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, ClassVar, Protocol

from mwbot.config import CostConfig
from mwbot.data.base import MarketDataSource
from mwbot.domain import (
    Fill,
    InvalidTransition,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Trade,
    utcnow,
)
from mwbot.execution.base import (
    ExecutionAdapter,
    ExecutionError,
    ExternalOrder,
    Holding,
    PreparedOrder,
    SessionStatus,
)
from mwbot.trading.fills import try_fill

log = logging.getLogger(__name__)
ONE_MIN = timedelta(minutes=1)


class BrokerStore(Protocol):
    def persist(self, broker: PaperBroker) -> None: ...


def _ceil_minute(ts: datetime) -> datetime:
    floor = ts.replace(second=0, microsecond=0)
    return floor if floor == ts else floor + ONE_MIN


class PaperBroker(ExecutionAdapter):
    name: ClassVar[str] = "paper"
    is_real_money: ClassVar[bool] = False

    def __init__(
        self,
        starting_cash: float,
        costs: CostConfig | None = None,
        *,
        clock: Callable[[], datetime] = utcnow,
        store: BrokerStore | None = None,
    ) -> None:
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.costs = costs or CostConfig()
        self.clock = clock
        self.store = store
        self.orders: dict[str, Order] = {}
        self._by_client: dict[str, str] = {}
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []
        self.trades: list[Trade] = []
        self.marks: dict[str, float] = {}
        self.realized_pnl = 0.0
        self.commissions_paid = 0.0
        self.session_date = ""
        self._counter = 0
        self._trade_counter = 0
        self._cursor: dict[str, datetime] = {}
        self._events: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ accounting
    def equity(self) -> float:
        return self.cash + sum(p.quantity * self.marks.get(p.symbol, p.avg_cost) for p in self.positions.values())

    def reserved_cash(self) -> float:
        total = 0.0
        for o in self.orders.values():
            if o.side == Side.BUY and not o.is_terminal:
                total += o.remaining * float(o.meta.get("ref_price", o.limit_price or 0.0))
        return total

    def buying_power(self) -> float:
        return max(self.cash - self.reserved_cash(), 0.0)

    def unrealized_pnl(self) -> float:
        return sum(p.quantity * (self.marks.get(p.symbol, p.avg_cost) - p.avg_cost) for p in self.positions.values())

    def pending_sell_qty(self, symbol: str) -> int:
        return sum(o.remaining for o in self.orders.values() if o.side == Side.SELL and o.symbol == symbol and not o.is_terminal)

    def pending_buy_symbols(self) -> set[str]:
        return {o.symbol for o in self.orders.values() if o.side == Side.BUY and not o.is_terminal}

    def open_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if not o.is_terminal]

    def drain_events(self) -> list[dict[str, Any]]:
        ev, self._events = self._events, []
        return ev

    def _emit(self, kind: str, when: datetime, **data: Any) -> None:
        self._events.append({"kind": kind, "ts": when.isoformat(), **data})

    def _persist(self) -> None:
        if self.store:
            self.store.persist(self)

    # ------------------------------------------------------------------ marks
    def mark(self, symbol: str, last: float, high: float | None = None) -> None:
        if last > 0 and math.isfinite(last):
            self.marks[symbol] = last
        pos = self.positions.get(symbol)
        if pos is not None:
            ref = max(last, high or 0.0)
            pos.high_water_mark = max(pos.high_water_mark, ref)

    # ------------------------------------------------------------------ order entry
    def _next_id(self) -> str:
        self._counter += 1
        return f"ORD-{self._counter:06d}"

    def _reject(self, req: OrderRequest, now: datetime, reason: str) -> Order:
        o = self._build(req, now)
        o.reject_reason = reason
        o.transition(OrderStatus.REJECTED, now, reason)
        self.orders[o.id] = o
        self._by_client[req.client_order_id] = o.id
        self._emit("order_rejected", now, order_id=o.id, symbol=req.symbol, reason=reason)
        log.warning("order rejected", extra={"symbol": req.symbol, "reason": reason})
        self._persist()
        return o

    def _build(self, req: OrderRequest, now: datetime) -> Order:
        return Order(
            id=self._next_id(), client_order_id=req.client_order_id, symbol=req.symbol, side=req.side,
            quantity=req.quantity, order_type=req.order_type, limit_price=req.limit_price,
            time_in_force=req.time_in_force, strategy=req.strategy, reason=req.reason, created_at=now,
            meta={**req.meta, "session": self.session_date},
        )

    def submit(self, req: OrderRequest, now: datetime | None = None, expires_at: datetime | None = None) -> Order:
        """Submit an order. Idempotent: the same ``client_order_id`` returns the original order."""
        now = now or self.clock()
        existing = self._by_client.get(req.client_order_id)
        if existing:
            return self.orders[existing]
        if req.quantity < 1 or int(req.quantity) != req.quantity:
            return self._reject(req, now, "quantity must be a positive whole number of shares")
        if req.order_type == OrderType.LIMIT and (req.limit_price is None or req.limit_price <= 0):
            return self._reject(req, now, "limit order needs a positive limit price")
        ref = req.limit_price or self.marks.get(req.symbol)
        if req.side == Side.SELL:
            pos = self.positions.get(req.symbol)
            held = pos.quantity if pos else 0
            if req.quantity > held - self.pending_sell_qty(req.symbol):
                return self._reject(req, now, f"cannot sell {req.quantity} {req.symbol}: only {held - self.pending_sell_qty(req.symbol)} available (no shorting)")
        else:
            if ref is None:
                return self._reject(req, now, "no reference price for sizing buying power")
            need = req.quantity * ref * (1.0 if req.order_type == OrderType.LIMIT else 1.005)
            if need > self.buying_power() + 1e-9:
                return self._reject(req, now, f"insufficient buying power: need ${need:,.2f}, have ${self.buying_power():,.2f}")
        o = self._build(req, now)
        o.meta["ref_price"] = float(ref) if ref is not None else 0.0
        o.eligible_at = now + timedelta(seconds=self.costs.latency_seconds)
        o.expires_at = expires_at
        o.transition(OrderStatus.PENDING, now, "accepted")
        self.orders[o.id] = o
        self._by_client[req.client_order_id] = o.id
        self._cursor[o.id] = _ceil_minute(o.eligible_at)
        self._emit("order_submitted", now, order_id=o.id, symbol=o.symbol, side=str(o.side),
                   quantity=o.quantity, strategy=o.strategy, reason=o.reason)
        self._persist()
        return o

    def cancel(self, order_id: str, now: datetime | None = None) -> bool:
        now = now or self.clock()
        o = self.orders.get(order_id)
        if o is None or o.is_terminal:
            return False
        o.transition(OrderStatus.CANCELED, now, "canceled by request")
        self._emit("order_canceled", now, order_id=o.id, symbol=o.symbol)
        self._persist()
        return True

    def cancel_all(self, now: datetime | None = None, side: Side | None = None) -> int:
        n = 0
        for o in list(self.open_orders()):
            if side is None or o.side == side:
                n += int(self.cancel(o.id, now))
        return n

    # ------------------------------------------------------------------ simulation step
    def process(self, source: MarketDataSource, now: datetime) -> None:
        """Try to fill/expire every open order using bars that completed by ``now``."""
        for o in sorted(self.open_orders(), key=lambda x: x.id):
            self._process_order(o, source, now)
        self._persist()

    def _process_order(self, o: Order, source: MarketDataSource, now: datetime) -> None:
        label = self._cursor.get(o.id) or _ceil_minute(o.eligible_at or o.created_at)
        while label + ONE_MIN <= now and not o.is_terminal:
            if o.expires_at is not None and label >= o.expires_at:
                break
            bar = source.bar_at(o.symbol, label)
            halted = source.is_halted(o.symbol, label)
            max_qty = None
            if o.side == Side.BUY and bar is not None:
                px_guess = float(bar["open"]) * 1.02
                max_qty = int(max((self.cash - 1.0) // max(px_guess, 0.0001), 0))
            res = try_fill(o, bar, halted, source.spread_bps(o.symbol), self.costs, max_qty)
            if res is not None:
                self._apply_fill(o, res.quantity, res.price, res.commission, label, res.note)
            if o.time_in_force.value == "ioc" and not o.is_terminal:
                o.transition(OrderStatus.CANCELED, label, "IOC remainder canceled")
                self._emit("order_canceled", label, order_id=o.id, symbol=o.symbol, note="IOC")
            label += ONE_MIN
        self._cursor[o.id] = label
        if not o.is_terminal and o.expires_at is not None and now >= o.expires_at:
            o.transition(OrderStatus.EXPIRED, now, "day order expired at close")
            self._emit("order_expired", now, order_id=o.id, symbol=o.symbol, unfilled=o.remaining)
            log.warning("order expired unfilled", extra={"order": o.id, "symbol": o.symbol, "remaining": o.remaining})

    def _apply_fill(self, o: Order, qty: int, price: float, commission: float, when: datetime, note: str) -> None:
        if qty <= 0:
            return
        o.apply_fill(qty, price, commission)
        fill = Fill(o.id, o.symbol, o.side, qty, price, commission, when, note)
        self.fills.append(fill)
        self.commissions_paid += commission
        if o.side == Side.BUY:
            self._apply_buy(o, qty, price, commission, when)
        else:
            self._apply_sell(o, qty, price, commission, when)
        new = OrderStatus.FILLED if o.remaining == 0 else OrderStatus.PARTIALLY_FILLED
        try:
            o.transition(new, when, note)
        except InvalidTransition as exc:  # pragma: no cover - defensive
            raise ExecutionError(str(exc)) from exc
        self._emit("fill", when, order_id=o.id, symbol=o.symbol, side=str(o.side), quantity=qty,
                   price=price, commission=commission, strategy=o.strategy, reason=o.reason, status=str(o.status))

    def _apply_buy(self, o: Order, qty: int, price: float, commission: float, when: datetime) -> None:
        self.cash -= qty * price + commission
        pos = self.positions.get(o.symbol)
        if pos is None:
            m = o.meta
            sl, tp = m.get("stop_loss_pct"), m.get("take_profit_pct")
            pos = Position(
                symbol=o.symbol, quantity=0, avg_cost=price, opened_at=when, strategy=o.strategy,
                entry_reason=o.reason,
                stop_price=round(price * (1 - sl / 100), 4) if sl else None,
                take_profit_price=round(price * (1 + tp / 100), 4) if tp else None,
                trailing_stop_pct=m.get("trailing_stop_pct"),
                max_hold_minutes=m.get("max_hold_minutes"),
                high_water_mark=price,
                reference=dict(m.get("reference", {})),
            )
            self.positions[o.symbol] = pos
        new_qty = pos.quantity + qty
        pos.avg_cost = (pos.avg_cost * pos.quantity + price * qty) / new_qty
        pos.quantity = new_qty
        pos.total_bought_qty += qty
        pos.total_bought_cost += qty * price
        pos.commissions += commission
        pos.high_water_mark = max(pos.high_water_mark, price)

    def _apply_sell(self, o: Order, qty: int, price: float, commission: float, when: datetime) -> None:
        pos = self.positions[o.symbol]
        self.cash += qty * price - commission
        self.realized_pnl += (price - pos.avg_cost) * qty - commission
        pos.quantity -= qty
        pos.total_sold_qty += qty
        pos.total_sold_proceeds += qty * price
        pos.commissions += commission
        if pos.quantity == 0:
            self._close_position(pos, o, when)

    def _close_position(self, pos: Position, o: Order, when: datetime) -> None:
        self._trade_counter += 1
        entry_px = pos.total_bought_cost / pos.total_bought_qty
        exit_px = pos.total_sold_proceeds / pos.total_sold_qty
        gross = pos.total_sold_proceeds - pos.total_bought_cost
        net = gross - pos.commissions
        trade = Trade(
            id=f"TRD-{self._trade_counter:06d}", symbol=pos.symbol, strategy=pos.strategy,
            quantity=pos.total_bought_qty, entry_time=pos.opened_at, entry_price=entry_px, exit_time=when,
            exit_price=exit_px, gross_pnl=gross, commissions=pos.commissions, net_pnl=net,
            return_pct=net / pos.total_bought_cost * 100 if pos.total_bought_cost else 0.0,
            hold_minutes=(when - pos.opened_at).total_seconds() / 60, entry_reason=pos.entry_reason,
            exit_reason=o.reason, session_date=self.session_date,
        )
        self.trades.append(trade)
        del self.positions[pos.symbol]
        self._emit("trade_closed", when, trade=trade.to_dict())

    def force_flatten(self, now: datetime, reason: str) -> list[str]:
        """Bookkeeping-only liquidation at the last mark minus adverse costs (used between backtest days).

        Real liquidation goes through orders and bars; this exists so one day's unresolved
        positions cannot silently leak into the next simulated day. Callers must flag it in results.
        """
        self.cancel_all(now)
        closed = []
        for sym, pos in list(self.positions.items()):
            mark = self.marks.get(sym, pos.avg_cost)
            adverse = (self.costs.assumed_spread_bps / 2 + self.costs.slippage_bps) / 1e4
            px = round(mark * (1 - adverse), 4)
            o = self.submit(OrderRequest(f"force-{self.session_date}-{sym}-{self._counter}", sym, Side.SELL,
                                         pos.quantity, strategy=pos.strategy, reason=reason), now)
            if o.status == OrderStatus.REJECTED:  # pragma: no cover - defensive
                continue
            self._apply_fill(o, pos.quantity, px, 0.0, now, "forced flatten at last mark")
            closed.append(sym)
        self._persist()
        return closed

    # ------------------------------------------------------------------ ExecutionAdapter
    def check_session(self) -> SessionStatus:
        return SessionStatus(adapter=self.name, logged_in=True, market_open=None, detail="local virtual engine")

    def get_buying_power(self) -> float:
        return self.buying_power()

    def get_holdings(self) -> list[Holding]:
        return [Holding(p.symbol, p.quantity, p.avg_cost, self.marks.get(p.symbol)) for p in self.positions.values()]

    def prepare_order(self, request: OrderRequest) -> PreparedOrder:
        ref = request.limit_price or self.marks.get(request.symbol)
        warnings = []
        if ref is None:
            warnings.append("no reference price available")
        return PreparedOrder(request=request, adapter=self.name, estimated_price=ref,
                             estimated_cost=None if ref is None else ref * request.quantity, warnings=warnings)

    def submit_order(self, prepared: PreparedOrder) -> ExternalOrder:
        o = self.submit(prepared.request)
        return self._to_external(o)

    def get_order_status(self, external_id: str) -> ExternalOrder:
        o = self.orders.get(external_id)
        if o is None:
            raise ExecutionError(f"unknown order {external_id}")
        return self._to_external(o)

    def cancel_order(self, external_id: str) -> bool:
        return self.cancel(external_id)

    @staticmethod
    def _to_external(o: Order) -> ExternalOrder:
        return ExternalOrder(o.id, o.client_order_id, o.status, o.filled_quantity,
                             o.avg_fill_price or None, o.reject_reason)
