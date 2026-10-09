"""Per-symbol scanner metrics. Pure functions of a ``StockContext`` (no future data possible).

Implementation note: the maths runs on NumPy prefix arrays. ``n = len(ctx.bars)`` is the number of
visible (complete) bars; every array access is limited to indices ``< n``.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np

from mwbot.data.arrays import BarArrays
from mwbot.data.profile import expected_fraction
from mwbot.domain import StockContext

_MIN_NS = 60 * 1_000_000_000


@dataclass(slots=True)
class StockMetrics:
    symbol: str
    price: float | None = None
    prev_close: float | None = None
    pct_change: float | None = None
    premarket_pct: float | None = None
    gap_pct: float | None = None
    volume: float = 0.0
    rvol: float | None = None
    rvol_estimated: bool = True
    dollar_volume: float = 0.0
    market_cap: float | None = None
    volatility_pct: float | None = None  # std of 1-minute returns, in %
    spread_pct: float | None = None
    momentum_short: float | None = None
    momentum_long: float | None = None
    vs_vwap_pct: float | None = None
    vwap: float | None = None
    dist_from_high_pct: float | None = None
    high_of_day: float | None = None
    regular_open: float | None = None
    pct_from_open: float | None = None
    n_bars: int = 0
    last_trade_age_seconds: float = 0.0
    halted: bool = False
    abnormal: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        return {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in d.items()}


def _momentum(a: BarArrays, n: int, minutes: int) -> float | None:
    last_ts = a.ts[n - 1]
    if last_ts - a.ts[0] < minutes * _MIN_NS:
        return None
    pos = int(np.searchsorted(a.ts[:n], last_ts - minutes * _MIN_NS, side="right")) - 1
    if pos < 0:
        return None
    ref = a.c[pos]
    return float((a.c[n - 1] / ref - 1.0) * 100) if ref > 0 else None


def compute_metrics(ctx: StockContext, short_min: int = 5, long_min: int = 15) -> StockMetrics:
    m = StockMetrics(symbol=ctx.symbol, prev_close=ctx.meta.prev_close, market_cap=ctx.meta.market_cap)
    m.halted = ctx.halted
    m.last_trade_age_seconds = ctx.data_age_seconds
    n = len(ctx.bars)
    m.n_bars = n
    if n == 0:
        return m
    a: BarArrays = ctx.arrays if ctx.arrays is not None else BarArrays.from_frame(ctx.bars)

    open_ns = ctx.session.open_utc.timestamp() * 1e9
    split = int(np.searchsorted(a.ts[:n], int(open_ns), side="left"))  # first regular-session bar
    last = float(a.c[n - 1])
    m.price = last
    if last <= 0 or not math.isfinite(last):
        m.abnormal.append("non_positive_price")

    if m.prev_close and m.prev_close > 0:
        m.pct_change = (last / m.prev_close - 1.0) * 100
        if split > 0:
            m.premarket_pct = (float(a.c[split - 1]) / m.prev_close - 1.0) * 100
        if split < n:
            m.regular_open = float(a.o[split])
            m.gap_pct = (m.regular_open / m.prev_close - 1.0) * 100
        if abs(m.pct_change) > 1000:
            m.abnormal.append("pct_change_over_1000")

    if split < n:
        m.regular_open = float(a.o[split])
        if m.regular_open > 0:
            m.pct_from_open = (last / m.regular_open - 1.0) * 100

    m.volume = float(a.cum_v[n - 1])
    m.dollar_volume = float(a.cum_dv[n - 1])

    # Relative volume vs an expected cumulative-volume curve (an ESTIMATE: see data/profile.py)
    adv = ctx.meta.avg_daily_volume
    if adv and adv > 0:
        open_utc = ctx.session.open_utc
        total_reg = (ctx.session.close_utc - open_utc).total_seconds()
        x = (ctx.as_of - open_utc).total_seconds() / total_reg
        if x < 0:
            pre_total = (open_utc - ctx.session.premarket_open_utc).total_seconds()
            x = max(-(1.0 - ((ctx.as_of - ctx.session.premarket_open_utc).total_seconds() / pre_total)), -1.0)
        expected = adv * expected_fraction(x)
        if expected >= max(0.003 * adv, 1.0):
            m.rvol = m.volume / expected

    # VWAP over the regular session if any bars exist there, otherwise premarket
    base_v = float(a.cum_v[split - 1]) if split > 0 and split < n else 0.0
    base_dv = float(a.cum_dv[split - 1]) if split > 0 and split < n else 0.0
    vol = m.volume - base_v
    if vol > 0:
        m.vwap = (m.dollar_volume - base_dv) / vol
        m.vs_vwap_pct = (last / m.vwap - 1.0) * 100 if m.vwap > 0 else None

    m.high_of_day = float(a.run_high[n - 1])
    if m.high_of_day > 0:
        m.dist_from_high_pct = (m.high_of_day - last) / m.high_of_day * 100

    m.momentum_short = _momentum(a, n, short_min)
    m.momentum_long = _momentum(a, n, long_min)

    if n >= 6:
        tail = a.c[max(0, n - 31) : n]
        rets = np.diff(np.log(tail))
        m.volatility_pct = float(np.std(rets, ddof=1) * 100)
        if np.max(np.abs(rets)) > 0.4:
            m.abnormal.append("extreme_one_minute_move")

    if ctx.quote is not None:
        if ctx.quote.ask < ctx.quote.bid or ctx.quote.bid <= 0:
            m.abnormal.append("crossed_or_invalid_quote")
        else:
            m.spread_pct = ctx.quote.spread_pct
    return m
