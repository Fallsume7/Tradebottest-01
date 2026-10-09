"""Ranking scanner: configurable filters + transparent weighted scoring.

Order of operations for every symbol:
  1. compute metrics (look-ahead-free);
  2. apply tradeability filters -> a list of human-readable blockers (empty = tradable);
  3. score each *available* component in [0, 1], weight, and renormalise over available weights;
  4. emit the score, per-component breakdown and plain-English reasons.

A metric the feed cannot supply is *unavailable*: depending on ``on_missing_data`` the component is
skipped (weights renormalise) or the symbol is excluded. Nothing is ever imputed.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mwbot.config import ScannerConfig, ScannerFilters
from mwbot.domain import StockContext
from mwbot.scanner.metrics import StockMetrics, compute_metrics


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass(slots=True)
class Component:
    name: str
    label: str
    raw: float | None
    score: float | None  # 0..1, None = unavailable
    weight: float
    points: float = 0.0  # contribution to the final 0-100 score
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "label": self.label,
            "raw": None if self.raw is None else round(self.raw, 4),
            "score": None if self.score is None else round(self.score, 3),
            "weight": self.weight, "points": round(self.points, 2), "note": self.note,
            "available": self.score is not None,
        }


@dataclass(slots=True)
class ScanResult:
    symbol: str
    name: str
    metrics: StockMetrics
    score: float = 0.0
    rank: int = 0
    components: list[Component] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    continuing: bool = False

    @property
    def tradable(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "name": self.name, "rank": self.rank, "score": round(self.score, 1),
            "tradable": self.tradable, "continuing": self.continuing,
            "metrics": self.metrics.to_dict(),
            "components": [c.to_dict() for c in self.components],
            "reasons": self.reasons, "warnings": self.warnings, "blockers": self.blockers,
        }


# component name -> (label, raw getter, normaliser, reason template)
def _norm_pct(v: float) -> float:
    return _clip(v / 25.0)


def _norm_rvol(v: float) -> float:
    return _clip(math.log10(v)) if v > 0 else 0.0


def _norm_mom(v: float) -> float:
    return _clip(v / 3.0)


def _norm_mom_long(v: float) -> float:
    return _clip(v / 6.0)


def _norm_vwap(v: float) -> float:
    return _clip(v / 5.0)


def _norm_high(v: float) -> float:
    return 1.0 - _clip(v / 10.0)


def _norm_liq(dollar_volume: float) -> float:
    return _clip((math.log10(max(dollar_volume, 1.0)) - 5.4) / 2.3)


def _norm_vol(v: float) -> float:
    # sweet spot ~0.5% per minute; very quiet or erratic both score low
    if v <= 0:
        return 0.0
    return _clip(1.0 - abs(math.log(v / 0.5)) / math.log(6.0))


def _spread_norm(max_spread: float) -> Callable[[float], float]:
    return lambda v: 1.0 - _clip(v / max_spread)


class Scanner:
    def __init__(self, config: ScannerConfig | None = None) -> None:
        self.config = config or ScannerConfig()

    # ------------------------------------------------------------------ filters
    def _filters(self, m: StockMetrics) -> tuple[list[str], list[str]]:
        f: ScannerFilters = self.config.filters
        missing_exclude = self.config.on_missing_data == "exclude"
        blockers: list[str] = []
        notes: list[str] = []

        def need(value: float | None, label: str) -> bool:
            """True if value present. If absent: exclude or note, per policy."""
            if value is not None:
                return True
            if missing_exclude:
                blockers.append(f"{label} unavailable from this feed")
            else:
                notes.append(f"{label} filter skipped: data unavailable")
            return False

        if m.price is None or m.n_bars == 0:
            return ["no price data yet"], notes
        if m.abnormal:
            blockers.append("abnormal data: " + ", ".join(m.abnormal))
        if f.exclude_halted and m.halted:
            blockers.append("trading halted")
        if m.n_bars < f.min_bars:
            blockers.append(f"only {m.n_bars} bars (<{f.min_bars}) - not enough history")
        if m.last_trade_age_seconds > f.max_data_age_seconds:
            blockers.append(f"last trade {m.last_trade_age_seconds / 60:.1f} min ago (stale/illiquid)")
        if not (f.min_price <= m.price <= f.max_price):
            blockers.append(f"price ${m.price:.2f} outside ${f.min_price:g}-${f.max_price:g}")
        if need(m.pct_change, "% change"):
            assert m.pct_change is not None
            if m.pct_change < f.min_pct_change:
                blockers.append(f"change {m.pct_change:+.1f}% below {f.min_pct_change:g}%")
            if f.max_pct_change is not None and m.pct_change > f.max_pct_change:
                blockers.append(f"change {m.pct_change:+.1f}% above {f.max_pct_change:g}% (suspicious)")
        if m.volume < f.min_volume:
            blockers.append(f"volume {m.volume:,.0f} below {f.min_volume:,.0f}")
        if m.dollar_volume < f.min_dollar_volume:
            blockers.append(f"dollar volume ${m.dollar_volume:,.0f} below ${f.min_dollar_volume:,.0f}")
        if f.min_rvol > 0 and need(m.rvol, "relative volume") and (m.rvol or 0) < f.min_rvol:
            blockers.append(f"RVOL {m.rvol:.1f}x below {f.min_rvol:g}x")
        if f.min_market_cap is not None and need(m.market_cap, "market cap"):
            assert m.market_cap is not None
            if m.market_cap < f.min_market_cap:
                blockers.append(f"market cap ${m.market_cap / 1e6:.0f}M below ${f.min_market_cap / 1e6:.0f}M")
        if f.max_market_cap is not None and need(m.market_cap, "market cap"):
            assert m.market_cap is not None
            if m.market_cap > f.max_market_cap:
                blockers.append(f"market cap ${m.market_cap / 1e6:.0f}M above ${f.max_market_cap / 1e6:.0f}M")
        if f.max_spread_pct is not None and need(m.spread_pct, "bid-ask spread"):
            assert m.spread_pct is not None
            if m.spread_pct > f.max_spread_pct:
                blockers.append(f"spread {m.spread_pct:.2f}% above {f.max_spread_pct:g}%")
        return blockers, notes

    # ------------------------------------------------------------------ scoring
    def _components(self, m: StockMetrics) -> list[Component]:
        w = self.config.weights
        max_spread = self.config.filters.max_spread_pct or 2.0
        sh, lg = self.config.momentum_short_minutes, self.config.momentum_long_minutes
        specs: list[tuple[str, str, float | None, float, Callable[[float], float]]] = [
            ("pct_change", "Change vs prev close (%)", m.pct_change, w.pct_change, _norm_pct),
            ("premarket", "Premarket change (%)", m.premarket_pct, w.premarket, _norm_pct),
            ("rvol", "Relative volume (x)", m.rvol, w.rvol, _norm_rvol),
            ("momentum_5m", f"{sh}-min momentum (%)", m.momentum_short, w.momentum_5m, _norm_mom),
            ("momentum_15m", f"{lg}-min momentum (%)", m.momentum_long, w.momentum_15m, _norm_mom_long),
            ("vs_vwap", "Price vs VWAP (%)", m.vs_vwap_pct, w.vs_vwap, _norm_vwap),
            ("near_high", "Distance below day high (%)", m.dist_from_high_pct, w.near_high, _norm_high),
            ("liquidity", "Dollar volume ($)", m.dollar_volume if m.n_bars else None, w.liquidity, _norm_liq),
            ("volatility", "1-min volatility (%)", m.volatility_pct, w.volatility, _norm_vol),
            ("spread", "Bid-ask spread (%)", m.spread_pct, w.spread, _spread_norm(max_spread)),
        ]
        comps = []
        for name, label, raw, weight, fn in specs:
            if raw is None or weight <= 0:
                note = "unavailable" if raw is None else "weight 0"
                comps.append(Component(name, label, raw, None, weight, note=note))
            else:
                comps.append(Component(name, label, raw, fn(raw), weight))
        total_w = sum(c.weight for c in comps if c.score is not None)
        for c in comps:
            if c.score is not None and total_w > 0:
                c.points = 100.0 * c.weight * c.score / total_w
        return comps

    @staticmethod
    def _reasons(m: StockMetrics, comps: list[Component]) -> tuple[list[str], list[str]]:
        reasons: list[str] = []
        warnings: list[str] = []
        by = {c.name: c for c in comps}

        def good(name: str) -> bool:
            c = by[name]
            return c.score is not None and c.score >= 0.55

        if m.pct_change is not None and good("pct_change"):
            reasons.append(f"Up {m.pct_change:+.1f}% on the day")
        if m.premarket_pct is not None and good("premarket"):
            reasons.append(f"Premarket gap {m.premarket_pct:+.1f}%")
        if m.rvol is not None and good("rvol"):
            reasons.append(f"Relative volume {m.rvol:.1f}x normal for this time of day")
        if m.momentum_short is not None and good("momentum_5m"):
            reasons.append(f"Short-term momentum {m.momentum_short:+.2f}%")
        if m.vs_vwap_pct is not None and good("vs_vwap"):
            reasons.append(f"Holding {m.vs_vwap_pct:.1f}% above VWAP")
        if m.dist_from_high_pct is not None and good("near_high"):
            reasons.append(f"Within {m.dist_from_high_pct:.1f}% of the day high")
        if good("liquidity"):
            reasons.append(f"Liquid: ${m.dollar_volume / 1e6:.1f}M traded")
        if m.vs_vwap_pct is not None and m.vs_vwap_pct < 0:
            warnings.append(f"Below VWAP ({m.vs_vwap_pct:.1f}%) - move may be fading")
        if m.momentum_short is not None and m.momentum_short < 0:
            warnings.append(f"Negative short-term momentum ({m.momentum_short:+.2f}%)")
        if m.dist_from_high_pct is not None and m.dist_from_high_pct > 8:
            warnings.append(f"{m.dist_from_high_pct:.1f}% off the high - pullback or exhaustion?")
        for c in comps:
            if c.score is None and c.weight > 0:
                warnings.append(f"{c.label}: unavailable from this feed (excluded from score)")
        if m.rvol is not None and m.rvol_estimated:
            warnings.append("RVOL is estimated from average daily volume and a standard intraday curve")
        return reasons, warnings

    @staticmethod
    def _continuing(m: StockMetrics) -> bool:
        """Is the move still going? Positive recent momentum, above VWAP, near the highs."""
        if m.momentum_short is None or m.vs_vwap_pct is None or m.dist_from_high_pct is None:
            return False
        return bool(m.momentum_short > 0 and m.vs_vwap_pct > 0 and m.dist_from_high_pct < 6.0)

    # ------------------------------------------------------------------ public
    def scan_one(self, ctx: StockContext) -> ScanResult:
        m = compute_metrics(ctx, self.config.momentum_short_minutes, self.config.momentum_long_minutes)
        blockers, notes = self._filters(m)
        comps = self._components(m)
        reasons, warnings = self._reasons(m, comps)
        res = ScanResult(
            symbol=ctx.symbol, name=ctx.meta.name or ctx.symbol, metrics=m,
            score=sum(c.points for c in comps), components=comps, reasons=reasons,
            warnings=warnings + notes, blockers=blockers, continuing=self._continuing(m),
        )
        return res

    def scan(self, contexts: list[StockContext]) -> list[ScanResult]:
        """Scan all contexts; tradable first by score, then filtered-out (also by score). Ranks are 1-based
        among tradable symbols; filtered-out symbols get rank 0."""
        results = [self.scan_one(c) for c in contexts]
        ok = sorted((r for r in results if r.tradable), key=lambda r: (-r.score, r.symbol))
        bad = sorted((r for r in results if not r.tradable), key=lambda r: (-r.score, r.symbol))
        for i, r in enumerate(ok, 1):
            r.rank = i
        return ok + bad
