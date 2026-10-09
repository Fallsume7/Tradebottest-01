"""Each strategy against representative price sequences: entries, rejections, exits, interface."""

import pytest
from pydantic import ValidationError

from mwbot.domain import Position
from mwbot.scanner.scanner import Scanner
from mwbot.strategies.builtin import (
    STRATEGY_CLASSES,
    CustomWeighted,
    GapContinuation,
    MomentumBreakout,
    OpeningMomentum,
    VolumeSurge,
    build_strategy,
)

from .conftest import at, build_day, make_source, meta, relaxed_settings
from .scenarios import evaluate, fader, inp_for, passed_labels, runner_day, source_for, strong_runner


def sc() -> Scanner:
    return Scanner(relaxed_settings().scanner)


def position(session, strategy, minutes=20, **kw):
    base = dict(
        symbol="TST",
        quantity=100,
        avg_cost=11.5,
        opened_at=at(session, minutes),
        strategy=strategy,
        high_water_mark=11.8,
    )
    base.update(kw)
    return Position(**base)


# ----------------------------------------------------------------------------- A: opening momentum
def test_opening_momentum_enters_strong_drive(session_info):
    src = source_for(session_info, TST=strong_runner(session_info))
    ev = evaluate(OpeningMomentum(), src, "TST", 18, sc())
    assert ev.passed, [c.label for c in ev.failed()]
    assert ev.stop_loss_pct == 2.5 and ev.take_profit_pct == 5.0 and "Open drive" in ev.explanation()


def test_opening_momentum_respects_entry_window(session_info):
    src = source_for(session_info, TST=strong_runner(session_info))
    early = evaluate(OpeningMomentum(), src, "TST", 6, sc())
    assert not early.passed and not passed_labels(early)["Entry window (10-60 min after open)"]
    late = evaluate(OpeningMomentum({"end_minute": 12}), src, "TST", 18, sc())
    assert not late.passed


def test_opening_momentum_rejects_fading_stock(session_info):
    src = source_for(session_info, TST=fader(session_info))
    ev = evaluate(OpeningMomentum(), src, "TST", 40, sc())
    assert not ev.passed
    failed = {c.label for c in ev.failed()}
    assert failed & {"Above VWAP", "Near opening-range high", "Short momentum %", "Open drive %"}


def test_opening_momentum_rvol_check_uses_threshold(session_info):
    bars = strong_runner(session_info)
    src = make_source(session_info, {"TST": (meta(adv=60_000_000), bars)})  # huge ADV -> RVOL < 1
    ev = evaluate(
        OpeningMomentum({"min_rvol": 2.0}),
        src,
        "TST",
        18,
        Scanner(
            relaxed_settings().scanner.model_copy(
                update={"filters": relaxed_settings().scanner.filters.model_copy(update={"min_rvol": 0.0})}
            )
        ),
    )
    assert not passed_labels(ev)["Relative volume"] and not ev.passed


def test_opening_momentum_exit_when_momentum_lost(session_info):
    fade_day = runner_day(session_info, [(15, 11.7, 28_000), (25, 10.9, 20_000)])
    src = source_for(session_info, TST=fade_day)
    s = OpeningMomentum()
    assert s.evaluate_exit(inp_for(src, "TST", 15, sc()), position(session_info, s.id)) is None
    d = s.evaluate_exit(inp_for(src, "TST", 38, sc()), position(session_info, s.id))
    assert d is not None and "Momentum lost" in d.reason


# ----------------------------------------------------------------------------- B: gap continuation
def test_gap_continuation_enters_holding_gap(session_info):
    src = source_for(session_info, TST=strong_runner(session_info))
    ev = evaluate(GapContinuation(), src, "TST", 15, sc())
    assert ev.passed, [(c.label, c.detail) for c in ev.failed()]


def test_gap_continuation_rejects_small_gap_and_thin_premarket(session_info):
    src = make_source(
        session_info, {"TST": (meta(prev_close=10.4), runner_day(session_info, [(20, 11.5, 20_000)]))}
    )
    ev = evaluate(GapContinuation(), src, "TST", 15, sc())
    assert not passed_labels(ev)["Gap vs previous close"]  # 10.95 vs prev close 10.4 = 5.3% < 8%
    thin = evaluate(
        GapContinuation({"min_premarket_volume": 5_000_000}),
        source_for(session_info, TST=strong_runner(session_info)),
        "TST",
        15,
        sc(),
    )
    assert not passed_labels(thin)["Premarket volume"]


