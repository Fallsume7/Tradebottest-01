"""Performance statistics. Every ratio states when it is not statistically meaningful."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np

from mwbot.domain import Trade

MIN_DAYS_FOR_RATIOS = 20
MIN_TRADES_FOR_STATS = 30


def max_drawdown(equity: Sequence[float]) -> tuple[float, float]:
    """(max drawdown as positive fraction of peak, max drawdown in $)."""
    if len(equity) < 2:
        return 0.0, 0.0
    arr = np.asarray(equity, dtype=float)
    peak = np.maximum.accumulate(arr)
    dd = (peak - arr) / np.where(peak > 0, peak, 1.0)
    dd_abs = peak - arr
    return float(dd.max()), float(dd_abs.max())


def profit_factor(pnls: Sequence[float]) -> float | None:
    gains = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    if losses == 0:
        return None if gains == 0 else float("inf")
    return gains / losses


def trade_stats(trades: Sequence[Trade]) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"n_trades": 0, "win_rate": None, "net_pnl": 0.0, "avg_win": None, "avg_loss": None,
                "payoff_ratio": None, "profit_factor": None, "expectancy": None, "avg_return_pct": None,
                "avg_hold_minutes": None, "best_trade": None, "worst_trade": None}
    pnls = [t.net_pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    avg_win = float(np.mean(wins)) if wins else None
    avg_loss = float(np.mean(losses)) if losses else None
    pf = profit_factor(pnls)
    return {
        "n_trades": n,
        "win_rate": len(wins) / n,
        "net_pnl": float(sum(pnls)),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff_ratio": (avg_win / abs(avg_loss)) if avg_win and avg_loss else None,
        "profit_factor": None if pf is None else (None if math.isinf(pf) else pf),
        "expectancy": float(np.mean(pnls)),
        "avg_return_pct": float(np.mean([t.return_pct for t in trades])),
        "avg_hold_minutes": float(np.mean([t.hold_minutes for t in trades])),
        "best_trade": float(max(pnls)),
        "worst_trade": float(min(pnls)),
    }


def daily_ratio_stats(daily_returns_pct: Sequence[float]) -> dict[str, Any]:
    n = len(daily_returns_pct)
    out: dict[str, Any] = {"n_days": n, "mean_daily_pct": None, "std_daily_pct": None, "sharpe": None,
                           "sortino": None, "ratios_note": None}
    if n == 0:
        return out
    r = np.asarray(daily_returns_pct, dtype=float) / 100
    out["mean_daily_pct"] = float(r.mean() * 100)
    out["best_day_pct"] = float(r.max() * 100)
    out["worst_day_pct"] = float(r.min() * 100)
    if n < MIN_DAYS_FOR_RATIOS:
        out["ratios_note"] = (f"Only {n} trading days: Sharpe/Sortino are not statistically meaningful "
                              f"(need >= {MIN_DAYS_FOR_RATIOS}) and are withheld.")
        out["std_daily_pct"] = float(r.std(ddof=1) * 100) if n > 1 else None
        return out
    sd = r.std(ddof=1)
    out["std_daily_pct"] = float(sd * 100)
    if sd > 0:
        out["sharpe"] = float(r.mean() / sd * math.sqrt(252))
    downside = r[r < 0]
    dsd = math.sqrt(float(np.mean(downside**2))) if len(downside) else 0.0
    if dsd > 0:
        out["sortino"] = float(r.mean() / dsd * math.sqrt(252))
    out["ratios_note"] = "Annualised with sqrt(252); assumes independent daily returns and 0% risk-free rate."
    return out


def compound(returns_pct: Sequence[float]) -> float:
    v = 1.0
    for r in returns_pct:
        v *= 1 + r / 100
    return (v - 1) * 100
