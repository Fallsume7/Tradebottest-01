"""Model check: walk-forward comparison of the forecasters on a dataset (what the Strategy Lab shows)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mwbot.config import AppSettings
from mwbot.data.base import DataUnavailableError, MarketDataProvider
from mwbot.eligibility.universe import EligibilityUniverse
from mwbot.forecast.dataset import Sample, build_day_samples
from mwbot.forecast.evaluation import MIN_TEST_DAYS, finalise_model, walk_forward_evaluate
from mwbot.scanner.scanner import Scanner


def evaluate_models(
    provider: MarketDataProvider,
    settings: AppSettings,
    universe: EligibilityUniverse | None,
    *,
    max_days: int | None = None,
    stride_min: int = 20,
    horizon: float = 60.0,
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[dict[str, Any], Any]:
    """Returns (report, finalised ridge model or None). Chronological walk-forward only: every model is fitted on
    sessions strictly before the one it is scored on, with the probability map fitted on the sessions just before."""
    dates = provider.available_dates()
    if not dates:
        raise DataUnavailableError("this provider has no fixed set of dates to evaluate on")
    dates = sorted(dates)[-max_days:] if max_days else sorted(dates)
    scanner = Scanner(settings.scanner, settings.game)
    applied = universe is not None and not universe.ignore
    samples: dict[Any, list[Sample]] = {}
    for i, d in enumerate(dates):
        if progress:
            progress(i, len(dates), d.isoformat())
        src = provider.open_session(d)
        if not hasattr(src, "full_day"):
            raise DataUnavailableError("model check needs a replayable dataset (historical or synthetic)")
        samples[d] = build_day_samples(
            src,  # type: ignore[arg-type]
            settings.effective_costs(),
            scanner,
            universe if applied else None,
            horizon=horizon,
            stride_min=stride_min,
        )
    ev = walk_forward_evaluate(samples)
    model = finalise_model(samples, ev) if ev.get("ok") else None
    feed = provider.feed_info()
    ev["data"] = {
        "feed": feed.to_dict(),
        "dataset": getattr(provider, "name", ""),
        "n_days": len(dates),
        "eligibility_applied": applied,
        "min_test_days_for_calibrated": MIN_TEST_DAYS,
        "note": (
            "Synthetic data: this checks the machinery, not real-market predictability."
            if feed.kind.value == "synthetic"
            else "Historical data: survivorship bias may flatter results unless the dataset is point-in-time."
        ),
    }
    if not applied:
        ev.setdefault("warnings", []).insert(0, "Eligibility NOT applied to the samples (all tickers used).")
    return ev, model
