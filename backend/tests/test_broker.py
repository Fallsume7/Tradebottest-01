"""Virtual order execution + portfolio accounting."""

from datetime import timedelta

import pytest

from mwbot.config import CostConfig
from mwbot.domain import OrderRequest, OrderStatus, OrderType, Side, TimeInForce
from mwbot.execution.base import Holding, reconcile_holdings
from mwbot.trading.broker import PaperBroker

from .conftest import TEST_DAY, at, build_day, make_source, meta  # noqa: F401

FREE = CostConfig(
    slippage_bps=0,
    assumed_spread_bps=0,
    impact_bps_per_pct_participation=0,
    latency_seconds=0,
    max_participation=1.0,
)


@pytest.fixture()
def world(session_info):
    """Flat $10 stock with generous volume; returns (source, broker factory)."""
    bars = build_day(session_info, 10.0, [], base_volume=100_000, wiggle=0.0)
    src = make_source(session_info, {"TST": (meta(), bars)}, spread_bps=0)

    def new(costs=FREE, cash=100_000.0):
        b = PaperBroker(cash, costs)
        b.session_date = session_info.date
        b.mark("TST", 10.0)
        return b

    return src, new


def buy(b, now, qty=100, cid="b1", **kw):
    return b.submit(
        OrderRequest(cid, "TST", Side.BUY, qty, meta={"stop_loss_pct": 5, "take_profit_pct": 10}, **kw), now
    )


def test_buy_then_sell_round_trip_accounting(world, session_info):
    src, new = world
    b = new()
    t = at(session_info, 10)
    o = buy(b, t, 100)
    assert o.status == OrderStatus.PENDING and b.buying_power() == pytest.approx(100_000 - 1000)
    b.process(src, t + timedelta(minutes=2))
    assert o.status == OrderStatus.FILLED and o.avg_fill_price == pytest.approx(10.0)
    pos = b.positions["TST"]
    assert (
        pos.quantity == 100
        and pos.stop_price == pytest.approx(9.5)
        and pos.take_profit_price == pytest.approx(11.0)
    )
    assert b.cash == pytest.approx(99_000)
    s = b.submit(OrderRequest("s1", "TST", Side.SELL, 100, reason="test exit"), t + timedelta(minutes=3))
    b.process(src, t + timedelta(minutes=6))
    assert s.status == OrderStatus.FILLED and "TST" not in b.positions
    assert (
        len(b.trades) == 1
        and b.trades[0].net_pnl == pytest.approx(0.0)
        and b.trades[0].exit_reason == "test exit"
    )
    assert b.cash == pytest.approx(100_000)


def test_never_fills_on_the_signal_bar(world, session_info):
    src, new = world
    b = new(CostConfig(latency_seconds=5, slippage_bps=0, assumed_spread_bps=0))
    t = at(session_info, 10)
    o = buy(b, t)
    b.process(src, t)  # nothing complete yet
    assert o.filled_quantity == 0
    b.process(
        src, t + timedelta(minutes=1)
    )  # bar labelled t completes, but order only eligible t+5s -> bar t+1m
    assert o.filled_quantity == 0
    b.process(src, t + timedelta(minutes=2))
    assert o.status == OrderStatus.FILLED
    assert b.fills[0].timestamp == t + timedelta(minutes=1)


def test_idempotent_client_order_id(world, session_info):
    src, new = world
    b = new()
    t = at(session_info, 10)
    o1 = buy(b, t, 100, cid="same")
    o2 = buy(b, t, 100, cid="same")
    assert o1 is o2 and len(b.orders) == 1
    b.process(src, t + timedelta(minutes=3))
    assert b.positions["TST"].quantity == 100  # not 200


