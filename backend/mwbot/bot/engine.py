"""The trading bot: one ``tick`` = one decision cycle at a point in (simulated or real) time.

Per tick:  fills -> data -> mark-to-market -> (every N minutes: scan + rank opportunities) -> exits -> entries.
Everything the strategies and forecasters see comes from ``source.contexts_at(now)`` which cannot contain
bars that were incomplete at ``now``.

Position management never depends on eligibility: if a ticker's eligibility changes while we hold it, we
flag it and keep managing it with the exit rules the position was opened with.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from mwbot.calendar import Phase, SessionSchedule, to_chicago
from mwbot.config import AppSettings, ModeProfile, TradingMode
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
from mwbot.eligibility.models import EligibilityStatus
from mwbot.eligibility.universe import EligibilityUniverse
from mwbot.execution.base import ExecutionAdapter, ExecutionError, MirrorSkipped
from mwbot.forecast.features import MarketState, market_state
from mwbot.forecast.models import Forecaster
from mwbot.forecast.ranking import Opportunity, OpportunityRanker
from mwbot.scanner.metrics import compute_metrics
from mwbot.scanner.scanner import Scanner, ScanResult
from mwbot.strategies.base import Evaluation, SignalInput, Strategy
from mwbot.strategies.builtin import build_strategy
from mwbot.trading.broker import PaperBroker
from mwbot.trading.risk import BotState, DayStats, RiskManager, hard_exit_reason

log = logging.getLogger(__name__)
MARKETABLE_LIMIT_BUFFER = 0.006  # used when the game only allows limit orders
STALE_LIMIT_EXIT_MINUTES = 2


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


def classify_exit(reason: str) -> str:
    """Exit category for reporting: target | risk | forecast | end_of_day | time | rotation | emergency | limit."""
    r = reason.lower()
    if "profit target" in r:
        return "target"
    if "emergency" in r:
        return "emergency"
    if "manual" in r or "closed by you" in r:
        return "manual"
    if "daily loss" in r:
        return "limit"
    if "end-of-day" in r or "unresolved at close" in r:
        return "end_of_day"
    if "rotation" in r:
        return "rotation"
    if "forecast" in r:
        return "forecast"
    if "time stop" in r or "deadline" in r:
        return "time"
    if any(
        k in r
        for k in (
            "stop loss",
            "trailing stop",
            "breakeven",
            "failed breakout",
            "momentum lost",
            "gap failed",
            "surge faded",
        )
    ):
        return "risk"
    return "other"


class TradingBot:
    def __init__(
        self,
        settings: AppSettings,
        broker: PaperBroker,
        *,
        mirror: ExecutionAdapter | None = None,
        eligibility: EligibilityUniverse | None = None,
        forecaster: Forecaster | None = None,
    ) -> None:
        self.settings = settings
        self.broker = broker
        self.mirror = mirror
        self.state = BotState.STOPPED
        # None => the eligibility layer is NOT applied (unit tests / explicitly labelled backtests).
        self.universe: EligibilityUniverse = (
            eligibility if eligibility is not None else EligibilityUniverse.allow_all()
        )
        self.ranker = OpportunityRanker(forecaster)
        self.scanner = Scanner(settings.scanner, settings.game)
        self.risk = RiskManager(settings.effective_risk())
        self.strategies: dict[str, Strategy] = {}
        self.stats: DayStats | None = None
        self.latest_scan: list[ScanResult] = []
        self.opportunities: list[Opportunity] = []
        self.held_forecasts: dict[str, Opportunity] = {}
        self.market: MarketState = MarketState()
        self.latest_evaluations: dict[str, list[Evaluation]] = {}
        self.decision_log: deque[dict[str, Any]] = deque(maxlen=1000)
        self.alerts: deque[dict[str, Any]] = deque(maxlen=200)
        self.equity_curve: list[tuple[datetime, float]] = []
        self.phase = Phase.CLOSED
        self.daily_halted = False
        self.flatten_requested = False
        self.last_tick: datetime | None = None
        self.on_decision: Any = None  # optional callback(dict) used by persistence
        self.hist_lookup: Any = None  # optional (ctx, horizon) -> (mean_pct, n) from EARLIER sessions only
        self._exit_attempts: dict[str, int] = {}
        self._last_rank_at: datetime | None = None
        self._last_monitor_at: datetime | None = None
        self._noted: dict[str, datetime] = {}
        self._flagged_held: set[str] = set()
        self._manual_close: set[str] = set()
        self._announced_liquidation = False
        self._last_top: str | None = None
        # Backtests set fast=True: skip scanning outside the entry phase (nothing consumes it there).
        self.fast = False
        self.configure_strategies()
        self._sync_broker_rules()

    # ------------------------------------------------------------------ configuration
    @property
    def profile(self) -> ModeProfile:
        return self.settings.profile_for()

    def configure_strategies(self) -> None:
        sel = self.settings.strategies
        self.strategies = {sid: build_strategy(sid, sel.params.get(sid)) for sid in sel.enabled}

    def _sync_broker_rules(self) -> None:
        self.broker.costs = self.settings.effective_costs()
        self.broker.supported_order_types = frozenset(self.settings.game.supported_order_types)

    def apply_settings(self, settings: AppSettings) -> None:
        """Live configuration change. New rules govern FUTURE decisions; open positions keep the plan they
        were opened with (``existing_positions_policy``) - but never get *looser* risk limits."""
        old_mode = self.settings.trading.mode
        self.settings = settings
        self.scanner = Scanner(settings.scanner, settings.game)
        self.risk = RiskManager(settings.effective_risk())
        self.configure_strategies()
        self._sync_broker_rules()
        if settings.trading.mode != old_mode:
            self._note(
                "control",
                f"Mode changed to {settings.trading.mode.label}: applies to new trades; "
                f"open positions keep their original plan ({settings.trading.existing_positions_policy.replace('_', ' ')})",
            )
        if settings.trading.existing_positions_policy == "tighten_only":
            self._tighten_open_positions()

    def _tighten_open_positions(self) -> None:
        prof = self.profile
        for pos in self.broker.positions.values():
            cap_stop = round(pos.avg_cost * (1 - prof.risk.stop_loss_pct / 100), 4)
            if pos.stop_price is None or cap_stop > pos.stop_price:
                pos.stop_price = cap_stop
            tp = prof.plan.trailing_pct
            if tp is not None and (pos.trailing_stop_pct is None or tp < pos.trailing_stop_pct):
                pos.trailing_stop_pct = tp

    def set_universe(self, universe: EligibilityUniverse) -> None:
        self.universe = universe
        self._last_rank_at = None  # re-rank immediately with the new list

    def set_forecaster(self, forecaster: Forecaster) -> None:
        self.ranker.forecaster = forecaster

    # ------------------------------------------------------------------ controls
    def start(self) -> None:
        if self.state == BotState.EMERGENCY_STOP:
            raise RuntimeError("emergency stop is active; call reset_emergency() first")
        self.state = BotState.RUNNING
        self._note("control", "Bot started: looking for opportunities and managing positions")

    def pause(self) -> None:
        if self.state == BotState.RUNNING:
            self.state = BotState.PAUSED
            self._note("control", "Entries paused: no new trades; open positions are still managed")

    def resume(self) -> None:
        if self.state == BotState.PAUSED:
            self.state = BotState.RUNNING
            self._note("control", "Entries resumed")

    def stop(self) -> None:
        """No new trades. Open positions are still managed (target/stop/forecast/end-of-day) until flat."""
        if self.state != BotState.EMERGENCY_STOP:
            self.state = BotState.STOPPED
            self._note(
                "control", "Bot stopped: no new trades; open positions are still being managed until closed"
            )

    def request_close(self, symbol: str) -> bool:
        """Sell one holding at the next opportunity (a simulated order; subject to the usual fill model)."""
        sym = symbol.upper()
        if sym not in self.broker.positions:
            return False
        self._manual_close.add(sym)
        self._note("control", f"You asked to close {sym}: selling it now", symbol=sym)
        return True

    def request_flatten(self) -> None:
        """Sell every holding at the next opportunity without stopping the bot."""
        self.flatten_requested = True
        self._note("control", "You asked to close all positions: selling everything now")

    def emergency_stop(self, now: datetime | None = None) -> None:
        now = now or self.broker.clock()
        self.state = BotState.EMERGENCY_STOP
        flatten = self.settings.trading.emergency_behavior == "flatten_all"
        self.flatten_requested = flatten
        n = self.broker.cancel_all(now, side=Side.BUY)
        what = (
            "flattening all positions"
            if flatten
            else "open positions stay managed by their normal exit rules"
        )
        self._alert("critical", "emergency_stop", f"EMERGENCY STOP: canceled {n} pending buys; {what}", now)

    def reset_emergency(self) -> None:
        self.flatten_requested = False
        self.state = BotState.STOPPED
        self._note("control", "Emergency stop cleared; bot is STOPPED until started")

    # ------------------------------------------------------------------ logging helpers
    def _note(
        self,
        kind: str,
        message: str,
        *,
        now: datetime | None = None,
        symbol: str = "",
        strategy: str = "",
        **details: Any,
    ) -> None:
        entry = {
            "ts": (now or self.broker.clock()).isoformat(),
            "kind": kind,
            "symbol": symbol,
            "strategy": strategy,
            "message": message,
            "details": details,
        }
        self.decision_log.append(entry)
        if self.on_decision:
            self.on_decision(entry)

    def _note_once(
        self, key: str, message: str, now: datetime, every_minutes: float = 1e9, **kw: Any
    ) -> None:
        last = self._noted.get(key)
        if last is not None and (now - last) < timedelta(minutes=every_minutes):
            return
        self._noted[key] = now
        self._note("info", message, now=now, **kw)

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
        self.flatten_requested = (
            self.state == BotState.EMERGENCY_STOP
            and self.settings.trading.emergency_behavior == "flatten_all"
        )
        self.latest_scan = []
        self.opportunities = []
        self.held_forecasts = {}
        self.latest_evaluations = {}
        self._exit_attempts = {}
        self._last_rank_at = None
        self._last_monitor_at = None
        self._noted = {}
        self._flagged_held = set()
        self._announced_liquidation = False
        self._last_top = None
        self._note(
            "session",
            f"Session {schedule.session.date} begins (start equity ${self.stats.start_equity:,.2f})",
        )

    def _feed_gate(self, source: MarketDataSource) -> tuple[bool, str]:
        fi = source.feed_info()
        if (
            self.mirror is not None
            and self.mirror.requires_real_feed
            and fi.kind in (FeedKind.SYNTHETIC, FeedKind.HISTORICAL)
        ):
            return (
                False,
                f"feed is {fi.kind.value.upper()}: refusing to send orders to {self.mirror.name} "
                "(needs a real market-data feed)",
            )
        if fi.kind == FeedKind.DELAYED and not self.settings.allow_delayed_feed_trading:
            return (
                False,
                "feed is DELAYED; automated entries disabled (enable 'allow delayed-feed trading' to override)",
            )
        return True, ""

    def _elig_date(self, schedule: SessionSchedule) -> date | None:
        """Live/demo sessions use the current list; replays use the list as of the session date."""
        if self.settings.game.eligibility_mode == "point_in_time":
            return date.fromisoformat(schedule.session.date)
        return None

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

        if phase in (Phase.CLOSED, Phase.POST_CLOSE) or (
            self.state == BotState.STOPPED and not self.broker.positions
        ):
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
            self._alert(
                "critical", "stale_feed", f"Market data stale ({feed_age:.0f}s old): entries blocked", now
            )
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

        # 3. scan + rank (every `scanner_refresh_minutes`; fast/backtest mode only while entries are possible)
        refresh = self.settings.schedule.scanner_refresh_minutes
        due = self._last_rank_at is None or (now - self._last_rank_at) >= timedelta(minutes=refresh)
        need_scan = contexts and due and ((not self.fast) or phase == Phase.TRADING)
        as_of = self._elig_date(schedule)
        if need_scan:
            self._refresh_opportunities(contexts, as_of, now, schedule)
        rep.n_tradable = sum(1 for r in self.latest_scan if r.tradable)

        # 4. exits (always, in every phase, even paused / stale / ineligible: exits reduce risk)
        self._manage_exits(by_symbol, source, schedule, now, rep, as_of)

        # 5. entries
        can_enter = (
            contexts
            and not stale
            and phase == Phase.TRADING
            and self.state == BotState.RUNNING
            and not self.daily_halted
        )
        if can_enter and need_scan:
            self._look_for_entries(source, by_symbol, schedule, now, rep)
        elif phase == Phase.FINAL_CHECK and self.broker.positions:
            left = ", ".join(f"{s}x{p.quantity}" for s, p in self.broker.positions.items())
            self._alert(
                "critical",
                "unresolved_positions",
                f"Close approaching with unresolved positions: {left}",
                now,
            )

        self._drain_broker_events(now)
        if self.flatten_requested and not self.broker.positions and self.state != BotState.EMERGENCY_STOP:
            self.flatten_requested = False  # a one-off "close everything" request is done
        rep.equity = self.broker.equity()
        self.broker._persist()  # latest marks + any orders submitted this tick survive a crash/restart
        return rep

    # ------------------------------------------------------------------ ranking
    def _refresh_opportunities(
        self, contexts: list[StockContext], as_of: date | None, now: datetime, schedule: SessionSchedule
    ) -> None:
        self._last_rank_at = now
        results = self.scanner.scan(contexts, self.universe, as_of)
        self.latest_scan = results
        self.market = market_state([r.metrics for r in results])
        held = set(self.broker.positions) | self.broker.pending_buy_symbols()
        self.opportunities = self.ranker.rank(
            contexts,
            results,
            self.market,
            self.profile,
            self.settings.effective_costs(),
            held=held,
            as_of=as_of,
            hist_lookup=self.hist_lookup,
        )
        # friendly bookkeeping: a note when the best idea changes, and once per symbol for strong-but-unusable names
        acts = [o for o in self.opportunities if o.actionable and not o.held]
        top = acts[0] if acts else None
        if top and top.symbol != self._last_top and top.forecast and top.plan:
            self._note(
                "scan",
                f"Scanned {len(results)} stocks. Best supported idea: {top.symbol} - expected "
                f"{top.forecast.expected_net_return_pct:+.2f}% after costs, target ${top.plan.target_price:.2f}",
                now=now,
                symbol=top.symbol,
            )
        self._last_top = top.symbol if top else None
        if top is None and self.state == BotState.RUNNING and schedule.phase_at(now) == Phase.TRADING:
            self._explain_waiting(now)
        for r in results:
            if (
                r.eligibility_status == EligibilityStatus.CONFIRMED_UNAVAILABLE
                and r.basic_ok
                and r.score >= 40
            ):
                reason = r.eligibility.record.rejection_reason if r.eligibility else ""
                self._note_once(
                    f"unavail:{r.symbol}",
                    f"Skipping {r.symbol}: it scores well but is marked unavailable in your game ({reason or 'no reason recorded'})",
                    now,
                    symbol=r.symbol,
                )
            elif not r.basic_ok and abs(r.metrics.pct_change or 0) >= 8 and r.metrics.price:
                self._note_once(
                    f"blocked:{r.symbol}",
                    f"Skipping {r.symbol}: {r.basic_blockers[0] if r.basic_blockers else 'data not usable'}",
                    now,
                    symbol=r.symbol,
                )

    def _explain_waiting(self, now: datetime) -> None:
        """Say, at most every 30 minutes, why nothing is being bought (so a quiet bot never looks broken)."""
        cands = [o for o in self.opportunities if o.scan.opportunity_ok and o.forecast and not o.held]
        if not cands:
            self._note_once(
                "waiting:none",
                "Waiting: no stock that is confirmed tradable in your game has usable data right now",
                now,
                every_minutes=30,
            )
            return
        best = cands[0]
        failed = [g for g in best.gates if not g.passed]
        why = f"{failed[0].label.lower()}: {failed[0].detail}" if failed else "no gate failed"
        self._note_once(
            "waiting:best",
            f"Waiting for a better setup: the best candidate {best.symbol} does not qualify yet - {why}",
            now,
            every_minutes=30,
            symbol=best.symbol,
        )

    # ------------------------------------------------------------------ events
    def _drain_broker_events(self, now: datetime) -> None:
        for ev in self.broker.drain_events():
            kind = ev["kind"]
            if kind == "fill":
                side = ev["side"]
                verb = "Bought" if side == "buy" else "Sold"
                self._note(
                    "fill",
                    f"{verb} {ev['quantity']} {ev['symbol']} at ${ev['price']:.4f} ({ev['status'].replace('_', ' ')})",
                    now=now,
                    symbol=ev["symbol"],
                    strategy=ev["strategy"],
                    reason=ev["reason"],
                )
            elif kind == "order_rejected":
                self._alert(
                    "warning", "order_rejected", f"Order rejected for {ev['symbol']}: {ev['reason']}", now
                )
            elif kind == "order_expired":
                self._alert(
                    "warning",
                    "order_expired",
                    f"{ev['symbol']} order expired with {ev['unfilled']} shares unfilled",
                    now,
                )
            elif kind == "trade_closed":
                t = ev["trade"]
                self._note(
                    "trade",
                    f"Closed {t['symbol']} for {t['net_pnl']:+.2f} ({t['return_pct']:+.2f}%): {t['exit_reason']}",
                    now=now,
                    symbol=t["symbol"],
                    strategy=t["strategy"],
                    exit_category=classify_exit(t["exit_reason"]),
                )
                self._manual_close.discard(t["symbol"])
                if self.stats is not None:
                    self.stats.last_exit[t["symbol"]] = now
                    if t["net_pnl"] < 0:
                        self.stats.stopped_symbols.add(t["symbol"])

    # ------------------------------------------------------------------ exits
    def _profile_for_position(self, pos: Position) -> ModeProfile:
        """Rules a position is managed by: the mode it was opened under (unless policy says adopt tighter ones)."""
        if self.settings.trading.existing_positions_policy == "keep_original" and pos.mode:
            try:
                return self.settings.profile_for(TradingMode(pos.mode))
            except ValueError:
                pass
        return self.profile

    def _manage_exits(
        self,
        by_symbol: dict[str, StockContext],
        source: MarketDataSource,
        schedule: SessionSchedule,
        now: datetime,
        rep: TickReport,
        as_of: date | None,
    ) -> None:
        assert self.stats is not None
        if (
            not self.daily_halted
            and self.risk.daily_loss_breached(self.broker.equity(), self.stats.start_equity)
            and self.broker.positions
        ):
            self.daily_halted = True
            self.broker.cancel_all(now, side=Side.BUY)
            self._alert(
                "critical",
                "daily_loss_limit",
                f"Daily loss limit {self.risk.cfg.max_daily_loss_pct:g}% breached: flattening and blocking entries",
                now,
            )
        if (
            schedule.phase_at(now) in (Phase.LIQUIDATING, Phase.FINAL_CHECK)
            and self.broker.positions
            and not self._announced_liquidation
        ):
            self._announced_liquidation = True
            self._note(
                "info",
                f"Preparing end-of-day exits for {len(self.broker.positions)} open position(s)",
                now=now,
            )
        self._reprice_stale_exits(now)
        monitor = self._last_monitor_at is None or (now - self._last_monitor_at) >= timedelta(
            minutes=self.settings.schedule.monitor_minutes
        )
        if monitor:
            self._last_monitor_at = now
        for sym, pos in list(self.broker.positions.items()):
            ctx = by_symbol.get(sym)
            self._flag_eligibility(sym, pos, now, as_of)
            if ctx is not None and not ctx.bars.empty:
                self._ratchet_stop(pos, ctx, now)
            if self.broker.pending_sell_qty(sym) >= pos.quantity:
                continue  # an exit is already working
            reason = self._exit_reason(pos, ctx, source, schedule, now, monitor, as_of)
            if reason is None:
                continue
            qty = pos.quantity - self.broker.pending_sell_qty(sym)
            n = self._exit_attempts.get(sym, 0)
            self._exit_attempts[sym] = n + 1
            otype, limit = self._order_type_for(Side.SELL, ctx.last_price if ctx else pos.avg_cost)
            req = OrderRequest(
                client_order_id=f"{schedule.session.date}-exit-{sym}-{pos.opened_at:%H%M}-{n}",
                symbol=sym,
                side=Side.SELL,
                quantity=qty,
                order_type=otype,
                limit_price=limit,
                strategy=pos.strategy,
                reason=reason,
            )
            self._submit(req, schedule, now)
            rep.exits.append(f"{sym}: {reason}")
            self._note(
                "exit",
                f"Selling {sym}: {reason}",
                now=now,
                symbol=sym,
                strategy=pos.strategy,
                exit_category=classify_exit(reason),
            )

    def _order_type_for(self, side: Side, price: float | None) -> tuple[OrderType, float | None]:
        types = set(self.settings.game.supported_order_types)
        if "market" in types or "limit" not in types or price is None:
            return OrderType.MARKET, None
        buf = MARKETABLE_LIMIT_BUFFER
        return OrderType.LIMIT, round(price * (1 + buf if side == Side.BUY else 1 - buf), 4)

    def _reprice_stale_exits(self, now: datetime) -> None:
        """A limit exit that has not filled for a couple of minutes is cancelled so the next tick re-prices it."""
        for o in self.broker.open_orders():
            if (
                o.side == Side.SELL
                and o.order_type == OrderType.LIMIT
                and (now - o.created_at) >= timedelta(minutes=STALE_LIMIT_EXIT_MINUTES)
                and self.broker.cancel(o.id, now)
            ):
                self._note(
                    "info",
                    f"Sell order for {o.symbol} did not fill; re-pricing it",
                    now=now,
                    symbol=o.symbol,
                )

    def _flag_eligibility(self, sym: str, pos: Position, now: datetime, as_of: date | None) -> None:
        """Flag (never block) a held ticker whose eligibility is no longer 'confirmed tradable'."""
        if sym in self._flagged_held or self.universe.ignore:
            return
        rec = self.universe.record_on(sym, as_of)
        if rec.status != EligibilityStatus.CONFIRMED_TRADABLE:
            self._flagged_held.add(sym)
            self._alert(
                "warning",
                "eligibility_changed",
                f"Held {sym} is now '{rec.status.label}' in your game list. It will still be managed and sold by its normal exit rules.",
                now,
            )

    def _ratchet_stop(self, pos: Position, ctx: StockContext, now: datetime) -> None:
        """Move the stop to breakeven once the trade is part-way to its target (locks in not losing)."""
        bp = pos.plan.get("breakeven_progress") if pos.plan else None
        tgt = pos.take_profit_price
        price = ctx.last_price
        if not bp or pos.breakeven_armed or tgt is None or price is None or tgt <= pos.avg_cost:
            return
        progress = (price - pos.avg_cost) / (tgt - pos.avg_cost)
        if progress >= bp:
            cost_buf = float(pos.plan.get("cost_pct", 0.0)) / 100
            new_stop = round(pos.avg_cost * (1 + cost_buf), 4)
            if pos.stop_price is None or new_stop > pos.stop_price:
                pos.stop_price = new_stop
            pos.breakeven_armed = True
            self._note(
                "info",
                f"{pos.symbol} is {progress:.0%} of the way to its target: stop moved to breakeven (${new_stop:.2f})",
                now=now,
                symbol=pos.symbol,
            )

    def _exit_reason(
        self,
        pos: Position,
        ctx: StockContext | None,
        source: MarketDataSource,
        schedule: SessionSchedule,
        now: datetime,
        monitor: bool,
        as_of: date | None,
    ) -> str | None:
        if pos.symbol in self._manual_close:
            return "Closed by you from the dashboard (manual close)"
        if self.flatten_requested or (
            self.state == BotState.EMERGENCY_STOP
            and self.settings.trading.emergency_behavior == "flatten_all"
        ):
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
        if not monitor:
            return None
        held_min = (now - pos.opened_at).total_seconds() / 60
        if pos.plan:  # opened by the predictive strategy
            return self._forecast_exit(pos, ctx, schedule, now, held_min, as_of)
        strat = self.strategies.get(pos.strategy)
        if strat is not None:
            scan = self.scanner.scan_one(ctx, self.universe, as_of)
            inp = SignalInput(ctx, scan.metrics, scan, (now - schedule.open).total_seconds() / 60)
            d = strat.evaluate_exit(inp, pos)
            if d:
                return d.reason
        return None

    def _forecast_exit(
        self,
        pos: Position,
        ctx: StockContext,
        schedule: SessionSchedule,
        now: datetime,
        held_min: float,
        as_of: date | None,
    ) -> str | None:
        prof = self._profile_for_position(pos)
        fx = prof.forecast_exit
        scan = self.scanner.scan_one(ctx, self.universe, as_of)
        mins_left = max((schedule.close - now).total_seconds() / 60, 0.0)
        remaining_plan = (pos.deadline - now).total_seconds() / 60 if pos.deadline else mins_left
        horizon = max(min(remaining_plan, mins_left - prof.plan.end_buffer_minutes), 5.0)
        opp = self.ranker.evaluate(
            ctx, scan, self.market, prof, self.settings.effective_costs(), horizon=horizon
        )
        self.held_forecasts[pos.symbol] = opp
        fc = opp.forecast
        if not fx.enabled or fc is None:
            return None
        hold_net = fc.expected_return_pct - fc.cost_pct / 2  # only the sell half of the cost is still ahead
        bad = hold_net < fx.min_net_return_to_hold_pct or fc.p_positive < fx.p_positive_floor
        pos.bad_forecasts = pos.bad_forecasts + 1 if bad else 0
        if held_min < prof.turnover.min_hold_minutes:
            return None
        if pos.bad_forecasts >= fx.confirm_evaluations:
            return (
                f"Forecast exit: updated outlook is {hold_net:+.2f}% expected over ~{horizon:.0f} more minutes "
                f"({pos.bad_forecasts} checks in a row)"
            )
        return None

    # ------------------------------------------------------------------ entries
    def _look_for_entries(
        self,
        source: MarketDataSource,
        by_symbol: dict[str, StockContext],
        schedule: SessionSchedule,
        now: datetime,
        rep: TickReport,
    ) -> None:
        assert self.stats is not None
        allowed, why = self._feed_gate(source)
        prof = self.profile
        passing: list[tuple[float, Evaluation, ScanResult, StockContext, Opportunity | None]] = []
        self.latest_evaluations = {}
        mins = (now - schedule.open).total_seconds() / 60

        # predictive strategy: the ranked opportunities
        pred = self.strategies.get("expected_opportunity")
        if pred is not None:
            for o in self.opportunities:
                ctx = by_symbol.get(o.symbol)
                if ctx is None or o.held or not o.actionable:
                    continue
                ev = pred.evaluate_entry(SignalInput(ctx, o.scan.metrics, o.scan, mins, opportunity=o))
                self.latest_evaluations.setdefault(o.symbol, []).append(ev)
                if ev.passed:
                    passing.append((ev.strength, ev, o.scan, ctx, o))

        # classic momentum strategies: only confirmed-tradable names that pass the classic filters
        classic = {k: v for k, v in self.strategies.items() if k != "expected_opportunity"}
        if classic:
            for r in [r for r in self.latest_scan if r.actionable][: self.settings.scanner.top_n]:
                ctx = by_symbol.get(r.symbol)
                if ctx is None:
                    continue
                inp = SignalInput(ctx, r.metrics, r, mins)
                for strat in classic.values():
                    ev = strat.evaluate_entry(inp)
                    self.latest_evaluations.setdefault(r.symbol, []).append(ev)
                    if ev.passed:
                        passing.append((ev.strength * (0.5 + r.score / 200), ev, r, ctx, None))

        passing.sort(key=lambda x: (-x[0], x[1].symbol, x[1].strategy))
        for _rank, ev, r, ctx, opp in passing:
            price = ctx.last_price
            assert price is not None
            sl = ev.stop_loss_pct or self.risk.cfg.stop_loss_pct
            block = self._turnover_block(ev.symbol, prof, now)
            decision = self.risk.check_entry(
                state=self.state,
                phase=schedule.phase_at(now),
                symbol=ev.symbol,
                strategy=ev.strategy,
                price=price,
                stop_loss_pct=sl,
                equity=self.broker.equity(),
                buying_power=self.broker.buying_power(),
                positions=self.broker.positions,
                pending_buy_symbols=self.broker.pending_buy_symbols(),
                stats=self.stats,
                feed_age_seconds=source.feed_age_seconds(now),
                feed_allows_trading=allowed,
                feed_block_reason=why,
                deployed_value=self.broker.deployed_value(),
            )
            if block or not decision.approved:
                why_not = block or "; ".join(decision.reasons)
                if not block and any("max simultaneous" in x for x in decision.reasons) and opp is not None:
                    self._maybe_rotate(opp, prof, schedule, now)
                self._note_once(
                    f"skip:{ev.symbol}:{why_not[:40]}",
                    f"Not buying {ev.symbol}: {why_not}",
                    now,
                    every_minutes=20,
                    symbol=ev.symbol,
                    strategy=ev.strategy,
                )
                continue
            reason = (
                opp.explanation["summary"]
                if opp is not None
                else f"{self.strategies[ev.strategy].name}: {ev.explanation()}"
            )[:600]
            otype, limit = self._order_type_for(Side.BUY, price)
            req = OrderRequest(
                client_order_id=f"{schedule.session.date}-{ev.strategy}-{ev.symbol}-buy",
                symbol=ev.symbol,
                side=Side.BUY,
                quantity=decision.quantity,
                order_type=otype,
                limit_price=limit,
                strategy=ev.strategy,
                reason=reason,
                meta={
                    "stop_loss_pct": sl,
                    "take_profit_pct": ev.take_profit_pct
                    if ev.take_profit_pct is not None
                    else self.risk.cfg.take_profit_pct,
                    "trailing_stop_pct": ev.trailing_stop_pct
                    if ev.trailing_stop_pct is not None
                    else self.risk.cfg.trailing_stop_pct,
                    "max_hold_minutes": ev.max_hold_minutes
                    if ev.max_hold_minutes is not None
                    else self.risk.cfg.max_hold_minutes,
                    "reference": dict(ev.reference),
                    "sizing": decision.sizing,
                    "scan_score": r.score,
                    "mode": prof.mode.value,
                    "plan": {**ev.plan, "cost_pct": (opp.forecast.cost_pct if opp and opp.forecast else 0.0)}
                    if ev.plan
                    else {},
                    "forecast": ev.forecast,
                },
            )
            order = self._submit(req, schedule, now)
            if order is not None and order.status != OrderStatus.REJECTED:
                self.stats.trades_opened += 1
                self.stats.entered_keys.add((ev.strategy, ev.symbol))
                self.stats.entry_times.append(now)
                rep.entries.append(f"{ev.symbol}: {ev.strategy} x{decision.quantity}")
                extra = ""
                if ev.plan:
                    extra = f" Target ${ev.plan['target_price']:.2f}, risk exit ${ev.plan['stop_price']:.2f}."
                self._note(
                    "entry",
                    f"Opening a simulated position: buying {decision.quantity} {ev.symbol}.{extra} {ev.explanation()[:160]}",
                    now=now,
                    symbol=ev.symbol,
                    strategy=ev.strategy,
                    sizing=decision.sizing,
                    score=round(r.score, 1),
                )

    def _turnover_block(self, symbol: str, prof: ModeProfile, now: datetime) -> str:
        assert self.stats is not None
        t = prof.turnover
        last = self.stats.last_exit.get(symbol)
        if last is not None and (now - last) < timedelta(minutes=t.cooldown_minutes):
            return f"sold {int((now - last).total_seconds() // 60)} min ago; waiting {t.cooldown_minutes:g} min before buying it again"
        recent = [x for x in self.stats.entry_times if (now - x) < timedelta(hours=1)]
        if len(recent) >= t.max_entries_per_hour:
            return f"already opened {len(recent)} trades in the last hour (limit {t.max_entries_per_hour})"
        return ""

    def _maybe_rotate(
        self, cand: Opportunity, prof: ModeProfile, schedule: SessionSchedule, now: datetime
    ) -> None:
        """Positions are full: sell the weakest holding only if the new idea beats it by a real margin."""
        t = prof.turnover
        if not t.rotation_enabled or cand.forecast is None:
            return
        worst: tuple[float, Position] | None = None
        for sym, pos in self.broker.positions.items():
            held = self.held_forecasts.get(sym)
            if held is None or held.forecast is None or self.broker.pending_sell_qty(sym) > 0:
                continue
            if (now - pos.opened_at).total_seconds() / 60 < t.min_hold_minutes or pos.breakeven_armed:
                continue  # too new, or already working well (protected at breakeven)
            if worst is None or held.utility < worst[0]:
                worst = (held.utility, pos)
        if worst is None:
            return
        advantage = cand.utility - worst[0]
        needed = t.min_advantage_pct + cand.forecast.cost_pct
        if advantage < needed:
            return
        pos = worst[1]
        reason = f"Rotation: {cand.symbol} looks better by {advantage:+.2f} pts (needs {needed:.2f}) than holding {pos.symbol}"
        n = self._exit_attempts.get(pos.symbol, 0)
        self._exit_attempts[pos.symbol] = n + 1
        ctx_price = self.broker.marks.get(pos.symbol, pos.avg_cost)
        otype, limit = self._order_type_for(Side.SELL, ctx_price)
        self._submit(
            OrderRequest(
                client_order_id=f"{schedule.session.date}-exit-{pos.symbol}-{pos.opened_at:%H%M}-{n}",
                symbol=pos.symbol,
                side=Side.SELL,
                quantity=pos.quantity,
                order_type=otype,
                limit_price=limit,
                strategy=pos.strategy,
                reason=reason,
            ),
            schedule,
            now,
        )
        self._note(
            "exit", f"Selling {pos.symbol}: {reason}", now=now, symbol=pos.symbol, exit_category="rotation"
        )

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
            self._note(
                "mirror",
                f"Mirrored {req.side.value} {req.quantity} {req.symbol} to {self.mirror.name}: {ext.status}",
                now=now,
                symbol=req.symbol,
            )
        except MirrorSkipped as skip:
            self._note(
                "mirror",
                f"{self.mirror.name}: {req.side.value} {req.quantity} {req.symbol} NOT sent - {skip}",
                now=now,
                symbol=req.symbol,
            )
        except ExecutionError as exc:
            self._alert(
                "critical",
                "mirror_failed",
                f"{self.mirror.name} rejected/failed {req.side.value} {req.symbol}: {exc}",
                now,
            )
            if self.settings.pause_on_mirror_failure:
                self.pause()

    # ------------------------------------------------------------------ end of session
    def finish_session(self, schedule: SessionSchedule, now: datetime) -> dict[str, Any]:
        """Final bookkeeping after the close. Never claims liquidation succeeded if positions remain."""
        assert self.stats is not None
        b = self.broker
        b.cancel_all(now)
        unresolved = [
            {
                "symbol": s,
                "quantity": p.quantity,
                "mark": b.marks.get(s),
                "unrealized_pnl": round(p.quantity * (b.marks.get(s, p.avg_cost) - p.avg_cost), 2),
            }
            for s, p in b.positions.items()
        ]
        if unresolved:
            self._alert(
                "critical",
                "overnight_risk",
                "Positions NOT liquidated at close (overnight exposure): "
                + ", ".join(f"{u['symbol']}x{u['quantity']}" for u in unresolved),
                now,
            )
        recon = None
        if self.mirror is not None:
            try:
                from mwbot.execution.base import Holding

                rep = self.mirror.reconcile(
                    [Holding(p.symbol, p.quantity, p.avg_cost) for p in b.positions.values()]
                )
                recon = {"ok": rep.ok, "summary": rep.summary()}
                if not rep.ok:
                    self._alert(
                        "critical",
                        "reconcile_mismatch",
                        f"Reconciliation mismatch vs {self.mirror.name}: {rep.summary()}",
                        now,
                    )
            except ExecutionError as exc:
                self._alert("critical", "reconcile_failed", f"Reconciliation failed: {exc}", now)
        from mwbot.bot.report import build_daily_report

        report = build_daily_report(self, schedule.session.date, unresolved, recon)
        self._note(
            "session", f"Session {schedule.session.date} complete: P/L ${report['pnl']:+,.2f}", now=now
        )
        self.stats = None
        return report


__all__ = ["TradingBot", "TickReport", "classify_exit", "compute_metrics"]
