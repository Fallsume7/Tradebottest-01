"""Deterministic SYNTHETIC market data for demo mode, tests and sample datasets.

Everything produced here is fabricated: tickers are invented, prices are random-walk paths
shaped by named "regimes" (gap-and-run, gap-and-fade, breakout, ...). The point is to give the
scanner / strategies / simulator realistic *shapes* to chew on, including traps (illiquid spikes,
fades, halts). It says nothing about real markets and every surface labels it SYNTHETIC.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from mwbot.calendar import session_for, trading_days
from mwbot.data.base import DataUnavailableError, MarketDataProvider, MarketDataSource
from mwbot.data.profile import PREMARKET_SHARE, session_weights
from mwbot.data.replay import ReplaySource, SymbolDay
from mwbot.domain import FeedInfo, FeedKind, SessionInfo, StockMeta

GENERATOR_VERSION = "synthetic-v1"

SYMBOL_POOL: list[tuple[str, str]] = [
    ("ZNTR", "Zentra Dynamics"), ("QBLT", "Qubolt Energy"), ("VYRO", "Vyro Therapeutics"),
    ("KLMX", "Kalmex Mining"), ("PRXL", "Praxel Robotics"), ("DRVN", "Drivn Mobility"),
    ("AXBO", "Axbo Biologics"), ("MNTS", "Montis Labs"), ("SLVR", "Silvera Metals"),
    ("TRNX", "Tronex Systems"), ("FLUX", "Fluxon Power"), ("OPAL", "Opalis Pharma"),
    ("BRZO", "Brezo Foods"), ("HYDR", "Hydrion Water"), ("GLXY", "Glaxia Media"),
    ("NVLT", "Novalt Semiconductor"), ("CRBN", "Carbonic Materials"), ("ECLP", "Eclipta Space"),
    ("WVRN", "Waveron Wireless"), ("JTSM", "Jetsum Logistics"), ("LMNA", "Lumina Optics"),
    ("PYRX", "Pyrex Gaming"), ("RDNT", "Radiant Solar"), ("STRK", "Strika Cannabis"),
    ("TLMN", "Telmon Health"), ("UVXA", "Uvexa Genomics"), ("VRTX9", "Vertexa Analytics"),
    ("XRTL", "Xertal Aerospace"), ("YNDR", "Yondar Retail"), ("ZPHR", "Zephyra Apparel"),
    ("ABYS", "Abyssal Marine"), ("BLZR", "Blazr Fitness"), ("CNDR", "Condor Shipping"),
    ("DYNO", "Dynova Chemicals"), ("EMBR", "Embera Lithium"), ("FRGE", "Forgex Steel"),
    ("GRVT", "Gravitas Capital"), ("HLIX", "Helixor Bio"), ("IGNT", "Igniton Fuel Cells"),
    ("JUNO", "Junova Education"), ("KRYP", "Krypta Payments"), ("LUXR", "Luxor Hotels"),
    ("MRDN", "Meridian Telecom"), ("NMBS", "Nimbus Cloud"), ("ORCH", "Orchard Farms"),
    ("PLSR", "Pulsar Imaging"), ("QNTM", "Quantum Drift"), ("RVLN", "Revelon Biotech"),
    ("SYNQ", "Synqra Software"),
]

REGIME_MIX: list[tuple[str, int]] = [
    ("gap_run", 3), ("gap_fade", 3), ("breakout", 3), ("volume_surge", 3), ("opening_runner", 3),
    ("illiquid_spike", 3), ("halt", 1), ("downtrend", 2),
]


def stable_seed(*parts: object) -> int:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(h[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


def _round_px(a: np.ndarray) -> np.ndarray:
    return np.where(a >= 1.0, np.round(a, 2), np.round(a, 4))


@dataclass(slots=True)
class _Plan:
    drift: np.ndarray
    sigma: np.ndarray
    volmult: np.ndarray
    trade_prob: np.ndarray
    jumps: dict[int, float]
    halt: tuple[int, int] | None = None


def _plan_for(regime: str, rng: np.random.Generator, n: int, pre_n: int, reg_n: int, sigma0: float) -> _Plan:
    """Shape the day. Many setups deliberately FAIL (about half): real momentum setups do."""
    drift = np.zeros(n)
    sigma = np.full(n, sigma0)
    volmult = np.ones(n)
    prob = np.concatenate([np.full(pre_n, 0.22), np.ones(reg_n)])
    jumps: dict[int, float] = {}
    halt = None
    t0 = pre_n  # regular open index
    cont = rng.random() < 0.5  # does this setup continue, or fail?

    def reg(a: int, b: int) -> slice:
        return slice(min(t0 + a, n), min(t0 + b, n))

    if regime in ("gap_run", "gap_fade", "halt"):
        gap = rng.uniform(0.12, 0.40)
        news = int(rng.integers(max(pre_n - 220, 5), pre_n - 60))
        jumps[news] = np.log1p(gap) * 0.7
        jumps[news + 2] = np.log1p(gap) * 0.3
        prob[news:pre_n] = 0.85
        volmult[news:pre_n] = rng.uniform(5, 9)
        sigma[news:pre_n] *= 2.0
        sigma[reg(0, 25)] *= 3.0
        volmult[reg(0, 45)] = rng.uniform(4, 8)
        volmult[reg(45, 120)] = rng.uniform(1.8, 3.0)
        if regime == "gap_fade" or not cont:
            drift[reg(0, 6)] = 0.0012
            drift[reg(6, 150)] = -rng.uniform(0.0005, 0.0010)
        else:
            run_len = int(rng.integers(50, 120))
            drift[reg(0, run_len)] = rng.uniform(0.0003, 0.0007)
            drift[reg(run_len, run_len + 60)] = -0.0002
            drift[reg(run_len + 60, reg_n)] = rng.uniform(-0.0001, 0.0001)
        if regime == "halt":
            h0 = int(rng.integers(35, 110))
            halt = (t0 + h0, t0 + h0 + 10)
            jumps[t0 + h0 + 10] = rng.uniform(-0.06, 0.10)
            volmult[t0 + h0 + 10 : t0 + h0 + 16] = 10
    elif regime == "breakout":
        news = int(rng.integers(pre_n - 120, pre_n - 40))
        jumps[news] = np.log1p(rng.uniform(0.02, 0.06))
        prob[news:pre_n] = 0.6
        volmult[news:pre_n] = 2.5
        volmult[reg(0, 90)] = 1.4
        sigma[reg(0, 90)] *= 0.7
        tb = int(rng.integers(60, 120))
        volmult[reg(tb, tb + 28)] = rng.uniform(5, 9)
        if cont:
            drift[reg(tb, tb + 28)] = rng.uniform(0.0010, 0.0018)
            drift[reg(tb + 28, tb + 80)] = rng.uniform(-0.0003, 0.0004)
        else:  # false breakout: pops then reverses through the base
            drift[reg(tb, tb + 6)] = 0.0015
            drift[reg(tb + 6, tb + 60)] = -rng.uniform(0.0003, 0.0007)
    elif regime == "volume_surge":
        ts = int(rng.integers(25, 240))
        drift[reg(ts, ts + 8)] = rng.uniform(0.002, 0.004)
        volmult[reg(ts, ts + 10)] = rng.uniform(10, 18)
        sigma[reg(ts, ts + 10)] *= 2.5
        if cont:
            drift[reg(ts + 8, ts + 50)] = 0.0004
            volmult[reg(ts + 10, ts + 50)] = 3.0
        else:
            drift[reg(ts + 8, ts + 50)] = -0.0008
    elif regime == "opening_runner":
        jumps[pre_n - 30] = np.log1p(rng.uniform(0.02, 0.05))
        prob[pre_n - 90 : pre_n] = 0.5
        volmult[pre_n - 90 : pre_n] = 3.0
        drift[reg(0, 30)] = rng.uniform(0.0010, 0.0018)
        if cont:
            drift[reg(30, 100)] = rng.uniform(0.0002, 0.0006)
        else:
            drift[reg(30, 120)] = -rng.uniform(0.0004, 0.0008)
        volmult[reg(0, 40)] = rng.uniform(5, 9)
        volmult[reg(40, 110)] = 2.5
        sigma[reg(0, 30)] *= 2.0
    elif regime == "illiquid_spike":
        prob[:] = np.where(np.arange(n) < t0, 0.04, 0.18)
        sigma[:] = sigma0 * 4
        for _ in range(int(rng.integers(2, 5))):
            jumps[int(rng.integers(5, n - 5))] = rng.choice([-1, 1]) * rng.uniform(0.05, 0.14)
        jumps[int(rng.integers(t0 - 30, t0 + 60))] = rng.uniform(0.10, 0.25)
    elif regime == "downtrend":
        jumps[int(rng.integers(pre_n - 200, pre_n - 40))] = -np.log1p(rng.uniform(0.08, 0.20))
        volmult[pre_n - 120 : pre_n] = 3.0
        drift[reg(0, 180)] = -0.0004
        volmult[reg(0, 60)] = 3.5
    return _Plan(drift, sigma, volmult, prob, jumps, halt)


@dataclass(slots=True)
class SymbolProfile:
    """Stable per-symbol character (does not change day to day)."""

    base_price: float
    adv: float
    shares: float
    sigma0: float
    illiquid: bool


def symbol_profile(seed: int, symbol: str, illiquid: bool) -> SymbolProfile:
    rng = np.random.default_rng(stable_seed(seed, symbol, "profile"))
    tier = rng.random()
    if illiquid:
        return SymbolProfile(float(np.exp(rng.uniform(np.log(0.4), np.log(4.0)))), float(rng.uniform(15_000, 60_000)),
                             float(rng.uniform(2e6, 12e6)), 0.0035, True)
    if tier < 0.15:
        return SymbolProfile(float(np.exp(rng.uniform(np.log(0.5), np.log(2.0)))), float(rng.uniform(400_000, 2_500_000)),
                             float(rng.uniform(20e6, 200e6)), 0.0030, False)
    if tier < 0.85:
        return SymbolProfile(float(np.exp(rng.uniform(np.log(2.0), np.log(18.0)))), float(rng.uniform(250_000, 3_000_000)),
                             float(rng.uniform(8e6, 90e6)), 0.0018, False)
    return SymbolProfile(float(np.exp(rng.uniform(np.log(25.0), np.log(180.0)))), float(rng.uniform(800_000, 6_000_000)),
                         float(rng.uniform(40e6, 400e6)), 0.0009, False)


def overnight_open_price(prev_close: float, base_price: float, rng: np.random.Generator) -> float:
    """Chain days: small overnight noise plus partial reversion toward the symbol's base price."""
    p = prev_close * float(np.exp(rng.normal(0.0, 0.015) - 0.35 * np.log(prev_close / base_price)))
    return round(p, 2) if p >= 1 else round(p, 4)


