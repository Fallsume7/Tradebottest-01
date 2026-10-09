"""Forecasters: a transparent rule-based baseline, a ridge regression, and an optional gradient-boosted model.

All three answer the same question: *over the next ``horizon`` minutes, what is the distribution of the
return of buying now and selling at the horizon?* They return a ``Forecast`` that says what kind of
estimate it is. **Only a forecaster whose out-of-sample calibration passed (see ``calibration.py``) is
labelled calibrated**; everything else is labelled a heuristic/uncalibrated estimate and the UI must say so.
No language model is involved anywhere.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

import numpy as np

from mwbot.forecast.calibration import BinCalibrator
from mwbot.forecast.features import FEATURE_ORDER, Features

Z90 = 1.2816  # one-sided 90 % normal quantile


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(slots=True)
class Contribution:
    name: str
    label: str
    value: float | None  # the (normalised) signal in [-1, 1]
    points: float  # contribution to the expected return, in percentage points


@dataclass(slots=True)
class Forecast:
    symbol: str
    horizon_minutes: float
    expected_return_pct: float  # gross mean over the horizon
    expected_net_return_pct: float  # after modelled round-trip costs
    sigma_pct: float  # forecast standard deviation of the horizon return = the uncertainty
    p_positive: float  # P(net return > 0)
    upside_pct: float  # ~90th percentile of the (gross) horizon return
    downside_pct: float  # ~10th percentile of the (gross) horizon return (negative = loss)
    cost_pct: float
    method: str
    kind: str  # heuristic | statistical | ml
    calibrated: bool
    confidence: float | None  # only set for calibrated forecasters
    contributions: list[Contribution] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.calibrated:
            return f"MODEL FORECAST - {self.method} (validated out-of-sample)"
        return f"MODEL FORECAST - {self.method} (heuristic/uncalibrated estimate, not a reliable probability)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "horizon_minutes": round(self.horizon_minutes, 1),
            "expected_return_pct": round(self.expected_return_pct, 4),
            "expected_net_return_pct": round(self.expected_net_return_pct, 4),
            "uncertainty_pct": round(self.sigma_pct, 4),
            "p_positive": round(self.p_positive, 4),
            "upside_pct": round(self.upside_pct, 4),
            "downside_pct": round(self.downside_pct, 4),
            "cost_pct": round(self.cost_pct, 4),
            "method": self.method,
            "kind": self.kind,
            "calibrated": self.calibrated,
            "confidence": None if self.confidence is None else round(self.confidence, 3),
            "label": self.label,
            "contributions": [
                {
                    "name": c.name,
                    "label": c.label,
                    "value": None if c.value is None else round(c.value, 3),
                    "points": round(c.points, 4),
                }
                for c in self.contributions
            ],
            "warnings": self.warnings,
        }


def sigma_horizon(f: Features, horizon: float) -> float:
    """Volatility of the horizon return in %: per-minute vol scaled by sqrt(time), damped a little because
    minute-to-minute noise partly mean-reverts."""
    return max(f.sigma_1m_pct * math.sqrt(max(horizon, 1.0)) * 0.9, 0.05)


class Forecaster(ABC):
    name: ClassVar[str]
    kind: ClassVar[str]
    calibrated: bool = False

    @abstractmethod
    def predict(self, f: Features, horizon: float, cost_pct: float) -> Forecast: ...


# --------------------------------------------------------------------------- heuristic baseline
DEFAULT_WEIGHTS: dict[str, float] = {
    "momentum": 1.0,
    "acceleration": 0.5,
    "vwap": 0.5,
    "trend": 0.6,
    "breakout": 0.7,
    "reversal": 0.5,
    "volume": 0.5,
    "relative_strength": 0.6,
    "extension": 0.9,
    "catalyst": 0.3,
    "history": 0.3,
}
LABELS = {
    "momentum": "Short-term momentum",
    "acceleration": "Momentum accelerating / fading",
    "vwap": "Price vs VWAP",
    "trend": "Moving-average trend",
    "breakout": "Breakout above recent highs",
    "reversal": "Recovery from the day's low",
    "volume": "Volume confirming the move",
    "relative_strength": "Strength vs the market",
    "extension": "Already stretched / exhausted",
    "catalyst": "News / catalyst",
    "history": "Same-time pattern in earlier sessions",
}


def _t(x: float) -> float:
    return math.tanh(x)


class HeuristicForecaster(Forecaster):
    """Transparent rules, deliberately modest: even a strong set of signals only shifts the expected return by
    ``edge`` standard deviations of the horizon move. It exists so the bot works with no history to learn from."""

    name = "heuristic-v1"
    kind = "heuristic"

    def __init__(self, weights: dict[str, float] | None = None, edge: float = 0.30) -> None:
        self.weights = {**DEFAULT_WEIGHTS, **(weights or {})}
        self.edge = edge

    def signals(self, f: Features) -> dict[str, float | None]:
        s1 = f.sigma_1m_pct
        sig: dict[str, float | None] = {}

        m5 = None if f.ret_5m is None else f.ret_5m / (s1 * math.sqrt(5))
        m15 = None if f.ret_15m is None else f.ret_15m / (s1 * math.sqrt(15))
        parts = [v for v in (m5, m15) if v is not None]
        sig["momentum"] = _t(0.55 * sum(parts) / len(parts)) if parts else None
        sig["acceleration"] = None if f.accel_5m is None else _t(0.8 * f.accel_5m / (s1 * math.sqrt(5)))
        sig["vwap"] = (
            None if f.vs_vwap_pct is None else _t(0.6 * min(f.vs_vwap_pct / (s1 * math.sqrt(30)), 2.5))
        )
        sig["trend"] = None if f.ema_gap_pct is None else _t(0.8 * f.ema_gap_pct / (s1 * math.sqrt(10)))

        # breakout: above the prior 30-bar high with volume = strong; just under resistance with volume = mild
        b: float | None = None
        if f.breakout_pct is not None:
            if f.breakout_pct > 0:
                vol_factor = 0.5 + 0.5 * min(f.rvol or 1.0, 3.0) / 3.0
                b = min(1.0, 0.8 * f.breakout_pct / (s1 * math.sqrt(15))) * vol_factor
            elif f.resistance_price and f.rvol and f.rvol > 1.5 and (f.ret_5m or 0) > 0:
                b = 0.2
            else:
                b = 0.0
        sig["breakout"] = b

        # reversal: bouncing off the low (+) vs. falling knife (-)
        rev: float | None = None
        if f.range_pos is not None and f.recovery_pct is not None:
            rev = 0.0
            if (
                f.range_pos < 0.4
                and f.recovery_pct > 0
                and (f.ret_5m or 0) > 0
                and (f.accel_5m or 0) > 0
                and (f.minutes_since_low or 999) <= 60
            ):
                rev = 0.6 * min(1.0, f.recovery_pct / (s1 * math.sqrt(20)) / 2.0)
            elif f.range_pos < 0.25 and (f.ret_5m or 0) < 0 and (f.ret_15m or 0) < 0:
                rev = -0.5
        sig["reversal"] = rev

        mom = sig["momentum"]
        if f.rvol is not None and mom is not None:
            direction = 1.0 if mom > 0.05 else (-1.0 if mom < -0.05 else 0.0)
            sig["volume"] = 0.8 * _t(0.6 * math.log2(max(f.rvol, 0.25))) * direction
        else:
            sig["volume"] = None

        sig["relative_strength"] = (
            None
            if f.rel_strength is None
            else _t(0.5 * f.rel_strength / (s1 * math.sqrt(max(f.minutes_since_open, 5.0))))
        )

        # extension / exhaustion: far from the open and no longer accelerating => expect give-back
        ext: float | None = None
        if f.pct_from_open is not None:
            e = f.pct_from_open / (s1 * math.sqrt(max(f.minutes_since_open, 5.0)))
            ext = 0.0
            if e > 2.5:
                fading = (f.accel_5m is not None and f.accel_5m <= 0) or (
                    f.ret_5m is not None and f.ret_5m <= 0
                )
                ext = -_t((e - 2.5) * 0.5) * (1.0 if fading else 0.4)
            if f.vs_vwap_pct is not None and f.vs_vwap_pct / (s1 * math.sqrt(30)) > 3.5:
                ext = (ext or 0.0) - 0.3
            ext = max(-1.0, ext)
        sig["extension"] = ext

        sig["catalyst"] = None if f.catalyst_sentiment is None else 0.5 * f.catalyst_sentiment
        sig["history"] = None
        if f.hist_mean_pct is not None and f.hist_n >= 5:
            sig["history"] = _t(f.hist_mean_pct / max(s1 * 8, 0.05))
        return sig

    def predict(self, f: Features, horizon: float, cost_pct: float) -> Forecast:
        sig = self.signals(f)
        used = {k: v for k, v in sig.items() if v is not None and self.weights.get(k, 0) > 0}
        wsum = sum(self.weights[k] for k in used) or 1.0
        z = sum(self.weights[k] * v for k, v in used.items()) / wsum
        sh = sigma_horizon(f, horizon)
        mu = self.edge * z * sh
        contribs = [
            Contribution(k, LABELS[k], sig[k], self.edge * self.weights[k] * (sig[k] or 0.0) / wsum * sh)
            for k in sig
            if sig[k] is not None
        ]
        contribs.sort(key=lambda c: -abs(c.points))
        warn = [
            f"signal unavailable: {LABELS[k].lower()}"
            for k, v in sig.items()
            if v is None and k not in ("history",)
        ]
        if f.sigma_source != "measured":
            warn.append(f"volatility estimated from {f.sigma_source}")
        return Forecast(
            symbol=f.symbol,
            horizon_minutes=horizon,
            expected_return_pct=mu,
            expected_net_return_pct=mu - cost_pct,
            sigma_pct=sh,
            p_positive=norm_cdf((mu - cost_pct) / sh),
            upside_pct=mu + Z90 * sh,
            downside_pct=mu - Z90 * sh,
            cost_pct=cost_pct,
            method=self.name,
            kind=self.kind,
            calibrated=False,
            confidence=None,
            contributions=contribs,
            warnings=warn,
        )


# --------------------------------------------------------------------------- statistical (ridge)
class RidgeForecaster(Forecaster):
    """Ridge regression of the *volatility-normalised* forward return on the normalised features.

    Normalising by the horizon volatility makes one coefficient set meaningful across cheap/expensive and
    calm/volatile stocks. Fit only on sessions strictly earlier than the day being forecast.
    """

    name = "ridge-v1"
    kind = "statistical"

    def __init__(self, alpha: float = 30.0) -> None:
        self.alpha = alpha
        self.coef: np.ndarray | None = None
        self.intercept = 0.0
        self.mean: np.ndarray | None = None
        self.scale: np.ndarray | None = None
        self.resid_sd = 1.0
        self.n_train = 0
        self.trained_through: str = ""
        self.calibrator: BinCalibrator | None = None
        self.calibrated = False

    @property
    def fitted(self) -> bool:
        return self.coef is not None

    def fit(self, x: np.ndarray, y_z: np.ndarray) -> RidgeForecaster:
        if len(x) < 100:
            raise ValueError("need at least 100 training samples")
        self.mean = x.mean(axis=0)
        self.scale = np.where(x.std(axis=0) > 1e-9, x.std(axis=0), 1.0)
        xs = (x - self.mean) / self.scale
        y = np.clip(y_z, -6, 6)
        self.intercept = float(y.mean())
        a = xs.T @ xs + self.alpha * np.eye(xs.shape[1])
        self.coef = np.linalg.solve(a, xs.T @ (y - self.intercept))
        resid = y - (xs @ self.coef + self.intercept)
        self.resid_sd = float(max(resid.std() * 1.1, 0.3))  # inflate: in-sample residuals are optimistic
        self.n_train = len(x)
        return self

    def predict_z(self, f: Features) -> float:
        assert self.coef is not None and self.mean is not None and self.scale is not None
        xs = (f.vector() - self.mean) / self.scale
        return float(xs @ self.coef + self.intercept)

    def predict(self, f: Features, horizon: float, cost_pct: float) -> Forecast:
        if not self.fitted:
            raise RuntimeError("model not fitted")
        sh = sigma_horizon(f, horizon)
        z = self.predict_z(f)
        mu = z * sh
        sd = self.resid_sd * sh
        p_raw = norm_cdf((mu - cost_pct) / sd)
        p = self.calibrator(p_raw) if (self.calibrator and self.calibrated) else p_raw
        warn = [] if self.calibrated else ["probabilities not validated out-of-sample"]
        if f.missing:
            warn.append("missing inputs treated as neutral: " + ", ".join(f.missing[:3]))
        assert self.mean is not None and self.scale is not None
        coefs = self.coef * (f.vector() - self.mean) / self.scale
        contribs = [
            Contribution(FEATURE_ORDER[i], FEATURE_ORDER[i].replace("_", " "), None, float(coefs[i] * sh))
            for i in range(len(FEATURE_ORDER))
        ]
        contribs.sort(key=lambda c: -abs(c.points))
        return Forecast(
            symbol=f.symbol,
            horizon_minutes=horizon,
            expected_return_pct=mu,
            expected_net_return_pct=mu - cost_pct,
            sigma_pct=sd,
            p_positive=p,
            upside_pct=mu + Z90 * sd,
            downside_pct=mu - Z90 * sd,
            cost_pct=cost_pct,
            method=self.name,
            kind=self.kind,
            calibrated=self.calibrated,
            confidence=None,
            contributions=contribs[:8],
            warnings=warn,
        )

    def to_dict(self) -> dict[str, Any]:
        assert self.coef is not None and self.mean is not None and self.scale is not None
        return {
            "alpha": self.alpha,
            "coef": self.coef.tolist(),
            "intercept": self.intercept,
            "mean": self.mean.tolist(),
            "scale": self.scale.tolist(),
            "resid_sd": self.resid_sd,
            "n_train": self.n_train,
            "trained_through": self.trained_through,
            "calibrated": self.calibrated,
            "cal_x": self.calibrator.x.tolist() if self.calibrator else None,
            "cal_y": self.calibrator.y.tolist() if self.calibrator else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RidgeForecaster:
        m = cls(d["alpha"])
        m.coef, m.intercept = np.asarray(d["coef"]), d["intercept"]
        m.mean, m.scale = np.asarray(d["mean"]), np.asarray(d["scale"])
        m.resid_sd, m.n_train, m.trained_through = d["resid_sd"], d["n_train"], d.get("trained_through", "")
        m.calibrated = bool(d.get("calibrated"))
        if d.get("cal_x"):
            c = BinCalibrator()
            c.x, c.y, c.fitted = np.asarray(d["cal_x"]), np.asarray(d["cal_y"]), True
            m.calibrator = c
        return m


# --------------------------------------------------------------------------- optional ML
def sklearn_available() -> bool:
    try:
        import sklearn  # noqa: F401
    except ImportError:
        return False
    return True


class GradientBoostedForecaster(RidgeForecaster):
    """Optional ML model (scikit-learn ``HistGradientBoostingRegressor``) on the same features/targets.

    Requires ``pip install "mwbot[ml]"``. Shares prediction/calibration plumbing with the ridge model.
    """

    name = "gbm-v1"
    kind = "ml"

    def __init__(self, **params: Any) -> None:
        super().__init__(alpha=0.0)
        self.params = {
            "max_depth": 3,
            "max_iter": 80,
            "learning_rate": 0.06,
            "min_samples_leaf": 40,
            "l2_regularization": 1.0,
            **params,
        }
        self.model: Any = None

    @property
    def fitted(self) -> bool:
        return self.model is not None

    def fit(self, x: np.ndarray, y_z: np.ndarray) -> GradientBoostedForecaster:
        if not sklearn_available():
            raise RuntimeError("scikit-learn is not installed (pip install 'mwbot[ml]')")
        from sklearn.ensemble import HistGradientBoostingRegressor

        if len(x) < 200:
            raise ValueError("need at least 200 training samples")
        y = np.clip(y_z, -6, 6)
        self.model = HistGradientBoostingRegressor(random_state=0, **self.params).fit(x, y)
        self.resid_sd = float(max((y - self.model.predict(x)).std() * 1.25, 0.3))
        self.n_train = len(x)
        return self

    def predict_z(self, f: Features) -> float:
        return float(self.model.predict(f.vector().reshape(1, -1))[0])

    def predict(self, f: Features, horizon: float, cost_pct: float) -> Forecast:
        sh = sigma_horizon(f, horizon)
        mu = self.predict_z(f) * sh
        sd = self.resid_sd * sh
        p_raw = norm_cdf((mu - cost_pct) / sd)
        p = self.calibrator(p_raw) if (self.calibrator and self.calibrated) else p_raw
        return Forecast(
            symbol=f.symbol,
            horizon_minutes=horizon,
            expected_return_pct=mu,
            expected_net_return_pct=mu - cost_pct,
            sigma_pct=sd,
            p_positive=p,
            upside_pct=mu + Z90 * sd,
            downside_pct=mu - Z90 * sd,
            cost_pct=cost_pct,
            method=self.name,
            kind=self.kind,
            calibrated=self.calibrated,
            confidence=None,
            warnings=[] if self.calibrated else ["probabilities not validated out-of-sample"],
        )

    def to_dict(self) -> dict[str, Any]:  # not persisted: retrain on demand
        raise NotImplementedError("GBM models are retrained on demand, not persisted")
