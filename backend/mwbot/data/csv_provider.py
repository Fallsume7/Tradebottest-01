"""Historical dataset provider (CSV / CSV.GZ) with strict validation.

Dataset directory layout::

    manifest.json   {"name", "synthetic": bool, "timezone": "UTC"|"America/New_York"|..., "adjusted": bool}
    bars.csv[.gz]   timestamp,symbol,open,high,low,close,volume      (1-minute bars, label = bar open)
    meta.csv        date,symbol[,name,prev_close,avg_daily_volume,market_cap,float_shares]   (optional)
    halts.csv       symbol,start,end                                  (optional)

Safeguards:

* naive timestamps are only accepted if the manifest names a timezone (never guessed);
* rows failing OHLC sanity are dropped and counted in the warnings;
* missing ``prev_close`` / ``avg_daily_volume`` are derived ONLY from earlier sessions in the same
  dataset (never from the future);
* suspected unadjusted splits are flagged;
* HISTORICAL data is labelled as such; manifest ``"synthetic": true`` datasets are labelled SYNTHETIC.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from mwbot.calendar import session_for
from mwbot.data.base import DataUnavailableError, MarketDataProvider, MarketDataSource
from mwbot.data.replay import ReplaySource, SymbolDay
from mwbot.domain import FeedInfo, FeedKind, StockMeta

log = logging.getLogger(__name__)

REQUIRED_BAR_COLUMNS = ["timestamp", "symbol", "open", "high", "low", "close", "volume"]


class DatasetError(Exception):
    pass


def detect_possible_splits(daily_close: pd.Series, tolerance: float = 0.04) -> list[tuple[str, float]]:
    """Flag overnight moves that look like unadjusted N:1 / 1:N splits (ratio near 2,3,4,5,10 or inverse)."""
    out: list[tuple[str, float]] = []
    ratio = daily_close / daily_close.shift(1)
    for ts, r in ratio.dropna().items():
        for k in (2, 3, 4, 5, 8, 10, 20):
            if abs(r - 1 / k) / (1 / k) < tolerance or abs(r - k) / k < tolerance:
                out.append((str(ts)[:10], float(r)))
                break
    return out


class CsvHistoricalProvider(MarketDataProvider):
    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.warnings: list[str] = []
        self._manifest = self._load_manifest()
        self.name = f"csv:{self._manifest.get('name', self.directory.name)}"
        self._bars = self._load_bars()
        self._meta = self._load_meta()
        self._halts = self._load_halts()
        self._dates = sorted({d for d in self._session_dates()})
        self._cache: dict[date, ReplaySource] = {}
        self._check_splits()

    # ---------------------------------------------------------------- loading
    def _load_manifest(self) -> dict[str, object]:
        path = self.directory / "manifest.json"
        if not path.exists():
            raise DatasetError(f"{path} missing - every dataset must declare its timezone and adjustment status")
        m = json.loads(path.read_text(encoding="utf-8"))
        if "timezone" not in m:
            raise DatasetError("manifest.json must declare 'timezone' (e.g. 'UTC' or 'America/New_York')")
        if not m.get("adjusted", False):
            self.warnings.append(
                "Dataset is not declared split/dividend-adjusted; backtests may contain split artefacts."
            )
        return m

    def _bars_path(self) -> Path:
        for name in ("bars.csv.gz", "bars.csv"):
            p = self.directory / name
            if p.exists():
                return p
        raise DatasetError(f"no bars.csv[.gz] in {self.directory}")

    def _load_bars(self) -> pd.DataFrame:
        df = pd.read_csv(self._bars_path())
        missing = [c for c in REQUIRED_BAR_COLUMNS if c not in df.columns]
        if missing:
            raise DatasetError(f"bars file missing columns: {missing}")
        tz = str(self._manifest["timezone"])
        ts = pd.to_datetime(df["timestamp"], utc=False)
        if ts.dt.tz is None:
            ts = ts.dt.tz_localize(tz, ambiguous="raise", nonexistent="raise")
        df["timestamp"] = ts.dt.tz_convert("UTC")
        n0 = len(df)
        ok = (
            (df["high"] >= df[["open", "close"]].max(axis=1) - 1e-9)
            & (df["low"] <= df[["open", "close"]].min(axis=1) + 1e-9)
            & (df["low"] > 0)
            & (df["volume"] >= 0)
        )
        df = df[ok]
        df = df.drop_duplicates(subset=["symbol", "timestamp"], keep="last")
        dropped = n0 - len(df)
        if dropped:
            self.warnings.append(f"Dropped {dropped} invalid/duplicate bar rows during load.")
        df = df.sort_values(["symbol", "timestamp"], kind="stable").set_index("timestamp")
        return df

    def _load_meta(self) -> pd.DataFrame | None:
        p = self.directory / "meta.csv"
        return pd.read_csv(p) if p.exists() else None

    def _load_halts(self) -> dict[str, list[tuple[datetime, datetime]]]:
        p = self.directory / "halts.csv"
        out: dict[str, list[tuple[datetime, datetime]]] = {}
        if not p.exists():
            return out
        for row in pd.read_csv(p).itertuples():
            s = pd.Timestamp(row.start)
            e = pd.Timestamp(row.end)
            out.setdefault(str(row.symbol), []).append((s.to_pydatetime(), e.to_pydatetime()))
        return out

    def _session_dates(self) -> set[date]:
        et = self._bars.index.tz_convert("America/New_York")
        return {d for d in set(et.date) if session_for(d) is not None}

    def _check_splits(self) -> None:
        et = self._bars.index.tz_convert("America/New_York")
        daily = self._bars.assign(_d=et.date).groupby(["symbol", "_d"])["close"].last()
        for sym in daily.index.get_level_values(0).unique():
            hits = detect_possible_splits(daily.loc[sym])
            if hits:
                self.warnings.append(f"{sym}: possible unadjusted split(s) on {hits}")

    # ---------------------------------------------------------------- provider API
    def feed_info(self) -> FeedInfo:
        synthetic = bool(self._manifest.get("synthetic", False))
        if synthetic:
            return FeedInfo(
                kind=FeedKind.SYNTHETIC,
                provider=self.name,
                label="SYNTHETIC sample dataset (replayed as historical) - fabricated prices",
                warnings=list(self.warnings),
            )
        return FeedInfo(
            kind=FeedKind.HISTORICAL,
            provider=self.name,
            label="HISTORICAL replay - not live; prices are from the past",
            warnings=list(self.warnings),
        )

    def available_dates(self) -> list[date]:
        return list(self._dates)

    def _day_bars(self, d: date, sym: str) -> pd.DataFrame:
        s = session_for(d)
        assert s is not None
        sub = self._bars[self._bars["symbol"] == sym]
        sub = sub.loc[(sub.index >= s.premarket_open_utc) & (sub.index < s.close_utc)]
        return sub[["open", "high", "low", "close", "volume"]]

    def _meta_for(self, d: date, sym: str, bars_today: pd.DataFrame) -> StockMeta:
        row = None
        if self._meta is not None:
            m = self._meta[(self._meta["symbol"] == sym) & (self._meta["date"] == d.isoformat())]
            if not m.empty:
                row = m.iloc[0]

        def get(col: str) -> float | None:
            if row is not None and col in row.index and pd.notna(row[col]):
                return float(row[col])
            return None

        prev_close = get("prev_close")
        adv = get("avg_daily_volume")
        if prev_close is None or adv is None:
            # derive from strictly earlier sessions
            et = self._bars.index.tz_convert("America/New_York")
            hist = self._bars[(self._bars["symbol"] == sym) & (et.date < d)]
            if not hist.empty:
                het = hist.index.tz_convert("America/New_York")
                reg = hist[(het.time >= pd.Timestamp("09:30").time()) & (het.time < pd.Timestamp("16:00").time())]
                if prev_close is None and not reg.empty:
                    prev_close = float(reg["close"].iloc[-1])
                if adv is None and not reg.empty:
                    per_day = reg.groupby(reg.index.tz_convert("America/New_York").date)["volume"].sum()
                    adv = float(per_day.tail(10).mean())
        name = str(row["name"]) if row is not None and "name" in row.index and pd.notna(row["name"]) else sym
        return StockMeta(
            symbol=sym,
            name=name,
            prev_close=prev_close,
            avg_daily_volume=adv,
            market_cap=get("market_cap"),
            float_shares=get("float_shares"),
            typical_spread_bps=None,  # bar data carries no quotes: spread is genuinely unavailable
        )

    def open_session(self, d: date) -> MarketDataSource:
        if d in self._cache:
            return self._cache[d]
        session = session_for(d)
        if session is None or d not in self._dates:
            raise DataUnavailableError(f"{d} is not available in dataset {self.name}")
        days: dict[str, SymbolDay] = {}
        et_idx = self._bars.index
        in_day = self._bars.loc[(et_idx >= session.premarket_open_utc) & (et_idx < session.close_utc)]
        for sym, grp in in_day.groupby("symbol"):
            bars = grp[["open", "high", "low", "close", "volume"]]
            meta = self._meta_for(d, str(sym), bars)
            halts = [
                (max(s.astimezone(UTC), session.premarket_open_utc), e.astimezone(UTC))
                for s, e in self._halts.get(str(sym), [])
                if session.premarket_open_utc <= s.astimezone(UTC) < session.close_utc
            ]
            days[str(sym)] = SymbolDay(meta=meta, bars=bars, halts=halts)
        src = ReplaySource(session, days, self.feed_info(), synthetic_quotes=False)
        self._cache[d] = src
        return src

    def benchmark_returns(self, dates: list[date]) -> dict[date, float]:
        """Equal-weight average open->close return (%) of the dataset universe, per date.

        Used as a *simple* benchmark in backtests. It is a universe average, not an index.
        """
        out: dict[date, float] = {}
        for d in dates:
            src = self.open_session(d)
            rets: list[float] = []
            for sym in src.symbols():
                sd = src.full_day(sym)
                if sd is None:
                    continue
                reg = sd.bars.loc[(sd.bars.index >= src.session.open_utc)]
                if len(reg) >= 2 and reg["open"].iloc[0] > 0:
                    rets.append(float(reg["close"].iloc[-1] / reg["open"].iloc[0] - 1) * 100)
            out[d] = float(np.mean(rets)) if rets else 0.0
        return out
