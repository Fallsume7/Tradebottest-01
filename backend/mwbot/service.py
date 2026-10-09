"""Application service: the single object the API (and CLI) talk to.

Owns settings, the data provider, the paper broker + bot, the (threaded) session runner, the
APScheduler live-clock loop and backtest jobs. Thread-safety: one ``RLock`` guards bot/broker state.
"""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mwbot.backtest.engine import (
    BacktestConfig,
    BacktestResult,
    compare_strategies,
    run_backtest,
    universe_day_return,
)
from mwbot.backtest.metrics import compound, daily_ratio_stats, max_drawdown, trade_stats
from mwbot.bot.engine import TradingBot
from mwbot.bot.report import report_markdown
from mwbot.bot.runner import SessionRunner
from mwbot.calendar import (
    CHICAGO,
    build_schedule,
    is_trading_day,
    next_trading_day,
    previous_trading_day,
    to_chicago,
)
from mwbot.config import (
    PRESET_FILTERS,
    AppSettings,
    EnvSettings,
    scanner_config_for_preset,
)
from mwbot.data.base import DataUnavailableError, MarketDataProvider, MarketDataSource
from mwbot.data.csv_provider import CsvHistoricalProvider, DatasetError
from mwbot.data.massive import MassiveProvider
from mwbot.data.synthetic import SyntheticProvider
from mwbot.db.store import SqlStore, make_engine
from mwbot.domain import FeedInfo, FeedKind, OrderRequest, OrderType, Side, TimeInForce, utcnow
from mwbot.execution.base import ExecutionAdapter
from mwbot.logging_setup import ring_buffer
from mwbot.scanner.scanner import Scanner
from mwbot.strategies.base import SignalInput
from mwbot.strategies.builtin import STRATEGY_CLASSES, build_strategy
from mwbot.trading.broker import PaperBroker
from mwbot.trading.risk import BotState

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


class ServiceError(Exception):
    """User-facing error (HTTP 4xx)."""


@dataclass(slots=True)
class BacktestJob:
    id: str
    status: str = "queued"  # queued | running | done | error | cancelled
    progress: float = 0.0
    message: str = ""
    result_ids: list[str] = field(default_factory=list)
    error: str = ""
    cancel: bool = False


