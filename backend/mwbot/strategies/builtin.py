"""The five built-in strategies (A-E). Rules are documented in docs/STRATEGIES.md."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, ClassVar

import pandas as pd
from pydantic import BaseModel, Field

from mwbot.domain import Position
from mwbot.scanner import scanner as sc
from mwbot.strategies.base import (
    Condition,
    Evaluation,
    ExitDecision,
    SignalInput,
    Strategy,
    held_minutes,
)


def _fmt(v: float | None, spec: str = ".2f", suffix: str = "") -> str:
    return "n/a" if v is None else f"{v:{spec}}{suffix}"


def _ge(label: str, v: float | None, thr: float, spec: str = ".2f", suffix: str = "") -> Condition:
    return Condition(label, v is not None and v >= thr, f"{_fmt(v, spec, suffix)} (need >= {thr:g}{suffix})")


def _momentum_lost_exit(inp: SignalInput, mom_floor: float, vwap_tol: float) -> ExitDecision | None:
    m = inp.metrics
    if (
        m.vs_vwap_pct is not None and m.momentum_short is not None
        and m.vs_vwap_pct < -vwap_tol and m.momentum_short < mom_floor
    ):
        return ExitDecision(
            f"Momentum lost: {m.vs_vwap_pct:+.2f}% vs VWAP and {m.momentum_short:+.2f}% short-term momentum"
        )
    return None


# --------------------------------------------------------------------------- A: opening momentum


class OpeningMomentumParams(BaseModel):
    start_minute: float = Field(10, ge=0, le=120, description="Earliest minute after open to enter")
    end_minute: float = Field(60, ge=1, le=240, description="Latest minute after open to enter")
    min_open_drive_pct: float = Field(2.0, ge=0, le=50, description="Min % gain from the regular-session open")
    min_rvol: float = Field(2.0, ge=0, le=50, description="Min relative volume (skipped if unavailable)")
    min_momentum_pct: float = Field(0.2, ge=-5, le=10, description="Min % gain over the short momentum window")
    max_extension_pct: float = Field(30.0, ge=1, le=300, description="Skip if already up more than this % from open")
    or_tolerance_pct: float = Field(1.0, ge=0, le=10, description="Price must be within this % of the opening-range high")
    min_scan_score: float = Field(35.0, ge=0, le=100, description="Min scanner score")
    stop_loss_pct: float = Field(2.5, gt=0, le=20, description="Initial stop below entry (%)")
    take_profit_pct: float | None = Field(5.0, gt=0, le=100, description="Profit target (%)")
    trailing_stop_pct: float | None = Field(2.0, gt=0, le=20, description="Trailing stop from high-water mark (%)")
    max_hold_minutes: int | None = Field(90, ge=1, le=390, description="Time stop (minutes)")
    exit_vwap_tolerance_pct: float = Field(0.3, ge=0, le=5, description="Exit if this far below VWAP with negative momentum")


class OpeningMomentum(Strategy):
    id = "opening_momentum"
    name = "A - Opening Momentum"
    summary = "Buy strong early-session drives with unusual volume that are still making progress."
    entry_rules: ClassVar[list[str]] = [
        "Within the entry window after the open",
        "Up at least X% from the regular-session open, still near the opening-range high",
        "Relative volume above threshold and price above VWAP",
        "Positive short-term momentum; not already over-extended",
        "Scanner marks the symbol tradable with a minimum score",
    ]
    exit_rules: ClassVar[list[str]] = [
        "Price below VWAP with negative short-term momentum",
        "Hard exits: stop, profit target, trailing stop, time stop, end-of-day liquidation",
    ]
    risk_rules: ClassVar[list[str]] = ["Stop distance and sizing come from parameters and global risk limits"]
    ParamsModel = OpeningMomentumParams
    params: OpeningMomentumParams

    def evaluate_entry(self, inp: SignalInput) -> Evaluation:
        p, m = self.params, inp.metrics
        reg = inp.regular_bars
        conds = [self._in_window(inp, p.start_minute, p.end_minute)]
        conds.append(Condition("Scanner: tradable", bool(inp.scan and inp.scan.tradable),
                               "; ".join(inp.scan.blockers[:2]) if inp.scan and inp.scan.blockers else "ok"))
        conds.append(_ge("Scanner score", inp.scan.score if inp.scan else None, p.min_scan_score, ".0f"))
        drive = None
        or_ok = False
        if len(reg) >= 3 and m.regular_open:
            drive = (m.price / m.regular_open - 1) * 100 if m.price else None
            first = reg.head(min(len(reg), max(int(p.start_minute), 5)))
            or_high = float(first["high"].max())
            or_ok = bool(m.price and m.price >= or_high * (1 - p.or_tolerance_pct / 100))
            conds.append(Condition("Near opening-range high", or_ok, f"px {m.price} vs OR high {or_high:.2f}"))
        else:
            conds.append(Condition("Near opening-range high", False, "not enough regular-session bars"))
        conds.append(_ge("Open drive %", drive, p.min_open_drive_pct, ".2f", "%"))
        conds.append(Condition("Not over-extended", drive is not None and drive <= p.max_extension_pct,
                               f"{_fmt(drive)}% from open (max {p.max_extension_pct:g}%)"))
        if m.rvol is None:
            conds.append(Condition("Relative volume", True, "unavailable - check skipped"))
        else:
            conds.append(_ge("Relative volume", m.rvol, p.min_rvol, ".1f", "x"))
        conds.append(Condition("Above VWAP", m.vs_vwap_pct is not None and m.vs_vwap_pct > 0, f"{_fmt(m.vs_vwap_pct)}%"))
        conds.append(_ge("Short momentum %", m.momentum_short, p.min_momentum_pct, ".2f", "%"))
        strength = ((drive or 0) / 10 + (m.rvol or 1) / 10 + (inp.scan.score if inp.scan else 0) / 100) / 3
        return self._eval(inp, conds, strength)

    def evaluate_exit(self, inp: SignalInput, position: Position) -> ExitDecision | None:
        return _momentum_lost_exit(inp, 0.0, self.params.exit_vwap_tolerance_pct)


# --------------------------------------------------------------------------- B: premarket gap continuation


class GapContinuationParams(BaseModel):
    start_minute: float = Field(10, ge=0, le=120)
    end_minute: float = Field(75, ge=1, le=240)
    min_gap_pct: float = Field(8.0, ge=1, le=300, description="Min premarket / opening gap vs previous close (%)")
    max_total_change_pct: float = Field(80.0, ge=5, le=500, description="Skip if already up more than this vs prev close")
    min_premarket_volume: float = Field(100_000, ge=0, description="Min shares traded before the open")
    hold_open_tolerance_pct: float = Field(0.5, ge=0, le=10, description="Price may dip this % below the regular open")
    pm_high_tolerance_pct: float = Field(1.5, ge=0, le=20, description="Price must be within this % of the premarket high")
    min_momentum_pct: float = Field(0.0, ge=-5, le=10)
    min_scan_score: float = Field(30.0, ge=0, le=100)
    stop_loss_pct: float = Field(4.0, gt=0, le=25)
    take_profit_pct: float | None = Field(8.0, gt=0, le=200)
    trailing_stop_pct: float | None = Field(3.0, gt=0, le=25)
    max_hold_minutes: int | None = Field(120, ge=1, le=390)


class GapContinuation(Strategy):
    id = "gap_continuation"
    name = "B - Premarket Gap Continuation"
    summary = "After a large premarket gap, buy only if the gap is holding and buyers are still pushing."
    entry_rules: ClassVar[list[str]] = [
        "Premarket gap >= X% on meaningful premarket volume",
        "After the open, price holds the regular open and sits near the premarket high",
        "Above VWAP with non-negative short-term momentum",
        "Not already over-extended vs previous close",
    ]
    exit_rules: ClassVar[list[str]] = [
        "Price loses the regular open (gap-fail)",
        "Below VWAP with negative momentum",
        "Hard exits: stop, target, trailing stop, time stop, end-of-day liquidation",
    ]
    risk_rules: ClassVar[list[str]] = ["Wider stop (gappers are volatile) paired with smaller risk-based size"]
    ParamsModel = GapContinuationParams
    params: GapContinuationParams

    def evaluate_entry(self, inp: SignalInput) -> Evaluation:
        p, m = self.params, inp.metrics
        pre, reg = inp.premarket_bars, inp.regular_bars
        gap = m.gap_pct if m.gap_pct is not None else m.premarket_pct
        pm_vol = float(pre["volume"].sum()) if len(pre) else 0.0
        conds = [self._in_window(inp, p.start_minute, p.end_minute)]
        conds.append(Condition("Scanner: tradable", bool(inp.scan and inp.scan.tradable),
                               "; ".join(inp.scan.blockers[:2]) if inp.scan and inp.scan.blockers else "ok"))
        conds.append(_ge("Scanner score", inp.scan.score if inp.scan else None, p.min_scan_score, ".0f"))
        conds.append(_ge("Gap vs previous close", gap, p.min_gap_pct, ".1f", "%"))
        conds.append(Condition("Premarket volume", pm_vol >= p.min_premarket_volume,
                               f"{pm_vol:,.0f} (need >= {p.min_premarket_volume:,.0f})"))
        conds.append(Condition("Not over-extended", m.pct_change is not None and m.pct_change <= p.max_total_change_pct,
                               f"{_fmt(m.pct_change)}% vs prev close (max {p.max_total_change_pct:g}%)"))
        holds = False
        near_pm_high = False
        if len(reg) and m.regular_open and m.price:
            holds = m.price >= m.regular_open * (1 - p.hold_open_tolerance_pct / 100)
        if len(pre) and m.price:
            pm_high = float(pre["high"].max())
            near_pm_high = m.price >= pm_high * (1 - p.pm_high_tolerance_pct / 100)
        conds.append(Condition("Holding the regular open", holds, f"open {_fmt(m.regular_open)}, price {_fmt(m.price)}"))
        conds.append(Condition("Near/above premarket high", near_pm_high, "continuation test"))
        conds.append(Condition("Above VWAP", m.vs_vwap_pct is not None and m.vs_vwap_pct > 0, f"{_fmt(m.vs_vwap_pct)}%"))
        conds.append(_ge("Short momentum %", m.momentum_short, p.min_momentum_pct, ".2f", "%"))
        strength = ((gap or 0) / 40 + (inp.scan.score if inp.scan else 0) / 100) / 2
        return self._eval(inp, conds, strength)

    def evaluate_exit(self, inp: SignalInput, position: Position) -> ExitDecision | None:
        m = inp.metrics
        if m.regular_open and m.price and m.price < m.regular_open * (1 - self.params.hold_open_tolerance_pct / 100 - 0.005):
            return ExitDecision(f"Gap failed: price {m.price:.2f} lost the regular open {m.regular_open:.2f}")
        return _momentum_lost_exit(inp, 0.0, 0.3)


# --------------------------------------------------------------------------- C: momentum breakout


class BreakoutParams(BaseModel):
    start_minute: float = Field(15, ge=0, le=300)
    end_minute: float = Field(240, ge=1, le=390)
    lookback_minutes: int = Field(30, ge=5, le=120, description="Bars used to define the reference high")
    breakout_buffer_pct: float = Field(0.15, ge=0, le=5, description="Close must exceed the reference high by this %")
    volume_multiple: float = Field(2.5, ge=1, le=50, description="Breakout bar volume vs average of lookback bars")
    max_range_pct: float = Field(12.0, ge=0.5, le=100, description="Reject if the lookback range is wider (not a base)")
    min_pct_change: float = Field(2.0, ge=-50, le=500, description="Min % change on the day")
    min_scan_score: float = Field(30.0, ge=0, le=100)
    stop_loss_pct: float = Field(2.5, gt=0, le=20)
    take_profit_pct: float | None = Field(6.0, gt=0, le=100)
    trailing_stop_pct: float | None = Field(2.5, gt=0, le=20)
    max_hold_minutes: int | None = Field(90, ge=1, le=390)
    failed_breakout_pct: float = Field(0.5, ge=0, le=10, description="Exit if close falls this % below the broken level")


class MomentumBreakout(Strategy):
    id = "momentum_breakout"
    name = "C - Momentum Breakout"
    summary = "Buy a volume-confirmed break above a recent intraday high after a contained base."
    entry_rules: ClassVar[list[str]] = [
        "Last completed bar closes above the highest high of the prior N minutes (+buffer)",
        "Breakout bar volume >= multiple of the lookback average",
        "Lookback range is contained (a base, not a vertical spike), price above VWAP",
        "Up on the day and scanner-tradable",
    ]
    exit_rules: ClassVar[list[str]] = [
        "Failed breakout: close back below the broken level",
        "Hard exits: stop, target, trailing stop, time stop, end-of-day liquidation",
    ]
    risk_rules: ClassVar[list[str]] = ["Stop is a fixed % below entry; size from risk-per-trade"]
    ParamsModel = BreakoutParams
    params: BreakoutParams

    def evaluate_entry(self, inp: SignalInput) -> Evaluation:
        p, m = self.params, inp.metrics
        bars = inp.ctx.bars
        conds = [self._in_window(inp, p.start_minute, p.end_minute)]
        conds.append(Condition("Scanner: tradable", bool(inp.scan and inp.scan.tradable),
                               "; ".join(inp.scan.blockers[:2]) if inp.scan and inp.scan.blockers else "ok"))
        conds.append(_ge("Scanner score", inp.scan.score if inp.scan else None, p.min_scan_score, ".0f"))
        ref = None
        vol_ratio = None
        range_pct = None
        broke = False
        if len(bars) >= p.lookback_minutes + 2:
            window = bars.iloc[-(p.lookback_minutes + 1) : -1]  # prior bars, excluding the breakout bar
            last = bars.iloc[-1]
            ref = float(window["high"].max())
            lo = float(window["low"].min())
            range_pct = (ref / lo - 1) * 100 if lo > 0 else None
            avg_vol = float(window["volume"].mean())
            vol_ratio = float(last["volume"]) / avg_vol if avg_vol > 0 else None
            broke = float(last["close"]) > ref * (1 + p.breakout_buffer_pct / 100)
        conds.append(Condition("Close above prior-high + buffer", broke, f"reference high {_fmt(ref)}"))
        conds.append(_ge("Breakout volume vs avg", vol_ratio, p.volume_multiple, ".1f", "x"))
        conds.append(Condition("Contained base", range_pct is not None and range_pct <= p.max_range_pct,
                               f"lookback range {_fmt(range_pct, '.1f')}% (max {p.max_range_pct:g}%)"))
        conds.append(Condition("Above VWAP", m.vs_vwap_pct is not None and m.vs_vwap_pct > 0, f"{_fmt(m.vs_vwap_pct)}%"))
        conds.append(_ge("Change on day", m.pct_change, p.min_pct_change, ".1f", "%"))
        ev = self._eval(inp, conds, ((vol_ratio or 0) / 10 + (inp.scan.score if inp.scan else 0) / 100) / 2)
        if ref:
            ev.reference["breakout_level"] = ref
        return ev

    def evaluate_exit(self, inp: SignalInput, position: Position) -> ExitDecision | None:
        m = inp.metrics
        ref = position.reference.get("breakout_level")
        if ref and m.price and m.price < ref * (1 - self.params.failed_breakout_pct / 100) and held_minutes(position, inp) >= 2:
            return ExitDecision(f"Failed breakout: price {m.price:.2f} back below level {ref:.2f}")
        return None


# --------------------------------------------------------------------------- D: volume surge


class VolumeSurgeParams(BaseModel):
    start_minute: float = Field(10, ge=0, le=300)
    end_minute: float = Field(300, ge=1, le=390)
    surge_window: int = Field(3, ge=1, le=15, description="Minutes in the surge measurement window")
    baseline_window: int = Field(30, ge=10, le=120, description="Prior minutes used for the baseline volume")
    surge_multiple: float = Field(5.0, ge=1.5, le=100, description="Recent per-minute volume vs baseline")
    min_move_pct: float = Field(1.5, ge=0, le=50, description="Min % price move across the surge window")
    min_surge_dollar_volume: float = Field(150_000, ge=0, description="Min $ traded in the surge window (anti-illiquid-spike)")
    min_active_bars: int = Field(2, ge=1, le=15, description="Min distinct bars with trades in the window")
    min_scan_score: float = Field(25.0, ge=0, le=100)
    stop_loss_pct: float = Field(2.5, gt=0, le=20)
    take_profit_pct: float | None = Field(5.0, gt=0, le=100)
    trailing_stop_pct: float | None = Field(2.0, gt=0, le=20)
    max_hold_minutes: int | None = Field(45, ge=1, le=390)
    fade_volume_ratio: float = Field(1.0, ge=0, le=10, description="Exit if recent volume decays to this multiple of baseline")


class VolumeSurge(Strategy):
    id = "volume_surge"
    name = "D - Volume Surge"
    summary = "Buy abnormal volume that is moving price, filtered against thin one-print spikes."
    entry_rules: ClassVar[list[str]] = [
        "Recent per-minute volume >= multiple of the prior baseline",
        "Price moved up at least X% across the surge window",
        "Enough dollar volume and distinct trading bars in the window (not a single print)",
        "Above VWAP; scanner-tradable (price, spread, halt filters)",
    ]
    exit_rules: ClassVar[list[str]] = [
        "Volume fades back toward baseline while price stalls",
        "Hard exits: stop, target, trailing stop, short time stop, end-of-day liquidation",
    ]
    risk_rules: ClassVar[list[str]] = ["Short max-hold; surges mean-revert quickly"]
    ParamsModel = VolumeSurgeParams
    params: VolumeSurgeParams

    def _surge_stats(self, inp: SignalInput) -> tuple[float | None, float | None, float, int]:
        p = self.params
        bars = inp.ctx.bars
        if len(bars) < p.surge_window + 5:
            return None, None, 0.0, 0
        end = inp.ctx.as_of
        t0 = end - timedelta(minutes=p.surge_window)
        i_recent = int(bars.index.searchsorted(pd.Timestamp(t0), side="left"))
        i_base = int(bars.index.searchsorted(pd.Timestamp(t0 - timedelta(minutes=p.baseline_window)), side="left"))
        recent = bars.iloc[i_recent:]
        base = bars.iloc[i_base:i_recent]
        if recent.empty or base.empty:
            return None, None, 0.0, 0
        recent_rate = float(recent["volume"].sum()) / p.surge_window
        base_rate = float(base["volume"].sum()) / p.baseline_window
        ratio = recent_rate / base_rate if base_rate > 0 else None
        prior = bars[bars.index < recent.index[0]]
        ref_px = float(prior["close"].iloc[-1]) if len(prior) else float(recent["open"].iloc[0])
        move = (float(recent["close"].iloc[-1]) / ref_px - 1) * 100 if ref_px > 0 else None
        dollars = float((recent["close"] * recent["volume"]).sum())
        return ratio, move, dollars, len(recent)

    def evaluate_entry(self, inp: SignalInput) -> Evaluation:
        p, m = self.params, inp.metrics
        ratio, move, dollars, active = self._surge_stats(inp)
        conds = [self._in_window(inp, p.start_minute, p.end_minute)]
        conds.append(Condition("Scanner: tradable", bool(inp.scan and inp.scan.tradable),
                               "; ".join(inp.scan.blockers[:2]) if inp.scan and inp.scan.blockers else "ok"))
        conds.append(_ge("Scanner score", inp.scan.score if inp.scan else None, p.min_scan_score, ".0f"))
        conds.append(_ge("Volume vs baseline", ratio, p.surge_multiple, ".1f", "x"))
        conds.append(_ge("Price move in window", move, p.min_move_pct, ".2f", "%"))
        conds.append(Condition("Dollar volume in window", dollars >= p.min_surge_dollar_volume,
                               f"${dollars:,.0f} (need >= ${p.min_surge_dollar_volume:,.0f})"))
        conds.append(Condition("Distinct trading bars", active >= p.min_active_bars,
                               f"{active} (need >= {p.min_active_bars})"))
        conds.append(Condition("Above VWAP", m.vs_vwap_pct is not None and m.vs_vwap_pct > 0, f"{_fmt(m.vs_vwap_pct)}%"))
        return self._eval(inp, conds, ((ratio or 0) / 20 + (move or 0) / 8) / 2)

    def evaluate_exit(self, inp: SignalInput, position: Position) -> ExitDecision | None:
        ratio, move, _d, _a = self._surge_stats(inp)
        if ratio is not None and ratio < self.params.fade_volume_ratio and held_minutes(position, inp) >= 3:
            mom = inp.metrics.momentum_short
            if mom is not None and mom <= 0:
                return ExitDecision(f"Surge faded: volume {ratio:.1f}x baseline and momentum {mom:+.2f}%")
        return None


# --------------------------------------------------------------------------- E: custom weighted


class CustomWeightedParams(BaseModel):
    start_minute: float = Field(10, ge=0, le=300)
    end_minute: float = Field(180, ge=1, le=390)
    w_pct_change: float = Field(1.0, ge=0, le=10)
    w_rvol: float = Field(1.5, ge=0, le=10)
    w_momentum: float = Field(2.0, ge=0, le=10)
    w_vwap: float = Field(1.0, ge=0, le=10)
    w_near_high: float = Field(1.0, ge=0, le=10)
    w_liquidity: float = Field(0.5, ge=0, le=10)
    entry_threshold: float = Field(62.0, ge=0, le=100, description="Weighted score (0-100) needed to enter")
    exit_threshold: float = Field(30.0, ge=0, le=100, description="Exit when the score drops below this")
    require_positive_momentum: bool = True
    stop_loss_pct: float = Field(3.0, gt=0, le=20)
    take_profit_pct: float | None = Field(6.0, gt=0, le=100)
    trailing_stop_pct: float | None = Field(2.5, gt=0, le=20)
    max_hold_minutes: int | None = Field(120, ge=1, le=390)


class CustomWeighted(Strategy):
    id = "custom_weighted"
    name = "E - Custom Weighted"
    summary = "Combine normalised signals with your own weights; enter above one threshold, exit below another."
    entry_rules: ClassVar[list[str]] = [
        "Weighted score of % change, RVOL, momentum, VWAP, nearness to high and liquidity >= entry threshold",
        "Optionally requires positive short-term momentum; scanner-tradable",
    ]
    exit_rules: ClassVar[list[str]] = [
        "Weighted score falls below the exit threshold",
        "Hard exits: stop, target, trailing stop, time stop, end-of-day liquidation",
    ]
    risk_rules: ClassVar[list[str]] = ["Parameters fully user-controlled; hysteresis (entry > exit) avoids churn"]
    ParamsModel = CustomWeightedParams
    params: CustomWeightedParams

    def _score(self, inp: SignalInput) -> tuple[float, list[str]]:
        p, m = self.params, inp.metrics
        parts: list[tuple[str, float, float | None]] = [
            ("pct_change", p.w_pct_change, None if m.pct_change is None else sc._norm_pct(m.pct_change)),
            ("rvol", p.w_rvol, None if m.rvol is None else sc._norm_rvol(m.rvol)),
            ("momentum", p.w_momentum, None if m.momentum_short is None else sc._norm_mom(m.momentum_short)),
            ("vwap", p.w_vwap, None if m.vs_vwap_pct is None else sc._norm_vwap(m.vs_vwap_pct)),
            ("near_high", p.w_near_high, None if m.dist_from_high_pct is None else sc._norm_high(m.dist_from_high_pct)),
            ("liquidity", p.w_liquidity, sc._norm_liq(m.dollar_volume) if m.n_bars else None),
        ]
        avail = [(n, w, s) for n, w, s in parts if s is not None and w > 0]
        tw = sum(w for _n, w, _s in avail)
        if tw <= 0:
            return 0.0, []
        score = 100 * sum(w * s for _n, w, s in avail if s is not None) / tw
        return score, [f"{n}={s:.2f}x{w:g}" for n, w, s in avail if s is not None]

    def evaluate_entry(self, inp: SignalInput) -> Evaluation:
        p = self.params
        score, parts = self._score(inp)
        conds = [self._in_window(inp, p.start_minute, p.end_minute)]
        conds.append(Condition("Scanner: tradable", bool(inp.scan and inp.scan.tradable),
                               "; ".join(inp.scan.blockers[:2]) if inp.scan and inp.scan.blockers else "ok"))
        conds.append(Condition("Weighted score", score >= p.entry_threshold,
                               f"{score:.0f} (need >= {p.entry_threshold:g}) [{', '.join(parts)}]"))
        if p.require_positive_momentum:
            mom = inp.metrics.momentum_short
            conds.append(Condition("Positive short momentum", mom is not None and mom > 0, _fmt(mom, ".2f", "%")))
        return self._eval(inp, conds, score / 100)

    def evaluate_exit(self, inp: SignalInput, position: Position) -> ExitDecision | None:
        score, _ = self._score(inp)
        if score < self.params.exit_threshold:
            return ExitDecision(f"Weighted score {score:.0f} fell below exit threshold {self.params.exit_threshold:g}")
        return None


STRATEGY_CLASSES: dict[str, type[Strategy]] = {
    c.id: c for c in (OpeningMomentum, GapContinuation, MomentumBreakout, VolumeSurge, CustomWeighted)
}


def build_strategy(strategy_id: str, params: dict[str, Any] | None = None) -> Strategy:
    if strategy_id not in STRATEGY_CLASSES:
        raise KeyError(f"unknown strategy {strategy_id!r}; available: {sorted(STRATEGY_CLASSES)}")
    return STRATEGY_CLASSES[strategy_id](params)
