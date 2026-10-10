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
from pydantic import ValidationError

from mwbot import settings_schema as schema
from mwbot.backtest.engine import (
    BacktestConfig,
    BacktestResult,
    compare_modes,
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
    TradingMode,
    scanner_config_for_preset,
)
from mwbot.data.base import DataUnavailableError, MarketDataProvider, MarketDataSource
from mwbot.data.csv_provider import CsvHistoricalProvider, DatasetError
from mwbot.data.massive import MassiveProvider
from mwbot.data.synthetic import SyntheticProvider
from mwbot.db.store import SqlStore, make_engine
from mwbot.domain import FeedInfo, FeedKind, OrderRequest, OrderType, Side, TimeInForce, utcnow
from mwbot.eligibility.csvio import export_csv, parse_csv
from mwbot.eligibility.demo import demo_seed
from mwbot.eligibility.models import EligibilityRecord, EligibilityStatus
from mwbot.eligibility.universe import EligibilityUniverse
from mwbot.eligibility.verifier import MarketWatchVerifier
from mwbot.execution.base import ExecutionAdapter
from mwbot.forecast.jobs import evaluate_models
from mwbot.forecast.models import Forecaster, HeuristicForecaster, RidgeForecaster
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


class SettingsError(ServiceError):
    """Settings rejected; ``errors`` lists the offending fields so the UI can mark them."""

    def __init__(self, message: str, errors: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


@dataclass(slots=True)
class BacktestJob:
    id: str
    status: str = "queued"  # queued | running | done | error | cancelled
    progress: float = 0.0
    message: str = ""
    result_ids: list[str] = field(default_factory=list)
    error: str = ""
    cancel: bool = False
    kind: str = "backtest"  # backtest | compare_modes | model_check
    payload: dict[str, Any] | None = None


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
        self._build_provider()
        self.broker = PaperBroker(self.settings.risk.starting_capital, self.settings.costs, store=self.store)
        self.restored = self.store.restore_broker(self.broker)
        self.mirror: ExecutionAdapter | None = None
        self.eligibility_source = ""
        self.bot = TradingBot(self.settings, self.broker, eligibility=self._load_universe())
        self.bot.on_decision = self.store.add_decision
        self.forecaster_info: dict[str, Any] = {}
        self._apply_forecaster()
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
        self.settings_saved_at: str | None = None
        self._build_mirror()
        if self.bot.state == BotState.STOPPED:
            self.bot.state = BotState(self.store.get_kv("bot_state", "stopped"))
        self._recover_after_restart()

    # ------------------------------------------------------------------ eligibility + forecaster
    def _load_universe(self) -> EligibilityUniverse:
        """The user's own list. A first-run DEMO install is seeded with a clearly labelled demo list (so demo mode
        works out of the box); real lists are never invented."""
        recs = self.store.eligibility_all()
        self.eligibility_source = "your list"
        if not recs and self.settings.data_mode == "demo" and not self.store.get_kv("eligibility_seeded"):
            recs = demo_seed()
            self.store.eligibility_upsert(recs)
            self.store.set_kv("eligibility_seeded", True)
            self.eligibility_source = "demo list (made-up, for demo mode)"
        elif not recs:
            ds = getattr(self.provider, "eligibility_records", None)
            if callable(ds):
                recs = list(ds())
                self.eligibility_source = "dataset's own eligibility history"
        return EligibilityUniverse(recs)

    def refresh_universe(self) -> None:
        with self.lock:
            self.bot.set_universe(self._load_universe())

    def _apply_forecaster(self) -> None:
        choice = self.settings.trading.forecaster
        model: Forecaster | None = None
        saved = self.store.get_kv("model:ridge")
        if saved and choice in ("auto", "statistical"):
            try:
                cand = RidgeForecaster.from_dict(saved)
                if choice == "statistical" or cand.calibrated:
                    model = cand
            except (KeyError, ValueError, TypeError):
                log.warning("saved forecast model could not be loaded; using the rule-based model")
        self.bot.set_forecaster(model or HeuristicForecaster())
        used = model if model is not None else self.bot.ranker.forecaster
        self.forecaster_info = {
            "name": used.name,
            "kind": used.kind,
            "calibrated": bool(getattr(used, "calibrated", False)),
            "trained_through": getattr(used, "trained_through", "") or None,
            "label": (
                "Validated out-of-sample forecast model"
                if getattr(used, "calibrated", False)
                else "Rule-of-thumb estimate - not a validated probability"
                if used.kind == "heuristic"
                else "Statistical model - NOT validated (treat probabilities as rough)"
            ),
            "requested": choice,
        }

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
        if self.env.execution_mode in ("mock_browser", "marketwatch"):
            from mwbot.execution.factory import create_adapter

            try:
                self.mirror = create_adapter(self.env)
                self.bot.mirror = self.mirror
                if self.env.execution_mode == "marketwatch":
                    st = self.mirror.check_session()
                    log.info("marketwatch session", extra={"logged_in": st.logged_in, "detail": st.detail})
                    if not st.logged_in:
                        self.bot._alert("warning", "marketwatch_session", st.detail, utcnow())
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
    def settings_schema(self) -> dict[str, Any]:
        with self.lock:
            out = schema.build_schema(self.settings)
            out["saved_at"] = self.settings_saved_at
            out["open_positions"] = len(self.broker.positions)
            out["session_running"] = self.session_status in ("running", "paused_stream")
            return out

    def _install(self, new: AppSettings) -> AppSettings:
        """Make ``new`` the live configuration. Caller holds the lock and has validated ``new``."""
        old = self.settings
        running = self.session_status in ("running", "paused_stream", "live")
        busy = running or bool(self.broker.positions)
        locked = [
            k for k in ("data_mode", "demo_seed", "historical_dataset") if getattr(new, k) != getattr(old, k)
        ]
        if new.risk.starting_capital != old.risk.starting_capital and (
            busy or self.broker.trades or self.broker.fills
        ):
            locked.append("risk.starting_capital")
        if locked and busy:
            raise SettingsError(
                f"Stop the session and close positions before changing: {', '.join(locked)}. "
                "Everything else can be changed while the bot runs.",
                [{"path": k, "message": "Cannot change while running or holding positions."} for k in locked],
            )
        if "risk.starting_capital" in locked:
            raise SettingsError(
                "Starting capital can only change on a fresh account. Reset the account first.",
                [
                    {
                        "path": "risk.starting_capital",
                        "message": "Reset the account to change starting capital.",
                    }
                ],
            )
        mode_changed = (
            new.data_mode != old.data_mode
            or new.demo_seed != old.demo_seed
            or new.historical_dataset != old.historical_dataset
        )
        capital_changed = new.risk.starting_capital != old.risk.starting_capital
        self.settings = new
        self.bot.apply_settings(new)
        if capital_changed and not self.broker.trades and not self.broker.positions:
            self.broker.cash = self.broker.starting_cash = new.risk.starting_capital
            self.broker._persist()
        if new.schedule != old.schedule and self.runner is not None and not self.runner.finished:
            sched = build_schedule(date.fromisoformat(self.runner.schedule.session.date), new.schedule)
            if sched is not None:
                self.runner.schedule = sched  # rules for the rest of today change too
        if mode_changed:
            self._build_provider()
            self.refresh_universe()
        if new.trading.forecaster != old.trading.forecaster:
            self._apply_forecaster()
        self.store.save_settings(new)
        self.settings_saved_at = utcnow().isoformat()
        self._sync_scheduler()
        return new

    def update_settings(self, patch: dict[str, Any]) -> AppSettings:
        """Nested patch (used by presets and older clients)."""
        with self.lock:
            merged = _deep_merge(self.settings.model_dump(mode="json"), patch)
            try:
                new = AppSettings.model_validate(merged)
                for sid in {*new.strategies.enabled, *new.strategies.params}:
                    build_strategy(sid, new.strategies.params.get(sid))
            except Exception as exc:
                raise ServiceError(f"Invalid settings: {exc}") from exc
            return self._install(new)

    def _base_for_changes(self, changes: dict[str, Any]) -> tuple[AppSettings, dict[str, Any]]:
        """A mode change is applied first (swapping in that mode's risk block) so ``mode.*`` paths in the same
        request refer to the NEW mode."""
        rest = dict(changes)
        mode = rest.pop("trading.mode", None)
        if mode is None or mode == self.settings.trading.mode.value:
            return self.settings, rest
        if mode not in {m.value for m in TradingMode}:
            raise SettingsError(
                "Unknown trading mode.", [{"path": "trading.mode", "message": "Pick one of the two modes."}]
            )
        return self.settings.with_mode(TradingMode(mode)), rest

    def preview_changes(self, changes: dict[str, Any]) -> dict[str, Any]:
        """What WOULD happen, without saving anything."""
        with self.lock:
            base, rest = self._base_for_changes(changes)
            new, errors = schema.apply_changes(base, rest)
            effects = schema.effects_of(
                self.settings,
                changes,
                session_running=self.session_status in ("running", "paused_stream", "live"),
                open_positions=len(self.broker.positions),
            )
            return {"valid": not errors, "errors": errors, "effects": effects}

    def update_changes(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Flat ``{path: value}`` changes from the Settings page (validated on the server with the same bounds)."""
        with self.lock:
            if not changes:
                return {
                    "settings": self.settings.model_dump(mode="json"),
                    "effects": [],
                    "saved_at": self.settings_saved_at,
                }
            base, rest = self._base_for_changes(changes)
            new, errors = schema.apply_changes(base, rest)
            if errors or new is None:
                raise SettingsError("; ".join(e["message"] for e in errors), errors)
            effects = schema.effects_of(
                self.settings,
                changes,
                session_running=self.session_status in ("running", "paused_stream", "live"),
                open_positions=len(self.broker.positions),
            )
            try:
                for sid in new.strategies.enabled:
                    build_strategy(sid, new.strategies.params.get(sid))
            except Exception as exc:
                raise SettingsError(f"Invalid strategy settings: {exc}") from exc
            self._install(new)
            return {
                "settings": self.settings.model_dump(mode="json"),
                "effects": effects,
                "saved_at": self.settings_saved_at,
            }

    def set_mode(self, mode: str) -> dict[str, Any]:
        with self.lock:
            before = self.settings.trading.mode.value
            res = self.update_changes({"trading.mode": mode})
            res["mode"] = {
                "from": before,
                "to": self.settings.trading.mode.value,
                "open_positions": len(self.broker.positions),
                "message": (
                    f"Now trading in {self.settings.trading.mode.label} mode. New trades use its rules; "
                    + (
                        f"your {len(self.broker.positions)} open position(s) keep the plan they were opened with. "
                        if self.broker.positions
                        else ""
                    )
                    + "Trade history is unchanged."
                ),
            }
            return res

    def reset_settings(self, category: str | None = None) -> dict[str, Any]:
        with self.lock:
            if category and category not in {c["id"] for c in schema.CATEGORIES}:
                raise ServiceError(f"unknown settings category {category!r}")
            busy = self.session_status in ("running", "paused_stream", "live") or bool(self.broker.positions)
            changes = {
                p: v
                for p, v in schema.recommended_changes(self.settings, category).items()
                if not (busy and schema.BY_PATH[p].effect == "idle_only")
            }
            res = self.update_changes(changes)
            res["reset"] = category or "all"
            return res

    def export_settings(self) -> dict[str, Any]:
        with self.lock:
            return {
                "app": "MarketWatchBot",
                "format": 1,
                "exported_at": utcnow().isoformat(),
                "note": "Settings only - no keys, no account data. Safe to share.",
                "settings": self.settings.model_dump(mode="json"),
            }

    def import_settings(self, payload: dict[str, Any], apply: bool = True) -> dict[str, Any]:
        with self.lock:
            if payload.get("app") != "MarketWatchBot" or "settings" not in payload:
                raise SettingsError("This file is not a MarketWatchBot settings export.")
            if int(payload.get("format", 0)) != 1:
                raise SettingsError("Unsupported settings file version.")
            try:
                new = AppSettings.model_validate(payload["settings"])
            except ValidationError as exc:
                errs = schema.friendly_errors(exc)
                raise SettingsError(
                    "The file's settings are not valid: " + "; ".join(e["message"] for e in errs), errs
                ) from exc
            changes = {
                f.path: schema.get_value(new, f.path)
                for f in schema.FIELDS
                if not schema._same(schema.get_value(new, f.path), schema.get_value(self.settings, f.path))
            }
            effects = schema.effects_of(
                self.settings,
                changes,
                session_running=self.session_status in ("running", "paused_stream", "live"),
                open_positions=len(self.broker.positions),
            )
            if apply:
                self._install(new)
            return {"applied": apply, "n_changes": len(changes), "effects": effects}

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
        """start | pause | resume | stop | flatten | emergency_stop | clear_emergency.

        pause   = no NEW entries; positions are still managed.
        stop    = no new entries and no more rank/scan work; positions are STILL managed until flat.
        flatten = sell everything now but keep the bot as it was.
        emergency_stop = block entries and (by default) sell everything; must be cleared before restarting.
        """
        with self.lock:
            if action == "start":
                self.bot.start()
            elif action == "pause":
                self.bot.pause()
            elif action == "resume":
                self.bot.resume()
            elif action == "stop":
                self.bot.stop()
            elif action == "flatten":
                if not self.broker.positions:
                    raise ServiceError("There are no open positions to close.")
                self.bot.request_flatten()
            elif action == "emergency_stop":
                self.bot.emergency_stop()
            elif action == "clear_emergency":
                self.bot.reset_emergency()
            else:
                raise ServiceError(f"unknown action {action!r}")
            self.store.set_kv("bot_state", str(self.bot.state))
            self.broker._persist()
            return {"state": str(self.bot.state), "action": action}

    def close_position(self, symbol: str) -> dict[str, Any]:
        with self.lock:
            if not self.bot.request_close(symbol):
                raise ServiceError(f"{symbol.upper()} is not an open position.")
            return {"closing": symbol.upper()}

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
            self.bot = TradingBot(
                self.settings, self.broker, mirror=self.mirror, eligibility=self._load_universe()
            )
            self.bot.on_decision = self.store.add_decision
            self._apply_forecaster()
            self.runner = None
            self.source = None
            self.last_report = None
            self.session_status = "idle"
            self.bot.state = BotState.STOPPED
            self.store.set_kv("bot_state", "stopped")

    # ------------------------------------------------------------------ scheduler (live clock)
    def start_scheduler(self) -> None:
        if self.env.autopilot and not self.settings.auto_schedule:
            self.update_settings({"auto_schedule": True})
            log.info("autopilot: live-clock schedule switched on")
        if self.env.autopilot and self.settings.data_mode != "connected":
            self.bot._alert(
                "warning",
                "autopilot_no_live_data",
                "Autopilot is on but data mode is not Connected: there is no real market data to trade on, "
                "so no orders will be placed.",
                utcnow(),
            )
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
                "mode": {
                    "id": self.settings.trading.mode.value,
                    "label": self.settings.trading.mode.label,
                    "recommended": self.settings.trading.mode == TradingMode.MAX_EXPECTED_RETURN,
                },
                "market": self._market_status(),
                "bot": {
                    "state": str(self.bot.state),
                    "entries_allowed": self.bot.state == BotState.RUNNING and not self.bot.daily_halted,
                    "daily_loss_halt": self.bot.daily_halted,
                    "emergency_behavior": self.settings.trading.emergency_behavior,
                },
                "forecaster": self.forecaster_info,
                "eligibility": {
                    "applied": not self.bot.universe.ignore,
                    "n_tickers": len(self.bot.universe),
                    "source": self.eligibility_source,
                },
                "settings_saved_at": self.settings_saved_at,
                "ui": self.settings.ui.model_dump(mode="json"),
                "alerts": list(self.bot.alerts)[-10:],
                "alerts_critical": sum(1 for a in self.bot.alerts if a["level"] == "critical"),
                "scheduler_enabled": bool(self.scheduler and self.scheduler.get_job("live_tick")),
                "restored_from_db": self.restored,
            }

    def _elig_as_of(self) -> date | None:
        return self.bot._elig_date(self.runner.schedule) if self.runner else None

    def _market_status(self) -> dict[str, Any]:
        """Plain-language market state for the top bar (simulated clock when replaying)."""
        labels = {
            "premarket_scan": ("Pre-market", False),
            "observing_open": ("Open - watching the open", True),
            "trading": ("Open", True),
            "no_new_entries": ("Open - no new trades", True),
            "liquidating": ("Open - closing positions", True),
            "final_check": ("Open - final check", True),
            "post_close": ("Closed", False),
            "closed": ("Closed", False),
        }
        if self.runner is None:
            return {"phase": "idle", "label": "No session running", "open": False, "simulated": True}
        phase = "post_close" if self.runner.finished else str(self.bot.phase)
        label, is_open = labels.get(phase, (phase, False))
        simulated = self.settings.data_mode != "connected"
        return {
            "phase": phase,
            "label": label + (" (simulated clock)" if simulated else ""),
            "open": is_open,
            "simulated": simulated,
        }

    def positions(self) -> list[dict[str, Any]]:
        """Open positions with their plan, progress toward target/stop/deadline, and eligibility flag."""
        with self.lock:
            now = self._now()
            as_of = self._elig_as_of()
            close = self.runner.schedule.close if self.runner else None
            out = []
            for p in self.broker.positions.values():
                mark = self.broker.marks.get(p.symbol, p.avg_cost)
                pending = [
                    o.id for o in self.broker.open_orders() if o.symbol == p.symbol and o.side == Side.SELL
                ]
                plan = p.plan or {}
                target, stop = p.take_profit_price, p.stop_price
                progress = None
                if target and target > p.avg_cost:
                    progress = (mark - p.avg_cost) / (target - p.avg_cost) * 100
                mins_left = (p.deadline - now).total_seconds() / 60 if (p.deadline and now) else None
                mins_close = (close - now).total_seconds() / 60 if (close and now) else None
                rec = self.bot.universe.record_on(p.symbol, as_of)
                held_fc = self.bot.held_forecasts.get(p.symbol)
                cur = None
                if held_fc and held_fc.forecast:
                    cur = {
                        "expected_net_return_pct": round(held_fc.forecast.expected_net_return_pct, 3),
                        "p_positive": round(held_fc.forecast.p_positive, 3),
                        "horizon_minutes": round(held_fc.forecast.horizon_minutes, 1),
                        "calibrated": held_fc.forecast.calibrated,
                        "label": held_fc.forecast.label,
                    }
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
                        "mode": p.mode,
                        "opened_at": p.opened_at.isoformat(),
                        "entry_reason": p.entry_reason,
                        "stop_price": stop,
                        "initial_stop_price": p.initial_stop_price,
                        "breakeven_armed": p.breakeven_armed,
                        "take_profit_price": target,
                        "target_return_pct": round((target / p.avg_cost - 1) * 100, 3) if target else None,
                        "trailing_stop_pct": p.trailing_stop_pct,
                        "high_water_mark": round(p.high_water_mark, 4),
                        "max_hold_minutes": p.max_hold_minutes,
                        "deadline": p.deadline.isoformat() if p.deadline else None,
                        "minutes_left": None if mins_left is None else round(max(mins_left, 0.0), 1),
                        "minutes_to_close": None if mins_close is None else round(max(mins_close, 0.0), 1),
                        "progress_to_target_pct": None if progress is None else round(progress, 1),
                        "stop_cushion_pct": round((mark - stop) / mark * 100, 3) if (stop and mark) else None,
                        "expected_net_return_pct": p.entry_forecast.get("expected_net_return_pct"),
                        "p_positive": p.entry_forecast.get("p_positive"),
                        "p_target": plan.get("p_target"),
                        "forecast_label": p.entry_forecast.get("label"),
                        "forecast_calibrated": bool(p.entry_forecast.get("calibrated", False)),
                        "plan_notes": plan.get("notes", []),
                        "current_forecast": cur,
                        "eligibility": rec.status.value,
                        "eligibility_label": rec.status.label,
                        "eligibility_flag": rec.status != EligibilityStatus.CONFIRMED_TRADABLE
                        and not self.bot.universe.ignore,
                        "closing": p.symbol in self.bot._manual_close,
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
                "n_actionable": sum(1 for r in res if r.actionable),
            }

    # ------------------------------------------------------------------ opportunities
    def _row(self, o: Any) -> dict[str, Any]:
        f, pl = o.forecast, o.plan
        failed = next((g for g in o.gates if not g.passed), None)
        return {
            "symbol": o.symbol,
            "name": o.name,
            "price": round(o.price, 4),
            "day_change_pct": o.scan.metrics.pct_change,
            "eligibility": o.scan.eligibility_status.value,
            "eligibility_label": o.scan.eligibility_status.label,
            "eligibility_note": (
                o.scan.eligibility.record.rejection_reason
                if o.scan.eligibility and o.scan.eligibility.record.rejection_reason
                else "; ".join(
                    v.message for v in (o.scan.eligibility.violations if o.scan.eligibility else [])
                )
            ),
            "actionable": o.actionable,
            "research_only": o.research_only,
            "held": o.held,
            "score": round(o.score, 1),
            "utility": round(o.utility, 3),
            "expected_net_return_pct": None if f is None else round(f.expected_net_return_pct, 3),
            "expected_return_pct": None if f is None else round(f.expected_return_pct, 3),
            "uncertainty_pct": None if f is None else round(f.sigma_pct, 3),
            "p_positive": None if f is None else round(f.p_positive, 3),
            "downside_pct": None if f is None else round(f.downside_pct, 3),
            "upside_pct": None if f is None else round(f.upside_pct, 3),
            "target_price": None if pl is None else round(pl.target_price, 4),
            "target_return_pct": None if pl is None else round(pl.target_return_pct, 3),
            "p_target": None if pl is None else round(pl.p_target, 3),
            "stop_price": None if pl is None else round(pl.stop_price, 4),
            "horizon_minutes": round(o.horizon_minutes, 1),
            "minutes_to_close": round(o.minutes_to_close, 1),
            "calibrated": bool(f.calibrated) if f else False,
            "forecast_label": f.label if f else None,
            "blocked_by": None if failed is None else f"{failed.label}: {failed.detail}",
            "summary": o.explanation.get("summary") if o.explanation else None,
        }

    def opportunities(self) -> dict[str, Any]:
        """Everything the Opportunities page and the dashboard's Top-5 need, in one read."""
        with self.lock:
            ops = self.bot.opportunities
            eligible = [o for o in ops if o.scan.opportunity_ok and not o.held]
            ready = [o for o in eligible if o.actionable]
            watching = [o for o in eligible if not o.actionable]
            research = [o for o in ops if o.research_only]
            scan = self.bot.latest_scan
            unavailable = [
                r
                for r in scan
                if r.eligibility_status == EligibilityStatus.CONFIRMED_UNAVAILABLE and r.basic_ok
            ]
            unavailable.sort(key=lambda r: -r.score)
            waiting = next(
                (
                    n["message"]
                    for n in reversed(self.bot.decision_log)
                    if str(n["message"]).startswith("Waiting")
                ),
                None,
            )
            mode = self.settings.trading.mode
            return {
                "as_of": self.bot.last_tick.isoformat() if self.bot.last_tick else None,
                "feed": self.feed(),
                "mode": {"id": mode.value, "label": mode.label},
                "forecaster": self.forecaster_info,
                "eligibility_source": self.eligibility_source,
                "eligibility_applied": not self.bot.universe.ignore,
                "top": [self._row(o) for o in (ready + watching)[:5]],
                "ready": [self._row(o) for o in ready],
                "watching": [self._row(o) for o in watching[:50]],
                "research": [self._row(o) for o in research[:50]],
                "unavailable_skipped": [
                    {
                        "symbol": r.symbol,
                        "name": r.name,
                        "score": round(r.score, 1),
                        "reason": r.eligibility.record.rejection_reason if r.eligibility else "",
                    }
                    for r in unavailable[:20]
                ],
                "counts": {
                    "scanned": len(scan),
                    "eligible": len(eligible),
                    "ready": len(ready),
                    "research": len(research),
                    "unavailable_skipped": len(unavailable),
                },
                "waiting": waiting if not ready else None,
                "note": (
                    "Ranked by expected return after costs for the time left today - not by how much a stock "
                    "has already gained. Forecasts are estimates"
                    + (
                        "."
                        if self.forecaster_info.get("calibrated")
                        else " and are NOT validated probabilities."
                    )
                ),
            }

    def opportunity(self, symbol: str) -> dict[str, Any]:
        with self.lock:
            for o in self.bot.opportunities:
                if o.symbol == symbol.upper():
                    d = o.to_dict()
                    d["row"] = self._row(o)
                    return d
            raise ServiceError(f"{symbol.upper()} is not in the current ranking")

    def stock(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()
        with self.lock:
            if self.runner is None or self.source is None:
                raise ServiceError("Start a session to inspect symbols.")
            now = self.runner.now
            ctx = self.source.context_for(symbol, now)
            if ctx is None:
                raise ServiceError(f"{symbol} is not in the current universe")
            scanner = Scanner(self.settings.scanner, self.settings.game)
            as_of = self._elig_as_of()
            scan = scanner.scan_one(ctx, self.bot.universe, as_of)
            mins = (now - self.runner.schedule.open).total_seconds() / 60
            opp = self.bot.ranker.evaluate(
                ctx,
                scan,
                self.bot.market,
                self.settings.profile_for(),
                self.settings.effective_costs(),
            )
            inp = SignalInput(ctx, scan.metrics, scan, mins, opportunity=opp)
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
                "opportunity": opp.to_dict(),
                "evaluations": evs,
                "halted": ctx.halted,
                "position": next((p for p in self.positions() if p["symbol"] == symbol), None),
                "held": pos is not None,
            }

    # ------------------------------------------------------------------ activity
    def activity(self, limit: int = 100) -> list[dict[str, Any]]:
        """The bot's own words, newest first: what it saw, bought, sold and why."""
        out = []
        for d in self.store.recent_decisions(min(limit, 500)):
            ts = d["ts"]
            iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            details = d.get("details") or {}
            out.append(
                {
                    "ts": iso,
                    "ts_chicago": to_chicago(ts).strftime("%H:%M") if hasattr(ts, "isoformat") else "",
                    "kind": d["kind"],
                    "symbol": d["symbol"],
                    "message": d["message"],
                    "exit_category": details.get("exit_category"),
                }
            )
        return out

    # ------------------------------------------------------------------ eligibility
    def eligibility_summary(self) -> dict[str, Any]:
        with self.lock:
            u = self.bot.universe
            counts = {s.value: 0 for s in EligibilityStatus}
            for t in u.tickers():
                counts[u.record_on(t, None).status.value] += 1
            return {
                "applied": not u.ignore,
                "n_tickers": len(u),
                "counts": counts,
                "source": self.eligibility_source,
                "rules": self.settings.game.model_dump(mode="json"),
                "verifier": {
                    "name": MarketWatchVerifier.name,
                    "available": False,
                    "message": "Automatic verification against MarketWatch is not implemented - it needs your "
                    "permission and your own logged-in browser on your computer. Maintain the list here or by CSV.",
                },
                "note": "A stock is only ever bought if it is marked Confirmed tradable here. Being listed on "
                "NYSE/Nasdaq or having market data does not make it available in your game.",
            }

    def eligibility_list(self, q: str = "", status: str = "all", limit: int = 500) -> list[dict[str, Any]]:
        with self.lock:
            u = self.bot.universe
            rows = []
            qq = q.strip().upper()
            for t in u.tickers():
                rec = u.record_on(t, None)
                if status != "all" and rec.status.value != status:
                    continue
                if qq and qq not in t and qq not in rec.name.upper():
                    continue
                d = rec.to_dict()
                d["n_history"] = len(u.history(t))
                rows.append(d)
            return rows[:limit]

    def eligibility_history(self, ticker: str) -> list[dict[str, Any]]:
        with self.lock:
            return [r.to_dict() for r in self.bot.universe.history(ticker.upper())]

    def eligibility_set(
        self,
        ticker: str,
        status: str,
        *,
        name: str = "",
        exchange: str = "",
        rejection_reason: str = "",
        restrictions: str = "",
        effective_from: date | None = None,
    ) -> dict[str, Any]:
        ticker = ticker.strip().upper()
        if not ticker or not ticker.replace(".", "").replace("-", "").isalnum() or len(ticker) > 10:
            raise ServiceError("Enter a valid ticker symbol.")
        try:
            st = EligibilityStatus(status)
        except ValueError as exc:
            raise ServiceError(
                "Status must be confirmed_tradable, confirmed_unavailable or unverified."
            ) from exc
        today = utcnow().astimezone(ET).date()
        # A manual statement is dated: it is true from `effective_from` (default today) onward, so replays of
        # earlier dates are never rewritten by what you know today.
        rec = EligibilityRecord(
            ticker=ticker,
            status=st,
            effective_from=effective_from or today,
            verified_on=today if st != EligibilityStatus.UNVERIFIED else None,
            name=name,
            exchange=exchange,
            rejection_reason=rejection_reason if st == EligibilityStatus.CONFIRMED_UNAVAILABLE else "",
            restrictions=restrictions,
            source="manual",
        )
        with self.lock:
            self.store.eligibility_upsert([rec])
            self.refresh_universe()
            return self.bot.universe.record_on(ticker, None).to_dict()

    def eligibility_delete(self, ticker: str) -> int:
        with self.lock:
            n = self.store.eligibility_delete(ticker)
            self.refresh_universe()
            return n

    def eligibility_clear(self) -> None:
        with self.lock:
            self.store.eligibility_clear()
            self.store.set_kv("eligibility_seeded", True)  # an emptied list stays empty
            self.refresh_universe()

    def eligibility_import_csv(self, text: str, dry_run: bool = False) -> dict[str, Any]:
        res = parse_csv(text, today=utcnow().astimezone(ET).date())
        out: dict[str, Any] = {
            "n_rows": len(res.records),
            "errors": res.errors,
            "applied": False,
            "preview": [r.to_dict() for r in res.records[:20]],
        }
        if res.errors and not res.records:
            raise ServiceError("Nothing imported: " + "; ".join(res.errors[:5]))
        if not dry_run:
            with self.lock:
                self.store.eligibility_upsert(res.records)
                self.store.set_kv("eligibility_seeded", True)
                self.refresh_universe()
            out["applied"] = True
        return out

    def eligibility_export_csv(self) -> str:
        return export_csv(self.store.eligibility_all())

    def eligibility_seed_demo(self) -> int:
        with self.lock:
            n = self.store.eligibility_upsert(demo_seed())
            self.store.set_kv("eligibility_seeded", True)
            self.refresh_universe()
            self.eligibility_source = "demo list (made-up, for demo mode)"
            return n

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

    def equity_series(self, range_: str = "1D") -> dict[str, Any]:
        """Equity curve for 1D / 1W / 1M / ALL, thinned to a chart-friendly size."""
        rows = self.store.equity_history(limit=50_000)
        n_days = {"1D": 1, "1W": 5, "1M": 22, "ALL": 10_000}.get(range_.upper())
        if n_days is None:
            raise ServiceError("range must be 1D, 1W, 1M or ALL")
        dates = sorted({r["date"] for r in rows})[-n_days:]
        sel = [r for r in rows if r["date"] in dates]
        stride = max(1, len(sel) // 400)
        pts = sel[::stride]
        if sel and pts[-1] is not sel[-1]:
            pts.append(sel[-1])
        start = float(pts[0]["equity"]) if pts else self.broker.starting_cash
        end = float(pts[-1]["equity"]) if pts else self.broker.equity()
        return {
            "range": range_.upper(),
            "points": pts,
            "start_equity": start,
            "end_equity": end,
            "change": round(end - start, 2),
            "change_pct": round((end / start - 1) * 100, 3) if start else 0.0,
            "feed": self.feed(),
        }

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

    def _universe_for_backtest(self, provider: MarketDataProvider) -> EligibilityUniverse | None:
        """Datasets that ship their own eligibility history use it; otherwise the user's own list (which is then
        treated as point-in-time, so a list typed today never claims to describe the past)."""
        if callable(getattr(provider, "eligibility_records", None)):
            return None
        u = EligibilityUniverse(self.store.eligibility_all())
        return u if len(u) else None

    def _submit_job(self, job: BacktestJob, work: Any) -> str:
        self.jobs[job.id] = job

        def runner() -> None:
            job.status = "running"
            try:
                work(job)
                if job.status == "running":
                    job.status = "cancelled" if job.cancel else "done"
                job.progress = 1.0
            except Exception as exc:
                log.exception("background job failed", extra={"kind": job.kind})
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"

        self._pool.submit(runner)
        return job.id

    def _demo_dates(self, n: int = 5) -> list[date]:
        d = previous_trading_day(utcnow().astimezone(ET).date())
        dates = [d]
        while len(dates) < n:
            dates.append(previous_trading_day(dates[-1]))
        return sorted(dates)

    def start_backtest(
        self, config: BacktestConfig, dataset: str | None, compare: list[str] | None = None
    ) -> str:
        provider = self._provider_for_backtest(dataset)
        for sid in config.strategies + (compare or []):
            if sid not in STRATEGY_CLASSES:
                raise ServiceError(f"unknown strategy {sid!r}")
        if dataset in (None, "", "demo") and not config.dates:
            config = config.model_copy(update={"dates": self._demo_dates()})
        universe = self._universe_for_backtest(provider)
        job = BacktestJob(id=uuid.uuid4().hex[:10])

        def work(j: BacktestJob) -> None:
            def prog(i: int, n: int, label: str) -> None:
                j.progress = i / max(n, 1)
                j.message = label

            if compare:
                results = compare_strategies(provider, config, compare, progress=prog)
            else:
                results = [
                    run_backtest(provider, config, universe=universe, progress=prog, cancel=lambda: j.cancel)
                ]
            for r in results:
                self._bt_cache[r.id] = r
                self.store.save_backtest(
                    r.id,
                    r.config.get("name", "backtest"),
                    r.config,
                    r.summary,
                    r.to_dict(include_trades=True),
                )
                j.result_ids.append(r.id)

        return self._submit_job(job, work)

    def start_compare_modes(self, config: BacktestConfig, dataset: str | None) -> str:
        """Maximum Expected Return vs Aggressive Upside (+ baselines) on identical data."""
        provider = self._provider_for_backtest(dataset)
        if dataset in (None, "", "demo") and not config.dates:
            config = config.model_copy(update={"dates": self._demo_dates()})
        universe = self._universe_for_backtest(provider)
        job = BacktestJob(id=uuid.uuid4().hex[:10], kind="compare_modes")

        def work(j: BacktestJob) -> None:
            def prog(i: int, n: int, label: str) -> None:
                j.progress = i / max(n, 1)
                j.message = label

            res = compare_modes(provider, config, universe=universe, progress=prog)
            res["id"] = j.id
            res["created_at"] = utcnow().isoformat()
            res["dataset"] = dataset or "demo"
            j.payload = res
            ids = list(self.store.get_kv("compare_ids", []))
            self.store.set_kv(f"compare:{j.id}", res)
            self.store.set_kv("compare_ids", [*ids, j.id][-5:])

        return self._submit_job(job, work)

    def start_model_check(self, dataset: str | None, max_days: int | None = None) -> str:
        """Walk-forward comparison of the forecasters; saves the trained model (never labelled calibrated unless
        the out-of-sample check passed)."""
        if dataset in (None, "", "demo"):
            raise ServiceError("Pick a replayable dataset (not the live demo market) for a model check.")
        provider = self._provider_for_backtest(dataset)
        universe = self._universe_for_backtest(provider)
        if universe is None and callable(getattr(provider, "eligibility_records", None)):
            universe = EligibilityUniverse(list(provider.eligibility_records()))  # type: ignore[attr-defined]
        job = BacktestJob(id=uuid.uuid4().hex[:10], kind="model_check")
        settings = self.settings.model_copy(deep=True)

        def work(j: BacktestJob) -> None:
            def prog(i: int, n: int, label: str) -> None:
                j.progress = i / max(n, 1)
                j.message = label

            report, model = evaluate_models(provider, settings, universe, max_days=max_days, progress=prog)
            report["id"] = j.id
            report["created_at"] = utcnow().isoformat()
            report["dataset"] = dataset
            if model is not None:
                self.store.set_kv("model:ridge", model.to_dict())
                report["saved_model"] = {
                    "name": model.name,
                    "calibrated": model.calibrated,
                    "trained_through": model.trained_through,
                }
            j.payload = report
            self.store.set_kv("model_check:latest", report)

        return self._submit_job(job, work)

    def strategy_lab_latest(self) -> dict[str, Any]:
        ids = self.store.get_kv("compare_ids", [])
        return {
            "comparison": self.store.get_kv(f"compare:{ids[-1]}") if ids else None,
            "model_check": self.store.get_kv("model_check:latest"),
            "forecaster": self.forecaster_info,
        }

    def job(self, job_id: str) -> dict[str, Any]:
        j = self.jobs.get(job_id)
        if j is None:
            raise ServiceError("unknown job")
        return {
            "id": j.id,
            "kind": j.kind,
            "status": j.status,
            "progress": j.progress,
            "message": j.message,
            "result_ids": j.result_ids,
            "error": j.error,
            "payload": j.payload,
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
