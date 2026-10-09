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
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, Field

from mwbot.backtest.metrics import (
    MIN_TRADES_FOR_STATS,
    compound,
    daily_ratio_stats,
    max_drawdown,
    trade_stats,
)
from mwbot.bot.engine import TradingBot, classify_exit
from mwbot.bot.runner import SessionRunner
from mwbot.config import (
    AppSettings,
    CostConfig,
    GameRules,
    RiskConfig,
    ScannerConfig,
    StrategySelection,
    TradingMode,
)
from mwbot.data.base import DataUnavailableError, MarketDataProvider
from mwbot.domain import FeedKind, Trade
from mwbot.eligibility.models import EligibilityRecord
from mwbot.eligibility.universe import EligibilityUniverse
from mwbot.forecast.dataset import Sample, build_day_samples
from mwbot.forecast.evaluation import fit_with_calibration
from mwbot.forecast.history import HistoryPatterns
from mwbot.forecast.models import Forecaster, HeuristicForecaster, RidgeForecaster
from mwbot.scanner.scanner import Scanner
from mwbot.trading.broker import PaperBroker
from mwbot.trading.risk import BotState

log = logging.getLogger(__name__)


MIN_STAT_TRAIN_DAYS = 3  # sessions of history before the statistical forecaster is used at all


