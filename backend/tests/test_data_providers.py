import gzip
import json
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from mwbot.data.base import DataUnavailableError
from mwbot.data.csv_provider import CsvHistoricalProvider, DatasetError, detect_possible_splits
from mwbot.data.profile import expected_fraction
from mwbot.data.synthetic import SyntheticProvider, generate_session, synthetic_regimes, write_sample_dataset
from mwbot.domain import FeedKind

from .conftest import TEST_DAY, at


# ------------------------------------------------------------------ synthetic
def test_synthetic_is_deterministic_and_seed_sensitive():
    _s, a = generate_session(TEST_DAY, 42)
    _s, b = generate_session(TEST_DAY, 42)
    _s, c = generate_session(TEST_DAY, 43)
    for sym in a:
        pd.testing.assert_frame_equal(a[sym].bars, b[sym].bars)
    assert any(not a[s].bars.equals(c[s].bars) for s in a if s in c)


def test_synthetic_bars_are_valid_ohlcv_in_utc_and_labelled():
    session, days = generate_session(TEST_DAY, 42)
    assert len(days) == 40
    for sd in days.values():
        b = sd.bars
        assert b.index.tz is not None and str(b.index.tz) == "UTC" and b.index.is_monotonic_increasing
        assert (b["high"] >= b[["open", "close"]].max(axis=1) - 1e-9).all()
        assert (b["low"] <= b[["open", "close"]].min(axis=1) + 1e-9).all()
        assert (b["low"] > 0).all() and (b["volume"] >= 1).all()
        assert b.index[0] >= session.premarket_open_utc and b.index[-1] < session.close_utc
        assert "(synthetic)" in sd.meta.name
    info = SyntheticProvider().feed_info()
    assert info.kind == FeedKind.SYNTHETIC and "SYNTHETIC" in info.label and not info.to_dict()["is_realtime"]


def test_synthetic_regimes_include_traps_and_halts():
    regimes = synthetic_regimes(TEST_DAY, 42)
    assert {"gap_fade", "illiquid_spike", "halt", "breakout", "volume_surge"} <= set(regimes.values())
    _s, days = generate_session(TEST_DAY, 42)
    halted = [s for s, r in regimes.items() if r == "halt"][0]
    assert days[halted].halts and days[halted].halts[0][0] < days[halted].halts[0][1]


def test_synthetic_requires_trading_day():
    with pytest.raises(DataUnavailableError):
        generate_session(date(2026, 11, 26), 42)


def test_volume_profile_is_monotonic_and_bounded():
    xs = np.linspace(-1, 1.2, 60)
    f = [expected_fraction(x) for x in xs]
    assert all(b >= a for a, b in zip(f, f[1:], strict=False)) and f[0] == 0 and f[-1] == 1
    assert 0.07 < expected_fraction(0.0) < 0.09


# ------------------------------------------------------------------ csv
def test_sample_dataset_loads_and_is_labelled_synthetic(sample_provider):
    info = sample_provider.feed_info()
    assert info.kind == FeedKind.SYNTHETIC and "SYNTHETIC" in info.label
    dates = sample_provider.available_dates()
    assert len(dates) == 10 and dates[0] == date(2026, 9, 14)
    src = sample_provider.open_session(dates[0])
    assert len(src.symbols()) == 30
    ctx = src.context_for(src.symbols()[0], at(src.session, 30))
    assert ctx.meta.prev_close and ctx.meta.avg_daily_volume


def test_sample_dataset_prices_are_continuous_across_days(sample_provider):
    """Chained generation: no fake splits between sessions."""
    assert not [w for w in sample_provider.warnings if "split" in w and w.count("(") > 6]


def _write_dataset(tmp_path, rows, tz="UTC", adjusted=True, synthetic=False, meta_rows=None):
    d = tmp_path / "ds"
    d.mkdir()
    pd.DataFrame(rows).to_csv(d / "bars.csv", index=False)
    (d / "manifest.json").write_text(
        json.dumps({"name": "t", "timezone": tz, "adjusted": adjusted, "synthetic": synthetic})
    )
    if meta_rows:
        pd.DataFrame(meta_rows).to_csv(d / "meta.csv", index=False)
    return d


def _rows(day, sym="AAA", n=60, px=10.0, naive=False, tz_offset="-04:00", start_hm=(9, 30)):
    out = []
    t = datetime(day.year, day.month, day.day, *start_hm)
    for i in range(n):
        ts = (
            (t + timedelta(minutes=i)).isoformat()
            if naive
            else (t + timedelta(minutes=i)).isoformat() + tz_offset
        )
        out.append(
            {
                "timestamp": ts,
                "symbol": sym,
                "open": px,
                "high": px + 0.05,
                "low": px - 0.05,
                "close": px,
                "volume": 1000,
            }
        )
    return out


