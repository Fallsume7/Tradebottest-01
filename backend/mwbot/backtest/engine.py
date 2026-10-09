"""Intraday backtester built on the *same* bot/runner/broker used for live simulation.

Anti-bias design (see docs/BACKTESTING.md):
* look-ahead: strategies only receive ``contexts_at(t)`` - bars with ``label + 1min <= t``;
* same-bar fills: orders fill on a bar opening at/after submit + latency (never the signal bar);
* costs: spread, slippage, size impact, participation caps, commissions from ``CostConfig``;
* time zones: every timestamp is tz-aware UTC; the calendar supplies session boundaries (DST/early closes);
* overfitting: optional chronological out-of-sample split + sample-size warnings;
* survivorship: reported as a limitation whenever the dataset cannot prove it is point-in-time.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
from pydantic import BaseModel, Field

from mwbot.backtest.metrics import (
    MIN_TRADES_FOR_STATS,
    compound,
    daily_ratio_stats,
    max_drawdown,
    trade_stats,
)
from mwbot.bot.engine import TradingBot
from mwbot.bot.runner import SessionRunner
from mwbot.config import AppSettings, CostConfig, RiskConfig, ScannerConfig, StrategySelection
from mwbot.data.base import DataUnavailableError, MarketDataProvider
from mwbot.domain import FeedKind, Trade
from mwbot.trading.broker import PaperBroker
from mwbot.trading.risk import BotState

log = logging.getLogger(__name__)


class BacktestConfig(BaseModel):
    name: str = "backtest"
    strategies: list[str] = Field(default_factory=lambda: ["opening_momentum"])
    strategy_params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    dates: list[date] | None = None  # default: every date in the dataset
    start: date | None = None
    end: date | None = None
    initial_capital: float = 100_000.0
    risk: RiskConfig = Field(default_factory=RiskConfig)
    costs: CostConfig = Field(default_factory=CostConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    oos_fraction: float = Field(0.0, ge=0.0, le=0.8)  # chronologically last share of days held out for reporting
    carry_capital: bool = True  # compound across days (False: every day starts from initial capital)

    def to_settings(self) -> AppSettings:
        risk = self.risk.model_copy(update={"starting_capital": self.initial_capital})
        return AppSettings(
            risk=risk, costs=self.costs, scanner=self.scanner,
            strategies=StrategySelection(enabled=list(self.strategies), params=self.strategy_params),
        )


@dataclass(slots=True)
class DayResult:
    date: str
    start_equity: float
    end_equity: float
    pnl: float
    return_pct: float
    n_trades: int
    benchmark_return_pct: float
    unresolved: list[str] = field(default_factory=list)
    alerts: int = 0


@dataclass(slots=True)
class BacktestResult:
    id: str
    config: dict[str, Any]
    feed: dict[str, Any]
    days: list[DayResult]
    trades: list[Trade]
    equity_curve: list[tuple[str, float]]  # (iso ts, equity), 5-minute samples
    summary: dict[str, Any]
    warnings: list[str]
    result_hash: str

    def to_dict(self, include_trades: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id, "config": self.config, "feed": self.feed,
            "days": [dd.__dict__ for dd in self.days], "equity_curve": self.equity_curve,
            "summary": self.summary, "warnings": self.warnings, "result_hash": self.result_hash,
        }
        if include_trades:
            d["trades"] = [t.to_dict() for t in self.trades]
        return d


def universe_day_return(source: Any) -> float:
    """Equal-weight average open->close % return of the symbols in a session (simple benchmark)."""
    rets = []
    for sym in source.symbols():
        sd = source.full_day(sym)
        if sd is None:
            continue
        reg = sd.bars[sd.bars.index >= source.session.open_utc]
        if len(reg) >= 2 and float(reg["open"].iloc[0]) > 0:
            rets.append((float(reg["close"].iloc[-1]) / float(reg["open"].iloc[0]) - 1) * 100)
    return float(np.mean(rets)) if rets else 0.0


def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def run_backtest(
    provider: MarketDataProvider,
    config: BacktestConfig,
    *,
    progress: Callable[[int, int, str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> BacktestResult:
    available = provider.available_dates()
    if available is None and not config.dates:
        raise DataUnavailableError("This provider can generate any day: pass explicit `dates` for the backtest.")
    pool = config.dates or available or []
    dates = [d for d in pool if (config.start is None or d >= config.start) and (config.end is None or d <= config.end)]
    dates = sorted(set(dates))
    if not dates:
        raise DataUnavailableError("no dates selected / available in this dataset")

    settings = config.to_settings()
    feed = provider.feed_info()
    broker = PaperBroker(config.initial_capital, settings.costs)
    bot = TradingBot(settings, broker)
    bot.fast = True
    bot.state = BotState.RUNNING

    days: list[DayResult] = []
    curve: list[tuple[str, float]] = []
    daily_dd_equity: list[float] = [config.initial_capital]
    notes: list[str] = []
    for i, d in enumerate(dates):
        if cancel and cancel():
            notes.append(f"cancelled after {i} of {len(dates)} days")
            break
        if progress:
            progress(i, len(dates), d.isoformat())
        if not config.carry_capital and i > 0:
            broker.cash = config.initial_capital
        source = provider.open_session(d)
        bot.state = BotState.RUNNING
        runner = SessionRunner.for_date(bot, source, d)
        start_eq = broker.equity()
        bot.equity_curve.clear()
        report = runner.run()
        assert report is not None
        unresolved: list[str] = []
        if broker.positions:
            unresolved = list(broker.positions)
            broker.force_flatten(runner.now, "Backtest: unresolved at close - force-closed at last mark")
            notes.append(f"{d}: positions unresolved at close were force-closed with costs: {unresolved}")
        end_eq = broker.equity()
        for k, (ts, eq) in enumerate(bot.equity_curve):
            if k % 5 == 0:
                curve.append((ts.isoformat(), round(eq, 2)))
            daily_dd_equity.append(eq)
        daily_dd_equity.append(end_eq)
        bench = universe_day_return(source) if hasattr(source, "full_day") else 0.0
        days.append(DayResult(
            date=d.isoformat(), start_equity=round(start_eq, 2), end_equity=round(end_eq, 2),
            pnl=round(end_eq - start_eq, 2), return_pct=round((end_eq / start_eq - 1) * 100, 4) if start_eq else 0.0,
            n_trades=report["n_trades"], benchmark_return_pct=round(bench, 4), unresolved=unresolved,
            alerts=len(report["alerts"]),
        ))
    if progress:
        progress(len(days), len(dates), "done")

    summary = _summarise(config, days, broker.trades, daily_dd_equity)
    warnings = _warnings(config, feed.kind, summary, notes)
    cfg_dump = json.loads(config.model_dump_json())
    trades_canon = [t.to_dict() for t in broker.trades]
    result_hash = _hash({"cfg": cfg_dump, "trades": trades_canon, "provider": provider.name,
                         "dates": [d.isoformat() for d in dates]})
    return BacktestResult(
        id=result_hash[:12], config=cfg_dump, feed=feed.to_dict(), days=days, trades=list(broker.trades),
        equity_curve=curve, summary=summary, warnings=warnings, result_hash=result_hash,
    )


def _split_stats(config: BacktestConfig, days: list[DayResult], trades: list[Trade]) -> dict[str, Any]:
    if config.oos_fraction <= 0 or len(days) < 4:
        return {}
    k = max(1, int(round(len(days) * (1 - config.oos_fraction))))
    k = min(k, len(days) - 1)
    is_dates = {d.date for d in days[:k]}
    return {
        "in_sample": _block(days[:k], [t for t in trades if t.session_date in is_dates]),
        "out_of_sample": _block(days[k:], [t for t in trades if t.session_date not in is_dates]),
        "split_index": k,
    }


def _block(days: list[DayResult], trades: list[Trade]) -> dict[str, Any]:
    rets = [d.return_pct for d in days]
    return {"n_days": len(days), "total_return_pct": compound(rets), **trade_stats(trades)}


def _summarise(config: BacktestConfig, days: list[DayResult], trades: list[Trade], equity_path: list[float]) -> dict[str, Any]:
    rets = [d.return_pct for d in days]
    dd_frac, dd_abs = max_drawdown(equity_path)
    final = days[-1].end_equity if days else config.initial_capital
    s: dict[str, Any] = {
        "initial_capital": config.initial_capital,
        "final_equity": final,
        "total_return_pct": (final / config.initial_capital - 1) * 100 if config.carry_capital else compound(rets),
        "benchmark_return_pct": compound([d.benchmark_return_pct for d in days]),
        "max_drawdown_pct": dd_frac * 100,
        "max_drawdown_usd": dd_abs,
        "n_days": len(days),
        "days_with_unresolved_positions": sum(1 for d in days if d.unresolved),
        **trade_stats(trades),
        **daily_ratio_stats(rets),
    }
    by: dict[str, Any] = {}
    for sid in sorted({t.strategy for t in trades}):
        by[sid] = trade_stats([t for t in trades if t.strategy == sid])
    s["by_strategy"] = by
    s.update(_split_stats(config, days, trades))
    return s


def _warnings(config: BacktestConfig, kind: FeedKind, summary: dict[str, Any], notes: list[str]) -> list[str]:
    w: list[str] = list(notes)
    if kind == FeedKind.SYNTHETIC:
        w.append("SYNTHETIC data: results show how the machinery behaves on fabricated prices, not how any strategy performs in real markets.")
    else:
        w.append("Survivorship bias: unless your dataset is point-in-time (includes delisted/halted names as of each date), results are optimistic.")
    n = summary["n_trades"]
    if n < MIN_TRADES_FOR_STATS:
        w.append(f"Only {n} trades (< {MIN_TRADES_FOR_STATS}): win rate and averages are statistically weak; do not draw conclusions.")
    if summary["n_days"] < 20:
        w.append(f"Only {summary['n_days']} days: risk-adjusted ratios withheld; one lucky day can dominate the result.")
    w.append("Fills are modelled from 1-minute bars with documented spread/slippage/participation rules; real fills (queue position, hidden liquidity, halts) differ.")
    w.append("Tuning parameters on this same data and then reporting it is overfitting; use the out-of-sample split and fresh data.")
    if summary.get("days_with_unresolved_positions"):
        w.append(f"{summary['days_with_unresolved_positions']} day(s) ended with unliquidated positions that were force-closed in the simulation.")
    return w


def compare_strategies(
    provider: MarketDataProvider,
    base: BacktestConfig,
    strategy_ids: list[str],
    *,
    include_combined: bool = True,
    progress: Callable[[int, int, str], None] | None = None,
) -> list[BacktestResult]:
    """Run each strategy alone with identical data/costs/risk; optionally also all together."""
    results = []
    runs: list[list[str]] = [[sid] for sid in strategy_ids]
    if include_combined and len(strategy_ids) > 1:
        runs.append(list(strategy_ids))
    for k, ids in enumerate(runs):
        cfg = base.model_copy(update={"strategies": ids, "name": f"{base.name}: {'+'.join(ids)}"})
        results.append(run_backtest(provider, cfg, progress=(lambda i, n, d, k=k: progress(k * n + i, len(runs) * n, d)) if progress else None))
    return results


def trades_to_csv(trades: list[Trade]) -> str:
    import csv
    import io

    buf = io.StringIO()
    cols = ["id", "session_date", "symbol", "strategy", "quantity", "entry_time", "entry_price", "exit_time",
            "exit_price", "gross_pnl", "commissions", "net_pnl", "return_pct", "hold_minutes", "entry_reason", "exit_reason"]
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for t in trades:
        w.writerow({c: t.to_dict()[c] for c in cols})
    return buf.getvalue()


def summary_to_csv(result: BacktestResult) -> str:
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["metric", "value"])
    for k, v in result.summary.items():
        if isinstance(v, (dict, list)):
            continue
        w.writerow([k, v])
    w.writerow([])
    w.writerow(["date", "start_equity", "end_equity", "pnl", "return_pct", "n_trades", "benchmark_return_pct"])
    for d in result.days:
        w.writerow([d.date, d.start_equity, d.end_equity, d.pnl, d.return_pct, d.n_trades, d.benchmark_return_pct])
    return buf.getvalue()


__all__ = [
    "BacktestConfig", "BacktestResult", "DayResult", "run_backtest", "compare_strategies",
    "trades_to_csv", "summary_to_csv",
]