class BacktestConfig(BaseModel):
    name: str = "backtest"
    strategies: list[str] = Field(default_factory=lambda: ["expected_opportunity"])
    mode: TradingMode = TradingMode.MAX_EXPECTED_RETURN
    game: GameRules = Field(default_factory=lambda: GameRules(eligibility_mode="point_in_time"))
    # point_in_time: only stocks confirmed tradable ON EACH DATE may be traded (needs an eligibility history).
    # none: eligibility is NOT applied - allowed only if asked for, and every result is labelled as such.
    eligibility: Literal["point_in_time", "none"] = "point_in_time"
    # heuristic: transparent rules. statistical: ridge regression re-fitted every day on EARLIER days only.
    forecaster: Literal["heuristic", "statistical"] = "heuristic"
    use_history_patterns: bool = True  # same-time-of-day returns from earlier sessions only
    strategy_params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    dates: list[date] | None = None  # default: every date in the dataset
    start: date | None = None
    end: date | None = None
    initial_capital: float = 100_000.0
    risk: RiskConfig | None = None  # None = the chosen mode's own preset
    costs: CostConfig = Field(default_factory=CostConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    oos_fraction: float = Field(
        0.0, ge=0.0, le=0.8
    )  # chronologically last share of days held out for reporting
    carry_capital: bool = True  # compound across days (False: every day starts from initial capital)

    def to_settings(self) -> AppSettings:
        base = AppSettings(
            costs=self.costs,
            scanner=self.scanner,
            game=self.game.model_copy(
                update={
                    "eligibility_mode": "point_in_time"
                    if self.eligibility == "point_in_time"
                    else "current_list"
                }
            ),
            strategies=StrategySelection(enabled=list(self.strategies), params=self.strategy_params),
        )
        # the explicit `risk` block describes the mode being tested; the other mode keeps its preset
        if self.risk is not None:
            base.trading.profiles[self.mode.value] = base.trading.profiles[self.mode.value].model_copy(
                update={"risk": self.risk}
            )
        out = base.with_mode(self.mode)
        out.risk = out.risk.model_copy(update={"starting_capital": self.initial_capital})
        return out


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
    n_eligible_confirmed: int | None = None  # symbols confirmed tradable that day (None = not applied)
    n_symbols: int = 0


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
            "id": self.id,
            "config": self.config,
            "feed": self.feed,
            "days": [asdict(dd) for dd in self.days],
            "equity_curve": self.equity_curve,
            "summary": self.summary,
            "warnings": self.warnings,
            "result_hash": self.result_hash,
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


def _universe_for(
    provider: MarketDataProvider, config: BacktestConfig, supplied: EligibilityUniverse | None
) -> tuple[EligibilityUniverse, dict[str, Any]]:
    if config.eligibility == "none":
        return EligibilityUniverse.allow_all(), {
            "applied": False,
            "note": "Eligibility NOT applied: every ticker was treated as tradable. Results do not respect your game's stock list.",
        }
    if supplied is not None and not supplied.ignore:
        return supplied, {
            "applied": True,
            "mode": "point_in_time",
            "n_tickers": len(supplied),
            "source": "supplied",
        }
    recs = getattr(provider, "eligibility_records", None)
    records: list[EligibilityRecord] = list(recs()) if callable(recs) else []
    if not records:
        raise DataUnavailableError(
            "This dataset has no MarketWatch eligibility history, so a point-in-time backtest cannot say which stocks "
            "were tradable. Import an eligibility list (Opportunities > Eligibility) or explicitly choose "
            "eligibility='none' (the result is then labelled 'eligibility not applied')."
        )
    return EligibilityUniverse(records), {
        "applied": True,
        "mode": "point_in_time",
        "n_tickers": len({r.ticker for r in records}),
        "source": getattr(provider, "name", "dataset"),
    }


def _statistical_forecaster(history: dict[date, list[Sample]]) -> Forecaster | None:
    """Ridge model fitted on EARLIER sessions only (latest two are used to fit the probability map)."""
    days = sorted(history)
    if len(days) < MIN_STAT_TRAIN_DAYS:
        return None
    cal_start = max(1, len(days) - 2)
    train = [x for d in days[:cal_start] for x in history[d]]
    cal = [x for d in days[cal_start:] for x in history[d]]
    m = fit_with_calibration(RidgeForecaster, train, cal)
    if m is not None:
        m.trained_through = days[-1].isoformat()  # type: ignore[attr-defined]
    return m


def run_backtest(
    provider: MarketDataProvider,
    config: BacktestConfig,
    *,
    universe: EligibilityUniverse | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> BacktestResult:
    available = provider.available_dates()
    if available is None and not config.dates:
        raise DataUnavailableError(
            "This provider can generate any day: pass explicit `dates` for the backtest."
        )
    pool = config.dates or available or []
    dates = [
        d
        for d in pool
        if (config.start is None or d >= config.start) and (config.end is None or d <= config.end)
    ]
    dates = sorted(set(dates))
    if not dates:
        raise DataUnavailableError("no dates selected / available in this dataset")

    settings = config.to_settings()
    feed = provider.feed_info()
    uni, elig_info = _universe_for(provider, config, universe)
    broker = PaperBroker(config.initial_capital, settings.costs)
    heuristic = HeuristicForecaster()
    bot = TradingBot(settings, broker, eligibility=uni, forecaster=heuristic)
    bot.fast = True
    bot.state = BotState.RUNNING
    patterns = HistoryPatterns() if config.use_history_patterns else None
    if patterns is not None:
        bot.hist_lookup = patterns.lookup
    sample_scanner = Scanner(settings.scanner, settings.game)
    history: dict[date, list[Sample]] = {}
    forecaster_log: list[str] = []

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
        # forecaster for THIS day: trained strictly on earlier days
        used = "heuristic"
        if config.forecaster == "statistical":
            model = _statistical_forecaster(history)
            if model is not None:
                bot.set_forecaster(model)
                used = f"statistical (trained through {getattr(model, 'trained_through', '?')}, uncalibrated)"
            else:
                bot.set_forecaster(heuristic)
                used = f"heuristic (fewer than {MIN_STAT_TRAIN_DAYS} earlier sessions to train on)"
        forecaster_log.append(f"{d}: {used}")
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
        syms = source.symbols() if hasattr(source, "symbols") else []
        n_conf = (
            None
            if uni.ignore
            else sum(1 for sy in syms if uni.record_on(sy, d).status.value == "confirmed_tradable")
        )
        days.append(
            DayResult(
                date=d.isoformat(),
                start_equity=round(start_eq, 2),
                end_equity=round(end_eq, 2),
                pnl=round(end_eq - start_eq, 2),
                return_pct=round((end_eq / start_eq - 1) * 100, 4) if start_eq else 0.0,
                n_trades=report["n_trades"],
                benchmark_return_pct=round(bench, 4),
                unresolved=unresolved,
                alerts=len(report["alerts"]),
                n_eligible_confirmed=n_conf,
                n_symbols=len(syms),
            )
        )
        # learn from this session only AFTER it is over (feeds the next days' forecasts)
        if hasattr(source, "full_day"):
            if patterns is not None:
                patterns.add_session(source)
            if config.forecaster == "statistical":
                try:
                    history[d] = build_day_samples(
                        source,  # type: ignore[arg-type]
                        settings.effective_costs(),
                        sample_scanner,
                        None if uni.ignore else uni,
                        stride_min=20,
                    )
                except Exception as exc:  # training data is best-effort; never break the backtest
                    notes.append(f"{d}: could not build training samples ({exc})")
    if progress:
        progress(len(days), len(dates), "done")

    summary = _summarise(config, days, broker.trades, daily_dd_equity)
    summary["eligibility"] = elig_info
    summary["forecaster"] = {
        "requested": config.forecaster,
        "per_day": forecaster_log,
        "calibrated": False,
        "note": "Probabilities are heuristic/uncalibrated unless a walk-forward evaluation says otherwise (see Strategy Lab > Model check).",
    }
    summary["mode"] = config.mode.value
    warnings = _warnings(config, feed.kind, summary, notes)
    if not elig_info["applied"]:
        warnings.insert(0, elig_info["note"])
    cfg_dump = json.loads(config.model_dump_json())
    trades_canon = [t.to_dict() for t in broker.trades]
    result_hash = _hash(
        {
            "cfg": cfg_dump,
            "trades": trades_canon,
            "provider": provider.name,
            "dates": [d.isoformat() for d in dates],
        }
    )
    return BacktestResult(
        id=result_hash[:12],
        config=cfg_dump,
        feed=feed.to_dict(),
        days=days,
        trades=list(broker.trades),
        equity_curve=curve,
        summary=summary,
        warnings=warnings,
        result_hash=result_hash,
    )


EXIT_LABELS = {
    "target": "Profit target",
    "risk": "Stop / trailing / risk exit",
    "forecast": "Forecast deteriorated",
    "end_of_day": "End-of-day exit",
    "time": "Planned deadline",
    "rotation": "Rotated to a better idea",
    "emergency": "Emergency stop",
    "manual": "Closed manually",
    "limit": "Daily loss limit",
    "other": "Other",
}


def exit_breakdown(trades: list[Trade]) -> dict[str, Any]:
    """How trades ended: counts and shares per exit type, plus the headline frequencies."""
    n = len(trades)
    counts: dict[str, int] = {}
    for t in trades:
        k = classify_exit(t.exit_reason)
        counts[k] = counts.get(k, 0) + 1
    share = {k: (v / n if n else None) for k, v in counts.items()}
    return {
        "counts": counts,
        "share": share,
        "labels": {k: EXIT_LABELS.get(k, k) for k in counts},
        "target_hit_rate": (counts.get("target", 0) / n) if n else None,
        "stop_rate": (counts.get("risk", 0) / n) if n else None,
        "end_of_day_rate": ((counts.get("end_of_day", 0) + counts.get("time", 0)) / n) if n else None,
        "forecast_exit_rate": (counts.get("forecast", 0) / n) if n else None,
    }


def forecast_accuracy(trades: list[Trade]) -> dict[str, Any]:
    """How well the entry forecasts matched what happened. Always reported with the sample size and an
    honesty note; never used to relabel a forecast as reliable."""
    pred = [t for t in trades if t.predicted_net_return_pct is not None]
    if not pred:
        return {"n": 0, "note": "No predictive trades in this run."}
    p = np.asarray([t.predicted_net_return_pct for t in pred], dtype=float)
    r = np.asarray([t.return_pct for t in pred], dtype=float)
    pp = np.asarray([t.predicted_p_positive if t.predicted_p_positive is not None else np.nan for t in pred])
    hit = np.asarray([1.0 if t.net_pnl > 0 else 0.0 for t in pred])
    pt = np.asarray([t.predicted_p_target if t.predicted_p_target is not None else np.nan for t in pred])
    reached = np.asarray([1.0 if classify_exit(t.exit_reason) == "target" else 0.0 for t in pred])
    out: dict[str, Any] = {
        "n": len(pred),
        "mean_predicted_net_pct": float(p.mean()),
        "mean_realised_net_pct": float(r.mean()),
        "mean_abs_error_pct": float(np.mean(np.abs(p - r))),
        "bias_pct": float((p - r).mean()),
        "predicted_win_rate": None if np.isnan(pp).all() else float(np.nanmean(pp)),
        "realised_win_rate": float(hit.mean()),
        "predicted_target_rate": None if np.isnan(pt).all() else float(np.nanmean(pt)),
        "realised_target_rate": float(reached.mean()),
        "correlation": None,
        "calibrated": all(t.forecast_calibrated for t in pred),
    }
    if len(pred) >= 10 and p.std() > 0 and r.std() > 0:
        out["correlation"] = float(np.corrcoef(p, r)[0, 1])
    out["note"] = (
        "Compares what the model expected at entry with what the simulated trade returned (net). "
        + (
            ""
            if out["calibrated"]
            else "These forecasts are NOT calibrated: do not read the probabilities as reliable. "
        )
        + (f"Only {len(pred)} trades: very noisy." if len(pred) < MIN_TRADES_FOR_STATS else "")
    )
    return out


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


def _summarise(
    config: BacktestConfig, days: list[DayResult], trades: list[Trade], equity_path: list[float]
) -> dict[str, Any]:
    rets = [d.return_pct for d in days]
    dd_frac, dd_abs = max_drawdown(equity_path)
    final = days[-1].end_equity if days else config.initial_capital
    s: dict[str, Any] = {
        "initial_capital": config.initial_capital,
        "final_equity": final,
        "total_return_pct": (final / config.initial_capital - 1) * 100
        if config.carry_capital
        else compound(rets),
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
    s["exits"] = exit_breakdown(trades)
    s["forecast_accuracy"] = forecast_accuracy(trades)
    s.update(_split_stats(config, days, trades))
    return s


def _warnings(config: BacktestConfig, kind: FeedKind, summary: dict[str, Any], notes: list[str]) -> list[str]:
    w: list[str] = list(notes)
    if kind == FeedKind.SYNTHETIC:
        w.append(
            "SYNTHETIC data: results show how the machinery behaves on fabricated prices, not how any strategy performs in real markets."
        )
    else:
        w.append(
            "Survivorship bias: unless your dataset is point-in-time (includes delisted/halted names as of each date), results are optimistic."
        )
    n = summary["n_trades"]
    if n < MIN_TRADES_FOR_STATS:
        w.append(
            f"Only {n} trades (< {MIN_TRADES_FOR_STATS}): win rate and averages are statistically weak; do not draw conclusions."
        )
    if summary["n_days"] < 20:
        w.append(
            f"Only {summary['n_days']} days: risk-adjusted ratios withheld; one lucky day can dominate the result."
        )
    w.append(
        "Fills are modelled from 1-minute bars with documented spread/slippage/participation rules; real fills (queue position, hidden liquidity, halts) differ."
    )
    w.append(
        "Tuning parameters on this same data and then reporting it is overfitting; use the out-of-sample split and fresh data."
    )
    if summary.get("days_with_unresolved_positions"):
        w.append(
            f"{summary['days_with_unresolved_positions']} day(s) ended with unliquidated positions that were force-closed in the simulation."
        )
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
        results.append(run_backtest(provider, cfg, progress=_scaled(progress, k, len(runs))))
    return results


ROW_KEYS = (
    "total_return_pct",
    "final_equity",
    "max_drawdown_pct",
    "win_rate",
    "avg_return_pct",
    "profit_factor",
    "n_trades",
    "avg_hold_minutes",
    "net_pnl",
)


def _row(label: str, mode: str, r: BacktestResult) -> dict[str, Any]:
    s = r.summary
    ex = s.get("exits", {})
    row: dict[str, Any] = {"label": label, "mode": mode, "id": r.id}
    row.update({k: s.get(k) for k in ROW_KEYS})
    row.update(
        target_hit_rate=ex.get("target_hit_rate"),
        stop_rate=ex.get("stop_rate"),
        end_of_day_rate=ex.get("end_of_day_rate"),
        forecast_exit_rate=ex.get("forecast_exit_rate"),
    )
    oos = s.get("out_of_sample")
    row["out_of_sample"] = (
        {k: oos.get(k) for k in ("n_days", "total_return_pct", "n_trades", "win_rate", "avg_return_pct")}
        if oos
        else None
    )
    return row


def _verdict(rows: list[dict[str, Any]]) -> str:
    a, b = rows[0], rows[1]
    if min(a["n_trades"] or 0, b["n_trades"] or 0) < MIN_TRADES_FOR_STATS:
        return (
            f"Too few trades to say which mode is better ({a['n_trades']} vs {b['n_trades']}; "
            f"{MIN_TRADES_FOR_STATS}+ each needed). Treat the table as a demonstration of the machinery, not a recommendation."
        )
    hi, lo = (a, b) if (a["total_return_pct"] or 0) >= (b["total_return_pct"] or 0) else (b, a)
    dd_note = (
        f" with a deeper worst drawdown ({hi['max_drawdown_pct']:.2f}% vs {lo['max_drawdown_pct']:.2f}%)"
        if (hi["max_drawdown_pct"] or 0) > (lo["max_drawdown_pct"] or 0)
        else ""
    )
    return (
        f"On this data and period, {hi['label']} returned more ({hi['total_return_pct']:+.2f}% vs "
        f"{lo['total_return_pct']:+.2f}%){dd_note}. This is a historical/synthetic comparison with costs; "
        "it does not predict future results and the gap may be noise."
    )


def compare_modes(
    provider: MarketDataProvider,
    base: BacktestConfig,
    *,
    universe: EligibilityUniverse | None = None,
    include_baselines: bool = True,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """Run Maximum Expected Return and Aggressive Upside on IDENTICAL data, costs, eligibility and forecaster.

    Optionally also a simple top-gainer momentum baseline (the old approach) and the equal-weight
    buy-and-hold benchmark, so the predictive modes are always shown next to something simpler."""
    modes = [TradingMode.MAX_EXPECTED_RETURN, TradingMode.AGGRESSIVE_UPSIDE]
    total = len(modes) + (1 if include_baselines else 0)
    results: dict[str, BacktestResult] = {}
    for k, m in enumerate(modes):
        cfg = base.model_copy(
            update={
                "mode": m,
                "risk": None,
                "strategies": ["expected_opportunity"],
                "name": f"{base.name}: {m.label}",
            }
        )
        results[m.value] = run_backtest(
            provider, cfg, universe=universe, progress=_scaled(progress, k, total)
        )
    baseline: BacktestResult | None = None
    if include_baselines:
        bcfg = base.model_copy(
            update={
                "mode": TradingMode.MAX_EXPECTED_RETURN,
                "risk": None,
                "strategies": ["opening_momentum"],
                "name": f"{base.name}: simple top-gainer momentum",
            }
        )
        baseline = run_backtest(
            provider, bcfg, universe=universe, progress=_scaled(progress, len(modes), total)
        )
    rows = [_row(m.label, m.value, results[m.value]) for m in modes]
    if baseline is not None:
        rows.append(_row("Simple momentum (baseline)", "baseline_momentum", baseline))
    first = results[modes[0].value]
    rows.append(
        {
            "label": "Buy & hold universe (benchmark)",
            "mode": "benchmark",
            "total_return_pct": first.summary["benchmark_return_pct"],
            "n_trades": None,
        }
    )
    warnings: list[str] = []
    for r in (*results.values(), *([baseline] if baseline else [])):
        for w in r.warnings:
            if w not in warnings:
                warnings.append(w)
    return {
        "rows": rows,
        "verdict": _verdict(rows[:2]),
        "same_data": {
            "dataset": getattr(provider, "name", ""),
            "feed": first.feed,
            "n_days": first.summary["n_days"],
            "eligibility": first.summary.get("eligibility"),
            "forecaster": base.forecaster,
        },
        "equity_curves": {
            **{m: results[m].equity_curve for m in results},
            **({"baseline_momentum": baseline.equity_curve} if baseline else {}),
        },
        "results": {m: results[m].to_dict(include_trades=False) for m in results},
        "baseline": baseline.to_dict(include_trades=False) if baseline else None,
        "warnings": warnings,
    }


def _scaled(
    progress: Callable[[int, int, str], None] | None, k: int, runs: int
) -> Callable[[int, int, str], None] | None:
    if progress is None:
        return None

    def inner(i: int, n: int, label: str) -> None:
        progress(k * n + i, runs * n, label)

    return inner


def trades_to_csv(trades: list[Trade]) -> str:
    import csv
    import io

    buf = io.StringIO()
    cols = [
        "id",
        "session_date",
        "symbol",
        "strategy",
        "quantity",
        "entry_time",
        "entry_price",
        "exit_time",
        "exit_price",
        "gross_pnl",
        "commissions",
        "net_pnl",
        "return_pct",
        "hold_minutes",
        "entry_reason",
        "exit_reason",
    ]
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
    w.writerow(
        ["date", "start_equity", "end_equity", "pnl", "return_pct", "n_trades", "benchmark_return_pct"]
    )
    for d in result.days:
        w.writerow(
            [d.date, d.start_equity, d.end_equity, d.pnl, d.return_pct, d.n_trades, d.benchmark_return_pct]
        )
    return buf.getvalue()


__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "DayResult",
    "run_backtest",
    "compare_strategies",
    "compare_modes",
    "exit_breakdown",
    "forecast_accuracy",
    "trades_to_csv",
    "summary_to_csv",
]
