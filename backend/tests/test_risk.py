from datetime import UTC, datetime, timedelta

import pytest

from mwbot.calendar import Phase
from mwbot.config import RiskConfig
from mwbot.domain import Position
from mwbot.trading.risk import BotState, DayStats, RiskManager, hard_exit_reason, size_position

NOW = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)


def pos(**kw) -> Position:
    base = dict(
        symbol="TST",
        quantity=100,
        avg_cost=10.0,
        opened_at=NOW - timedelta(minutes=10),
        stop_price=9.5,
        take_profit_price=11.0,
        trailing_stop_pct=None,
        high_water_mark=10.0,
        max_hold_minutes=None,
    )
    base.update(kw)
    return Position(**base)


# ------------------------------------------------------------- sizing
def test_size_is_min_of_risk_allocation_and_cash():
    cfg = RiskConfig(risk_per_trade_pct=0.5, max_position_pct=10)
    # risk: 100k*0.5% = $500 / ($10*3% = $0.30) = 1666; allocation: 10k/10 = 1000; cash plentiful
    q, br = size_position(100_000, 10.0, 3.0, cfg, 100_000)
    assert q == 1000 and br["allocation"] == 1000 and br["risk"] == 1666
    # tighter stop -> risk-based size grows, allocation still caps
    q2, _ = size_position(100_000, 10.0, 0.5, cfg, 100_000)
    assert q2 == 1000
    # wide stop -> risk binds
    q3, br3 = size_position(100_000, 10.0, 10.0, cfg, 100_000)
    assert q3 == 500 == br3["risk"]
    # limited cash binds (with 1% buffer)
    q4, br4 = size_position(100_000, 10.0, 3.0, cfg, 2_000)
    assert q4 == br4["cash"] == 198


@pytest.mark.parametrize("price,stop,eq", [(0, 3, 1e5), (10, 0, 1e5), (10, 3, 0)])
def test_size_zero_for_invalid_inputs(price, stop, eq):
    assert size_position(eq, price, stop, RiskConfig(), 1e5)[0] == 0


# ------------------------------------------------------------- exits
def test_stop_loss_uses_bar_low():
    assert "Stop loss" in hard_exit_reason(pos(), 10.1, 9.49, 9.6, NOW)
    assert hard_exit_reason(pos(), 10.1, 9.51, 9.6, NOW) is None


def test_take_profit_uses_bar_high():
    assert "Profit target" in hard_exit_reason(pos(), 11.01, 10.5, 10.9, NOW)


def test_trailing_stop_follows_high_water_mark():
    p = pos(stop_price=None, take_profit_price=None, trailing_stop_pct=2.0, high_water_mark=12.0)
    assert "Trailing stop" in hard_exit_reason(p, 11.9, 11.7, 11.8, NOW)  # trail = 11.76
    assert hard_exit_reason(p, 11.9, 11.8, 11.85, NOW) is None
    flat = pos(stop_price=None, take_profit_price=None, trailing_stop_pct=2.0, high_water_mark=10.0)
    assert hard_exit_reason(flat, 10, 9.7, 9.8, NOW) is None  # trailing arms only once in profit


def test_time_stop():
    p = pos(stop_price=None, take_profit_price=None, max_hold_minutes=10)
    assert "Time stop" in hard_exit_reason(p, 10, 10, 10, NOW)
    assert (
        hard_exit_reason(pos(stop_price=None, take_profit_price=None, max_hold_minutes=30), 10, 10, 10, NOW)
        is None
    )


def test_stop_checked_before_target_when_both_hit():
    assert "Stop loss" in hard_exit_reason(
        pos(), 11.5, 9.0, 10.0, NOW
    )  # wide bar touching both: assume the worst


# ------------------------------------------------------------- gate
def gate(rm: RiskManager, **over):
    args = dict(
        state=BotState.RUNNING,
        phase=Phase.TRADING,
        symbol="NEW",
        strategy="s",
        price=10.0,
        stop_loss_pct=3.0,
        equity=100_000.0,
        buying_power=100_000.0,
        positions={},
        pending_buy_symbols=set(),
        stats=DayStats(100_000.0),
        feed_age_seconds=1.0,
        feed_allows_trading=True,
    )
    args.update(over)
    return rm.check_entry(**args)


def test_gate_approves_clean_entry():
    d = gate(RiskManager(RiskConfig()))
    assert d.approved and d.quantity > 0


