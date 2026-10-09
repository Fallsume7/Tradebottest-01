"""Deterministic, look-ahead-safe replay of a fully materialised session."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from mwbot.data.arrays import BarArrays
from mwbot.data.base import MarketDataSource
from mwbot.domain import FeedInfo, Quote, SessionInfo, StockContext, StockMeta

ONE_MIN = timedelta(minutes=1)


@dataclass(slots=True)
class SymbolDay:
    meta: StockMeta
    bars: pd.DataFrame  # UTC DatetimeIndex (bar open time), columns open/high/low/close/volume
    halts: list[tuple[datetime, datetime]] = field(default_factory=list)
    _idx_ns: np.ndarray | None = None
    _arrays: BarArrays | None = None

    def arrays(self) -> BarArrays:
        if self._arrays is None:
            self._arrays = BarArrays.from_frame(self.bars)
        return self._arrays

    def index_ns(self) -> np.ndarray:
        if self._idx_ns is None:
            self._idx_ns = self.bars.index.as_unit("ns").asi8.copy()
        return self._idx_ns


class ReplaySource(MarketDataSource):
    """Serves ``SymbolDay`` data bar-by-bar. A bar labelled ``t`` becomes visible at ``t + 1min``."""

    def __init__(self, session: SessionInfo, days: dict[str, SymbolDay], feed: FeedInfo, *, synthetic_quotes: bool = False) -> None:
        self.session = session
        self._days = days
        self._feed = feed
        self._synthetic_quotes = synthetic_quotes

    def feed_info(self) -> FeedInfo:
        return self._feed

    def symbols(self) -> list[str]:
        return list(self._days)

    def _n_visible(self, sd: SymbolDay, as_of: datetime) -> int:
        cutoff = pd.Timestamp(as_of - ONE_MIN).as_unit("ns")  # bars with label <= as_of-1min are complete
        if cutoff.tzinfo is None:
            raise ValueError("as_of must be tz-aware")
        return int(np.searchsorted(sd.index_ns(), cutoff.value, side="right"))

    def _context(self, sd: SymbolDay, as_of: datetime) -> StockContext:
        n = self._n_visible(sd, as_of)
        bars = sd.bars.iloc[:n]
        halted = any(s <= as_of < e for s, e in sd.halts)
        age = float("inf")
        quote = None
        if n:
            last_complete = bars.index[-1].to_pydatetime() + ONE_MIN
            age = (as_of - last_complete).total_seconds()
            if self._synthetic_quotes and sd.meta.typical_spread_bps and not halted:
                px = float(bars["close"].iloc[-1])
                half = px * sd.meta.typical_spread_bps / 20_000
                quote = Quote(bid=round(px - half, 4), ask=round(px + half, 4), timestamp=as_of)
        return StockContext(
            meta=sd.meta,
            bars=bars,
            as_of=as_of,
            session=self.session,
            quote=quote,
            halted=halted,
            data_age_seconds=max(age, 0.0),
            feed=self._feed,
            arrays=sd.arrays(),
        )

    def contexts_at(self, as_of: datetime) -> list[StockContext]:
        return [self._context(sd, as_of) for sd in self._days.values()]

    def context_for(self, symbol: str, as_of: datetime) -> StockContext | None:
        sd = self._days.get(symbol)
        return self._context(sd, as_of) if sd else None

    # ---- convenience for execution simulation (the broker looks at *future* bars by design:
    # it is the simulated market, not a strategy). Strategies never get this method.
    def bar_at(self, symbol: str, label: datetime) -> pd.Series | None:
        sd = self._days.get(symbol)
        if sd is None:
            return None
        ts = pd.Timestamp(label)
        try:
            row = sd.bars.loc[ts]
        except KeyError:
            return None
        return row if isinstance(row, pd.Series) else row.iloc[0]

    def is_halted(self, symbol: str, ts: datetime) -> bool:
        sd = self._days.get(symbol)
        return bool(sd and any(s <= ts < e for s, e in sd.halts))

    def spread_bps(self, symbol: str) -> float | None:
        sd = self._days.get(symbol)
        return sd.meta.typical_spread_bps if sd else None

    def full_day(self, symbol: str) -> SymbolDay | None:
        return self._days.get(symbol)
