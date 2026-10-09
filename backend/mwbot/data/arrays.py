"""NumPy mirror of a bar frame with precomputed running aggregates (fast scanner metrics).

All aggregates are *cumulative from the first bar*, so reading position ``n-1`` only ever
reflects bars ``0..n-1`` - the same visibility guarantee as slicing the DataFrame.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def index_ns(idx: pd.Index) -> np.ndarray:
    """Nanosecond epoch ints for a datetime index (pandas 3 may store us/ms units; normalise)."""
    return np.asarray(pd.DatetimeIndex(idx).as_unit("ns").asi8).copy()  # type: ignore[attr-defined]


@dataclass(slots=True)
class BarArrays:
    ts: np.ndarray  # int64 ns
    o: np.ndarray
    h: np.ndarray
    low: np.ndarray
    c: np.ndarray
    v: np.ndarray
    cum_v: np.ndarray
    cum_dv: np.ndarray  # cumulative typical_price * volume
    run_high: np.ndarray

    @classmethod
    def from_frame(cls, df: pd.DataFrame) -> BarArrays:
        o = df["open"].to_numpy(dtype=float)
        h = df["high"].to_numpy(dtype=float)
        low = df["low"].to_numpy(dtype=float)
        c = df["close"].to_numpy(dtype=float)
        v = df["volume"].to_numpy(dtype=float)
        tp = (h + low + c) / 3.0
        return cls(
            ts=index_ns(df.index),
            o=o,
            h=h,
            low=low,
            c=c,
            v=v,
            cum_v=np.cumsum(v),
            cum_dv=np.cumsum(tp * v),
            run_high=np.maximum.accumulate(h) if len(h) else h,
        )
