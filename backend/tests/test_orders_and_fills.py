from datetime import UTC, datetime

import pandas as pd
import pytest

from mwbot.config import CostConfig
from mwbot.domain import (
    InvalidTransition,
    Order,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)
from mwbot.trading.fills import commission_for, try_fill

T0 = datetime(2026, 10, 8, 14, 0, tzinfo=UTC)


def order(side=Side.BUY, qty=100, typ=OrderType.MARKET, limit=None, **kw) -> Order:
    return Order(
        id="O1",
        client_order_id="c1",
        symbol="TST",
        side=side,
        quantity=qty,
        order_type=typ,
        limit_price=limit,
        time_in_force=TimeInForce.DAY,
        strategy="t",
        reason="",
        created_at=T0,
        **kw,
    )


def bar(o=10.0, h=10.2, low=9.9, c=10.1, v=50_000) -> pd.Series:
    return pd.Series({"open": o, "high": h, "low": low, "close": c, "volume": v})


FREE = CostConfig(slippage_bps=0, assumed_spread_bps=0, impact_bps_per_pct_participation=0)


def test_state_machine_allows_normal_path_and_blocks_illegal():
    o = order()
    o.transition(OrderStatus.PENDING, T0)
    o.transition(OrderStatus.PARTIALLY_FILLED, T0)
    o.transition(OrderStatus.FILLED, T0)
    with pytest.raises(InvalidTransition):
        o.transition(OrderStatus.CANCELED, T0)  # terminal
    with pytest.raises(InvalidTransition):
        order().transition(OrderStatus.FILLED, T0)  # NEW -> FILLED not allowed


def test_market_buy_fills_at_open_plus_costs():
    costs = CostConfig(slippage_bps=10, assumed_spread_bps=40, impact_bps_per_pct_participation=0)
    res = try_fill(order(), bar(o=10.0, h=10.5), False, None, costs)
    assert res is not None and res.quantity == 100
    # adverse = 20bps half-spread + 10bps slippage = 30bps
    assert res.price == pytest.approx(10.0 * 1.003, abs=1e-4)


def test_market_sell_fills_below_open():
    res = try_fill(
        order(Side.SELL),
        bar(o=10.0, low=9.0),
        False,
        None,
        CostConfig(slippage_bps=10, assumed_spread_bps=40, impact_bps_per_pct_participation=0),
    )
    assert res.price == pytest.approx(10.0 * (1 - 0.003), abs=1e-4)


def test_fill_price_is_clamped_to_bar_range():
    res = try_fill(order(), bar(o=10.0, h=10.001, low=9.99), False, None, CostConfig(slippage_bps=500))
    assert res.price <= 10.001


def test_uses_feed_spread_when_available():
    res_wide = try_fill(order(), bar(h=11), False, 200.0, FREE)
    res_none = try_fill(order(), bar(h=11), False, 0.0, FREE)
    assert res_wide.price > res_none.price


def test_participation_cap_gives_partial_fill():
    o = order(qty=1000)
    res = try_fill(o, bar(v=5_000), False, None, CostConfig(max_participation=0.10))
    assert res.quantity == 500  # 10% of 5000


def test_no_fill_when_halted_missing_bar_or_thin_volume():
    assert try_fill(order(), bar(), True, None, FREE) is None
    assert try_fill(order(), None, False, None, FREE) is None
    assert try_fill(order(), bar(v=50), False, None, CostConfig(min_fill_volume=100)) is None


def test_larger_orders_pay_more_impact():
    c = CostConfig(
        slippage_bps=0, assumed_spread_bps=0, impact_bps_per_pct_participation=5, max_participation=1.0
    )
    small = try_fill(order(qty=100), bar(h=20, v=100_000), False, None, c)
    large = try_fill(order(qty=10_000), bar(h=20, v=100_000), False, None, c)
    assert large.price > small.price


@pytest.mark.parametrize(
    "side,limit,b,expect",
    [
        (Side.BUY, 9.95, bar(o=10.0, low=9.90), 9.95),  # traded through the limit -> fill at limit
        (Side.BUY, 9.95, bar(o=10.0, low=9.95), None),  # merely touching is not a fill (conservative)
        (Side.BUY, 10.5, bar(o=10.0), 10.0),  # opens better than limit -> fill at open
        (Side.SELL, 10.15, bar(o=10.0, h=10.2), 10.15),
        (Side.SELL, 10.2, bar(o=10.0, h=10.2), None),
        (Side.SELL, 9.5, bar(o=10.0), 10.0),
    ],
)
def test_limit_order_rules(side, limit, b, expect):
    res = try_fill(order(side, typ=OrderType.LIMIT, limit=limit), b, False, None, CostConfig(slippage_bps=50))
    if expect is None:
        assert res is None
    else:
        assert res.price == pytest.approx(expect)


def test_commissions_per_share_per_order_and_minimum():
    c = CostConfig(commission_per_share=0.005, commission_per_order=1.0, commission_min_per_order=2.5)
    assert commission_for(c, 100, True) == pytest.approx(2.5)  # 0.5+1.0=1.5 -> min 2.5
    assert commission_for(c, 1000, True) == pytest.approx(6.0)  # 5+1
    assert commission_for(c, 1000, False) == pytest.approx(5.0)  # follow-up partial: no per-order fee