@pytest.mark.parametrize(
    "over,fragment",
    [
        ({"state": BotState.PAUSED}, "paused"),
        ({"state": BotState.EMERGENCY_STOP}, "emergency_stop"),
        ({"phase": Phase.NO_NEW_ENTRIES}, "entries only allowed"),
        ({"phase": Phase.OBSERVING}, "entries only allowed"),
        ({"phase": Phase.LIQUIDATING}, "entries only allowed"),
        ({"feed_age_seconds": 999.0}, "stale"),
        ({"feed_allows_trading": False, "feed_block_reason": "feed is DELAYED"}, "DELAYED"),
        ({"equity": 96_000.0}, "daily loss limit"),
        ({"positions": {"NEW": pos(symbol="NEW")}}, "already holding"),
        ({"pending_buy_symbols": {"NEW"}}, "already holding"),
        ({"price": 0.0}, "invalid price"),
    ],
)
def test_gate_rejections(over, fragment):
    d = gate(RiskManager(RiskConfig(max_daily_loss_pct=3.0)), **over)
    assert not d.approved and any(fragment in r for r in d.reasons), d.reasons


def test_gate_max_positions_and_trades_per_day_and_reentry():
    rm = RiskManager(RiskConfig(max_positions=2, max_trades_per_day=3))
    held = {"A": pos(symbol="A"), "B": pos(symbol="B")}
    assert any("max simultaneous" in r for r in gate(rm, positions=held).reasons)
    # pending buys count toward the cap
    assert not gate(rm, positions={"A": pos(symbol="A")}, pending_buy_symbols={"Z"}).approved
    st = DayStats(100_000.0, trades_opened=3)
    assert any("max trades per day" in r for r in gate(rm, stats=st).reasons)
    st2 = DayStats(100_000.0, stopped_symbols={"NEW"})
    assert any("re-entry" in r for r in gate(rm, stats=st2).reasons)
    st3 = DayStats(100_000.0, entered_keys={("s", "NEW")})
    assert any("already traded" in r for r in gate(rm, stats=st3).reasons)


def test_daily_loss_limit_boundary():
    rm = RiskManager(RiskConfig(max_daily_loss_pct=3.0))
    assert not rm.daily_loss_breached(97_001, 100_000)
    assert rm.daily_loss_breached(97_000, 100_000)


# ------------------------------------------------------------- capital budget
def _entry(
    rm: RiskManager, *, price: float = 10.0, equity: float = 100_000, cash: float = 100_000, deployed=0.0
):
    return rm.check_entry(
        state=BotState.RUNNING,
        phase=Phase.TRADING,
        symbol="NEW",
        strategy="s",
        price=price,
        stop_loss_pct=3.0,
        equity=equity,
        buying_power=cash,
        positions={},
        pending_buy_symbols=set(),
        stats=DayStats(start_equity=equity),
        feed_age_seconds=1.0,
        feed_allows_trading=True,
        deployed_value=deployed,
    )


def test_no_budget_means_whole_account():
    d = _entry(RiskManager(RiskConfig(max_position_pct=10)))
    assert d.approved and d.quantity == 1000  # 10% of $100k at $10


def test_budget_scales_position_size_down():
    d = _entry(RiskManager(RiskConfig(max_position_pct=10, capital_budget=20_000)))
    assert d.approved and d.quantity == 200  # 10% of the $20k budget, not of the $100k account


def test_budget_limits_total_deployed_money():
    rm = RiskManager(RiskConfig(max_position_pct=50, capital_budget=10_000))
    d = _entry(rm, deployed=8_000)
    assert d.approved and d.quantity <= 198  # only $2,000 (less the cash buffer) of room left
    assert d.quantity * 10.0 <= 2_000


def test_budget_fully_used_blocks_entries_with_a_clear_reason():
    d = _entry(RiskManager(RiskConfig(capital_budget=10_000)), deployed=10_000)
    assert not d.approved and "capital budget" in d.reasons[0]
    d2 = _entry(RiskManager(RiskConfig(capital_budget=10_000)), deployed=9_995, price=10.0)
    assert not d2.approved  # less than one share of room


def test_budget_larger_than_account_changes_nothing():
    a = _entry(RiskManager(RiskConfig(max_position_pct=10, capital_budget=1_000_000)))
    b = _entry(RiskManager(RiskConfig(max_position_pct=10)))
    assert a.quantity == b.quantity