def test_gap_continuation_rejects_failed_gap_and_exits_on_gap_fail(session_info):
    failing = runner_day(session_info, [(8, 10.2, 30_000), (30, 10.0, 20_000)])
    src = source_for(session_info, TST=failing)
    ev = evaluate(GapContinuation(), src, "TST", 15, sc())
    assert not ev.passed and not passed_labels(ev)["Holding the regular open"]
    s = GapContinuation()
    d = s.evaluate_exit(inp_for(src, "TST", 20, sc()), position(session_info, s.id, minutes=5))
    assert d is not None and "Gap failed" in d.reason


# ----------------------------------------------------------------------------- C: breakout
def breakout_day(session, volume_at_break=60_000, base_range=0.0, seed=2):
    # 30 min rally, 40 min tight base near 11.3, then a one-minute break to 11.6
    return runner_day(
        session,
        [(30, 11.3, 20_000), (40, 11.3, 8_000), (1, 11.6, volume_at_break), (10, 11.65, 15_000)],
        seed=seed,
    )


def test_breakout_enters_volume_confirmed_break(session_info):
    src = source_for(session_info, TST=breakout_day(session_info))
    s = MomentumBreakout()
    ev = evaluate(s, src, "TST", 71, sc())
    assert ev.passed, [(c.label, c.detail) for c in ev.failed()]
    assert ev.reference["breakout_level"] > 11.2


def test_breakout_needs_volume_confirmation(session_info):
    src = source_for(session_info, TST=breakout_day(session_info, volume_at_break=8_500))
    ev = evaluate(MomentumBreakout(), src, "TST", 71, sc())
    assert not ev.passed and not passed_labels(ev)["Breakout volume vs avg"]


def test_breakout_not_triggered_before_the_break(session_info):
    src = source_for(session_info, TST=breakout_day(session_info))
    ev = evaluate(MomentumBreakout(), src, "TST", 69, sc())  # base still intact
    assert not passed_labels(ev)["Close above prior-high + buffer"]


def test_breakout_rejects_wide_range(session_info):
    wide = runner_day(session_info, [(30, 12.8, 20_000), (40, 11.0, 8_000), (1, 13.2, 60_000)])
    src = source_for(session_info, TST=wide)
    ev = evaluate(MomentumBreakout({"max_range_pct": 5}), src, "TST", 71, sc())
    assert not passed_labels(ev)["Contained base"]


def test_breakout_failed_breakout_exit(session_info):
    day = runner_day(
        session_info, [(30, 11.3, 20_000), (40, 11.3, 8_000), (1, 11.6, 60_000), (5, 11.0, 30_000)]
    )
    src = source_for(session_info, TST=day)
    s = MomentumBreakout()
    pos = position(session_info, s.id, minutes=71, reference={"breakout_level": 11.35})
    assert s.evaluate_exit(inp_for(src, "TST", 72, sc()), pos) is None  # held <2 min
    d = s.evaluate_exit(inp_for(src, "TST", 76, sc()), pos)
    assert d is not None and "Failed breakout" in d.reason


# ----------------------------------------------------------------------------- D: volume surge
def surge_day(session, move_to=11.5):
    return runner_day(session, [(60, 11.0, 10_000), (3, move_to, 120_000), (10, move_to, 20_000)])


def test_volume_surge_enters_real_surge(session_info):
    src = source_for(session_info, TST=surge_day(session_info))
    ev = evaluate(VolumeSurge(), src, "TST", 63, sc())
    assert ev.passed, [(c.label, c.detail) for c in ev.failed()]


def test_volume_surge_rejects_surge_without_price_move(session_info):
    src = source_for(session_info, TST=surge_day(session_info, move_to=11.0))
    ev = evaluate(VolumeSurge(), src, "TST", 63, sc())
    assert not ev.passed and not passed_labels(ev)["Price move in window"]


