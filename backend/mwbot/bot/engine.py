"""The trading bot: one ``tick`` = one decision cycle at a point in (simulated or real) time.

Per tick:  fills -> data -> mark-to-market -> scan -> exits -> entries.
Everything the strategies see comes from ``source.contexts_at(now)`` which cannot contain
bars that were incomplete at ``now``.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from mwbot.calendar import Phase, SessionSchedule, to_chicago
from mwbot.config import AppSettings
from mwbot.data.base import DataUnavailableError, MarketDataSource
from mwbot.domain import (
    FeedKind,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    StockContext,
)
from mwbot.execution.base import ExecutionAdapter, ExecutionError
from mwbot.scanner.metrics import compute_metrics
from mwbot.scanner.scanner import Scanner, ScanResult
from mwbot.strategies.base import Evaluation, SignalInput, Strategy
from mwbot.strategies.builtin import build_strategy
from mwbot.trading.broker import PaperBroker
from mwbot.trading.risk import BotState, DayStats, RiskManager, hard_exit_reason

log = logging.getLogger(__name__)


@dataclass(slots=True)
class TickReport:
    now: datetime
    phase: Phase
    equity: float
    n_symbols: int = 0
    n_tradable: int = 0
    entries: list[str] = field(default_factory=list)
    exits: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class TradingBot:
    def __init__(
        self,
        settings: AppSettings,
        broker: PaperBroker,
        *,
        mirror: ExecutionAdapter | None = None,
    ) -> None:
        self.settings = settings
        self.broker = broker
        self.mirror = mirror
        self.state = BotState.STOPPED
        self.scanner = Scanner(settings.scanner)
        self.risk = RiskManager(settings.risk)
        self.strategies: dict[str, Strategy] = {}
        self.configure_strategies()
        self.stats: DayStats | None = None
        self.latest_scan: list[ScanResult] = []
        self.latest_evaluations: dict[str, list[Evaluation]] = {}
        self.decision_log: deque[dict[str, Any]] = deque(maxlen=1000)
        self.alerts: deque[dict[str, Any]] = deque(maxlen=200)
        self.equity_curve: list[tuple[datetime, float]] = []
        self.phase = Phase.CLOSED
        self.daily_halted = False
        self.flatten_requested = False
        self.last_tick: datetime | None = None
        self.on_decision: Any = None  # optional callback(dict) used by persistence
        self._exit_attempts: dict[str, int] = {}
        # Backtests set fast=True: skip scanning outside the entry phase (nothing consumes it there).
        self.fast = False

    # ------------------------------------------------------------------ configuration
    def configure_strategies(self) -> None:
        sel = self.settings.strategies
        self.strategies = {sid: build_strategy(sid, sel.params.get(sid)) for sid in sel.enabled}

    def apply_settings(self, settings: AppSettings) -> None:
        self.settings = settings
        self.scanner = Scanner(settings.scanner)
        self.risk = RiskManager(settings.risk)
        self.broker.costs = settings.costs
        self.configure_strategies()

    # ------------------------------------------------------------------ controls
    def start(self) -> None:
        if self.state == BotState.EMERGENCY_STOP:
            raise RuntimeError("emergency stop is active; call reset_emergency() first")
        self.state = BotState.RUNNING
        self._note("control", "Bot started")

    def pause(self) -> None:
        if self.state == BotState.RUNNING:
            self.state = BotState.PAUSED
            self._note("control", "Bot paused: no new entries, open positions still managed")

    def resume(self) -> None:
        if self.state == BotState.PAUSED:
            self.state = BotState.RUNNING
            self._note("control", "Bot resumed")

    def emergency_stop(self, now: datetime | None = None) -> None:
        now = now or self.broker.clock()
        self.state = BotState.EMERGENCY_STOP
        self.flatten_requested = True
        n = self.broker.cancel_all(now, side=Side.BUY)
        self._alert("critical", "emergency_stop", f"EMERGENCY STOP: canceled {n} pending buys; flattening positions", now)

    def reset_emergency(self) -> None:
        self.flatten_requested = False
        self.state = BotState.STOPPED
        self._note("control", "Emergency stop cleared; bot is STOPPED until started")

    # ------------------------------------------------------------------ logging helpers
    def _note(self, kind: str, message: str, *, now: datetime | None = None, symbol: str = "", strategy: str = "",
              **details: Any) -> None:
        entry = {"ts": (now or self.broker.clock()).isoformat(), "kind": kind, "symbol": symbol,
                 "strategy": strategy, "message": message, "details": details}
        self.decision_log.append(entry)
        if self.on_decision:
            self.on_decision(entry)

    def _alert(self, level: str, code: str, message: str, now: datetime | None = None) -> None:
        now = now or self.broker.clock()
        if self.alerts and self.alerts[-1]["code"] == code and self.alerts[-1]["message"] == message:
            return  # collapse immediate repeats
        a = {"ts": now.isoformat(), "level": level, "code": code, "message": message}
        self.alerts.append(a)
        (log.error if level == "critical" else log.warning)(message, extra={"code": code})
        self._note("alert", message, now=now, code=code, level=level)

    # ------------------------------------------------------------------ session lifecycle
    def begin_session(self, schedule: SessionSchedule) -> None:
        self.broker.session_date = schedule.session.date
        self.stats = DayStats(start_equity=self.broker.equity())
        self.daily_halted = False
        self.flatten_requested = self.state == BotState.EMERGENCY_STOP
        self.latest_scan = []
        self.latest_evaluations = {}
        self._exit_attempts = {}
        self._note("session", f"Session {schedule.session.date} begins (start equity ${self.stats.start_equity:,.2f})")

    def _feed_gate(self, source: MarketDataSource) -> tuple[bool, str]:
        fi = source.feed_info()
        if fi.kind == FeedKind.DELAYED and not self.settings.allow_delayed_feed_trading:
            return False, "feed is DELAYED; automated entries disabled (enable 'allow delayed-feed trading' to override)"
        return True, ""

    # ------------------------------------------------------------------ main cycle
    def tick(self, source: MarketDataSource, schedule: SessionSchedule, now: datetime) -> TickReport:
        if self.stats is None:
            self.begin_session(schedule)
        assert self.stats is not None
        phase = schedule.phase_at(now)
        self.phase = phase
        self.last_tick = now
        self.broker.clock = lambda: now  # sim time follows the runner
        rep = TickReport(now=now, phase=phase, equity=self.broker.equity())

        # 1. fills from bars that completed since the last tick
        self.broker.process(source, now)
        self._drain_broker_events(now)

        if phase in (Phase.CLOSED, Phase.POST_CLOSE) or self.state == BotState.STOPPED and not self.broker.positions:
            rep.equity = self.broker.equity()
            return rep

        # 2. data
        try:
            contexts = source.contexts_at(now)
        except DataUnavailableError as exc:
            self._alert("critical", "data_unavailable", f"Market data unavailable: {exc}", now)
            rep.warnings.append(str(exc))
            contexts = []
        feed_age = source.feed_age_seconds(now) if contexts else float("inf")
        stale = feed_age > self.settings.risk.max_data_age_seconds
        if stale and contexts:
            self._alert("critical", "stale_feed", f"Market data stale ({feed_age:.0f}s old): entries blocked", now)
            rep.warnings.append("stale feed")

        by_symbol = {c.symbol: c for c in contexts}
        for sym in list(self.broker.positions):  # make sure held names are priced
            if sym not in by_symbol:
                try:
                    c = source.context_for(sym, now)
                except DataUnavailableError:
                    c = None
                if c is not None:
                    by_symbol[sym] = c
        for c in by_symbol.values():
            if c.last_price is not None:
                self.broker.mark(c.symbol, c.last_price, float(c.bars["high"].iloc[-1]))
        rep.equity = self.broker.equity()
        self.equity_curve.append((now, rep.equity))
        rep.n_symbols = len(contexts)

        # 3. scan
        need_scan = (not self.fast) or phase == Phase.TRADING
        results = self.scanner.scan(contexts) if contexts and need_scan else []
        if need_scan:
            self.latest_scan = results
        scan_by = {r.symbol: r for r in results}
        rep.n_tradable = sum(1 for r in results if r.tradable)

        # 4. exits (always, in every phase, even paused / stale: exits reduce risk)
        self._manage_exits(by_symbol, scan_by, schedule, now, rep)

        # 5. entries
        if contexts and not stale and phase == Phase.TRADING and self.state == BotState.RUNNING and not self.daily_halted:
            self._look_for_entries(source, by_symbol, results, scan_by, schedule, now, rep)
        elif phase == Phase.FINAL_CHECK and self.broker.positions:
            left = ", ".join(f"{s}x{p.quantity}" for s, p in self.broker.positions.items())
            self._alert("critical", "unresolved_positions", f"Close approaching with unresolved positions: {left}", now)

        self._drain_broker_events(now)
        rep.equity = self.broker.equity()
        return rep

    def _drain_broker_events(self, now: datetime) -> None:
        for ev in self.broker.drain_events():
            kind = ev["kind"]
            if kind == "fill":
                side = ev["side"]
                self._note(
                    "fill", f"{side.upper()} {ev['quantity']} {ev['symbol']} @ {ev['price']:.4f} ({ev['status']})",
                    now=now, symbol=ev["symbol"], strategy=ev["strategy"], reason=ev["reason"],
                )
            elif kind == "order_rejected":
                self._alert("warning", "order_rejected", f"Order rejected for {ev['symbol']}: {ev['reason']}", now)
            elif kind == "order_expired":
                self._alert("warning", "order_expired", f"{ev['symbol']} order expired with {ev['unfilled']} shares unfilled", now)
            elif kind == "trade_closed":
                t = ev["trade"]
                self._note("trade", f"Closed {t['symbol']} {t['net_pnl']:+.2f} ({t['return_pct']:+.2f}%): {t['exit_reason']}",
                           now=now, symbol=t["symbol"], strategy=t["strategy"])
                if t["net_pnl"] < 0 and self.stats is not None:
                    self.stats.stopped_symbols.add(t["symbol"])

    # ------------------------------------------------------------------ exits
    def _manage_exits(self, by_symbol: dict[str, StockContext], scan_by: dict[str, ScanResult],
                      schedule: SessionSchedule, now: datetime, rep: TickReport) -> None:
        assert self.stats is not None
        if not self.daily_halted and self.risk.daily_loss_breached(self.broker.equity(), self.stats.start_equity) and self.broker.positions:
            self.daily_halted = True
            self.broker.cancel_all(now, side=Side.BUY)
            self._alert("critical", "daily_loss_limit",
                        f"Daily loss limit {self.settings.risk.max_daily_loss_pct:g}% breached: flattening and blocking entries", now)
        for sym, pos in list(self.broker.positions.items()):
            if self.broker.pending_sell_qty(sym) >= pos.quantity:
                continue  # an exit is already working
            ctx = by_symbol.get(sym)
            reason = self._exit_reason(pos, ctx, scan_by.get(sym), schedule, now)
            if reason is None:
                continue
            qty = pos.quantity - self.broker.pending_sell_qty(sym)
            n = self._exit_attempts.get(sym, 0)
            self._exit_attempts[sym] = n + 1
            req = OrderRequest(
                client_order_id=f"{schedule.session.date}-exit-{sym}-{pos.opened_at:%H%M}-{n}",
                symbol=sym, side=Side.SELL, quantity=qty, order_type=OrderType.MARKET,
                strategy=pos.strategy, reason=reason,
            )
            self._submit(req, schedule, now)
            rep.exits.append(f"{sym}: {reason}")
            self._note("exit", reason, now=now, symbol=sym, strategy=pos.strategy)

    def _exit_reason(self, pos: Position, ctx: StockContext | None, scan: ScanResult | None,
                     schedule: SessionSchedule, now: datetime) -> str | None:
        if self.flatten_requested or self.state == BotState.EMERGENCY_STOP:
            return "Emergency stop: flatten"
        if self.daily_halted:
            return "Daily loss limit: flatten"
        phase = schedule.phase_at(now)
        if phase in (Phase.LIQUIDATING, Phase.FINAL_CHECK):
            return f"End-of-day liquidation (scheduled {to_chicago(schedule.liquidation_start):%H:%M} CT)"
        if ctx is None or ctx.bars.empty:
            return None
        last = ctx.bars.iloc[-1]
        hard = hard_exit_reason(pos, float(last["high"]), float(last["low"]), float(last["close"]), now)
        if hard:
            return hard
        strat = self.strategies.get(pos.strategy)
        if strat is not None:
            m = scan.metrics if scan else compute_metrics(ctx)
            inp = SignalInput(ctx, m, scan, (now - schedule.open).total_seconds() / 60)
            d = strat.evaluate_exit(inp, pos)
            if d:
                return d.reason
        return None

    # ------------------------------------------------------------------ entries
    def _look_for_entries(self, source: MarketDataSource, by_symbol: dict[str, StockContext],
                          results: list[ScanResult], scan_by: dict[str, ScanResult],
                          schedule: SessionSchedule, now: datetime, rep: TickReport) -> None:
        assert self.stats is not None
        allowed, why = self._feed_gate(source)
        candidates = [r for r in results if r.tradable][: self.settings.scanner.top_n]
        passing: list[tuple[float, Evaluation, ScanResult, StockContext]] = []
        self.latest_evaluations = {}
        mins = (now - schedule.open).total_seconds() / 60
        for r in candidates:
            ctx = by_symbol[r.symbol]
            inp = SignalInput(ctx, r.metrics, r, mins)
            evs = []
            for strat in self.strategies.values():
                ev = strat.evaluate_entry(inp)
                evs.append(ev)
                if ev.passed:
                    passing.append((ev.strength * (0.5 + r.score / 200), ev, r, ctx))
            self.latest_evaluations[r.symbol] = evs
        passing.sort(key=lambda x: (-x[0], x[1].symbol, x[1].strategy))
        for _rank, ev, r, ctx in passing:
            price = ctx.last_price
            assert price is not None
            sl = ev.stop_loss_pct or self.settings.risk.stop_loss_pct
            decision = self.risk.check_entry(
                state=self.state, phase=schedule.phase_at(now), symbol=ev.symbol, strategy=ev.strategy,
                price=price, stop_loss_pct=sl, equity=self.broker.equity(),
                buying_power=self.broker.buying_power(), positions=self.broker.positions,
                pending_buy_symbols=self.broker.pending_buy_symbols(), stats=self.stats,
                feed_age_seconds=source.feed_age_seconds(now), feed_allows_trading=allowed, feed_block_reason=why,
            )
            if not decision.approved:
                self._note("skip", f"{ev.symbol} {ev.strategy} signal blocked: {'; '.join(decision.reasons)}",
                           now=now, symbol=ev.symbol, strategy=ev.strategy)
                continue
            reason = f"{self.strategies[ev.strategy].name}: {ev.explanation()}"[:600]
            req = OrderRequest(
                client_order_id=f"{schedule.session.date}-{ev.strategy}-{ev.symbol}-buy",
                symbol=ev.symbol, side=Side.BUY, quantity=decision.quantity, order_type=OrderType.MARKET,
                strategy=ev.strategy, reason=reason,
                meta={
                    "stop_loss_pct": sl,
                    "take_profit_pct": ev.take_profit_pct if ev.take_profit_pct is not None else self.settings.risk.take_profit_pct,
                    "trailing_stop_pct": ev.trailing_stop_pct if ev.trailing_stop_pct is not None else self.settings.risk.trailing_stop_pct,
                    "max_hold_minutes": ev.max_hold_minutes if ev.max_hold_minutes is not None else self.settings.risk.max_hold_minutes,
                    "reference": dict(ev.reference), "sizing": decision.sizing, "scan_score": r.score,
                },
            )
            order = self._submit(req, schedule, now)
            if order is not None and order.status != OrderStatus.REJECTED:
                self.stats.trades_opened += 1
                self.stats.entered_keys.add((ev.strategy, ev.symbol))
                rep.entries.append(f"{ev.symbol}: {ev.strategy} x{decision.quantity}")
                self._note("entry", f"BUY {decision.quantity} {ev.symbol} ({ev.strategy}): {ev.explanation()}",
                           now=now, symbol=ev.symbol, strategy=ev.strategy, sizing=decision.sizing,
                           score=round(r.score, 1))

    # ------------------------------------------------------------------ order plumbing
    def _submit(self, req: OrderRequest, schedule: SessionSchedule, now: datetime) -> Any:
        order = self.broker.submit(req, now, expires_at=schedule.close)
        if self.mirror is not None and order.status != OrderStatus.REJECTED:
            self._mirror(req, order.id, now)
        return order

    def _mirror(self, req: OrderRequest, internal_id: str, now: datetime) -> None:
        assert self.mirror is not None
        try:
            prepared = self.mirror.prepare_order(req)
            ext = self.mirror.submit_order(prepared)
            self.broker.orders[internal_id].meta["external_id"] = ext.external_id
            self._note("mirror", f"Mirrored {req.side.value} {req.quantity} {req.symbol} to {self.mirror.name}: {ext.status}",
                       now=now, symbol=req.symbol)
        except ExecutionError as exc:
            self._alert("critical", "mirror_failed", f"{self.mirror.name} rejected/failed {req.side.value} {req.symbol}: {exc}", now)
            if self.settings.pause_on_mirror_failure:
                self.pause()

    # ------------------------------------------------------------------ end of session
    def finish_session(self, schedule: SessionSchedule, now: datetime) -> dict[str, Any]:
        """Final bookkeeping after the close. Never claims liquidation succeeded if positions remain."""
        assert self.stats is not None
        b = self.broker
        b.cancel_all(now)
        unresolved = [
            {"symbol": s, "quantity": p.quantity, "mark": b.marks.get(s), "unrealized_pnl": round(p.quantity * (b.marks.get(s, p.avg_cost) - p.avg_cost), 2)}
            for s, p in b.positions.items()
        ]
        if unresolved:
            self._alert("critical", "overnight_risk", "Positions NOT liquidated at close (overnight exposure): " +
                        ", ".join(f"{u['symbol']}x{u['quantity']}" for u in unresolved), now)
        recon = None
        if self.mirror is not None:
            try:
                from mwbot.execution.base import Holding
                rep = self.mirror.reconcile([Holding(p.symbol, p.quantity, p.avg_cost) for p in b.positions.values()])
                recon = {"ok": rep.ok, "summary": rep.summary()}
                if not rep.ok:
                    self._alert("critical", "reconcile_mismatch", f"Reconciliation mismatch vs {self.mirror.name}: {rep.summary()}", now)
            except ExecutionError as exc:
                self._alert("critical", "reconcile_failed", f"Reconciliation failed: {exc}", now)
        from mwbot.bot.report import build_daily_report
        report = build_daily_report(self, schedule.session.date, unresolved, recon)
        self._note("session", f"Session {schedule.session.date} complete: P/L ${report['pnl']:+,.2f}", now=now)
        self.stats = None
        return report