def test_rejections_do_not_touch_cash(world, session_info):
    _, new = world
    b = new(cash=500)
    t = at(session_info, 10)
    o = buy(b, t, 100)
    assert o.status == OrderStatus.REJECTED and "insufficient buying power" in o.reject_reason
    sell = b.submit(OrderRequest("s", "TST", Side.SELL, 10), t)
    assert sell.status == OrderStatus.REJECTED and "no shorting" in sell.reject_reason
    bad = b.submit(OrderRequest("z", "TST", Side.BUY, 0), t)
    assert bad.status == OrderStatus.REJECTED
    lim = b.submit(OrderRequest("l", "TST", Side.BUY, 1, OrderType.LIMIT), t)
    assert lim.status == OrderStatus.REJECTED
    assert b.cash == 500


def test_pending_buys_reserve_buying_power(world, session_info):
    _, new = world
    b = new(cash=1500)
    t = at(session_info, 10)
    assert buy(b, t, 100, cid="a").status == OrderStatus.PENDING
    second = buy(b, t, 100, cid="b")
    assert second.status == OrderStatus.REJECTED  # only ~$500 left unreserved


def test_partial_fills_accumulate_across_bars(session_info):
    bars = build_day(session_info, 10.0, [], base_volume=1_000, wiggle=0.0)
    src = make_source(session_info, {"TST": (meta(), bars)}, spread_bps=0)
    b = PaperBroker(
        100_000,
        CostConfig(
            slippage_bps=0,
            assumed_spread_bps=0,
            impact_bps_per_pct_participation=0,
            latency_seconds=0,
            max_participation=0.10,
            min_fill_volume=100,
        ),
    )
    b.mark("TST", 10.0)
    t = at(session_info, 10)
    o = buy(b, t, 250)
    b.process(src, t + timedelta(minutes=1))
    assert o.status == OrderStatus.PARTIALLY_FILLED and o.filled_quantity == 100
    b.process(src, t + timedelta(minutes=3))
    assert o.status == OrderStatus.FILLED and o.filled_quantity == 250
    assert len(b.fills) == 3 and b.positions["TST"].quantity == 250


def test_ioc_cancels_unfilled_remainder(session_info):
    bars = build_day(session_info, 10.0, [], base_volume=1_000, wiggle=0.0)
    src = make_source(session_info, {"TST": (meta(), bars)}, spread_bps=0)
    b = PaperBroker(
        100_000, CostConfig(max_participation=0.10, latency_seconds=0, slippage_bps=0, assumed_spread_bps=0)
    )
    b.mark("TST", 10.0)
    t = at(session_info, 10)
    o = b.submit(OrderRequest("i", "TST", Side.BUY, 500, time_in_force=TimeInForce.IOC), t)
    b.process(src, t + timedelta(minutes=5))
    assert o.status == OrderStatus.CANCELED and o.filled_quantity == 100


def test_halt_blocks_fills_until_resume(session_info):
    bars = build_day(session_info, 10.0, [], base_volume=100_000, wiggle=0.0)
    src = make_source(
        session_info,
        {"TST": (meta(), bars)},
        spread_bps=0,
        halts={"TST": [(at(session_info, 10), at(session_info, 15))]},
    )
    b = PaperBroker(100_000, FREE)
    b.mark("TST", 10.0)
    o = buy(b, at(session_info, 10))
    b.process(src, at(session_info, 14))
    assert o.filled_quantity == 0, "no fills while halted"
    b.process(src, at(session_info, 17))
    assert o.status == OrderStatus.FILLED and b.fills[0].timestamp >= at(session_info, 15)


def test_day_order_expires_unfilled_and_is_reported(session_info):
    bars = build_day(session_info, 10.0, [], base_volume=100_000, wiggle=0.0)
    src = make_source(session_info, {"TST": (meta(), bars)}, spread_bps=0)
    b = PaperBroker(100_000, FREE)
    b.mark("TST", 10.0)
    t = at(session_info, 10)
    o = b.submit(
        OrderRequest("lim", "TST", Side.BUY, 10, OrderType.LIMIT, limit_price=5.0),
        t,
        expires_at=at(session_info, 30),
    )
    b.process(src, at(session_info, 31))
    assert o.status == OrderStatus.EXPIRED
    assert any(e["kind"] == "order_expired" for e in b.drain_events())


