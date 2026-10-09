"""Representative intraday price sequences used by scanner / strategy / bot tests."""

from __future__ import annotations

import numpy as np
import pandas as pd

from mwbot.domain import SessionInfo
from mwbot.scanner.scanner import Scanner
from mwbot.strategies.base import SignalInput, Strategy

from .conftest import ONE, build_day, make_source, meta

PRE = [(200, 10.0, 1500), (30, 10.95, 6000), (100, 10.95, 3000)]  # 330 premarket minutes, +9.5% gap on $10 prev close


def runner_day(session: SessionInfo, regular, p0=10.0, seed=1) -> pd.DataFrame:
    """Gap up ~9.5% then whatever ``regular`` segments describe (they start at the regular open)."""
    return build_day(session, p0, PRE + list(regular), seed=seed)


def strong_runner(session):
    # 20 min rally to 11.7 on heavy volume, then 15 min of higher-low grind
    return runner_day(session, [(20, 11.7, 28_000), (30, 11.85, 14_000)])


def fader(session):
    # +60% premarket then bleeds: lower highs, below VWAP
    pre = [(200, 10.0, 1500), (30, 16.0, 9000), (100, 16.0, 3000)]
    return build_day(session, 10.0, pre + [(5, 16.4, 40_000), (40, 14.0, 30_000)], seed=3)


def source_for(session, **bars_by_symbol):
    return make_source(session, {s: (meta(s), b) for s, b in bars_by_symbol.items()})


def inp_for(src, sym: str, minutes: float, scanner: Scanner | None = None) -> SignalInput:
    now = src.session.open_utc + minutes * ONE
    ctx = src.context_for(sym, now)
    assert ctx is not None
    sc = (scanner or Scanner()).scan_one(ctx)
    return SignalInput(ctx, sc.metrics, sc, minutes)


def evaluate(strategy: Strategy, src, sym: str, minutes: float, scanner: Scanner | None = None):
    return strategy.evaluate_entry(inp_for(src, sym, minutes, scanner))


def passed_labels(ev) -> dict[str, bool]:
    return {c.label: c.passed for c in ev.conditions}


def scramble_future(bars: pd.DataFrame, cutoff, seed: int = 99) -> pd.DataFrame:
    """Replace every bar at/after ``cutoff`` with garbage - signals before cutoff must not change."""
    out = bars.copy()
    rng = np.random.default_rng(seed)
    mask = out.index >= cutoff
    n = int(mask.sum())
    junk = rng.uniform(0.01, 500, n)
    out.loc[mask, "open"] = junk
    out.loc[mask, "close"] = junk * 1.1
    out.loc[mask, "high"] = junk * 1.5
    out.loc[mask, "low"] = junk * 0.5
    out.loc[mask, "volume"] = rng.integers(1, 10_000_000, n)
    return out