def _make_symbol(
    symbol: str, name: str, regime: str, session: SessionInfo, rng: np.random.Generator,
    prof: SymbolProfile, p0: float,
) -> SymbolDay:
    pre_n = int((session.open_utc - session.premarket_open_utc).total_seconds() // 60)
    reg_n = int((session.close_utc - session.open_utc).total_seconds() // 60)
    n = pre_n + reg_n
    adv, shares, sigma0 = prof.adv, prof.shares, prof.sigma0
    plan = _plan_for(regime, rng, n, pre_n, reg_n, sigma0)
    ret = plan.drift + plan.sigma * rng.standard_normal(n)
    for i, j in plan.jumps.items():
        if 0 <= i < n:
            ret[i] += j
    logp = np.log(p0) + np.cumsum(ret)
    close = _round_px(np.exp(logp))
    close = np.maximum(close, 0.0101)
    open_ = np.concatenate([[p0], close[:-1]])
    wiggle = np.abs(plan.sigma * rng.standard_normal(n)) * 0.6
    high = np.maximum(open_, close) * (1 + wiggle)
    low = np.minimum(open_, close) * (1 - wiggle)
    high, low = _round_px(high), _round_px(low)
    high = np.maximum(high, np.maximum(open_, close))
    low = np.minimum(low, np.minimum(open_, close))

    w = np.concatenate([np.full(pre_n, PREMARKET_SHARE / pre_n), (1 - PREMARKET_SHARE) * session_weights(reg_n)])
    noise = np.exp(rng.normal(0.0, 0.5, n) - 0.125)
    present = rng.random(n) < plan.trade_prob
    # sparse bars carry the volume of the minutes they stand in for
    vol = adv * w * plan.volmult * noise / np.maximum(plan.trade_prob, 1e-3)
    vol = np.maximum(np.round(vol), 1).astype(np.int64)

    idx = pd.DatetimeIndex(
        [session.premarket_open_utc + timedelta(minutes=i) for i in range(n)], tz="UTC"
    )
    halts: list[tuple[datetime, datetime]] = []
    if plan.halt:
        h0, h1 = plan.halt
        present[h0:h1] = False
        halts.append((idx[h0].to_pydatetime(), idx[min(h1, n - 1)].to_pydatetime()))
    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx
    )[present]
    df.index.name = "timestamp"

    spread_bps = float(np.clip(18 + 40 / np.sqrt(max(adv, 1) / 1e5) + (60 if p0 < 1 else 0), 8, 400))
    if prof.illiquid:
        spread_bps = float(rng.uniform(250, 600))
    meta = StockMeta(
        symbol=symbol,
        name=f"{name} (synthetic)",
        prev_close=p0,
        avg_daily_volume=round(adv),
        market_cap=round(shares * p0),
        float_shares=round(shares * 0.7),
        typical_spread_bps=round(spread_bps, 1),
    )
    return SymbolDay(meta=meta, bars=df, halts=halts)


def _universe(seed: int, n_symbols: int) -> tuple[list[str], set[str]]:
    """Stable symbol universe for a seed; the first 3 are permanently illiquid."""
    rng = np.random.default_rng(stable_seed(seed, "universe"))
    order = [SYMBOL_POOL[int(i)][0] for i in rng.permutation(len(SYMBOL_POOL))[:n_symbols]]
    return order, set(order[:3])


def synthetic_regimes(d: date, seed: int, n_symbols: int = 40) -> dict[str, str]:
    """Which regime each synthetic symbol follows on date ``d`` (ground truth for tests/UI debugging)."""
    universe, illiquid = _universe(seed, n_symbols)
    rng = np.random.default_rng(stable_seed(seed, d.isoformat(), "assign"))
    active = [s for s in universe if s not in illiquid]
    perm = [active[int(i)] for i in rng.permutation(len(active))]
    out: dict[str, str] = {s: "illiquid_spike" for s in illiquid}
    k = 0
    for regime, count in REGIME_MIX:
        if regime == "illiquid_spike":
            continue
        for _ in range(count):
            if k < len(perm):
                out[perm[k]] = regime
                k += 1
    for s in perm[k:]:
        out[s] = "quiet"
    return {s: out[s] for s in universe}


def generate_session(
    d: date, seed: int = 42, n_symbols: int = 40, prev_closes: dict[str, float] | None = None
) -> tuple[SessionInfo, dict[str, SymbolDay]]:
    """One synthetic session. Pass ``prev_closes`` to chain days (dataset writer); otherwise each
    symbol opens near its stable base price."""
    session = session_for(d)
    if session is None:
        raise DataUnavailableError(f"{d} is not a trading day")
    names = dict(SYMBOL_POOL)
    _universe_syms, illiquid = _universe(seed, n_symbols)
    days: dict[str, SymbolDay] = {}
    for sym, regime in synthetic_regimes(d, seed, n_symbols).items():
        rng = np.random.default_rng(stable_seed(seed, d.isoformat(), sym))
        prof = symbol_profile(seed, sym, sym in illiquid)
        if prev_closes and sym in prev_closes:
            p0 = overnight_open_price(prev_closes[sym], prof.base_price, rng)
        else:
            p0 = round(prof.base_price * float(np.exp(rng.normal(0, 0.05))), 2 if prof.base_price >= 1 else 4)
        days[sym] = _make_symbol(sym, names[sym], regime, session, rng, prof, p0)
    return session, days


class SyntheticProvider(MarketDataProvider):
    name = "synthetic-demo"

    def __init__(self, seed: int = 42, n_symbols: int = 40) -> None:
        self.seed = seed
        self.n_symbols = n_symbols
        self._cache: dict[date, ReplaySource] = {}

    def feed_info(self) -> FeedInfo:
        return FeedInfo(
            kind=FeedKind.SYNTHETIC,
            provider=self.name,
            label="SYNTHETIC demo data - fabricated prices, not a real market",
            warnings=["All prices, volumes and tickers are generated. Do not infer anything about real markets."],
        )

    def available_dates(self) -> list[date] | None:
        return None  # any trading day can be generated

    def open_session(self, d: date) -> MarketDataSource:
        if d not in self._cache:
            session, days = generate_session(d, self.seed, self.n_symbols)
            self._cache[d] = ReplaySource(session, days, self.feed_info(), synthetic_quotes=True)
            if len(self._cache) > 6:
                self._cache.pop(next(iter(self._cache)))
        return self._cache[d]


# --------------------------------------------------------------------------- sample dataset writer


def write_sample_dataset(out_dir: Path, start: date, end: date, seed: int = 7, n_symbols: int = 30) -> Path:
    """Write a labelled SYNTHETIC historical-style dataset (gzipped CSV + manifest)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    bars_parts: list[pd.DataFrame] = []
    meta_rows: list[dict[str, object]] = []
    halt_rows: list[dict[str, str]] = []
    dates = trading_days(start, end)
    prev_closes: dict[str, float] = {}
    for d in dates:
        _session, days = generate_session(d, seed, n_symbols, prev_closes)
        for sym, sd in days.items():
            reg_close = sd.bars[sd.bars.index < _session.close_utc]["close"]
            if len(reg_close):
                prev_closes[sym] = float(reg_close.iloc[-1])
            part = sd.bars.copy()
            part.insert(0, "symbol", sym)
            bars_parts.append(part)
            m = sd.meta
            meta_rows.append(
                {
                    "date": d.isoformat(), "symbol": sym, "name": m.name, "prev_close": m.prev_close,
                    "avg_daily_volume": m.avg_daily_volume, "market_cap": m.market_cap,
                    "float_shares": m.float_shares,
                }
            )
            for s, e in sd.halts:
                halt_rows.append({"symbol": sym, "start": s.isoformat(), "end": e.isoformat()})
    bars = pd.concat(bars_parts).sort_index(kind="stable")
    bars.index = bars.index.tz_convert("UTC")
    bars.to_csv(out_dir / "bars.csv.gz", index_label="timestamp")
    pd.DataFrame(meta_rows).to_csv(out_dir / "meta.csv", index=False)
    pd.DataFrame(halt_rows, columns=["symbol", "start", "end"]).to_csv(out_dir / "halts.csv", index=False)
    manifest = {
        "name": "synthetic-sample",
        "synthetic": True,
        "description": "FABRICATED minute bars (premarket + regular session) generated by mwbot.data.synthetic. "
        "Not real market data. Useful for tests, demos and exercising the backtester only.",
        "generator": GENERATOR_VERSION,
        "seed": seed,
        "timezone": "UTC",
        "adjusted": True,
        "dates": [d.isoformat() for d in dates],
        "deterministic": True,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return out_dir
