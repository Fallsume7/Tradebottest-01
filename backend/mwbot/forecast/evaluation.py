"""Chronological walk-forward evaluation of forecasters (no shuffling, no future data in training).

For test day *i* a statistical model is fitted on days ``< i - cal_days``; its raw probabilities are then
calibrated on the ``cal_days`` days just before *i*; the day-*i* forecasts are scored. Heuristic forecasts
need no fitting. Baselines (momentum, top-gainers, buy-everything) are scored on the same samples.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import date
from typing import Any

import numpy as np

from mwbot.forecast.calibration import BinCalibrator, calibration_report, spearman
from mwbot.forecast.dataset import Sample
from mwbot.forecast.models import (
    Forecaster,
    GradientBoostedForecaster,
    HeuristicForecaster,
    RidgeForecaster,
    norm_cdf,
    sklearn_available,
)

ModelFactory = Callable[[], Forecaster]

MIN_TEST_DAYS = 10  # distinct out-of-sample sessions required before any model may be called calibrated


def default_models() -> dict[str, ModelFactory]:
    models: dict[str, ModelFactory] = {"heuristic-v1": HeuristicForecaster, "ridge-v1": RidgeForecaster}
    if sklearn_available():
        models["gbm-v1"] = GradientBoostedForecaster
    return models


def _xy(samples: list[Sample]) -> tuple[np.ndarray, np.ndarray]:
    return np.vstack([s.features.vector() for s in samples]), np.asarray([s.y_z for s in samples])


def _raw_p(model: Forecaster, s: Sample) -> tuple[float, float, float]:
    """(raw p_positive without calibrator, expected net %, sigma %) for sample ``s``."""
    cal = getattr(model, "calibrator", None)
    was = getattr(model, "calibrated", False)
    if cal is not None:
        model.calibrator, model.calibrated = None, False  # type: ignore[attr-defined]
    fc = model.predict(s.features, s.horizon, s.cost_pct)
    if cal is not None:
        model.calibrator, model.calibrated = cal, was  # type: ignore[attr-defined]
    return fc.p_positive, fc.expected_net_return_pct, fc.sigma_pct


def fit_with_calibration(factory: ModelFactory, train: list[Sample], cal: list[Sample]) -> Forecaster | None:
    """Fit on ``train``; fit an isotonic probability map on ``cal`` (later days). ``None`` if it cannot be fitted."""
    model = factory()
    if isinstance(model, RidgeForecaster):
        if len(train) < (200 if isinstance(model, GradientBoostedForecaster) else 100):
            return None
        x, y = _xy(train)
        model.fit(x, y)
        if len(cal) >= 80:
            p = np.asarray([_raw_p(model, s)[0] for s in cal])
            out = np.asarray([1.0 if s.y_net_pct > 0 else 0.0 for s in cal])
            model.calibrator = BinCalibrator().fit(p, out)
            model.calibrated = False  # decided later, from OUT-OF-SAMPLE evidence only
    return model


def walk_forward_evaluate(
    samples_by_day: dict[date, list[Sample]],
    *,
    models: dict[str, ModelFactory] | None = None,
    min_train_days: int = 3,
    cal_days: int = 2,
    top_k: int = 3,
) -> dict[str, Any]:
    days = sorted(samples_by_day)
    models = models or default_models()
    warnings: list[str] = []
    if len(days) < min_train_days + 1:
        return {
            "ok": False,
            "reason": f"need at least {min_train_days + 1} sessions of data (have {len(days)})",
            "models": {},
            "baselines": {},
            "n_days": len(days),
            "test_days": [],
            "n_test_samples": 0,
            "warnings": warnings,
        }

    rows: dict[str, list[tuple[Sample, float, float, float, float]]] = defaultdict(
        list
    )  # model -> (sample, p_raw, p_cal, mu_net, sigma)
    test_days: list[str] = []
    for i, day in enumerate(days):
        if i < min_train_days:
            continue
        test = samples_by_day[day]
        if not test:
            continue
        test_days.append(day.isoformat())
        cal_start = max(0, i - cal_days)
        train = [s for d in days[:cal_start] for s in samples_by_day[d]]
        cal = [s for d in days[cal_start:i] for s in samples_by_day[d]]
        for name, factory in models.items():
            m = factory() if factory is HeuristicForecaster else fit_with_calibration(factory, train, cal)
            if m is None:
                continue
            for s in test:
                p_raw, mu_net, sig = _raw_p(m, s)
                cal_fn = getattr(m, "calibrator", None)
                p_cal = cal_fn(p_raw) if cal_fn is not None else p_raw
                rows[name].append((s, p_raw, p_cal, mu_net, sig))

    all_test = [s for d in days[min_train_days:] for s in samples_by_day[d]]
    out_models: dict[str, Any] = {}
    for name, r in rows.items():
        out_models[name] = _score(name, r, top_k, len(test_days))
    baselines = _baselines(all_test, top_k)
    n_test = len(all_test)
    if len(test_days) < MIN_TEST_DAYS:
        warnings.append(
            f"Only {len(test_days)} out-of-sample sessions (need {MIN_TEST_DAYS}+ to call any model calibrated): "
            "results are indicative, not conclusive."
        )
    if n_test < 300:
        warnings.append(f"Only {n_test} out-of-sample samples: results are indicative, not conclusive.")
    return {
        "ok": True,
        "n_days": len(days),
        "test_days": test_days,
        "n_test_samples": n_test,
        "horizon_minutes": float(np.median([s.horizon for s in all_test])) if all_test else None,
        "models": out_models,
        "baselines": baselines,
        "warnings": warnings,
    }


def _score(
    name: str, rows: list[tuple[Sample, float, float, float, float]], top_k: int, n_days: int
) -> dict[str, Any]:
    y = np.asarray([r[0].y_net_pct for r in rows])
    mu = np.asarray([r[3] for r in rows])
    pos = (y > 0).astype(float)
    cal_raw = calibration_report(np.asarray([r[1] for r in rows]), pos)
    cal_cal = calibration_report(np.asarray([r[2] for r in rows]), pos)
    # cross-sectional ranking quality per decision time
    by_t: dict[Any, list[tuple[float, float, Sample]]] = defaultdict(list)
    for s, _pr, _pc, m, _sg in rows:
        by_t[(s.day, s.t)].append((m, s.y_net_pct, s))
    ics, tops, alls = [], [], []
    for lst in by_t.values():
        if len(lst) < 8:
            continue
        pm = np.asarray([a for a, _b, _c in lst])
        rr = np.asarray([b for _a, b, _c in lst])
        ic = spearman(pm, rr)
        if ic is not None:
            ics.append(ic)
        order = np.argsort(-pm)[:top_k]
        tops.append(float(rr[order].mean()))
        alls.append(float(rr.mean()))
    use_cal = cal_cal if cal_cal.n and cal_cal.ece is not None and cal_cal.passes else cal_raw
    is_heuristic = name.startswith("heuristic")
    # Samples inside one session are strongly correlated, so a pile of them from a few days proves little:
    # a model can only be called calibrated after enough distinct out-of-sample SESSIONS.
    enough_days = n_days >= MIN_TEST_DAYS
    return {
        "n": len(rows),
        "mae_pct": float(np.mean(np.abs(mu - y))),
        "rmse_pct": float(np.sqrt(np.mean((mu - y) ** 2))),
        "bias_pct": float(np.mean(mu - y)),
        "mean_realized_net_pct": float(y.mean()),
        "share_profitable": float(pos.mean()),
        "rank_ic": float(np.mean(ics)) if ics else None,
        "rank_ic_samples": len(ics),
        "top_pick_net_pct": float(np.mean(tops)) if tops else None,
        "all_stocks_net_pct": float(np.mean(alls)) if alls else None,
        "top_pick_edge_pct": float(np.mean(tops) - np.mean(alls)) if tops else None,
        "top_k": top_k,
        "calibration_raw": cal_raw.to_dict(),
        "calibration_calibrated": cal_cal.to_dict(),
        # Hand-written heuristics are never *labelled* calibrated, however their probabilities score.
        "n_test_days": n_days,
        "calibrated": bool(use_cal.passes) and not is_heuristic and enough_days,
        "note": (
            "heuristic probabilities are never labelled calibrated"
            if is_heuristic
            else ("" if enough_days else f"needs >= {MIN_TEST_DAYS} out-of-sample sessions (have {n_days})")
        ),
        "verdict": _verdict(use_cal.passes and enough_days, np.mean(ics) if ics else None, len(rows), n_days),
    }


def _verdict(passes: bool, ic: float | None, n: int, n_days: int = MIN_TEST_DAYS) -> str:
    if n < 300 or n_days < MIN_TEST_DAYS:
        return (
            f"Too little out-of-sample data to judge ({n} samples over {n_days} sessions; "
            f"need {MIN_TEST_DAYS}+ sessions). Treat forecasts as heuristic estimates."
        )
    if passes and ic is not None and ic > 0.02:
        return "Probabilities passed the out-of-sample calibration check and rank stocks better than chance."
    if ic is not None and ic > 0.02:
        return "Ranks stocks somewhat better than chance, but the probabilities are NOT validated: do not read them as reliable."
    return "No demonstrated edge out-of-sample. Use as an explanation tool only."


def _baselines(samples: list[Sample], top_k: int) -> dict[str, Any]:
    by_t: dict[Any, list[Sample]] = defaultdict(list)
    for s in samples:
        by_t[(s.day, s.t)].append(s)
    res: dict[str, list[float]] = {
        "momentum_15m": [],
        "top_gainers_today": [],
        "buy_everything": [],
        "random_pick": [],
    }
    rng = np.random.default_rng(0)
    for lst in by_t.values():
        if len(lst) < 8:
            continue
        net = np.asarray([s.y_net_pct for s in lst])
        mom = np.asarray([s.features.ret_15m if s.features.ret_15m is not None else -9e9 for s in lst])
        gain = np.asarray(
            [s.features.pct_change_day if s.features.pct_change_day is not None else -9e9 for s in lst]
        )
        res["momentum_15m"].append(float(net[np.argsort(-mom)[:top_k]].mean()))
        res["top_gainers_today"].append(float(net[np.argsort(-gain)[:top_k]].mean()))
        res["buy_everything"].append(float(net.mean()))
        res["random_pick"].append(float(net[rng.permutation(len(net))[:top_k]].mean()))
    return {k: {"mean_net_pct": float(np.mean(v)) if v else None, "n_slices": len(v)} for k, v in res.items()}


def finalise_model(
    samples_by_day: dict[date, list[Sample]],
    evaluation: dict[str, Any],
    name: str = "ridge-v1",
    cal_days: int = 2,
) -> RidgeForecaster | None:
    """Fit the model for *future* use on all data. It is labelled calibrated ONLY if the walk-forward
    evaluation of the same procedure passed the out-of-sample calibration criteria."""
    days = sorted(samples_by_day)
    if len(days) < 3:
        return None
    factory = default_models().get(name)
    if factory is None or factory is HeuristicForecaster:
        return None
    cal_start = max(1, len(days) - cal_days)
    train = [s for d in days[:cal_start] for s in samples_by_day[d]]
    cal = [s for d in days[cal_start:] for s in samples_by_day[d]]
    model = fit_with_calibration(factory, train, cal)
    if not isinstance(model, RidgeForecaster):
        return None
    ev = evaluation.get("models", {}).get(name, {})
    model.calibrated = bool(ev.get("calibrated")) and model.calibrator is not None and model.calibrator.fitted
    model.trained_through = days[-1].isoformat()
    return model


__all__ = ["walk_forward_evaluate", "finalise_model", "default_models", "fit_with_calibration", "norm_cdf"]
