"""Forecast evaluation: calibration, accuracy and ranking quality. No number here is shown as "reliable"
unless it passes ``CalibrationReport.passes``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

MIN_SAMPLES_FOR_CALIBRATION = 300
MAX_ECE = 0.08
MIN_AUC = 0.55  # must actually discriminate winners from losers, not just echo the base rate
MIN_BRIER_SKILL = 0.01


@dataclass(slots=True)
class CalibrationReport:
    n: int
    brier: float | None
    brier_baseline: float | None  # always predicting the base rate
    ece: float | None  # expected calibration error (10 equal-count bins)
    base_rate: float | None
    bins: list[dict[str, float]] = field(default_factory=list)  # lo, hi, mean_pred, observed, n
    auc: float | None = None  # probability a random winner is ranked above a random loser

    @property
    def brier_skill(self) -> float | None:
        if self.brier is None or not self.brier_baseline:
            return None
        return 1.0 - self.brier / self.brier_baseline

    @property
    def passes(self) -> bool:
        """Trustworthy probabilities: enough out-of-sample data, small calibration error, real discrimination
        (AUC) and a Brier score clearly better than always predicting the base rate."""
        skill = self.brier_skill
        return bool(
            self.n >= MIN_SAMPLES_FOR_CALIBRATION
            and self.ece is not None
            and self.ece <= MAX_ECE
            and skill is not None
            and skill >= MIN_BRIER_SKILL
            and self.auc is not None
            and self.auc >= MIN_AUC
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "brier": self.brier,
            "brier_baseline": self.brier_baseline,
            "ece": self.ece,
            "base_rate": self.base_rate,
            "bins": self.bins,
            "passes": self.passes,
            "brier_skill": self.brier_skill,
            "auc": self.auc,
            "criteria": (
                f"n >= {MIN_SAMPLES_FOR_CALIBRATION}, ECE <= {MAX_ECE}, AUC >= {MIN_AUC}, "
                f"Brier skill >= {MIN_BRIER_SKILL} vs always predicting the base rate"
            ),
        }


def calibration_report(p_pred: np.ndarray, outcome: np.ndarray, n_bins: int = 10) -> CalibrationReport:
    p = np.asarray(p_pred, dtype=float)
    y = np.asarray(outcome, dtype=float)
    n = len(p)
    if n == 0:
        return CalibrationReport(0, None, None, None, None)
    base = float(y.mean())
    brier = float(np.mean((p - y) ** 2))
    brier_base = float(np.mean((base - y) ** 2))
    order = np.argsort(p)
    bins: list[dict[str, float]] = []
    ece = 0.0
    for chunk in np.array_split(order, min(n_bins, max(1, n // 20))):
        if len(chunk) == 0:
            continue
        mp, ob = float(p[chunk].mean()), float(y[chunk].mean())
        ece += len(chunk) / n * abs(mp - ob)
        bins.append(
            {
                "lo": float(p[chunk].min()),
                "hi": float(p[chunk].max()),
                "mean_pred": mp,
                "observed": ob,
                "n": float(len(chunk)),
            }
        )
    return CalibrationReport(n, brier, brier_base, float(ece), base, bins, auc_score(p, y))


class BinCalibrator:
    """Monotone (isotonic, pool-adjacent-violators) map from raw probability to observed frequency."""

    def __init__(self) -> None:
        self.x: np.ndarray = np.array([0.0, 1.0])
        self.y: np.ndarray = np.array([0.0, 1.0])
        self.fitted = False

    def fit(self, p: np.ndarray, outcome: np.ndarray) -> BinCalibrator:
        p = np.asarray(p, dtype=float)
        y = np.asarray(outcome, dtype=float)
        if len(p) < 50:
            return self
        o = np.argsort(p, kind="stable")
        xs, ys = p[o], y[o]
        # Pool Adjacent Violators: blocks of (sum of y, count, mean x); merge while the means are decreasing.
        blocks: list[list[float]] = []
        for xv, yv in zip(xs, ys, strict=True):
            blocks.append([float(yv), 1.0, float(xv)])
            while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
                s2, c2, x2 = blocks.pop()
                s1, c1, x1 = blocks.pop()
                blocks.append([s1 + s2, c1 + c2, (x1 * c1 + x2 * c2) / (c1 + c2)])
        bx = np.asarray([b[2] for b in blocks])
        by = np.asarray([b[0] / b[1] for b in blocks])
        if len(bx) > 25:  # keep the map small and stable
            idx = np.linspace(0, len(bx) - 1, 25).astype(int)
            bx, by = bx[idx], by[idx]
        self.x, self.y = bx, np.clip(by, 0.001, 0.999)
        self.fitted = True
        return self

    def __call__(self, p: float) -> float:
        if not self.fitted:
            return p
        return float(np.interp(p, self.x, self.y))


def spearman(a: np.ndarray, b: np.ndarray) -> float | None:
    if len(a) < 5 or np.std(a) == 0 or np.std(b) == 0:
        return None
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    return float(np.corrcoef(ra, rb)[0, 1])


def auc_score(p: np.ndarray, y: np.ndarray) -> float | None:
    """Area under the ROC curve via the rank-sum identity (ties get average ranks)."""
    p = np.asarray(p, dtype=float)
    y = np.asarray(y, dtype=float)
    pos = y > 0.5
    n1, n0 = int(pos.sum()), int((~pos).sum())
    if n1 == 0 or n0 == 0:
        return None
    order = np.argsort(p, kind="stable")
    ranks = np.empty(len(p))
    ps = p[order]
    i = 0
    while i < len(ps):
        j = i
        while j + 1 < len(ps) and ps[j + 1] == ps[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return float((ranks[pos].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))
