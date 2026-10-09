"""Backtests of the predictive strategy: eligibility handling, honesty labels, exit statistics, mode comparison."""

from __future__ import annotations

import pytest

from mwbot.backtest.engine import (
    BacktestConfig,
    compare_modes,
    exit_breakdown,
    forecast_accuracy,
    run_backtest,
)
from mwbot.calendar import session_for
from mwbot.config import TradingMode
from mwbot.data.base import DataUnavailableError
from mwbot.eligibility.models import EligibilityRecord, EligibilityStatus
from mwbot.eligibility.universe import EligibilityUniverse
from mwbot.forecast.history import HistoryPatterns
from mwbot.forecast.jobs import evaluate_models

from .conftest import TEST_DAY, build_day, make_source, meta
from .test_backtest import OneDayProvider

MER, AGG = TradingMode.MAX_EXPECTED_RETURN, TradingMode.AGGRESSIVE_UPSIDE


@pytest.fixture(scope="module")
def dates(sample_provider):
    return sample_provider.available_dates()[:4]


@pytest.fixture(scope="module")
def result(sample_provider, dates):
    return run_backtest(sample_provider, BacktestConfig(name="p", dates=dates))


def test_default_backtest_is_the_predictive_strategy_with_eligibility_applied(result):
    assert result.config["strategies"] == ["expected_opportunity"]
    el = result.summary["eligibility"]
    assert el["applied"] and el["mode"] == "point_in_time" and el["n_tickers"] > 0
    assert all(
        d.n_eligible_confirmed is not None and d.n_eligible_confirmed <= d.n_symbols for d in result.days
    )
    assert result.summary["forecaster"]["calibrated"] is False  # never claimed
    assert any("SYNTHETIC" in w for w in result.warnings)


def test_no_eligibility_history_means_refusal_not_a_silent_free_for_all():
    sess = session_for(TEST_DAY)
    src = make_source(sess, {"TST": (meta(), build_day(sess, 10.0, [(400, 10.0, 50_000)]))})
    with pytest.raises(DataUnavailableError, match="eligibility"):
        run_backtest(OneDayProvider(src), BacktestConfig(name="x"))


def test_explicitly_disabling_eligibility_is_allowed_but_loudly_labelled():
    sess = session_for(TEST_DAY)
    src = make_source(sess, {"TST": (meta(), build_day(sess, 10.0, [(400, 10.0, 50_000)]))})
    r = run_backtest(OneDayProvider(src), BacktestConfig(name="x", eligibility="none"))
    assert r.summary["eligibility"]["applied"] is False
    assert "NOT applied" in r.warnings[0]
    assert r.days[0].n_eligible_confirmed is None


def test_a_supplied_universe_excluding_everything_trades_nothing(sample_provider, dates):
    nothing = EligibilityUniverse([EligibilityRecord("ZZZZ", EligibilityStatus.CONFIRMED_TRADABLE)])
    r = run_backtest(sample_provider, BacktestConfig(name="x", dates=dates), universe=nothing)
    assert r.summary["n_trades"] == 0 and all(d.n_eligible_confirmed == 0 for d in r.days)


def test_traded_symbols_were_confirmed_tradable_on_the_trade_date(sample_provider, dates):
    recs = list(sample_provider.eligibility_records())
    uni = EligibilityUniverse(recs)
    r = run_backtest(sample_provider, BacktestConfig(name="x", dates=dates), universe=uni)
    from datetime import date

    for t in r.trades:
        rec = uni.record_on(t.symbol, date.fromisoformat(t.session_date))
        assert rec.status == EligibilityStatus.CONFIRMED_TRADABLE, (t.symbol, t.session_date)


def test_backtest_is_reproducible(sample_provider, dates, result):
    again = run_backtest(sample_provider, BacktestConfig(name="p", dates=dates))
    assert again.result_hash == result.result_hash


def test_exit_breakdown_adds_up(result):
    ex = result.summary["exits"]
    assert sum(ex["counts"].values()) == result.summary["n_trades"]
    if result.summary["n_trades"]:
        assert sum(ex["share"].values()) == pytest.approx(1.0)
        assert 0 <= ex["target_hit_rate"] <= 1 and 0 <= ex["stop_rate"] <= 1
    assert exit_breakdown([])["target_hit_rate"] is None