class AppService:
    def __init__(self, env: EnvSettings | None = None, store: SqlStore | None = None) -> None:
        self.env = env or EnvSettings()
        self.store = store or SqlStore(make_engine(self.env.database_url))
        self.lock = threading.RLock()
        self.settings: AppSettings = self.store.load_settings()
        if self.env.data_mode != "demo" and self.store.get_kv("settings") is None:
            self.settings.data_mode = self.env.data_mode
        self.provider_error = ""
        self.provider: MarketDataProvider | None = None
        self.broker = PaperBroker(self.settings.risk.starting_capital, self.settings.costs, store=self.store)
        self.restored = self.store.restore_broker(self.broker)
        self.mirror: ExecutionAdapter | None = None
        self.bot = TradingBot(self.settings, self.broker)
        self.bot.on_decision = self.store.add_decision
        self.runner: SessionRunner | None = None
        self.source: MarketDataSource | None = None
        self.session_status = "idle"  # idle | running | paused_stream | finished | error
        self.session_error = ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_report: dict[str, Any] | None = None
        self.jobs: dict[str, BacktestJob] = {}
        self._bt_cache: dict[str, BacktestResult] = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="backtest")
        self.scheduler: BackgroundScheduler | None = None
        self._live_runner: SessionRunner | None = None
        self._build_provider()
        self._build_mirror()
        if self.bot.state == BotState.STOPPED:
            self.bot.state = BotState(self.store.get_kv("bot_state", "stopped"))
        self._recover_after_restart()

    # ------------------------------------------------------------------ setup
    def _build_provider(self) -> None:
        self.provider_error = ""
        mode = self.settings.data_mode
        try:
            if mode == "demo":
                self.provider = SyntheticProvider(self.settings.demo_seed)
            elif mode == "historical":
                path = Path(self.env.historical_dir) / self.settings.historical_dataset
                self.provider = CsvHistoricalProvider(path)
            else:
                self.provider = MassiveProvider(
                    self.env.massive_api_key,
                    self.env.massive_base_url,
                    feed=self.env.massive_feed,
                    delay_seconds=self.env.massive_delay_seconds,
                )
        except (DataUnavailableError, DatasetError, FileNotFoundError, OSError) as exc:
            self.provider = None
            self.provider_error = f"{type(exc).__name__}: {exc}"
            log.warning("data provider unavailable", extra={"mode": mode, "error": self.provider_error})

    def _build_mirror(self) -> None:
        if self.env.execution_mode == "mock_browser":
            from mwbot.execution.factory import create_adapter

            try:
                self.mirror = create_adapter(self.env)
                self.bot.mirror = self.mirror
            except Exception as exc:  # adapter is optional; surface but do not crash
                self.bot.mirror = None
                self.mirror = None
                self.provider_error = self.provider_error or f"mock browser adapter unavailable: {exc}"
                log.warning("mirror adapter unavailable", extra={"error": str(exc)})

    def _recover_after_restart(self) -> None:
        """A restart cannot resume an in-memory replay. Never pretend: flag open exposure loudly."""
        if self.broker.positions or self.broker.open_orders():
            msg = (
                f"Restarted with {len(self.broker.positions)} open position(s) and "
                f"{len(self.broker.open_orders())} pending order(s) restored from the database. "
                "Start a session to resume management, or flatten via Positions."
            )
            self.bot._alert("warning", "restart_recovery", msg, utcnow())
            log.warning(msg)

    def close(self) -> None:
        self.stop_session()
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self.mirror:
            self.mirror.close()

    # ------------------------------------------------------------------ settings
    def update_settings(self, patch: dict[str, Any]) -> AppSettings:
        with self.lock:
            running = self.session_status == "running"
            merged = _deep_merge(self.settings.model_dump(mode="json"), patch)
            try:
                new = AppSettings.model_validate(merged)
                for sid in new.strategies.enabled:
                    build_strategy(sid, new.strategies.params.get(sid))
            except Exception as exc:
                raise ServiceError(f"Invalid settings: {exc}") from exc
            if running:
                locked = [
                    k
                    for k in (
                        "data_mode",
                        "demo_seed",
                        "historical_dataset",
                        "schedule",
                        "costs",
                        "auto_schedule",
                    )
                    if getattr(new, k) != getattr(self.settings, k)
                ]
                if new.risk.starting_capital != self.settings.risk.starting_capital:
                    locked.append("risk.starting_capital")
                if locked:
                    raise ServiceError(
                        f"Stop the running session before changing: {', '.join(locked)}. "
                        "Scanner, strategy and risk-limit changes are allowed live."
                    )
            mode_changed = (
                new.data_mode != self.settings.data_mode
                or new.demo_seed != self.settings.demo_seed
                or new.historical_dataset != self.settings.historical_dataset
            )
            capital_changed = new.risk.starting_capital != self.settings.risk.starting_capital
            self.settings = new
            self.bot.apply_settings(new)
            if capital_changed and not self.broker.trades and not self.broker.positions:
                self.broker.cash = self.broker.starting_cash = new.risk.starting_capital
                self.broker._persist()
            if mode_changed:
                self._build_provider()
            self.store.save_settings(new)
            self._sync_scheduler()
            return new

    def apply_scanner_preset(self, name: str) -> AppSettings:
        if name not in PRESET_FILTERS:
            raise ServiceError(f"unknown preset {name!r}")
        cfg = scanner_config_for_preset(name)
        return self.update_settings({"scanner": cfg.model_dump(mode="json")})

    # ------------------------------------------------------------------ session control
    def _pick_date(self, requested: date | None) -> date:
        if requested:
            if not is_trading_day(requested):
                raise ServiceError(f"{requested} is not a US exchange trading day")
            return requested
        mode = self.settings.data_mode
        if mode == "historical" and isinstance(self.provider, CsvHistoricalProvider):
            dates = self.provider.available_dates()
            done = {r["date"] for r in self.store.reports()}
            remaining = [d for d in dates if d.isoformat() not in done]
            if not remaining:
                raise ServiceError(
                    "All dates in this dataset have been replayed. Reset the account to replay again."
                )
            return remaining[0]
        if mode == "connected":
            today = utcnow().astimezone(ET).date()
            if not is_trading_day(today):
                raise ServiceError("Today is not a trading day; connected mode only serves the live session.")
            return today
        last = self.store.get_kv("last_demo_date")
        if last:
            return next_trading_day(date.fromisoformat(last))
        return previous_trading_day(utcnow().astimezone(ET).date() + timedelta(days=0))

    def start_session(
        self, d: date | None = None, speed: float | None = None, instant: bool = False
    ) -> dict[str, Any]:
        with self.lock:
            if self.session_status == "running":
                raise ServiceError("A session is already running.")
            if self.provider is None:
                raise ServiceError(self.provider_error or "No data provider available for this mode.")
            if self.bot.state == BotState.EMERGENCY_STOP:
                raise ServiceError("Emergency stop is active. Clear it in Settings/Controls before starting.")
            sess_date = self._pick_date(d)
            try:
                self.source = self.provider.open_session(sess_date)
            except DataUnavailableError as exc:
                raise ServiceError(str(exc)) from exc
            self.runner = SessionRunner.for_date(self.bot, self.source, sess_date)
            self.bot.fast = False
            if self.bot.state in (BotState.STOPPED,):
                self.bot.start()
            self.store.set_kv("bot_state", str(self.bot.state))
            self.last_report = None
            self.session_error = ""
            self.session_status = "running"
            self._stop.clear()
            sps = 0.0 if instant else 1.0 / max(speed or self.settings.demo_speed, 0.01)
            self._thread = threading.Thread(target=self._loop, args=(sps,), name="session", daemon=True)
            self._thread.start()
        return {"date": sess_date.isoformat(), "instant": instant}

    def _loop(self, seconds_per_step: float) -> None:
        try:
            while not self._stop.is_set():
                with self.lock:
                    assert self.runner is not None
                    rep = self.runner.step()
                    if rep is not None:
                        self.store.add_equity(rep.now, rep.equity, self.runner.schedule.session.date)
                    finished = self.runner.finished
                if finished:
                    self._finalise()
                    return
                if seconds_per_step > 0 and self._stop.wait(seconds_per_step):
                    break
        except Exception as exc:  # never die silently: surface on the dashboard
            log.exception("session loop crashed")
            with self.lock:
                self.session_status = "error"
                self.session_error = f"{type(exc).__name__}: {exc}"
                self.bot._alert("critical", "session_crash", f"Session loop crashed: {exc}", utcnow())
            return
        with self.lock:
            if self.session_status == "running":
                self.session_status = "idle"

    def _finalise(self) -> None:
        with self.lock:
            assert self.runner is not None and self.source is not None
            report = dict(self.runner.report or {})
            if hasattr(self.source, "full_day") and self.source.is_replay():
                report["benchmark_return_pct"] = round(universe_day_return(self.source), 4)
            self.last_report = report
            self.store.save_report(report)
            self.store.set_kv("last_demo_date", report["date"])
            self._write_report_file(report)
            self.session_status = "finished"

    def _write_report_file(self, report: dict[str, Any]) -> None:
        try:
            out = Path(self.env.data_dir) / "reports"
            out.mkdir(parents=True, exist_ok=True)
            (out / f"daily_{report['date']}.md").write_text(report_markdown(report), encoding="utf-8")
        except OSError as exc:
            log.warning("could not write report file", extra={"error": str(exc)})

    def step_session(self, n: int = 1) -> dict[str, Any]:
        """Manually advance a *not running* session (for step-through debugging in the UI)."""
        with self.lock:
            if self.session_status == "running":
                raise ServiceError("Session is streaming; pause it first.")
            if self.runner is None or self.runner.finished:
                raise ServiceError("No active session. Start one first.")
            for _ in range(max(1, min(n, 120))):
                rep = self.runner.step()
                if rep is not None:
                    self.store.add_equity(rep.now, rep.equity, self.runner.schedule.session.date)
                if self.runner.finished:
                    self._finalise()
                    break
            return self.status()

    def pause_stream(self) -> None:
        """Stop the streaming thread but keep the session stepable."""
        with self.lock:
            if self.session_status != "running":
                raise ServiceError("No streaming session.")
            self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        with self.lock:
            self.session_status = "paused_stream"

    def resume_stream(self, speed: float | None = None) -> None:
        with self.lock:
            if self.session_status != "paused_stream":
                raise ServiceError("Session is not paused.")
            self.session_status = "running"
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                args=(1.0 / max(speed or self.settings.demo_speed, 0.01),),
                name="session",
                daemon=True,
            )
            self._thread.start()

    def stop_session(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=10)
        with self.lock:
            if self.session_status in ("running", "paused_stream"):
                self.session_status = "idle"
                self.runner = None

    def wait_for_session(self, timeout: float = 120) -> None:
        t = self._thread
        if t:
            t.join(timeout)

    # ------------------------------------------------------------------ bot controls
    def control(self, action: str) -> dict[str, Any]:
        with self.lock:
            if action == "start":
                self.bot.start()
            elif action == "pause":
                self.bot.pause()
            elif action == "resume":
                self.bot.resume()
            elif action == "emergency_stop":
                self.bot.emergency_stop()
            elif action == "clear_emergency":
                self.bot.reset_emergency()
            else:
                raise ServiceError(f"unknown action {action!r}")
            self.store.set_kv("bot_state", str(self.bot.state))
            self.broker._persist()
            return {"state": str(self.bot.state)}

    def reset_account(self) -> None:
        with self.lock:
            if self.session_status == "running":
                raise ServiceError("Stop the running session first.")
            self.stop_session()
            self.store.reset_account()
            self.store.set_kv("last_demo_date", None)
            self.broker = PaperBroker(
                self.settings.risk.starting_capital, self.settings.costs, store=self.store
            )
            self.bot = TradingBot(self.settings, self.broker, mirror=self.mirror)
            self.bot.on_decision = self.store.add_decision
            self.runner = None
            self.source = None
            self.last_report = None
            self.session_status = "idle"
            self.bot.state = BotState.STOPPED
            self.store.set_kv("bot_state", "stopped")

    # ------------------------------------------------------------------ scheduler (live clock)
    def start_scheduler(self) -> None:
        if self.scheduler is None:
            self.scheduler = BackgroundScheduler(timezone=CHICAGO)
            self.scheduler.start()
        self._sync_scheduler()

    def _sync_scheduler(self) -> None:
        if self.scheduler is None:
            return
        job = self.scheduler.get_job("live_tick")
        if self.settings.auto_schedule and job is None:
            self.scheduler.add_job(
                self._live_tick,
                IntervalTrigger(seconds=self.settings.schedule.tick_seconds),
                id="live_tick",
                max_instances=1,
                coalesce=True,
            )
            log.info("live-clock scheduler enabled")
        elif not self.settings.auto_schedule and job is not None:
            self.scheduler.remove_job("live_tick")
            log.info("live-clock scheduler disabled")

    def _live_tick(self) -> None:
        """Wall-clock driven loop. Runs only on real exchange trading days between scanner start and report time."""
        now = utcnow()
        d = now.astimezone(ET).date()
        if not is_trading_day(d) or self.provider is None or self.session_status == "running":
            return
        sched = build_schedule(d, self.settings.schedule)
        if sched is None or now < sched.scanner_start or now > sched.report_at:
            return
        with self.lock:
            if self._live_runner is None or self._live_runner.schedule.session.date != d.isoformat():
                try:
                    src = self.provider.open_session(d)
                except DataUnavailableError as exc:
                    self.bot._alert("critical", "data_unavailable", f"Live session unavailable: {exc}", now)
                    return
                self.source = src
                self._live_runner = SessionRunner(self.bot, src, sched)
                self.runner = self._live_runner
                if self.bot.state == BotState.STOPPED:
                    self.bot.start()
            r = self._live_runner
            if r.finished:
                return
            r.now = now.replace(second=0, microsecond=0)
            rep = r.bot.tick(r.source, r.schedule, r.now)
            self.store.add_equity(rep.now, rep.equity, d.isoformat())
            if now >= sched.close:
                r.report = self.bot.finish_session(sched, r.now)
                r.finished = True
                self.session_status = "finished"
                self._finalise()
            else:
                self.session_status = "live"

    # ------------------------------------------------------------------ read models
    def _now(self) -> datetime | None:
        return self.runner.now if self.runner else None

    def feed(self) -> dict[str, Any]:
        if self.provider is None:
            return FeedInfo(
                FeedKind.HISTORICAL, "none", "NO DATA FEED", warnings=[self.provider_error]
            ).to_dict() | {"available": False}
        return self.provider.feed_info().to_dict() | {"available": True}

    def status(self) -> dict[str, Any]:
        with self.lock:
            now = self._now()
            sched = self.runner.schedule if self.runner else None
            b = self.broker
            if self.bot.stats:
                start_eq = self.bot.stats.start_equity
            elif self.last_report:  # session finished: keep showing that day's result
                start_eq = float(self.last_report["start_equity"])
            else:
                start_eq = b.equity()
            return {
                "bot_state": str(self.bot.state),
                "session_status": self.session_status,
                "session_error": self.session_error,
                "phase": str(self.bot.phase)
                if self.runner and not self.runner.finished
                else ("post_close" if self.runner and self.runner.finished else "idle"),
                "session_date": sched.session.date if sched else None,
                "sim_time_utc": now.isoformat() if now else None,
                "sim_time_chicago": to_chicago(now).strftime("%Y-%m-%d %H:%M") if now else None,
                "progress": self.runner.progress if self.runner else 0.0,
                "schedule": sched.to_dict() if sched else None,
                "data_mode": self.settings.data_mode,
                "execution_mode": self.env.execution_mode,
                "real_money": False,
                "feed": self.feed(),
                "provider_error": self.provider_error,
                "portfolio": {
                    "equity": round(b.equity(), 2),
                    "cash": round(b.cash, 2),
                    "buying_power": round(b.buying_power(), 2),
                    "starting_capital": b.starting_cash,
                    "total_return_pct": round((b.equity() / b.starting_cash - 1) * 100, 4)
                    if b.starting_cash
                    else 0.0,
                    "daily_pnl": round(b.equity() - start_eq, 2),
                    "daily_pnl_pct": round((b.equity() / start_eq - 1) * 100, 4) if start_eq else 0.0,
                    "realized_pnl": round(b.realized_pnl, 2),
                    "unrealized_pnl": round(b.unrealized_pnl(), 2),
                    "open_positions": len(b.positions),
                    "pending_orders": len(b.open_orders()),
                },
                "alerts": list(self.bot.alerts)[-10:],
                "alerts_critical": sum(1 for a in self.bot.alerts if a["level"] == "critical"),
                "scheduler_enabled": bool(self.scheduler and self.scheduler.get_job("live_tick")),
                "restored_from_db": self.restored,
            }

    def positions(self) -> list[dict[str, Any]]:
        with self.lock:
            out = []
            for p in self.broker.positions.values():
                mark = self.broker.marks.get(p.symbol, p.avg_cost)
                pending = [
                    o.id for o in self.broker.open_orders() if o.symbol == p.symbol and o.side == Side.SELL
                ]
                out.append(
                    {
                        "symbol": p.symbol,
                        "quantity": p.quantity,
                        "avg_cost": round(p.avg_cost, 4),
                        "mark": round(mark, 4),
                        "market_value": round(p.quantity * mark, 2),
                        "unrealized_pnl": round(p.unrealized(mark), 2),
                        "unrealized_pct": round((mark / p.avg_cost - 1) * 100, 3) if p.avg_cost else 0,
                        "strategy": p.strategy,
                        "opened_at": p.opened_at.isoformat(),
                        "entry_reason": p.entry_reason,
                        "stop_price": p.stop_price,
                        "take_profit_price": p.take_profit_price,
                        "trailing_stop_pct": p.trailing_stop_pct,
                        "high_water_mark": round(p.high_water_mark, 4),
                        "max_hold_minutes": p.max_hold_minutes,
                        "exit_order_ids": pending,
                    }
                )
            return out

    def orders(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.lock:
            rows = sorted(self.broker.orders.values(), key=lambda o: o.id, reverse=True)[:limit]
            return [
                {
                    "id": o.id,
                    "client_order_id": o.client_order_id,
                    "symbol": o.symbol,
                    "side": str(o.side),
                    "quantity": o.quantity,
                    "filled_quantity": o.filled_quantity,
                    "order_type": str(o.order_type),
                    "limit_price": o.limit_price,
                    "status": str(o.status),
                    "avg_fill_price": round(o.avg_fill_price, 4),
                    "strategy": o.strategy,
                    "reason": o.reason,
                    "reject_reason": o.reject_reason,
                    "created_at": o.created_at.isoformat(),
                    "updated_at": o.updated_at.isoformat() if o.updated_at else None,
                    "external_id": o.meta.get("external_id"),
                    "history": [[t.isoformat(), s, n] for t, s, n in o.history],
                }
                for o in rows
            ]

    def trades(self) -> list[dict[str, Any]]:
        with self.lock:
            return [t.to_dict() for t in reversed(self.broker.trades)]

    def scan(self) -> dict[str, Any]:
        with self.lock:
            res = self.bot.latest_scan
            return {
                "as_of": self.bot.last_tick.isoformat() if self.bot.last_tick else None,
                "feed": self.feed(),
                "config": self.settings.scanner.model_dump(mode="json"),
                "results": [r.to_dict() for r in res],
                "n_tradable": sum(1 for r in res if r.tradable),
            }

    def stock(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()
        with self.lock:
            if self.runner is None or self.source is None:
                raise ServiceError("Start a session to inspect symbols.")
            now = self.runner.now
            ctx = self.source.context_for(symbol, now)
            if ctx is None:
                raise ServiceError(f"{symbol} is not in the current universe")
            scanner = Scanner(self.settings.scanner)
            scan = scanner.scan_one(ctx)
            mins = (now - self.runner.schedule.open).total_seconds() / 60
            inp = SignalInput(ctx, scan.metrics, scan, mins)
            evs = [
                build_strategy(sid, self.settings.strategies.params.get(sid)).evaluate_entry(inp).to_dict()
                for sid in STRATEGY_CLASSES
            ]
            bars = ctx.bars.tail(600)
            pos = self.broker.positions.get(symbol)
            return {
                "symbol": symbol,
                "name": ctx.meta.name,
                "as_of": now.isoformat(),
                "feed": self.feed(),
                "bars": [
                    {
                        "t": int(pd.Timestamp(str(ts)).timestamp()),
                        "o": float(r.open),
                        "h": float(r.high),
                        "l": float(r.low),
                        "c": float(r.close),
                        "v": float(r.volume),
                    }
                    for ts, r in bars.iterrows()
                ],
                "open_time": int(self.runner.schedule.open.timestamp()),
                "scan": scan.to_dict(),
                "evaluations": evs,
                "halted": ctx.halted,
                "position": next((p for p in self.positions() if p["symbol"] == symbol), None),
                "held": pos is not None,
            }

    def manual_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "market",
        limit_price: float | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            if self.runner is None or self.runner.finished:
                raise ServiceError("Manual simulated orders need an active session.")
            if self.bot.state == BotState.EMERGENCY_STOP:
                raise ServiceError("Emergency stop is active.")
            req = OrderRequest(
                client_order_id=client_order_id or f"manual-{uuid.uuid4().hex[:12]}",
                symbol=symbol.upper(),
                side=Side(side),
                quantity=int(quantity),
                order_type=OrderType(order_type),
                limit_price=limit_price,
                time_in_force=TimeInForce.DAY,
                strategy="manual",
                reason="Manual simulated order from dashboard",
                meta={
                    "stop_loss_pct": self.settings.risk.stop_loss_pct,
                    "take_profit_pct": self.settings.risk.take_profit_pct,
                },
            )
            o = self.broker.submit(req, self.runner.now, expires_at=self.runner.schedule.close)
            return {"id": o.id, "status": str(o.status), "reject_reason": o.reject_reason}

    def cancel_order(self, order_id: str) -> bool:
        with self.lock:
            return self.broker.cancel(order_id)

    def equity_history(self) -> list[dict[str, Any]]:
        return self.store.equity_history()

    def decisions(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.recent_decisions(limit)

    def reports(self) -> list[dict[str, Any]]:
        return self.store.reports()

    def strategies(self) -> list[dict[str, Any]]:
        out = []
        for sid in STRATEGY_CLASSES:
            inst = build_strategy(sid, self.settings.strategies.params.get(sid))
            d = inst.describe()
            d["enabled"] = sid in self.settings.strategies.enabled
            out.append(d)
        return out

    def performance(self) -> dict[str, Any]:
        with self.lock:
            trades = list(self.broker.trades)
            reports = self.store.reports()
            start = self.broker.starting_cash
            eq = [start] + [r["end_equity"] for r in reports]
            daily = [r["pnl_pct"] for r in reports]
            bench = [
                float(r["benchmark_return_pct"]) for r in reports if r.get("benchmark_return_pct") is not None
            ]
            dd, dd_abs = max_drawdown(eq)
            by = {
                sid: trade_stats([t for t in trades if t.strategy == sid])
                for sid in sorted({t.strategy for t in trades})
            }
            return {
                "starting_capital": start,
                "equity": round(self.broker.equity(), 2),
                "total_return_pct": round((self.broker.equity() / start - 1) * 100, 4) if start else 0.0,
                "benchmark_return_pct": round(compound(bench), 4) if bench else None,
                "benchmark_note": "Equal-weight average open-to-close return of the simulated universe (not an index).",
                "max_drawdown_pct": round(dd * 100, 3),
                "max_drawdown_usd": round(dd_abs, 2),
                "daily": [
                    {
                        "date": r["date"],
                        "pnl": r["pnl"],
                        "pnl_pct": r["pnl_pct"],
                        "n_trades": r["n_trades"],
                        "end_equity": r["end_equity"],
                        "benchmark_pct": r.get("benchmark_return_pct"),
                    }
                    for r in reports
                ],
                "trade_stats": trade_stats(trades),
                "daily_stats": daily_ratio_stats(daily),
                "by_strategy": by,
            }

    def logs(self) -> list[dict[str, Any]]:
        return list(ring_buffer.records)[-100:]

    # ------------------------------------------------------------------ backtests
    def datasets(self) -> list[dict[str, Any]]:
        root = Path(self.env.historical_dir)
        out = []
        if root.exists():
            for p in sorted(root.iterdir()):
                if (p / "manifest.json").exists():
                    import json

                    m = json.loads((p / "manifest.json").read_text(encoding="utf-8"))
                    out.append(
                        {
                            "id": p.name,
                            "name": m.get("name", p.name),
                            "synthetic": bool(m.get("synthetic")),
                            "dates": m.get("dates"),
                            "description": m.get("description", ""),
                        }
                    )
        return out

    def _provider_for_backtest(self, dataset: str | None) -> MarketDataProvider:
        if dataset in (None, "", "demo"):
            return SyntheticProvider(self.settings.demo_seed)
        try:
            return CsvHistoricalProvider(Path(self.env.historical_dir) / dataset)
        except (DatasetError, FileNotFoundError, OSError) as exc:
            raise ServiceError(f"dataset {dataset!r} unavailable: {exc}") from exc

    def start_backtest(
        self, config: BacktestConfig, dataset: str | None, compare: list[str] | None = None
    ) -> str:
        provider = self._provider_for_backtest(dataset)
        for sid in config.strategies + (compare or []):
            if sid not in STRATEGY_CLASSES:
                raise ServiceError(f"unknown strategy {sid!r}")
        if dataset in (None, "", "demo") and not config.dates:
            n = 5
            d = previous_trading_day(utcnow().astimezone(ET).date())
            dates = [d]
            while len(dates) < n:
                dates.append(previous_trading_day(dates[-1]))
            config = config.model_copy(update={"dates": sorted(dates)})
        job = BacktestJob(id=uuid.uuid4().hex[:10])
        self.jobs[job.id] = job

        def work() -> None:
            job.status = "running"
            try:

                def prog(i: int, n: int, label: str) -> None:
                    job.progress = i / max(n, 1)
                    job.message = label

                if compare:
                    results = compare_strategies(provider, config, compare, progress=prog)
                else:
                    results = [run_backtest(provider, config, progress=prog, cancel=lambda: job.cancel)]
                for r in results:
                    self._bt_cache[r.id] = r
                    self.store.save_backtest(
                        r.id,
                        r.config.get("name", "backtest"),
                        r.config,
                        r.summary,
                        r.to_dict(include_trades=True),
                    )
                    job.result_ids.append(r.id)
                job.status = "cancelled" if job.cancel else "done"
                job.progress = 1.0
            except Exception as exc:
                log.exception("backtest failed")
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"

        self._pool.submit(work)
        return job.id

    def job(self, job_id: str) -> dict[str, Any]:
        j = self.jobs.get(job_id)
        if j is None:
            raise ServiceError("unknown job")
        return {
            "id": j.id,
            "status": j.status,
            "progress": j.progress,
            "message": j.message,
            "result_ids": j.result_ids,
            "error": j.error,
        }

    def backtest_csv(self, bt_id: str, kind: str) -> str:
        from mwbot.backtest.engine import summary_to_csv, trades_to_csv

        r = self._bt_cache.get(bt_id)
        if r is None:
            raise ServiceError("backtest not in memory; re-run it (CSV export uses the in-memory result)")
        return trades_to_csv(r.trades) if kind == "trades" else summary_to_csv(r)


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
