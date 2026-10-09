"""Intraday volume profile used for relative-volume estimates and for synthetic volume.

``expected_fraction(x)`` returns the cumulative fraction of a typical day's total volume that
has traded by position ``x`` where x=0 is the regular open and x=1 the regular close; negative
``x`` is premarket (modelled as ~8% of daily volume spread over the premarket window).

This is a documented *approximation* (U-shaped session curve). Relative volume computed with it
is flagged ``rvol_estimated`` in the scanner output; real per-minute historical averages are
better and can be plugged in later.
"""

from __future__ import annotations

import numpy as np

PREMARKET_SHARE = 0.08
_GRID = 391


def _weights() -> np.ndarray:
    x = np.linspace(0.0, 1.0, _GRID)
    return 1.0 + 3.0 * np.exp(-x / 0.06) + 2.0 * np.exp(-(1.0 - x) / 0.08)


_W = _weights()
_CUM = np.cumsum(_W) / _W.sum()


def session_weights(n_minutes: int) -> np.ndarray:
    """Per-minute volume weights for a regular session of ``n_minutes`` (sums to 1)."""
    x = np.linspace(0.0, 1.0, n_minutes)
    w = 1.0 + 3.0 * np.exp(-x / 0.06) + 2.0 * np.exp(-(1.0 - x) / 0.08)
    return w / w.sum()


def expected_fraction(x: float) -> float:
    """Cumulative expected share of full-day volume at session position ``x``."""
    if x <= -1.0:
        return 0.0
    if x < 0.0:
        # premarket: linear ramp of the premarket share over x in [-1, 0]
        return PREMARKET_SHARE * (1.0 + x) ** 1.5
    if x >= 1.0:
        return 1.0
    reg = float(np.interp(x, np.linspace(0.0, 1.0, _GRID), _CUM))
    return PREMARKET_SHARE + (1.0 - PREMARKET_SHARE) * reg
