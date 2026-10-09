"""Feature engineering for the forecasters. Pure functions of what was knowable at ``ctx.as_of``.

Everything is derived from complete bars up to the decision time (via the context's prefix arrays) plus
optional *market context* (what the rest of the universe is doing) and optional *historical pattern*
(same-time-of-day behaviour in EARLIER sessions only). A signal the data cannot support is ``None`` -
never imputed with a guess - and the forecasters say which signals they could not use.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta
from statistics import median

import numpy as np
import pandas as pd

from mwbot.data.arrays import BarArrays
from mwbot.domain import StockContext
from mwbot.scanner.metrics import StockMetrics, compute_metrics

# Order of the numeric vector used by statistical / ML models (all in volatility-normalised units)
FEATURE_ORDER = [
    "z_ret5",
    "z_ret15",
    "z_ret30",
    "z_accel",
    "z_vwap",
    "z_ema",
    "z_breakout",
    "range_pos_c",
    "log_rvol",
    "z_rel",
    "z_ext",
    "z_recover",
    "catalyst",
    "liq",
    "spread",
    "tod",
]


@dataclass(slots=True)
class MarketState:
    """What the universe is doing right now (equal-weight, regular session). ``None`` = not enough data."""

    ret_since_open_pct: float | None = None
    breadth_up: float | None = None  # fraction of symbols above their open
    n: int = 0


def market_state(metrics: list[StockMetrics], min_symbols: int = 8) -> MarketState:
    xs = [m.pct_from_open for m in metrics if m.pct_from_open is not None and m.n_bars >= 5 and not m.halted]
    # clip single-name outliers so one 60% runner does not define "the market"
    xs = [max(-15.0, min(15.0, x)) for x in xs]
    if len(xs) < min_symbols:
        return MarketState(None, None, len(xs))
    return MarketState(float(median(xs)), sum(1 for x in xs if x > 0) / len(xs), len(xs))


@dataclass(slots=True)
class Features:
    symbol: str
    price: float
    minutes_to_close: float
    minutes_since_open: float
    sigma_1m_pct: float  # per-minute volatility used for scaling (estimated, with floor)
    sigma_source: str  # "measured" | "atr" | "default"
    ret_1m: float | None = None
    ret_5m: float | None = None
    ret_15m: float | None = None
    ret_30m: float | None = None
    accel_5m: float | None = None  # change in 5-minute momentum vs 5 minutes ago (percentage points)
    atr_pct: float | None = None
    rvol: float | None = None
    vs_vwap_pct: float | None = None
    ema_gap_pct: float | None = None  # (EMA9 - EMA21) / price
    price_vs_ema21_pct: float | None = None
    breakout_pct: float | None = None  # close vs highest high of the prior 30 bars (>0 = above)
    range_pos: float | None = None  # 0 = at day low, 1 = at day high
    recovery_pct: float | None = None  # bounce off the day low
    minutes_since_low: float | None = None
    pct_from_open: float | None = None
    pct_change_day: float | None = None
    dist_from_high_pct: float | None = None
    market_ret_pct: float | None = None
    rel_strength: float | None = None  # stock % since open minus market % since open
    breadth_up: float | None = None
    dollar_volume: float = 0.0
    spread_pct: float | None = None
    resistance_price: float | None = None  # nearest recent swing high above the price
    catalyst: str = ""
    catalyst_sentiment: float | None = None
    hist_mean_pct: float | None = None  # same-time-of-day forward return in earlier sessions
    hist_n: int = 0
    missing: list[str] = field(default_factory=list)

    def vector(self) -> np.ndarray:
        """Volatility-normalised numeric vector for statistical models (missing -> 0, flagged via ``missing``)."""
        s = max(self.sigma_1m_pct, 1e-6)

        def z(v: float | None, minutes: float) -> float:
            return 0.0 if v is None else float(np.clip(v / (s * math.sqrt(max(minutes, 1.0))), -5, 5))

        msince = max(self.minutes_since_open, 5.0)
        rv = 0.0 if not self.rvol or self.rvol <= 0 else float(np.clip(math.log2(self.rvol), -3, 4))
        liq = float(np.clip((math.log10(max(self.dollar_volume, 1.0)) - 6.5) / 1.5, -3, 3))
        vec = [
            z(self.ret_5m, 5),
            z(self.ret_15m, 15),
            z(self.ret_30m, 30),
            z(self.accel_5m, 5),
            z(self.vs_vwap_pct, 30),
            z(self.ema_gap_pct, 10),
            z(self.breakout_pct, 15),
            0.0 if self.range_pos is None else self.range_pos - 0.5,
            rv,
            z(self.rel_strength, msince),
            z(self.pct_from_open, msince),
            z(self.recovery_pct, 30),
            0.0 if self.catalyst_sentiment is None else self.catalyst_sentiment,
            liq,
            0.0 if self.spread_pct is None else float(np.clip(self.spread_pct, 0, 5)),
            self.minutes_since_open / 390.0,
        ]
        return np.asarray(vec, dtype=float)


def _ema_last(closes: np.ndarray, span: int) -> float:
    return float(pd.Series(closes).ewm(span=span, adjust=False).mean().iloc[-1])


def compute_features(
    ctx: StockContext,
    metrics: StockMetrics | None = None,
    market: MarketState | None = None,
    hist_mean_pct: float | None = None,
    hist_n: int = 0,
) -> Features | None:
    """Features at ``ctx.as_of`` or ``None`` if there is not enough regular-session history to say anything."""
    m = metrics or compute_metrics(ctx)
    n = len(ctx.bars)
    if n < 10 or m.price is None or m.price <= 0:
        return None
    a: BarArrays = ctx.arrays if ctx.arrays is not None else BarArrays.from_frame(ctx.bars)
    open_utc, close_utc = ctx.session.open_utc, ctx.session.close_utc
    mins_to_close = max((close_utc - ctx.as_of).total_seconds() / 60.0, 0.0)
    mins_since_open = (ctx.as_of - open_utc).total_seconds() / 60.0
    missing: list[str] = []

    sigma = m.volatility_pct
    src = "measured"
    last_close = a.c[:n]
    tail = slice(max(0, n - 15), n)
    atr = (
        float(np.mean((a.h[tail] - a.low[tail]) / np.maximum(last_close[tail], 1e-9)) * 100)
        if n >= 3
        else None
    )
    if sigma is None or sigma <= 0:
        sigma, src = (atr / 1.6, "atr") if atr else (0.30, "default")
        missing.append("volatility (measured)")
    sigma = max(float(sigma), 0.02)  # a floor: some feeds print flat for many minutes

    ts = a.ts[:n]
    last_ts = ts[-1]

    def ret(minutes: int, back_extra: int = 0) -> float | None:
        ref_ts = last_ts - (minutes + back_extra) * 60_000_000_000
        if ts[0] > ref_ts:
            return None
        pos = int(np.searchsorted(ts, ref_ts, side="right")) - 1
        if pos < 0 or a.c[pos] <= 0:
            return None
        end = (
            a.c[n - 1 - 0]
            if back_extra == 0
            else a.c[int(np.searchsorted(ts, last_ts - back_extra * 60_000_000_000, side="right")) - 1]
        )
        return float((end / a.c[pos] - 1.0) * 100)

    r1, r5, r15, r30 = ret(1), ret(5), ret(15), ret(30)
    r5_prev = ret(5, 5)
    accel = (r5 - r5_prev) if (r5 is not None and r5_prev is not None) else None
    for name, v in (("5-min momentum", r5), ("15-min momentum", r15), ("30-min momentum", r30)):
        if v is None:
            missing.append(name)

    closes_tail = last_close[max(0, n - 120) : n]
    ema9 = _ema_last(closes_tail, 9) if len(closes_tail) >= 9 else None
    ema21 = _ema_last(closes_tail, 21) if len(closes_tail) >= 21 else None
    ema_gap = ((ema9 - ema21) / m.price * 100) if (ema9 is not None and ema21 is not None) else None
    p_vs_ema21 = ((m.price / ema21 - 1) * 100) if ema21 else None
    if ema_gap is None:
        missing.append("moving averages")

    # breakout vs prior 30 bars (excluding the latest)
    breakout = None
    resistance = None
    if n >= 32:
        prior_high = float(np.max(a.h[n - 31 : n - 1]))
        if prior_high > 0:
            breakout = (m.price / prior_high - 1.0) * 100
    lookback = slice(max(0, n - 150), max(0, n - 3))
    if n > 10:
        hi = float(np.max(a.h[lookback])) if (lookback.stop or 0) > (lookback.start or 0) else 0.0
        if hi > m.price * 1.001:
            resistance = hi

    # day range position / recovery from the low
    day_low = float(np.min(a.low[:n]))
    day_high = float(m.high_of_day or np.max(a.h[:n]))
    range_pos = (m.price - day_low) / (day_high - day_low) if day_high > day_low else None
    low_idx = int(np.argmin(a.low[:n]))
    mins_since_low = (last_ts - ts[low_idx]) / 60_000_000_000
    recovery = (m.price / day_low - 1.0) * 100 if day_low > 0 else None

    market_ret = market.ret_since_open_pct if market else None
    rel = None
    if market_ret is not None and m.pct_from_open is not None:
        rel = m.pct_from_open - market_ret
    elif market_ret is None:
        missing.append("market context")
    if m.rvol is None:
        missing.append("relative volume")
    if m.vs_vwap_pct is None:
        missing.append("VWAP")

    return Features(
        symbol=ctx.symbol,
        price=m.price,
        minutes_to_close=mins_to_close,
        minutes_since_open=mins_since_open,
        sigma_1m_pct=sigma,
        sigma_source=src,
        ret_1m=r1,
        ret_5m=r5,
        ret_15m=r15,
        ret_30m=r30,
        accel_5m=accel,
        atr_pct=atr,
        rvol=m.rvol,
        vs_vwap_pct=m.vs_vwap_pct,
        ema_gap_pct=ema_gap,
        price_vs_ema21_pct=p_vs_ema21,
        breakout_pct=breakout,
        range_pos=range_pos,
        recovery_pct=recovery,
        minutes_since_low=float(mins_since_low),
        pct_from_open=m.pct_from_open,
        pct_change_day=m.pct_change,
        dist_from_high_pct=m.dist_from_high_pct,
        market_ret_pct=market_ret,
        rel_strength=rel,
        breadth_up=market.breadth_up if market else None,
        dollar_volume=m.dollar_volume,
        spread_pct=m.spread_pct,
        resistance_price=resistance,
        catalyst=ctx.meta.catalyst,
        catalyst_sentiment=ctx.meta.catalyst_sentiment,
        hist_mean_pct=hist_mean_pct,
        hist_n=hist_n,
        missing=missing,
    )


def minutes_remaining(ctx: StockContext) -> float:
    return max((ctx.session.close_utc - ctx.as_of) / timedelta(minutes=1), 0.0)
