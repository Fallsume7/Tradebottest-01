"""Forecast framework: features (no look-ahead), calibration honesty, plan maths, walk-forward evaluation."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from mwbot.config import CostConfig, ModeProfile, TradingMode, preset_profile
from mwbot.data.replay import SymbolDay
from mwbot.forecast.calibration import (
    MIN_SAMPLES_FOR_CALIBRATION,
    BinCalibrator,
    auc_score,
    calibration_report,
)
from mwbot.forecast.costs import round_trip_cost_pct
from mwbot.forecast.dataset import build_day_samples
from mwbot.forecast.evaluation import default_models, finalise_model, walk_forward_evaluate
from mwbot.forecast.features import compute_features
from mwbot.forecast.models import HeuristicForecaster
from mwbot.forecast.plan import make_trade_plan, p_touch, usable_horizon
from mwbot.scanner.scanner import Scanner

from .conftest import at, build_day, make_source, meta


def _ctx(session, bars, minute):
    src = make_source(session, {"TST": (meta(), bars)})
    return src, src.contexts_at(at(session, minute))[0]


def _uptrend(session):
    return build_day(session, 10.0, [(30, 10.0, 60_000), (60, 10.6, 90_000)], base_volume=60_000)


# ------------------------------------------------------------------ features
def test_features_use_only_completed_bars(session_info):
    bars = _uptrend(session_info)
    src, ctx = _ctx(session_info, bars, 60)
    f = compute_features(ctx)
    assert f is not None and f.minutes_to_close == pytest.approx(330, abs=1.5)
    # Scramble every bar from the as-of time onward: the features must not move at all.
    t = at(session_info, 60)
    future = bars.index >= t
    scrambled = bars.copy()
    scrambled.loc[future, ["open", "high", "low", "close"]] *= 3.7
    scrambled.loc[future, "volume"] *= 11
    _, ctx2 = _ctx(session_info, scrambled, 60)
    f2 = compute_features(ctx2)
    assert f2 is not None and np.allclose(f.vector(), f2.vector(), equal_nan=True)


def test_features_need_history():
    # nothing to say before there are enough bars - never fabricated
    from mwbot.calendar import session_for

    from .conftest import TEST_DAY

    s = session_for(TEST_DAY)
    assert s is not None
    _, ctx = _ctx(s, _uptrend(s), -325)  # five premarket bars
    assert compute_features(ctx) is None


# ------------------------------------------------------------------ heuristic forecaster
def test_heuristic_is_never_labelled_calibrated_or_confident(session_info):
    _, ctx = _ctx(session_info, _uptrend(session_info), 75)
    f = compute_features(ctx)
    assert f is not None
    fc = HeuristicForecaster().predict(f, 45.0, 0.2)
    assert not fc.calibrated and fc.confidence is None and "heuristic" in fc.label
    assert fc.downside_pct < fc.expected_return_pct < fc.upside_pct
    assert fc.expected_net_return_pct == pytest.approx(fc.expected_return_pct - 0.2)
    assert 0.0 < fc.p_positive < 1.0 and fc.sigma_pct > 0


def test_heuristic_edge_is_modest_relative_to_uncertainty(session_info):
    """Even a perfect-looking trend may shift the mean by only a fraction of one standard deviation."""
    _, ctx = _ctx(session_info, _uptrend(session_info), 75)
    f = compute_features(ctx)
    assert f is not None
    fc = HeuristicForecaster().predict(f, 45.0, 0.0)
    assert abs(fc.expected_return_pct) <= 0.35 * fc.sigma_pct + 1e-9


# ------------------------------------------------------------------ calibration
def test_calibration_requires_enough_data_and_discrimination():
    rng = np.random.default_rng(1)
    n = 2000
    p = rng.uniform(0.3, 0.7, n)
    y = (rng.uniform(size=n) < p).astype(float)  # genuinely calibrated AND informative
    good = calibration_report(p, y)
    assert good.passes and good.ece is not None and good.ece < 0.08

    # a constant probability equal to the base rate is calibrated but useless -> must NOT pass
    flat = calibration_report(np.full(n, y.mean()), y)
    assert not flat.passes

    # too few samples never passes, however good it looks
    small = calibration_report(p[: MIN_SAMPLES_FOR_CALIBRATION - 1], y[: MIN_SAMPLES_FOR_CALIBRATION - 1])
    assert not small.passes

    # confident but wrong -> not calibrated
    bad = calibration_report(np.where(y > 0, 0.1, 0.9), y)
    assert not bad.passes


def test_bin_calibrator_is_monotone_and_bounded():
    rng = np.random.default_rng(2)
    p = rng.uniform(0, 1, 800)
    y = (rng.uniform(size=800) < p**2).astype(float)
    cal = BinCalibrator().fit(p, y)
    xs = np.linspace(0, 1, 50)
    out = np.array([cal(float(x)) for x in xs])
    assert cal.fitted and np.all(np.diff(out) >= -1e-12) and out.min() >= 0 and out.max() <= 1


def test_auc_is_one_half_for_noise():
    rng = np.random.default_rng(3)
    a = auc_score(rng.uniform(size=4000), (rng.uniform(size=4000) < 0.5).astype(float))
    assert a is not None and 0.45 < a < 0.55


# ------------------------------------------------------------------ costs and plan maths
def test_costs_grow_with_spread():
    c = CostConfig()
    assert round_trip_cost_pct(c, 0.5, 10.0) > round_trip_cost_pct(c, 0.1, 10.0) > 0


def test_p_touch_is_probability_and_monotone_in_target():
    vals = [p_touch(t, 0.1, 1.0) for t in (0.2, 0.5, 1.0, 2.0, 4.0)]
    assert all(0 <= v <= 1 for v in vals) and vals == sorted(vals, reverse=True)
    assert p_touch(1.0, 0.0, 1.0) == pytest.approx(
        2 * (1 - 0.8413), abs=1e-3
    )  # reflection principle: twice P(end above)
    assert p_touch(-1.0, 0.0, 1.0) == 1.0


def test_horizon_shrinks_toward_the_close():
    prof = preset_profile(TradingMode.MAX_EXPECTED_RETURN)
    h_morning, h_noon, h_late = (usable_horizon(prof, m) for m in (375, 180, 45))
    assert h_morning >= h_noon > h_late >= 0
    assert usable_horizon(prof, prof.plan.end_buffer_minutes - 1) == 0.0  # nothing usable inside the buffer


def test_plan_target_stop_and_deadline_are_consistent(session_info):
    _, ctx = _ctx(session_info, _uptrend(session_info), 75)
    f = compute_features(ctx)
    assert f is not None
    prof: ModeProfile = preset_profile(TradingMode.MAX_EXPECTED_RETURN)
    fc = HeuristicForecaster().predict(f, 45.0, 0.25)
    plan = make_trade_plan(f, fc, prof)
    assert plan.stop_price < plan.entry_price < plan.target_price
    assert plan.target_return_pct >= 1.3 * fc.cost_pct  # a target must clear costs
    assert 0 <= plan.p_target <= 1
    assert plan.max_hold_minutes <= f.minutes_to_close - prof.plan.end_buffer_minutes + 1e-6
    assert plan.notes  # always explains why the target/stop are where they are


# ------------------------------------------------------------------ walk-forward evaluation
@pytest.fixture(scope="module")
def samples_by_day(sample_provider):
    scanner = Scanner()
    out = {}
    for d in sample_provider.available_dates()[:6]:
        src = sample_provider.open_session(d)
        out[d] = build_day_samples(src, CostConfig(), scanner, None, stride_min=20)
    return out


def test_samples_have_forward_returns_from_after_the_decision(samples_by_day):
    n = sum(len(v) for v in samples_by_day.values())
    assert n > 100
    s = next(iter(samples_by_day.values()))[0]
    assert s.horizon >= 10 and s.sigma_h > 0 and np.isfinite(s.y_gross_pct)


def test_walk_forward_trains_only_on_earlier_days_and_is_honest(samples_by_day):
    ev = walk_forward_evaluate(samples_by_day, min_train_days=3)
    assert ev["ok"] and ev["n_test_samples"] > 0
    assert all(d > sorted(samples_by_day)[2].isoformat() for d in ev["test_days"])  # nothing before day 4
    # on a tiny synthetic sample nothing can be declared calibrated
    assert all(not m["calibrated"] for m in ev["models"].values())
    assert any("indicative" in w for w in ev["warnings"])
    assert "momentum_top_k" in ev["baselines"] or ev["baselines"]


def test_walk_forward_refuses_without_enough_days(samples_by_day):
    few = dict(list(samples_by_day.items())[:2])
    ev = walk_forward_evaluate(few, min_train_days=3)
    assert ev["ok"] is False and "at least" in ev["reason"]


def test_finalised_model_is_not_calibrated_unless_the_evaluation_passed(samples_by_day):
    ev = walk_forward_evaluate(samples_by_day, min_train_days=3)
    model = finalise_model(samples_by_day, ev, "ridge-v1")
    if model is not None:
        assert model.calibrated is False
        sample = next(iter(samples_by_day.values()))[0]
        fc = model.predict(sample.features, sample.horizon, sample.cost_pct)
        assert not fc.calibrated and fc.confidence is None
    assert "ridge-v1" in default_models()


def test_symbolday_arrays_expose_ns_index(session_info):
    """Guards the pandas-3 datetime-unit pitfall the dataset builder relies on."""
    sd = SymbolDay(meta=meta(), bars=_uptrend(session_info), halts=[])
    ts = sd.index_ns()
    assert ts.dtype == np.int64 and np.all(np.diff(ts) == int(timedelta(minutes=1).total_seconds() * 1e9))
