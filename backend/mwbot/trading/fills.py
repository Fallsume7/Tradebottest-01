"""Deterministic fill model. Rules (also in docs/ARCHITECTURE.md):

1. An order may only fill on a bar whose open time is >= ``order.eligible_at``
   (= submit time + latency). A signal evaluated at T therefore never fills on information
   from the bar that was still forming at T.
2. No fill on halted minutes, missing bars (nobody traded) or bars below ``min_fill_volume``.
3. Fill size per bar <= ``max_participation`` x bar volume. Remainders stay pending (partial fills).
4. MARKET orders fill at the bar OPEN adjusted against the trader by half-spread + slippage +
   size impact, clamped to the bar's [low, high] (a print cannot be outside the bar).
5. LIMIT orders fill only if the bar trades *through* the limit (strictly): buy needs low < limit,
   sell needs high > limit. If the bar opens already better than the limit, fill at the open.
   Limit fills pay no spread/slippage. (Conservative: touching the limit is not a fill.)
6. Commission: per share + per order (first fill of an order), subject to a minimum per order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from mwbot.config import CostConfig
from mwbot.domain import Order, OrderType, Side


@dataclass(slots=True)
class FillResult:
    quantity: int
    price: float
    commission: float
    note: str = ""


def commission_for(costs: CostConfig, qty: int, first_fill: bool) -> float:
    c = qty * costs.commission_per_share + (costs.commission_per_order if first_fill else 0.0)
    if first_fill:
        c = max(c, costs.commission_min_per_order)
    return round(c, 4)


def try_fill(
    order: Order,
    bar: pd.Series | None,
    halted: bool,
    spread_bps: float | None,
    costs: CostConfig,
    max_qty: int | None = None,
) -> FillResult | None:
    """Return a (possibly partial) fill for ``order`` on ``bar`` or ``None``. ``max_qty`` caps size (cash)."""
    if halted or bar is None:
        return None
    volume = float(bar["volume"])
    if volume < costs.min_fill_volume:
        return None
    cap = math.floor(volume * costs.max_participation)
    qty = min(order.remaining, cap)
    if max_qty is not None:
        qty = min(qty, max_qty)
    if qty <= 0:
        return None

    o, h, low = float(bar["open"]), float(bar["high"]), float(bar["low"])
    first = order.filled_quantity == 0
    if order.order_type == OrderType.LIMIT:
        assert order.limit_price is not None
        lim = order.limit_price
        if order.side == Side.BUY:
            if o <= lim:
                px, note = o, "limit buy filled at open (better than limit)"
            elif low < lim:
                px, note = lim, "limit buy traded through"
            else:
                return None
        else:
            if o >= lim:
                px, note = o, "limit sell filled at open (better than limit)"
            elif h > lim:
                px, note = lim, "limit sell traded through"
            else:
                return None
    else:
        spread = (spread_bps if spread_bps is not None else costs.assumed_spread_bps) / 1e4
        participation_pct = qty / volume * 100
        adverse = (
            spread / 2
            + costs.slippage_bps / 1e4
            + participation_pct * costs.impact_bps_per_pct_participation / 1e4
        )
        px = min(o * (1 + adverse), h) if order.side == Side.BUY else max(o * (1 - adverse), low)
        note = f"market fill: open {o:.4f}, adverse {adverse * 1e4:.1f}bps, participation {participation_pct:.2f}%"
    return FillResult(
        quantity=qty, price=round(px, 4), commission=commission_for(costs, qty, first), note=note
    )