def test_cancel_pending_order(world, session_info):
    _, new = world
    b = new()
    o = buy(b, at(session_info, 10))
    assert b.cancel(o.id) and o.status == OrderStatus.CANCELED
    assert not b.cancel(o.id)  # already terminal
    assert b.buying_power() == pytest.approx(100_000)


def test_commissions_reduce_pnl(session_info):
    bars = build_day(session_info, 10.0, [], base_volume=100_000, wiggle=0.0)
    src = make_source(session_info, {"TST": (meta(), bars)}, spread_bps=0)
    b = PaperBroker(
        100_000,
        CostConfig(
            commission_per_share=0.01,
            slippage_bps=0,
            assumed_spread_bps=0,
            impact_bps_per_pct_participation=0,
            latency_seconds=0,
            max_participation=1.0,
        ),
    )
    b.mark("TST", 10.0)
    t = at(session_info, 10)
    buy(b, t, 1000)
    b.process(src, t + timedelta(minutes=2))
    b.submit(OrderRequest("s", "TST", Side.SELL, 1000), t + timedelta(minutes=3))
    b.process(src, t + timedelta(minutes=6))
    tr = b.trades[0]
    assert (
        tr.gross_pnl == pytest.approx(0)
        and tr.commissions == pytest.approx(20)
        and tr.net_pnl == pytest.approx(-20)
    )
    assert b.cash == pytest.approx(100_000 - 20)


def test_equity_equals_cash_plus_marked_positions(world, session_info):
    src, new = world
    b = new()
    t = at(session_info, 10)
    buy(b, t, 100)
    b.process(src, t + timedelta(minutes=2))
    b.mark("TST", 11.0)
    assert b.equity() == pytest.approx(99_000 + 1_100)
    assert b.unrealized_pnl() == pytest.approx(100)


def test_force_flatten_closes_with_costs_and_logs_trade(world, session_info):
    src, new = world
    b = new(
        CostConfig(
            slippage_bps=10,
            assumed_spread_bps=0,
            impact_bps_per_pct_participation=0,
            latency_seconds=0,
            max_participation=1.0,
        )
    )
    t = at(session_info, 10)
    buy(b, t, 100)
    b.process(src, t + timedelta(minutes=2))
    closed = b.force_flatten(t + timedelta(minutes=3), "forced")
    assert closed == ["TST"] and not b.positions and b.trades[-1].exit_reason == "forced"
    assert b.trades[-1].exit_price < 10.0  # paid adverse costs


def test_reconcile_holdings_report():
    rep = reconcile_holdings(
        [Holding("A", 10), Holding("B", 5), Holding("C", 1)],
        [Holding("A", 10), Holding("B", 4), Holding("D", 2)],
    )
    assert not rep.ok and rep.matched == ["A"]
    assert (
        rep.quantity_mismatches == [("B", 5, 4)]
        and rep.missing_externally == ["C"]
        and rep.missing_internally == ["D"]
    )
    assert reconcile_holdings([Holding("A", 1)], [Holding("A", 1)]).ok


def test_paper_adapter_interface(world, session_info):
    src, new = world
    b = new()
    assert b.is_real_money is False and b.check_session().logged_in
    b.clock = lambda: at(session_info, 10)
    prepared = b.prepare_order(OrderRequest("x1", "TST", Side.BUY, 10))
    ext = b.submit_order(prepared)
    assert ext.status == OrderStatus.PENDING
    assert b.submit_order(prepared).external_id == ext.external_id  # idempotent through the adapter too
    assert b.cancel_order(ext.external_id)
    assert b.get_order_status(ext.external_id).status == OrderStatus.CANCELED
    assert b.get_buying_power() == pytest.approx(100_000)
