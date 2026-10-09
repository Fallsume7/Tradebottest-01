"""Data-provider abstraction.

Two layers:

* ``MarketDataProvider`` - knows how to open a trading session (a date) and describes its feed
  honestly (synthetic / historical / real-time / delayed).
* ``MarketDataSource`` - an opened session. The bot only ever asks it for ``contexts_at(as_of)``,
  which returns what was knowable at ``as_of`` and nothing later.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from mwbot.domain import FeedInfo, SessionInfo, StockContext


class DataUnavailableError(Exception):
    """The provider cannot supply the requested data (no subscription, outage, rate limit...)."""


class MarketDataSource(ABC):
    """One opened trading session."""

    session: SessionInfo

    @abstractmethod
    def feed_info(self) -> FeedInfo: ...

    @abstractmethod
    def symbols(self) -> list[str]: ...

    @abstractmethod
    def contexts_at(self, as_of: datetime) -> list[StockContext]:
        """Contexts for every symbol using only information available at ``as_of``."""

    @abstractmethod
    def context_for(self, symbol: str, as_of: datetime) -> StockContext | None: ...

    def feed_age_seconds(self, as_of: datetime) -> float:
        """How old the freshest data point is relative to ``as_of`` (0 for replay)."""
        return 0.0

    def is_replay(self) -> bool:
        return True

    # ---- "tape" interface used ONLY by the simulated broker to price fills. Strategies never use it.
    def bar_at(self, symbol: str, label: datetime) -> Any:
        """The bar opening at ``label`` (pandas Series with open/high/low/close/volume) or None."""
        ctx = self.context_for(symbol, label + timedelta(minutes=1))
        if ctx is None or ctx.bars.empty:
            return None
        ts = pd.Timestamp(label)
        if ts not in ctx.bars.index:
            return None
        row = ctx.bars.loc[ts]
        return row if isinstance(row, pd.Series) else row.iloc[0]

    def is_halted(self, symbol: str, ts: datetime) -> bool:
        return False

    def spread_bps(self, symbol: str) -> float | None:
        return None


class MarketDataProvider(ABC):
    name: str

    @abstractmethod
    def feed_info(self) -> FeedInfo: ...

    @abstractmethod
    def available_dates(self) -> list[date] | None:
        """Dates that can be replayed, or ``None`` for live providers."""

    @abstractmethod
    def open_session(self, d: date) -> MarketDataSource: ...
