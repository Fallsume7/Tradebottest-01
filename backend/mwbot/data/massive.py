"""Massive (formerly Polygon.io) market-data provider - READ-ONLY, bring-your-own-key.

Endpoints used (Polygon-compatible REST, base URL ``https://api.massive.com``)::

    GET /v2/snapshot/locale/us/markets/stocks/gainers           top gainers snapshot
    GET /v2/aggs/ticker/{sym}/range/1/minute/{from}/{to}         1-minute aggregates
    GET /v2/aggs/ticker/{sym}/range/1/day/{from}/{to}            daily aggregates (avg volume)
    GET /v3/reference/tickers/{sym}                              market cap (optional)

Auth: ``Authorization: Bearer <key>`` header (the key is never put in URLs or logs).

IMPORTANT - not verified against the live service from the build sandbox (its network policy blocks
massive.com). Field names follow the documented Polygon schema and are parsed defensively; unit tests
use recorded-shape fixtures through ``httpx.MockTransport``. Verify against your own key during local
integration (see docs/CLOUD_TO_LOCAL_HANDOFF.md). Subscription limits (403) degrade gracefully: the
affected signal becomes *unavailable* rather than invented.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from mwbot.calendar import session_for
from mwbot.data.base import DataUnavailableError, MarketDataProvider, MarketDataSource
from mwbot.data.http import AuthError, ProviderHTTPError, RateLimiter, request_json
from mwbot.domain import FeedInfo, FeedKind, Quote, SessionInfo, StockContext, StockMeta, utcnow

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


class MassiveClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.massive.com",
        *,
        calls_per_second: float = 5.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
    ) -> None:
        if not api_key:
            raise DataUnavailableError("MWBOT_MASSIVE_API_KEY is not set; connected mode needs your own key")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(10.0, connect=5.0),
            transport=transport,
        )
        self._limiter = RateLimiter(calls_per_second, sleep=sleep)
        self._sleep = sleep
        self._max_retries = max_retries

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return request_json(
            self._client,
            "GET",
            path,
            params=params,
            max_retries=self._max_retries,
            sleep=self._sleep,
            limiter=self._limiter,
            jitter=False,
        )

    def close(self) -> None:
        self._client.close()

    # --- typed helpers
    def gainers(self) -> list[dict[str, Any]]:
        data = self.get("/v2/snapshot/locale/us/markets/stocks/gainers", {"include_otc": "false"})
        return list(data.get("tickers") or [])

    def minute_bars(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        data = self.get(
            f"/v2/aggs/ticker/{symbol}/range/1/minute/{start.isoformat()}/{end.isoformat()}",
            {"adjusted": "true", "sort": "asc", "limit": 50000},
        )
        return parse_aggregates(data)

    def daily_bars(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        data = self.get(
            f"/v2/aggs/ticker/{symbol}/range/1/day/{start.isoformat()}/{end.isoformat()}",
            {"adjusted": "true", "sort": "asc", "limit": 120},
        )
        return parse_aggregates(data)

    def market_cap(self, symbol: str) -> float | None:
        data = self.get(f"/v3/reference/tickers/{symbol}")
        mc = (data.get("results") or {}).get("market_cap")
        return float(mc) if mc else None


def parse_aggregates(data: dict[str, Any]) -> pd.DataFrame:
    """Aggregates JSON -> UTC-indexed OHLCV frame. Rows with bad OHLC are dropped."""
    rows = data.get("results") or []
    if not rows:
        return pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"],
            index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
        )
    df = pd.DataFrame(rows).rename(
        columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "t": "ts"}
    )
    df["timestamp"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]].astype(float)
    bad = (
        (df["high"] < df[["open", "close"]].max(axis=1))
        | (df["low"] > df[["open", "close"]].min(axis=1))
        | (df["low"] <= 0)
    )
    if bad.any():
        log.warning("dropped malformed aggregate rows", extra={"count": int(bad.sum())})
    return df[~bad].sort_index()


class MassiveLiveSource(MarketDataSource):
    """Live (or delayed) session built from the gainers snapshot + minute aggregates."""

    def __init__(
        self, client: MassiveClient, session: SessionInfo, feed: FeedInfo, max_candidates: int = 30
    ) -> None:
        self.client = client
        self.session = session
        self._feed = feed
        self._max = max_candidates
        self._avg_vol: dict[str, float | None] = {}
        self._mcap: dict[str, float | None] = {}
        self._latest_update: datetime | None = None
        self._cache: tuple[float, list[StockContext]] | None = None
        self._ticker_snaps: dict[str, dict[str, Any]] = {}

    def feed_info(self) -> FeedInfo:
        return self._feed

    def is_replay(self) -> bool:
        return False

    def symbols(self) -> list[str]:
        return list(self._ticker_snaps)

    def feed_age_seconds(self, as_of: datetime) -> float:
        if self._latest_update is None:
            return float("inf")
        age = (as_of - self._latest_update).total_seconds() - self._feed.delay_seconds
        return max(age, 0.0)

    def _avg_daily_volume(self, symbol: str, today: date) -> float | None:
        if symbol not in self._avg_vol:
            try:
                d = self.client.daily_bars(symbol, today - timedelta(days=21), today - timedelta(days=1))
                self._avg_vol[symbol] = float(d["volume"].tail(10).mean()) if len(d) else None
            except (AuthError, ProviderHTTPError) as exc:
                log.warning("avg volume unavailable", extra={"symbol": symbol, "err": str(exc)})
                self._avg_vol[symbol] = None
        return self._avg_vol[symbol]

    def _market_cap(self, symbol: str) -> float | None:
        if symbol not in self._mcap:
            try:
                self._mcap[symbol] = self.client.market_cap(symbol)
            except (AuthError, ProviderHTTPError):
                self._mcap[symbol] = None
        return self._mcap[symbol]

    def contexts_at(self, as_of: datetime) -> list[StockContext]:
        if self._cache and time.monotonic() - self._cache[0] < 20:
            return self._cache[1]
        try:
            snaps = self.client.gainers()
        except AuthError as exc:
            raise DataUnavailableError(f"Gainers snapshot not available on your plan: {exc}") from exc
        except ProviderHTTPError as exc:
            raise DataUnavailableError(f"Gainers snapshot failed: {exc}") from exc
        out: list[StockContext] = []
        today_et = as_of.astimezone(ET).date()
        newest: datetime | None = None
        for snap in snaps[: self._max]:
            sym = str(snap.get("ticker", "")).upper()
            if not sym:
                continue
            self._ticker_snaps[sym] = snap
            try:
                bars = self.client.minute_bars(sym, today_et, today_et)
            except (AuthError, ProviderHTTPError) as exc:
                log.warning("minute bars unavailable", extra={"symbol": sym, "err": str(exc)})
                continue
            # keep only COMPLETE bars relative to the feed's own clock
            horizon = as_of - timedelta(seconds=self._feed.delay_seconds) - timedelta(minutes=1)
            bars = bars[bars.index <= pd.Timestamp(horizon)]
            prev = snap.get("prevDay") or {}
            quote_raw = snap.get("lastQuote") or {}
            quote = None
            bid, ask = quote_raw.get("p"), quote_raw.get("P")
            if bid and ask and ask >= bid > 0:
                quote = Quote(bid=float(bid), ask=float(ask), timestamp=as_of)
            upd_ns = snap.get("updated")
            if upd_ns:
                upd = datetime.fromtimestamp(int(upd_ns) / 1e9, UTC)
                newest = max(newest, upd) if newest else upd
            last_label = bars.index[-1].to_pydatetime() + timedelta(minutes=1) if len(bars) else None
            meta = StockMeta(
                symbol=sym,
                name=sym,
                prev_close=float(prev["c"]) if prev.get("c") else None,
                avg_daily_volume=self._avg_daily_volume(sym, today_et),
                market_cap=self._market_cap(sym),
                typical_spread_bps=None,
            )
            out.append(
                StockContext(
                    meta=meta,
                    bars=bars,
                    as_of=as_of,
                    session=self.session,
                    quote=quote,
                    halted=False,  # the snapshot endpoint does not report halts: genuinely unknown
                    data_age_seconds=(as_of - last_label).total_seconds() if last_label else float("inf"),
                    feed=self._feed,
                )
            )
        if newest:
            self._latest_update = newest
        self._cache = (time.monotonic(), out)
        return out

    def context_for(self, symbol: str, as_of: datetime) -> StockContext | None:
        for c in self.contexts_at(as_of):
            if c.symbol == symbol:
                return c
        return None


class MassiveProvider(MarketDataProvider):
    name = "massive"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.massive.com",
        *,
        feed: str = "delayed",
        delay_seconds: int = 900,
        calls_per_second: float = 5.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = MassiveClient(
            api_key, base_url, calls_per_second=calls_per_second, transport=transport, sleep=sleep
        )
        self._feed_kind = FeedKind.REALTIME if feed == "realtime" else FeedKind.DELAYED
        self._delay = 0 if feed == "realtime" else delay_seconds

    def feed_info(self) -> FeedInfo:
        if self._feed_kind == FeedKind.REALTIME:
            return FeedInfo(
                FeedKind.REALTIME, self.name, "Massive - real-time (as configured; verify your plan)"
            )
        return FeedInfo(
            FeedKind.DELAYED,
            self.name,
            f"Massive - DELAYED ~{self._delay // 60} min (as configured)",
            delay_seconds=self._delay,
            warnings=[
                "Delayed data: new positions are blocked unless 'allow trading on delayed feed' is enabled."
            ],
        )

    def available_dates(self) -> list[date] | None:
        return None

    def open_session(self, d: date) -> MarketDataSource:
        today = utcnow().astimezone(ET).date()
        if d != today:
            raise DataUnavailableError(
                "Massive provider serves the current session only; use `mwbot download-dataset` + historical mode for past days"
            )
        session = session_for(d)
        if session is None:
            raise DataUnavailableError(f"{d} is not a trading day")
        return MassiveLiveSource(self.client, session, self.feed_info())


def download_dataset(
    client: MassiveClient, symbols: list[str], start: date, end: date, out_dir: Path
) -> Path:
    """Download adjusted 1-minute bars into a historical-mode dataset directory (real data, user's key)."""
    import json

    frames = []
    for sym in symbols:
        df = client.minute_bars(sym, start, end)
        if df.empty:
            log.warning("no bars returned", extra={"symbol": sym})
            continue
        df = df.copy()
        df.insert(0, "symbol", sym)
        frames.append(df)
    if not frames:
        raise DataUnavailableError("no data returned for any symbol")
    out_dir.mkdir(parents=True, exist_ok=True)
    pd.concat(frames).sort_index().to_csv(out_dir / "bars.csv.gz", index_label="timestamp")
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": f"massive-{start}-{end}",
                "synthetic": False,
                "timezone": "UTC",
                "adjusted": True,
                "source": "massive aggregates (adjusted=true)",
                "symbols": symbols,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return out_dir
