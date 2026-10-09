import csv
import io
from datetime import date, timedelta

import pytest

from mwbot.backtest.engine import (
    BacktestConfig,
    compare_strategies,
    run_backtest,
    summary_to_csv,
    trades_to_csv,
)
from mwbot.backtest.metrics import (
    compound,
    daily_ratio_stats,
    max_drawdown,
    profit_factor,
    trade_stats,
)
from mwbot.calendar import build_schedule, session_for
from mwbot.config import CostConfig, ScheduleConfig
from mwbot.data.base import DataUnavailableError, MarketDataProvider
from mwbot.data.massive import MassiveProvider
from mwbot.domain import FeedInfo, FeedKind, Trade

from .conftest import TEST_DAY, at, make_source, meta
from .scenarios import runner_day


# ------------------------------------------------------------------ metrics maths
def _trade(pnl, ret=1.0, strat="s", day="2026-09-14", hold=10.0):
    t0 = at(session_for(TEST_DAY), 10)
    return Trade(
        "T",
        "X",
        strat,
        100,
        t0,
        10.0,
        t0 + timedelta(minutes=hold),
        10.0 + pnl / 100,
        pnl,
        0.0,
        pnl,
        ret,
        hold,
        "",
        "",
        day,
    )


def test_max_drawdown_known_series():
    dd, dd_abs = max_drawdown([100, 120, 90, 110, 80, 130])
    assert dd == pytest.approx((120 - 80) / 120) and dd_abs == pytest.approx(40)
    assert max_drawdown([100]) == (0.0, 0.0) and max_drawdown([100, 101, 102])[0] == 0.0


def test_profit_factor_and_trade_stats():
    assert profit_factor([100, -50, 50]) == pytest.approx(3.0)
    assert profit_factor([10, 20]) == float("inf") and profit_factor([]) is None
    st = trade_stats([_trade(100), _trade(-50), _trade(50), _trade(-25)])
    assert st["n_trades"] == 4 and st["win_rate"] == 0.5 and st["net_pnl"] == 75
    assert st["avg_win"] == 75 and st["avg_loss"] == -37.5 and st["payoff_ratio"] == pytest.approx(2.0)
    assert st["profit_factor"] == pytest.approx(150 / 75) and st["expectancy"] == pytest.approx(18.75)
    empty = trade_stats([])
    assert empty["n_trades"] == 0 and empty["win_rate"] is None


def test_sharpe_is_withheld_for_short_samples_and_computed_for_long():
    short = daily_ratio_stats([1.0, -0.5, 0.7])
    assert short["sharpe"] is None and "not statistically meaningful" in short["ratios_note"]
    long = daily_ratio_stats([0.5, -0.2, 0.3, 0.1, -0.1] * 6)
    assert long["n_days"] == 30 and long["sharpe"] is not None and long["sharpe"] > 0
    assert daily_ratio_stats([])["n_days"] == 0


def test_compound_returns():
    assert compound([10, 10]) == pytest.approx(21.0) and compound([]) == 0.0


# ------------------------------------------------------------------ engine
def cfg(**kw):
    base = dict(name="t", strategies=["opening_momentum"], dates=None)
    base.update(kw)
    return BacktestConfig(**base)


@pytest.fixture(scope="module")
def three_days(sample_provider):
    return sample_provider.available_dates()[:3]


@pytest.fixture(scope="module")
def base_result(sample_provider, three_days):
    return run_backtest(sample_provider, cfg(dates=three_days, oos_fraction=0.34))


def test_backtest_is_reproducible(sample_provider, three_days, base_result):
    again = run_backtest(sample_provider, cfg(dates=three_days, oos_fraction=0.34))
    assert again.result_hash == base_result.result_hash
    assert [t.to_dict() for t in again.trades] == [t.to_dict() for t in base_result.trades]
    assert again.equity_curve == base_result.equity_curve


def test_changing_config_changes_hash(sample_provider, three_days, base_result):
    other = run_backtest(
        sample_provider, cfg(dates=three_days, oos_fraction=0.34, costs=CostConfig(slippage_bps=25))
    )
    assert other.result_hash != base_result.result_hash


def test_higher_costs_reduce_profit(sample_provider, three_days):
    cheap = run_backtest(
        sample_provider, cfg(dates=three_days, costs=CostConfig(slippage_bps=1, assumed_spread_bps=5))
    )
    dear = run_backtest(
        sample_provider,
        cfg(
            dates=three_days,
            costs=CostConfig(slippage_bps=60, assumed_spread_bps=120, commission_per_share=0.01),
        ),
    )
    assert cheap.summary["n_trades"] > 0
    assert dear.summary["net_pnl"] < cheap.summary["net_pnl"]