def test_forecast_accuracy_is_reported_with_caveats(result):
    fa = result.summary["forecast_accuracy"]
    if fa["n"]:
        assert fa["calibrated"] is False and "NOT calibrated" in fa["note"]
        assert fa["mean_abs_error_pct"] >= 0
    else:
        assert forecast_accuracy([])["n"] == 0


def test_statistical_forecaster_never_trains_on_the_day_it_trades(sample_provider, dates):
    r = run_backtest(sample_provider, BacktestConfig(name="s", dates=dates, forecaster="statistical"))
    per_day = r.summary["forecaster"]["per_day"]
    assert len(per_day) == len(dates)
    assert "heuristic" in per_day[0] and "fewer than" in per_day[0]  # nothing earlier to learn from yet
    for line in per_day[1:]:
        if "trained through" in line:
            day, rest = line.split(":", 1)
            assert rest.split("trained through ")[1].split(",")[0] < day  # strictly earlier sessions


def test_history_patterns_only_know_earlier_sessions(sample_provider, dates):
    h = HistoryPatterns()
    src0 = sample_provider.open_session(dates[0])
    ctx = src0.contexts_at(src0.session.open_utc + __import__("datetime").timedelta(minutes=90))[0]
    assert h.lookup(ctx) == (None, 0)  # nothing learned yet
    h.add_session(src0)
    src1 = sample_provider.open_session(dates[1])
    ctx1 = next(
        c
        for c in src1.contexts_at(src1.session.open_utc + __import__("datetime").timedelta(minutes=90))
        if c.symbol == ctx.symbol
    )
    mean, n = h.lookup(ctx1)
    assert n == 1 and mean is not None
    assert h.lookup(ctx) == (mean, 1)  # same slot, still just the one earlier session


# ------------------------------------------------------------------ mode comparison
@pytest.fixture(scope="module")
def comparison(sample_provider, dates):
    return compare_modes(sample_provider, BacktestConfig(name="c", dates=dates))


def test_compare_modes_uses_identical_data_and_includes_baselines(comparison):
    rows = {r["mode"]: r for r in comparison["rows"]}
    assert set(rows) == {"max_expected_return", "aggressive_upside", "baseline_momentum", "benchmark"}
    sd = comparison["same_data"]
    assert sd["n_days"] == 4 and sd["eligibility"]["applied"]
    for m in ("max_expected_return", "aggressive_upside"):
        r = rows[m]
        for k in (
            "total_return_pct",
            "max_drawdown_pct",
            "win_rate",
            "avg_return_pct",
            "target_hit_rate",
            "n_trades",
        ):
            assert k in r
    assert (
        comparison["equity_curves"]["max_expected_return"]
        and comparison["equity_curves"]["aggressive_upside"]
    )


def test_comparison_verdict_is_honest_about_small_samples(comparison):
    rows = {r["mode"]: r for r in comparison["rows"]}
    few = min(rows["max_expected_return"]["n_trades"], rows["aggressive_upside"]["n_trades"])
    if few < 30:
        assert "Too few trades" in comparison["verdict"]
    assert "guarantee" not in comparison["verdict"].lower().replace("does not", "")
    assert any("SYNTHETIC" in w for w in comparison["warnings"])


def test_modes_use_their_own_risk_preset(sample_provider, dates):
    from mwbot.config import preset_profile

    cfg = BacktestConfig(name="m", dates=dates[:1], mode=AGG)
    assert cfg.to_settings().risk.max_position_pct == preset_profile(AGG).risk.max_position_pct
    assert (
        BacktestConfig(name="m").to_settings().risk.max_position_pct
        == preset_profile(MER).risk.max_position_pct
    )


# ------------------------------------------------------------------ model check
def test_model_check_reports_baselines_and_never_over_claims(sample_provider):
    from mwbot.config import AppSettings

    s = AppSettings()
    uni = EligibilityUniverse(list(sample_provider.eligibility_records()))
    ev, model = evaluate_models(sample_provider, s, uni, max_days=6)
    assert ev["ok"] and ev["data"]["eligibility_applied"] and "Synthetic" in ev["data"]["note"]
    assert all(
        not m["calibrated"] for m in ev["models"].values()
    )  # only 2-3 test sessions: cannot be calibrated
    assert "random_pick" in ev["baselines"]
    if model is not None:
        assert not model.calibrated
