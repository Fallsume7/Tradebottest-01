"""Shared fixtures and deterministic scenario builders (hand-made price sequences)."""

from __future__ import annotations

import os
import socket
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from mwbot.calendar import build_schedule, session_for
from mwbot.config import AppSettings, CostConfig, RiskConfig, ScannerConfig, ScannerFilters
from mwbot.data.csv_provider import CsvHistoricalProvider
from mwbot.data.replay import ReplaySource, SymbolDay
from mwbot.domain import FeedInfo, FeedKind, SessionInfo, StockMeta

TEST_DAY = date(2026, 10, 8)  # a normal Thursday, EDT
SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data" / "synthetic_sample"
ONE = timedelta(minutes=1)


def build_day(
    session: SessionInfo,
    p0: float,
    segments: list[tuple[int, float, float]],
    *,
    start_offset_min: int = 0,
    base_volume: float = 2_000.0,
    wiggle: float = 0.0015,
    seed: int = 1,
    default_tail_volume: float | None = None,
) -> pd.DataFrame:
    """Minute bars from premarket open. ``segments`` = [(n_minutes, end_price, volume_per_minute), ...].

    Prices move linearly to each segment's end price; after the last segment price stays flat.
    Fully deterministic (seeded noise), no future dependence.
    """
    rng = np.random.default_rng(seed)
    total = int((session.close_utc - session.premarket_open_utc).total_seconds() // 60)
    closes = np.full(total, p0, dtype=float)
    vols = np.full(total, base_volume, dtype=float)
    i, last = start_offset_min, p0
    if start_offset_min:
        closes[:start_offset_min] = p0
    for n, end, v in segments:
        n = min(n, total - i)
        if n <= 0:
            break
        closes[i : i + n] = np.linspace(last, end, n + 1)[1:]
        vols[i : i + n] = v
        i, last = i + n, end
    if i < total:
        closes[i:] = last
        vols[i:] = default_tail_volume if default_tail_volume is not None else base_volume
    opens = np.concatenate([[p0], closes[:-1]])
    noise = np.abs(rng.normal(0, wiggle, total))
    highs = np.maximum(opens, closes) * (1 + noise)
    lows = np.minimum(opens, closes) * (1 - noise)
    idx = pd.DatetimeIndex([session.premarket_open_utc + k * ONE for k in range(total)], tz="UTC", name="timestamp")
    return pd.DataFrame({"open": np.round(opens, 4), "high": np.round(highs, 4), "low": np.round(lows, 4),
                         "close": np.round(closes, 4), "volume": np.round(vols)}, index=idx)


def make_source(session: SessionInfo, days: dict[str, tuple[StockMeta, pd.DataFrame]], *,
                halts: dict[str, list[tuple[datetime, datetime]]] | None = None,
                spread_bps: float | None = 30.0) -> ReplaySource:
    sd = {}
    for sym, (meta, bars) in days.items():
        if spread_bps is not None and meta.typical_spread_bps is None:
            meta.typical_spread_bps = spread_bps
        sd[sym] = SymbolDay(meta=meta, bars=bars, halts=(halts or {}).get(sym, []))
    feed = FeedInfo(FeedKind.SYNTHETIC, "test", "SYNTHETIC test data")
    return ReplaySource(session, sd, feed, synthetic_quotes=spread_bps is not None)


def meta(sym: str = "TST", prev_close: float = 10.0, adv: float = 1_000_000, mcap: float | None = 200e6) -> StockMeta:
    return StockMeta(symbol=sym, name=f"{sym} Corp (test)", prev_close=prev_close, avg_daily_volume=adv, market_cap=mcap)


def at(session: SessionInfo, minutes_from_open: float) -> datetime:
    return session.open_utc + timedelta(minutes=minutes_from_open)


def relaxed_settings(**risk_overrides: object) -> AppSettings:
    """Settings that let small scripted scenarios qualify (low min_bars, no spread cap)."""
    f = ScannerFilters(min_price=1, max_price=50, min_pct_change=3, min_volume=20_000, min_rvol=1.0,
                       min_dollar_volume=50_000, max_spread_pct=2.0, min_bars=10, max_data_age_seconds=300)
    risk = RiskConfig(**{"max_data_age_seconds": 300, **risk_overrides})  # type: ignore[arg-type]
    return AppSettings(risk=risk, scanner=ScannerConfig(filters=f), costs=CostConfig(latency_seconds=5))


@pytest.fixture(scope="session")
def session_info() -> SessionInfo:
    s = session_for(TEST_DAY)
    assert s is not None
    return s


@pytest.fixture(scope="session")
def schedule(session_info):  # type: ignore[no-untyped-def]
    return build_schedule(TEST_DAY, AppSettings().schedule)


@pytest.fixture(scope="session")
def sample_provider() -> CsvHistoricalProvider:
    return CsvHistoricalProvider(SAMPLE_DIR)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
def mock_site():  # type: ignore[no-untyped-def]
    """Run the mock trading site on a free local port in a background thread."""
    import uvicorn

    from mwbot.mocksite.app import create_mock_app

    app = create_mock_app()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", app.state.exchange
    server.should_exit = True
    t.join(timeout=5)


@pytest.fixture()
def tmp_env(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    from mwbot.config import EnvSettings

    monkeypatch.setenv("MWBOT_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("MWBOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MWBOT_HISTORICAL_DIR", str(SAMPLE_DIR.parent))
    monkeypatch.delenv("MWBOT_MASSIVE_API_KEY", raising=False)
    return EnvSettings(_env_file=None)


def pytest_collection_modifyitems(config, items):  # type: ignore[no-untyped-def]
    if os.environ.get("MWBOT_SKIP_BROWSER") == "1":
        skip = pytest.mark.skip(reason="MWBOT_SKIP_BROWSER=1")
        for item in items:
            if "browser" in item.keywords:
                item.add_marker(skip)
