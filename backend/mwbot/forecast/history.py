"""Same-time-of-day patterns learned ONLY from earlier sessions (never the session being decided).

For each symbol and 15-minute slot it keeps the realised return over the following hour in every
earlier session. The forecaster sees the average as one weak signal ("history"); it never sees the
current or any later session because a session is added only after it has finished.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import pandas as pd

from mwbot.domain import StockContext

BUCKET_MIN = 15
HORIZON_MIN = 60


class HistoryPatterns:
    def __init__(self) -> None:
        self._r: dict[tuple[str, int], list[float]] = defaultdict(list)
        self.sessions = 0

    def add_session(self, source: Any) -> None:
        """Call after a session is over. ``source`` is a replay source with ``full_day``/``symbols``."""
        s = source.session
        grid = pd.date_range(
            s.open_utc, periods=int((s.close_utc - s.open_utc).total_seconds() // 60) + 1, freq="min"
        )
        total = len(grid)
        for sym in source.symbols():
            sd = source.full_day(sym)
            if sd is None or sd.bars.empty:
                continue
            closes = sd.bars["close"].reindex(grid, method="ffill")
            if closes.isna().all():
                continue
            m = BUCKET_MIN
            while m + HORIZON_MIN < total:
                c0, c1 = closes.iloc[m - 1], closes.iloc[m + HORIZON_MIN - 1]
                if pd.notna(c0) and pd.notna(c1) and c0 > 0:
                    self._r[(sym, m // BUCKET_MIN)].append(float((c1 / c0 - 1.0) * 100.0))
                m += BUCKET_MIN
        self.sessions += 1

    def lookup(self, ctx: StockContext) -> tuple[float | None, int]:
        minutes = (ctx.as_of - ctx.session.open_utc).total_seconds() / 60.0
        if minutes < BUCKET_MIN:
            return None, 0
        rs = self._r.get((ctx.symbol, int(round(minutes / BUCKET_MIN))), [])
        if not rs:
            return None, 0
        return float(sum(rs) / len(rs)), len(rs)
