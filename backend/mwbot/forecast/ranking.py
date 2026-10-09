"""Expected Opportunity Ranking: which *eligible* stock has the best risk-adjusted expected net return over
the time that is realistically left today - NOT which stock rose the most.

Pipeline per symbol: basic data/liquidity checks -> eligibility (from the list only) -> features -> forecast
-> trade plan -> gates -> utility/score. Unavailable stocks are never ranked; unverified ones are ranked
separately as RESEARCH candidates and can never be entered.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from mwbot.config import CostConfig, ModeProfile
from mwbot.domain import StockContext
from mwbot.eligibility.models import EligibilityStatus
from mwbot.forecast.costs import round_trip_cost_pct
from mwbot.forecast.features import Features, MarketState, compute_features
from mwbot.forecast.models import Forecast, Forecaster, HeuristicForecaster
from mwbot.forecast.plan import TradePlan, make_trade_plan, usable_horizon
from mwbot.scanner.scanner import ScanResult


@dataclass(slots=True)
class Gate:
    label: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "passed": bool(self.passed), "detail": self.detail}


@dataclass(slots=True)
class Opportunity:
    symbol: str
    name: str
    price: float
    scan: ScanResult
    features: Features | None
    forecast: Forecast | None
    plan: TradePlan | None
    horizon_minutes: float = 0.0
    minutes_to_close: float = 0.0
    utility: float = 0.0
    score: float = 0.0
    rank: int = 0
    gates: list[Gate] = field(default_factory=list)
    explanation: dict[str, Any] = field(default_factory=dict)
    held: bool = False

    @property
    def eligibility_status(self) -> EligibilityStatus:
        return self.scan.eligibility_status

    @property
    def actionable(self) -> bool:
        """Confirmed tradable (+ game rules) AND every gate passed: the only kind that may be entered."""
        return bool(
            self.scan.opportunity_ok and self.forecast and self.plan and all(g.passed for g in self.gates)
        )

    @property
    def research_only(self) -> bool:
        return self.scan.eligibility_status == EligibilityStatus.UNVERIFIED and self.scan.basic_ok

    def to_dict(self) -> dict[str, Any]:
        f = self.forecast
        p = self.plan
        return {
            "symbol": self.symbol,
            "name": self.name,
            "price": round(self.price, 4),
            "rank": self.rank,
            "score": round(self.score, 1),
            "utility": round(self.utility, 3),
            "actionable": self.actionable,
            "research_only": self.research_only,
            "held": self.held,
            "eligibility": self.scan.eligibility.to_dict() if self.scan.eligibility else None,
            "minutes_to_close": round(self.minutes_to_close, 1),
            "horizon_minutes": round(self.horizon_minutes, 1),
            "forecast": f.to_dict() if f else None,
            "plan": p.to_dict() if p else None,
            "gates": [g.to_dict() for g in self.gates],
            "explanation": self.explanation,
            "data_blockers": self.scan.basic_blockers,
            "day_change_pct": self.scan.metrics.pct_change,
        }


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(min(x, 30), -30)))


def liquidity_score(dollar_volume: float, spread_pct: float | None, cost_pct: float) -> float:
    liq = min(max((math.log10(max(dollar_volume, 1.0)) - 5.8) / 2.0, 0.0), 1.0)
    tight = 1.0 - min(cost_pct / 1.2, 1.0)
    return 0.6 * liq + 0.4 * tight


def utility_of(fc: Forecast, liq: float, profile: ModeProfile) -> float:
    o = profile.objective
    net = fc.expected_net_return_pct
    down_net = fc.downside_pct - fc.cost_pct
    up_net = fc.upside_pct - fc.cost_pct
    return (
        o.w_return * net
        + o.w_probability * (fc.p_positive - 0.5) * 2.0
        + o.w_liquidity * liq * 0.5
        - o.w_downside * max(0.0, -down_net)
        - o.w_uncertainty * fc.sigma_pct
        + o.w_upside * max(0.0, up_net)
    )


def evaluate_gates(
    f: Features, fc: Forecast, plan: TradePlan, profile: ModeProfile, calibrated_probs: bool
) -> list[Gate]:
    g = profile.gates
    gates = [
        Gate(
            "Enough time before the close",
            f.minutes_to_close >= g.min_minutes_to_close,
            f"{f.minutes_to_close:.0f} min left (need >= {g.min_minutes_to_close:.0f})",
        ),
        Gate(
            "Expected return after costs",
            fc.expected_net_return_pct >= g.min_expected_net_return_pct,
            f"{fc.expected_net_return_pct:+.2f}% (need >= {g.min_expected_net_return_pct:g}%)",
        ),
        Gate(
            "Chance of a positive net return",
            fc.p_positive >= g.min_p_positive,
            f"{fc.p_positive:.0%} (need >= {g.min_p_positive:.0%})"
            + ("" if calibrated_probs else " - heuristic, not validated"),
        ),
        Gate(
            "Chance of reaching the target",
            plan.p_target >= g.min_p_target,
            f"{plan.p_target:.0%} (need >= {g.min_p_target:.0%})",
        ),
        Gate(
            "Reward vs risk",
            plan.reward_risk >= g.min_reward_risk,
            f"{plan.reward_risk:.2f} (need >= {g.min_reward_risk:g})",
        ),
        Gate(
            "Not too volatile for this mode",
            fc.sigma_pct <= g.max_horizon_sigma_pct,
            f"typical move {fc.sigma_pct:.1f}% (max {g.max_horizon_sigma_pct:g}%)",
        ),
        Gate(
            "Trading costs",
            fc.cost_pct <= g.max_cost_pct,
            f"round trip {fc.cost_pct:.2f}% (max {g.max_cost_pct:g}%)",
        ),
        Gate("Room to the target", not plan.blockers, "; ".join(plan.blockers) or "ok"),
    ]
    if g.min_rvol > 0:
        gates.append(
            Gate(
                "Relative volume",
                (f.rvol or 0) >= g.min_rvol,
                f"{(f.rvol or 0):.1f}x (need >= {g.min_rvol:g}x)",
            )
        )
    return gates


def explain(f: Features, fc: Forecast, plan: TradePlan, opp_score: float) -> dict[str, Any]:
    """Plain-language explanation that never claims more certainty than the forecast has."""
    pos = [c for c in fc.contributions if c.points > 0.005][:4]
    neg = [c for c in fc.contributions if c.points < -0.005][:3]
    vwap = f.price / (1 + f.vs_vwap_pct / 100) if f.vs_vwap_pct is not None else None
    invalid = []
    if vwap:
        invalid.append(f"Price falls back below VWAP (about ${vwap:.2f}) and stays there")
    invalid.append(f"Price hits the risk exit at ${plan.stop_price:.2f}")
    invalid.append("Momentum turns negative while volume fades")
    invalid.append("The updated forecast turns negative (expected net return below the hold threshold)")
    kind = "validated model" if fc.calibrated else "rule-of-thumb estimate (not validated)"
    return {
        "summary": (
            f"Expected {fc.expected_net_return_pct:+.2f}% after costs over ~{fc.horizon_minutes:.0f} min "
            f"(uncertainty about +/-{fc.sigma_pct:.1f}%); target ${plan.target_price:.2f} ({plan.target_return_pct:+.2f}%), "
            f"risk exit ${plan.stop_price:.2f}. This is a {kind}."
        ),
        "drivers": [f"{c.label} ({c.points:+.2f} pts)" for c in pos],
        "risks": [f"{c.label} ({c.points:+.2f} pts)" for c in neg],
        "invalidation": invalid,
        "plan_notes": plan.notes,
        "data_note": "; ".join(fc.warnings),
        "calibrated": fc.calibrated,
    }


class OpportunityRanker:
    def __init__(self, forecaster: Forecaster | None = None) -> None:
        self.forecaster: Forecaster = forecaster or HeuristicForecaster()

    def horizon_for(self, profile: ModeProfile, minutes_to_close: float) -> float:
        return usable_horizon(profile, minutes_to_close)

    def evaluate(
        self,
        ctx: StockContext,
        scan: ScanResult,
        market: MarketState,
        profile: ModeProfile,
        costs: CostConfig,
        *,
        horizon: float | None = None,
        hist: tuple[float | None, int] = (None, 0),
    ) -> Opportunity:
        f = compute_features(ctx, scan.metrics, market, hist[0], hist[1])
        mins = max((ctx.session.close_utc - ctx.as_of).total_seconds() / 60.0, 0.0)
        opp = Opportunity(
            ctx.symbol, scan.name, scan.metrics.price or 0.0, scan, f, None, None, minutes_to_close=mins
        )
        if f is None:
            opp.gates = [Gate("Enough price history", False, "not enough bars yet")]
            return opp
        h = horizon if horizon is not None else self.horizon_for(profile, mins)
        opp.horizon_minutes = h
        cost = round_trip_cost_pct(
            costs,
            f.spread_pct,
            f.price,
            notional=max(profile.risk.max_position_pct / 100 * profile.risk.starting_capital, 1000.0),
        )
        fc = self.forecaster.predict(f, max(h, 1.0), cost)
        plan = make_trade_plan(f, fc, profile)
        opp.forecast, opp.plan = fc, plan
        opp.gates = evaluate_gates(f, fc, plan, profile, fc.calibrated)
        liq = liquidity_score(f.dollar_volume, f.spread_pct, cost)
        opp.utility = utility_of(fc, liq, profile)
        opp.score = 100.0 * _sigmoid(opp.utility)
        opp.explanation = explain(f, fc, plan, opp.score)
        return opp

    def rank(
        self,
        contexts: list[StockContext],
        scans: list[ScanResult],
        market: MarketState,
        profile: ModeProfile,
        costs: CostConfig,
        *,
        held: set[str] | None = None,
        as_of: date | None = None,  # noqa: ARG002 - eligibility was already resolved into the scans
        hist_lookup: Any = None,
    ) -> list[Opportunity]:
        by_ctx = {c.symbol: c for c in contexts}
        out: list[Opportunity] = []
        for sc in scans:
            if not sc.basic_ok or sc.eligibility_status == EligibilityStatus.CONFIRMED_UNAVAILABLE:
                continue  # unusable data, or known-unsupported: never recommended, never researched
            ctx = by_ctx.get(sc.symbol)
            if ctx is None:
                continue
            hist = hist_lookup(ctx) if hist_lookup else (None, 0)
            opp = self.evaluate(ctx, sc, market, profile, costs, hist=hist)
            opp.held = bool(held and sc.symbol in held)
            out.append(opp)
        out.sort(key=lambda o: (-o.utility, o.symbol))
        for i, o in enumerate([o for o in out if o.actionable], 1):
            o.rank = i
        return out