def test_csv_naive_timestamps_use_declared_manifest_timezone(tmp_path):
    d = date(2026, 10, 8)
    ds = _write_dataset(tmp_path, _rows(d, naive=True), tz="America/New_York")
    p = CsvHistoricalProvider(ds)
    src = p.open_session(d)
    first = src.full_day("AAA").bars.index[0]
    assert first == pd.Timestamp("2026-10-08 13:30", tz="UTC")  # 09:30 EDT


def test_csv_dst_conversion_winter_vs_summer(tmp_path):
    d = date(2026, 1, 15)
    ds = _write_dataset(tmp_path, _rows(d, naive=True), tz="America/New_York")
    first = CsvHistoricalProvider(ds).open_session(d).full_day("AAA").bars.index[0]
    assert first == pd.Timestamp("2026-01-15 14:30", tz="UTC")  # 09:30 EST


def test_csv_requires_manifest_with_timezone(tmp_path):
    d = tmp_path / "ds"
    d.mkdir()
    pd.DataFrame(_rows(date(2026, 10, 8))).to_csv(d / "bars.csv", index=False)
    with pytest.raises(DatasetError):
        CsvHistoricalProvider(d)
    (d / "manifest.json").write_text(json.dumps({"name": "x"}))
    with pytest.raises(DatasetError, match="timezone"):
        CsvHistoricalProvider(d)


def test_csv_drops_invalid_rows_and_duplicates_and_warns(tmp_path):
    d = date(2026, 10, 8)
    rows = _rows(d)
    rows[5]["high"] = 1.0  # high below open/close
    rows[6]["low"] = -1.0  # non-positive
    rows.append(dict(rows[0]))  # duplicate
    p = CsvHistoricalProvider(_write_dataset(tmp_path, rows))
    assert any("Dropped 3" in w for w in p.warnings)
    assert len(p.open_session(d).full_day("AAA").bars) == 58


def test_csv_unadjusted_manifest_warns(tmp_path):
    p = CsvHistoricalProvider(_write_dataset(tmp_path, _rows(date(2026, 10, 8)), adjusted=False))
    assert any("not declared split" in w for w in p.warnings)
    assert p.feed_info().kind == FeedKind.HISTORICAL and "HISTORICAL" in p.feed_info().label


def test_csv_missing_prev_close_derived_only_from_past(tmp_path):
    d1, d2 = date(2026, 10, 7), date(2026, 10, 8)
    rows = _rows(d1, px=10.0) + _rows(d2, px=99.0)  # day 2 prices must NOT leak into day 2's prev_close
    p = CsvHistoricalProvider(_write_dataset(tmp_path, rows))
    assert p.open_session(d1).full_day("AAA").meta.prev_close is None
    m2 = p.open_session(d2).full_day("AAA").meta
    assert m2.prev_close == pytest.approx(10.0) and m2.avg_daily_volume == pytest.approx(60_000)


def test_csv_unavailable_date_raises(sample_provider):
    with pytest.raises(DataUnavailableError):
        sample_provider.open_session(date(2026, 9, 12))  # Saturday


def test_split_detector():
    s = pd.Series([10.0, 10.2, 5.1, 5.0, 15.1], index=pd.to_datetime([f"2026-01-0{i}" for i in range(1, 6)]))
    hits = detect_possible_splits(s)
    assert [h[0] for h in hits] == ["2026-01-03", "2026-01-05"] and hits[0][1] == pytest.approx(0.5, abs=0.02)


def test_sample_writer_roundtrip_is_deterministic(tmp_path):
    a = write_sample_dataset(tmp_path / "a", date(2026, 9, 14), date(2026, 9, 15), seed=3, n_symbols=6)
    b = write_sample_dataset(tmp_path / "b", date(2026, 9, 14), date(2026, 9, 15), seed=3, n_symbols=6)
    da = pd.read_csv(a / "bars.csv.gz")
    db = pd.read_csv(b / "bars.csv.gz")
    pd.testing.assert_frame_equal(da, db)
    man = json.loads((a / "manifest.json").read_text())
    assert man["synthetic"] is True and "FABRICATED" in man["description"]
    with gzip.open(a / "bars.csv.gz", "rt") as f:
        assert f.readline().startswith("timestamp,symbol,open")
