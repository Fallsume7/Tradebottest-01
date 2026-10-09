"""Per-trade plan: entry estimate, profit target, stop, holding time - derived from the forecast, not fixed.

* **Target** = the price where the model estimates a ``target_prob_goal`` chance (default ~45 %) of
  *touching* it during the horizon (reflection principle for a drifting random walk), then capped by the
  nearest recent resistance (unless price is already breaking out) and by configured bounds. It is a
  *plausible upside level*, deliberately different from the *expected return* (a mean).
* **Stop** = a multiple of the stock's typical short-term adverse movement, within configured bounds.
* **Deadline** = the planning horizon, never later than the end-of-day buffer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from mwbot.config import ModeProfile
from mwbot.forecast.features import Features
from mwbot.forecast.models import Forecast, norm_cdf


def p_touch(target_pct: float, mu: float, sigma: float) -> float:
    """P(max over the horizon >= target) for Brownian motion with drift ``mu`` and std ``sigma`` over the horizon."""
    if target_pct <= 0:
        return 1.0
    if sigma <= 1e-9:
        return 1.0 if mu >= target_pct else 0.0
    a = 1.0 - norm_cdf((target_pct - mu) / sigma)
    expo = 2.0 * mu * target_pct / (sigma * sigma)
    b = 0.0 if expo > 60 else math.exp(max(expo, -60)) * (1.0 - norm_cdf((target_pct + mu) / sigma))
    return float(min(1.0, max(0.0, a + b)))


@dataclass(slots=True)
class TradePlan:
    entry_price: float  # estimated fill (last price + modelled buy-side cost)
    target_price: float
    target_return_pct: float  # gross, from the entry estimate
    target_net_return_pct: float
    p_target: float
    stop_price: float
    stop_pct: float
    max_hold_minutes: float
    reward_risk: float
    trailing_pct: float | None
    breakeven_progress: float | None
    horizon_minutes: float
    notes: list[str] = field(default_factory=list)  # why the target/stop are where they are
    blockers: list[str] = field(default_factory=list)  # reasons this plan is not tradeable

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_price": round(self.entry_price, 4),
            "target_price": round(self.target_price, 4),
            "target_return_pct": round(self.target_return_pct, 3),
            "target_net_return_pct": round(self.target_net_return_pct, 3),
            "p_target": round(self.p_target, 3),
            "stop_price": round(self.stop_price, 4),
            "stop_pct": round(self.stop_pct, 3),
            "max_hold_minutes": round(self.max_hold_minutes, 1),
            "reward_risk": round(self.reward_risk, 2),
            "trailing_pct": self.trailing_pct,
            "breakeven_progress": self.breakeven_progress,
            "horizon_minutes": round(self.horizon_minutes, 1),
            "notes": self.notes,
            "blockers": self.blockers,
        }


def usable_horizon(profile: ModeProfile, minutes_to_close: float) -> float:
    """Prediction horizon that shrinks as the close approaches: a share of the usable remaining session,
    capped, and never beyond the planned end-of-day exit."""
    p = profile.plan
    usable = max(minutes_to_close - p.end_buffer_minutes, 0.0)
    h = min(p.max_horizon_minutes, usable * p.horizon_fraction)
    return max(min(h, usable), 0.0) if usable < p.min_horizon_minutes else max(h, p.min_horizon_minutes)


def make_trade_plan(f: Features, fc: Forecast, profile: ModeProfile) -> TradePlan:
    pc = profile.plan
    price = f.price
    mu, sigma, cost = fc.expected_return_pct, fc.sigma_pct, fc.cost_pct
    notes: list[str] = []
    blockers: list[str] = []
    entry = price * (1 + cost / 2 / 100)  # buy-side half of the round trip

    t_min = max(pc.min_target_pct, 1.3 * cost)
    t_max = max(pc.max_target_pct, t_min)
    # largest target that still has the goal probability of being touched
    target = t_min
    grid = [t_min + (t_max - t_min) * i / 60 for i in range(61)]
    for t in grid:
        if p_touch(t, mu, sigma) >= pc.target_prob_goal:
            target = t
    notes.append(f"target set where the estimated chance of touching it is about {pc.target_prob_goal:.0%}")

    if f.resistance_price and (f.breakout_pct is None or f.breakout_pct <= 0):
        room = (f.resistance_price * 0.999 / entry - 1) * 100
        if room < target:
            if room < t_min:
                blockers.append(
                    f"overhead resistance at ${f.resistance_price:.2f} leaves too little room above the entry"
                )
            else:
                target = room
                notes.append(f"target capped just under recent resistance ${f.resistance_price:.2f}")
    p_t = p_touch(target, mu, sigma)

    sigma_stop = f.sigma_1m_pct * math.sqrt(min(max(fc.horizon_minutes, 5.0), 20.0))
    stop_pct = min(
        max(pc.stop_sigma_mult * sigma_stop, pc.min_stop_pct),
        min(pc.max_stop_pct, profile.risk.stop_loss_pct),
    )
    notes.append(
        f"stop {stop_pct:.2f}% below entry = {pc.stop_sigma_mult:g}x the stock's typical 20-minute swing, within limits"
    )

    rr = (target - cost) / (stop_pct + cost) if (stop_pct + cost) > 0 else 0.0
    hold = max(min(fc.horizon_minutes, f.minutes_to_close - pc.end_buffer_minutes), 0.0)
    return TradePlan(
        entry_price=entry,
        target_price=entry * (1 + target / 100),
        target_return_pct=target,
        target_net_return_pct=target - cost,
        p_target=p_t,
        stop_price=entry * (1 - stop_pct / 100),
        stop_pct=stop_pct,
        max_hold_minutes=hold,
        reward_risk=rr,
        trailing_pct=pc.trailing_pct,
        breakeven_progress=pc.breakeven_progress,
        horizon_minutes=fc.horizon_minutes,
        notes=notes,
        blockers=blockers,
    )