def test_entries_never_precede_observation_period_plus_one_bar(base_result):
    """Decision at >= open+observe, fill on a later bar: entry_time >= open + observe + 1 minute."""
    for t in base_result.trades:
        s = session_for(date.fromisoformat(t.session_date))
        sched = build_schedule(date.fromisoformat(t.session_date), ScheduleConfig())
        assert t.entry_time >= sched.entries_start + timedelta(minutes=1)
        assert t.exit_time > t.entry_time and t.exit_time <= s.close_utc


def test_result_structure_and_labels(base_result):
    s = base_result.summary
    assert base_result.feed["kind"] == "synthetic"
    assert any("SYNTHETIC" in w for w in base_result.warnings)
    assert any("Only" in w and "trades" in w for w in base_result.warnings) or s["n_trades"] >= 30
    assert any("overfitting" in w.lower() for w in base_result.warnings)
    assert s["sharpe"] is None and "not statistically meaningful" in s["ratios_note"]
    assert s["n_days"] == 3 and len(base_result.days) == 3
    assert s["final_equity"] == pytest.approx(base_result.days[-1].end_equity)
    assert sum(d.n_trades for d in base_result.days) == s["n_trades"]


def test_out_of_sample_split_is_chronological(sample_provider):
    res = run_backtest(sample_provider, cfg(dates=sample_provider.available_dates()[:5], oos_fraction=0.4))
    s = res.summary
    ins, oos = s["in_sample"], s["out_of_sample"]
    assert ins["n_days"] == 3 and oos["n_days"] == 2
    assert ins["n_trades"] + oos["n_trades"] == s["n_trades"]
    split_date = res.days[s["split_index"]].date
    in_dates = {t.session_date for t in res.trades if t.session_date < split_date}
    out_dates = {t.session_date for t in res.trades if t.session_date >= split_date}
    assert not (in_dates & out_dates) and (not in_dates or not out_dates or max(in_dates) < min(out_dates))


def test_no_fake_split_for_tiny_samples(base_result):
    assert "out_of_sample" not in base_result.summary  # 3 days: refuse to pretend there is an OOS test


def test_csv_exports(base_result):
    rows = list(csv.DictReader(io.StringIO(trades_to_csv(base_result.trades))))
    assert rows and len(rows) == len(base_result.trades)
    assert {"symbol", "strategy", "entry_price", "exit_price", "net_pnl", "exit_reason"} <= set(rows[0])
    summary = summary_to_csv(base_result)
    assert (
        summary.startswith("metric,value")
        and "total_return_pct" in summary
        and "benchmark_return_pct" in summary
    )


def test_compare_strategies_runs_each_plus_combined(sample_provider, three_days):
    res = compare_strategies(sample_provider, cfg(dates=three_days), ["opening_momentum", "gap_continuation"])
    assert len(res) == 3
    assert {tuple(r.config["strategies"]) for r in res} == {
        ("opening_momentum",),
        ("gap_continuation",),
        ("opening_momentum", "gap_continuation"),
    }
    assert len({r.id for r in res}) == 3


def test_backtest_requires_replayable_dataset():
    with pytest.raises(DataUnavailableError):
        run_backtest(MassiveProvider("k", transport=None), cfg())  # live provider has no dates


class OneDayProvider(MarketDataProvider):
    name = "scripted"

    def __init__(self, source):
        self.source = source

    def feed_info(self):
        return FeedInfo(FeedKind.HISTORICAL, "scripted", "HISTORICAL scripted")

    def available_dates(self):
        return [TEST_DAY]

    def open_session(self, d):
        return self.source


def test_unresolved_positions_are_force_closed_and_flagged():
    sess = session_for(TEST_DAY)
    sched = build_schedule(TEST_DAY, ScheduleConfig())
    halt = [(sched.liquidation_start - timedelta(minutes=2), sess.close_utc + timedelta(hours=1))]
    src = make_source(
        sess,
        {"TST": (meta(), runner_day(sess, [(20, 11.7, 28_000), (30, 11.85, 14_000)]))},
        halts={"TST": halt},
    )
    res = run_backtest(
        OneDayProvider(src),
        cfg(
            eligibility="none",  # hand-made provider: no eligibility history exists (and the result says so)
            strategy_params={
                "opening_momentum": {
                    "take_profit_pct": None,
                    "trailing_stop_pct": None,
                    "max_hold_minutes": None,
                    "stop_loss_pct": 20.0,
                }
            },
        ),
    )
    assert res.days[0].unresolved == ["TST"]
    assert res.summary["days_with_unresolved_positions"] == 1
    assert any("force-closed" in w for w in res.warnings)
    assert res.trades and "force-closed" in res.trades[-1].exit_reason
    assert res.feed["kind"] == "historical" and any("Survivorship" in w for w in res.warnings)


def test_carry_capital_false_resets_each_day(sample_provider, three_days):
    res = run_backtest(sample_provider, cfg(dates=three_days, carry_capital=False))
    assert all(d.start_equity == pytest.approx(100_000) for d in res.days)
