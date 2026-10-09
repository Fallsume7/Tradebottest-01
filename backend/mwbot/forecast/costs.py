"""Modelled round-trip trading cost, as a percentage of the position, used to turn gross forecasts into net."""

from __future__ import annotations

from mwbot.config import CostConfig


def round_trip_cost_pct(
    costs: CostConfig,
    spread_pct: float | None,
    price: float,
    notional: float = 10_000.0,
    participation_pct: float = 0.3,
) -> float:
    """Expected cost of buying then selling, in % of the position.

    Mirrors the simulator's fill model: half the quoted spread + slippage + size impact on each leg, plus
    commissions. ``spread_pct=None`` (unknown) falls back to ``costs.assumed_spread_bps``.
    """
    spread = (spread_pct / 100.0) if spread_pct is not None else costs.assumed_spread_bps / 1e4
    leg = (
        spread / 2
        + costs.slippage_bps / 1e4
        + participation_pct * costs.impact_bps_per_pct_participation / 1e4
    )
    shares = notional / max(price, 0.01)
    commission = 2 * (costs.commission_per_share * shares + costs.commission_per_order)
    commission = max(commission, 2 * costs.commission_min_per_order)
    return (2 * leg + commission / notional) * 100.0
