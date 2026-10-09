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
from datetime import date
from typing import Any

from mwbot.config import GameRules, ScannerConfig, ScannerFilters
from mwbot.domain import StockContext
from mwbot.eligibility.models import EligibilityCheck, EligibilityStatus
from mwbot.eligibility.universe import EligibilityUniverse
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
            "name": self.name,
            "label": self.label,
            "raw": None if self.raw is None else round(self.raw, 4),
            "score": None if self.score is None else round(self.score, 3),
            "weight": self.weight,
            "points": round(self.points, 2),
            "note": self.note,
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
    blockers: list[str] = field(default_factory=list)  # ALL filters (classic momentum scan)
    basic_blockers: list[str] = field(
        default_factory=list
    )  # data quality / liquidity only (no momentum filters)
    continuing: bool = False
    eligibility: EligibilityCheck | None = None  # None = eligibility was not evaluated for this scan

    @property
    def tradable(self) -> bool:
        """Passes every classic filter (including the momentum ones)."""
        return not self.blockers

    @property
    def basic_ok(self) -> bool:
        """Liquid, fresh, sane data - regardless of how much the stock has moved."""
        return not self.basic_blockers

    @property
    def eligibility_status(self) -> EligibilityStatus:
        return self.eligibility.status if self.eligibility else EligibilityStatus.UNVERIFIED

    @property
    def actionable(self) -> bool:
        """May appear in the PRIMARY recommendations: passes classic filters AND is confirmed tradable in the game."""
        return self.tradable and self.eligibility is not None and self.eligibility.actionable

    @property
    def opportunity_ok(self) -> bool:
        """Eligible for the predictive ranking: basic data checks + confirmed tradable in the game."""
        return self.basic_ok and self.eligibility is not None and self.eligibility.actionable

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "rank": self.rank,
            "score": round(self.score, 1),
            "tradable": self.tradable,
            "basic_ok": self.basic_ok,
            "actionable": self.actionable,
            "opportunity_ok": self.opportunity_ok,
            "continuing": self.continuing,
            "eligibility": self.eligibility.to_dict() if self.eligibility else None,
            "basic_blockers": self.basic_blockers,
            "metrics": self.metrics.to_dict(),
            "components": [c.to_dict() for c in self.components],
            "reasons": self.reasons,
            "warnings": self.warnings,
            "blockers": self.blockers,
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
    def __init__(self, config: ScannerConfig | None = None, rules: GameRules | None = None) -> None:
        self.config = config or ScannerConfig()
        self.rules = rules

    # ------------------------------------------------------------------ filters
    def _filters(self, m: StockMetrics) -> tuple[list[str], list[str], list[str]]:
        """Returns (all_blockers, basic_blockers, notes).

        *basic* blockers are about data quality and tradeability (price band, volume, spread, history,
        freshness, halts, abnormal prints). The remaining ones are the classic *momentum* filters
        (minimum % change, maximum % change, minimum RVOL) which only the old gainer-style strategies want.
        """
        f: ScannerFilters = self.config.filters
        missing_exclude = self.config.on_missing_data == "exclude"
        blockers: list[str] = []
        basic: list[str] = []
        notes: list[str] = []

        def block(msg: str, *, is_basic: bool = True) -> None:
            blockers.append(msg)
            if is_basic:
                basic.append(msg)

        def need(value: float | None, label: str, *, is_basic: bool = True) -> bool:
            """True if value present. If absent: exclude or note, per policy."""
            if value is not None:
                return True
            if missing_exclude:
                block(f"{label} unavailable from this feed", is_basic=is_basic)
            else:
                notes.append(f"{label} filter skipped: data unavailable")
            return False

        if m.price is None or m.n_bars == 0:
            return ["no price data yet"], ["no price data yet"], notes
        if m.abnormal:
            block("abnormal data: " + ", ".join(m.abnormal))
        if f.exclude_halted and m.halted:
            block("trading halted")
        if m.n_bars < f.min_bars:
            block(f"only {m.n_bars} bars (<{f.min_bars}) - not enough history")
        if m.last_trade_age_seconds > f.max_data_age_seconds:
            block(f"last trade {m.last_trade_age_seconds / 60:.1f} min ago (stale/illiquid)")
        if not (f.min_price <= m.price <= f.max_price):
            block(f"price ${m.price:.2f} outside ${f.min_price:g}-${f.max_price:g}")
        if need(m.pct_change, "% change", is_basic=False):
            assert m.pct_change is not None
            if m.pct_change < f.min_pct_change:
                block(f"change {m.pct_change:+.1f}% below {f.min_pct_change:g}%", is_basic=False)
            if f.max_pct_change is not None and m.pct_change > f.max_pct_change:
                block(f"change {m.pct_change:+.1f}% above {f.max_pct_change:g}% (suspicious)", is_basic=False)
        if m.volume < f.min_volume:
            block(f"volume {m.volume:,.0f} below {f.min_volume:,.0f}")
        if m.dollar_volume < f.min_dollar_volume:
            block(f"dollar volume ${m.dollar_volume:,.0f} below ${f.min_dollar_volume:,.0f}")
        if f.min_rvol > 0 and need(m.rvol, "relative volume", is_basic=False) and (m.rvol or 0) < f.min_rvol:
            block(f"RVOL {m.rvol:.1f}x below {f.min_rvol:g}x", is_basic=False)
        if f.min_market_cap is not None and need(m.market_cap, "market cap"):
            assert m.market_cap is not None
            if m.market_cap < f.min_market_cap:
                block(f"market cap ${m.market_cap / 1e6:.0f}M below ${f.min_market_cap / 1e6:.0f}M")
        if f.max_market_cap is not None and need(m.market_cap, "market cap"):
            assert m.market_cap is not None
            if m.market_cap > f.max_market_cap:
                block(f"market cap ${m.market_cap / 1e6:.0f}M above ${f.max_market_cap / 1e6:.0f}M")
        if f.max_spread_pct is not None and need(m.spread_pct, "bid-ask spread"):
            assert m.spread_pct is not None
            if m.spread_pct > f.max_spread_pct:
                block(f"spread {m.spread_pct:.2f}% above {f.max_spread_pct:g}%")
        return blockers, basic, notes

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
    def scan_one(
        self, ctx: StockContext, universe: EligibilityUniverse | None = None, as_of: date | None = None
    ) -> ScanResult:
        m = compute_metrics(ctx, self.config.momentum_short_minutes, self.config.momentum_long_minutes)
        blockers, basic, notes = self._filters(m)
        comps = self._components(m)
        reasons, warnings = self._reasons(m, comps)
        elig = None
        if universe is not None:
            # Eligibility comes ONLY from the list - never from the fact that market data exists for the ticker.
            elig = universe.check(ctx.symbol, price=m.price, rules=self.rules, as_of=as_of, exchange_hint="")
        return ScanResult(
            symbol=ctx.symbol,
            name=ctx.meta.name or ctx.symbol,
            metrics=m,
            score=sum(c.points for c in comps),
            components=comps,
            reasons=reasons,
            warnings=warnings + notes,
            blockers=blockers,
            basic_blockers=basic,
            continuing=self._continuing(m),
            eligibility=elig,
        )

    def scan(
        self,
        contexts: list[StockContext],
        universe: EligibilityUniverse | None = None,
        as_of: date | None = None,
    ) -> list[ScanResult]:
        """Classic momentum scan. Order: *actionable* (classic filters + confirmed tradable) first by score, then
        everything else by score. Ranks are 1-based among actionable symbols; others get rank 0.

        With ``universe=None`` eligibility is not evaluated (unit-test / legacy use): nothing is actionable.
        """
        results = [self.scan_one(c, universe, as_of) for c in contexts]
        key = (lambda r: r.actionable) if universe is not None else (lambda r: r.tradable)
        ok = sorted((r for r in results if key(r)), key=lambda r: (-r.score, r.symbol))
        bad = sorted((r for r in results if not key(r)), key=lambda r: (-r.score, r.symbol))
        for i, r in enumerate(ok, 1):
            r.rank = i
        return ok + bad
