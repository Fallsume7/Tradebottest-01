"""Build (features -> forward return) samples from replayable sessions.

Look-ahead rules: ``features`` come from ``source.contexts_at(t)`` (complete bars only). The *label* looks
into the future of ``t`` by construction - so samples are only ever used to (a) train models on days that
are strictly EARLIER than the day being forecast and (b) score forecasts after the fact. Entry is the next
bar's open (never the signal bar) and the exit is the close of the last bar inside the horizon.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np

from mwbot.config import CostConfig
from mwbot.data.replay import ReplaySource
from mwbot.eligibility.universe import EligibilityUniverse
from mwbot.forecast.costs import round_trip_cost_pct
from mwbot.forecast.features import Features, compute_features, market_state
from mwbot.forecast.models import sigma_horizon
from mwbot.scanner.scanner import Scanner

ONE = timedelta(minutes=1)


@dataclass(slots=True)
class Sample:
    day: date
    t: datetime
    features: Features
    horizon: float
    y_gross_pct: float  # realised gross return entry-open -> exit-close
    cost_pct: float
    sigma_h: float

    @property
    def y_net_pct(self) -> float:
        return self.y_gross_pct - self.cost_pct

    @property
    def y_z(self) -> float:
        return self.y_gross_pct / self.sigma_h


def build_day_samples(
    source: ReplaySource,
    costs: CostConfig,
    scanner: Scanner,
    universe: EligibilityUniverse | None = None,
    *,
    horizon: float = 60.0,
    stride_min: int = 10,
    first_minute: int = 15,
    min_minutes_left: float = 25.0,
    max_data_age_s: float = 180.0,
) -> list[Sample]:
    """Samples for the universe the bot could actually trade: stocks passing the scanner's basic (data-quality,
    liquidity, spread, price-band) filters and - if a ``universe`` is given - confirmed tradable in the game on
    that session's date. ``universe=None`` means eligibility is NOT applied (and the caller must say so)."""
    s = source.session
    day = date.fromisoformat(s.date)
    out: list[Sample] = []
    total = (s.close_utc - s.open_utc) / ONE
    m = float(first_minute)
    while total - m >= min_minutes_left:
        t = s.open_utc + m * ONE
        m += stride_min
        ctxs = source.contexts_at(t)
        scans = {c.symbol: scanner.scan_one(c, universe, day) for c in ctxs}
        metrics = {k: v.metrics for k, v in scans.items()}
        mkt = market_state(list(metrics.values()))
        h = min(horizon, (s.close_utc - t) / ONE - 2.0)
        if h < 10:
            continue
        for c in ctxs:
            sc = scans[c.symbol]
            mt = sc.metrics
            ok = sc.opportunity_ok if universe is not None else sc.basic_ok
            if not ok or mt.n_bars < 15 or mt.last_trade_age_seconds > max_data_age_s or mt.price is None:
                continue
            f = compute_features(c, mt, mkt)
            sd = source.full_day(c.symbol)
            if f is None or sd is None:
                continue
            ts = sd.index_ns()
            arr = sd.arrays()
            t_ns = int(np.datetime64(t.replace(tzinfo=None), "ns").astype("int64"))
            entry_pos = int(np.searchsorted(ts, t_ns + 60_000_000_000, side="left"))
            exit_ns = t_ns + int((1 + h - 1) * 60_000_000_000)
            exit_pos = int(np.searchsorted(ts, exit_ns, side="right")) - 1
            if entry_pos >= len(ts) or exit_pos < entry_pos or ts[entry_pos] > t_ns + 4 * 60_000_000_000:
                continue  # could not have entered within a few minutes (no prints): not a tradeable sample
            entry, exit_ = float(arr.o[entry_pos]), float(arr.c[exit_pos])
            if entry <= 0 or sd.halts and any(hs <= t <= he for hs, he in sd.halts):
                continue
            cost = round_trip_cost_pct(costs, f.spread_pct, f.price)
            out.append(Sample(day, t, f, h, (exit_ / entry - 1.0) * 100.0, cost, sigma_horizon(f, h)))
    return out