def test_volume_surge_rejects_single_print_spike(session_info):
    bars = surge_day(session_info)
    sparse = bars.drop(
        bars.index[
            [(i) for i, ts in enumerate(bars.index) if ts in (at(session_info, 60), at(session_info, 61))]
        ]
    )
    src = source_for(session_info, TST=sparse)
    ev = evaluate(VolumeSurge(), src, "TST", 63, sc())
    labels = passed_labels(ev)
    assert not labels["Distinct trading bars"] and not ev.passed


def test_volume_surge_rejects_thin_dollar_volume(session_info):
    tiny = runner_day(session_info, [(60, 11.0, 100), (3, 11.5, 1_200), (10, 11.5, 200)])
    src = source_for(session_info, TST=tiny)
    ev = evaluate(VolumeSurge(), src, "TST", 63, sc())
    assert not passed_labels(ev)["Dollar volume in window"]


def test_volume_surge_exit_when_volume_fades(session_info):
    day = runner_day(
        session_info, [(60, 11.0, 10_000), (3, 11.5, 120_000), (4, 11.4, 2_000), (10, 11.3, 1_500)]
    )
    src = source_for(session_info, TST=day)
    s = VolumeSurge({"baseline_window": 30})
    d = s.evaluate_exit(inp_for(src, "TST", 75, sc()), position(session_info, s.id, minutes=63))
    assert d is not None and "Surge faded" in d.reason


# ----------------------------------------------------------------------------- E: custom
def test_custom_weighted_thresholds_and_weights(session_info):
    src = source_for(session_info, TST=strong_runner(session_info))
    base = evaluate(CustomWeighted(), src, "TST", 18, sc())
    assert base.passed, [(c.label, c.detail) for c in base.conditions]
    strict = evaluate(CustomWeighted({"entry_threshold": 99}), src, "TST", 18, sc())
    assert not strict.passed
    # putting all weight on a component that is weak changes the score
    only_liq = evaluate(
        CustomWeighted(
            {"w_pct_change": 0, "w_rvol": 0, "w_momentum": 0, "w_vwap": 0, "w_near_high": 0, "w_liquidity": 1}
        ),
        src,
        "TST",
        18,
        sc(),
    )
    assert only_liq.conditions[2].detail != base.conditions[2].detail


def test_custom_weighted_exit_threshold(session_info):
    day = runner_day(session_info, [(15, 11.7, 28_000), (25, 10.3, 25_000)])
    src = source_for(session_info, TST=day)
    s = CustomWeighted({"exit_threshold": 45})
    assert s.evaluate_exit(inp_for(src, "TST", 14, sc()), position(session_info, s.id)) is None
    d = s.evaluate_exit(inp_for(src, "TST", 38, sc()), position(session_info, s.id))
    assert d is not None and "exit threshold" in d.reason


# ----------------------------------------------------------------------------- interface
@pytest.mark.parametrize("sid", sorted(STRATEGY_CLASSES))
def test_every_strategy_documents_itself_and_has_risk_controls(sid):
    s = build_strategy(sid)
    d = s.describe()
    assert d["entry_rules"] and d["exit_rules"] and d["risk_rules"] and d["params_schema"]
    assert "guarantee" in d["disclaimer"].lower()
    if sid == "expected_opportunity":
        # plan-based: the stop is derived per trade from volatility (see forecast/plan.py), not a fixed percent
        assert d["risk_rules"]
    else:
        assert s.params.stop_loss_pct > 0  # every other strategy defines a fixed stop


@pytest.mark.parametrize("sid", sorted(STRATEGY_CLASSES))
def test_every_strategy_survives_empty_and_tiny_histories(sid, session_info):
    bars = build_day(session_info, 10.0, [])
    src = make_source(session_info, {"TST": (meta(), bars)})
    s = build_strategy(sid)
    for minutes in (-300, -1, 0, 1, 3):
        ev = s.evaluate_entry(inp_for(src, "TST", minutes, sc()))
        assert ev.passed is False  # nothing to trade -> never a signal, never an exception


def test_parameter_validation_and_unknown_strategy():
    with pytest.raises(ValidationError):
        OpeningMomentum({"stop_loss_pct": -1})
    with pytest.raises(ValidationError):
        VolumeSurge({"surge_multiple": 0.5})
    with pytest.raises(KeyError):
        build_strategy("magic")
    assert OpeningMomentum({"min_open_drive_pct": 7}).params.min_open_drive_pct == 7
