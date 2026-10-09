"""Two trading modes, live settings changes, and bot controls (pause / stop / emergency) on scripted days."""

from __future__ import annotations

import pytest

from mwbot.config import AppSettings, TradingMode, preset_profile
from mwbot.domain import OrderType
from mwbot.trading.risk import BotState

from .conftest import TEST_DAY, build_day  # noqa: F401
from .test_opportunity_bot import (
    PRE,
    ScriptedForecaster,
    classify_exit,
    drift_then_drop,
    run,
    settings_for,
)

MER, AGG = TradingMode.MAX_EXPECTED_RETURN, TradingMode.AGGRESSIVE_UPSIDE


def flat_after_entry(sess, p1: float = 10.12):
    return build_day(
        sess, 10.0, [(int(PRE + 40), 10.0, 60_000), (10, p1, 150_000), (340, p1, 90_000)], base_volume=60_000
    )


# ------------------------------------------------------------------ presets
def test_default_mode_is_max_expected_return_and_works_without_configuration():
    s = AppSettings()
    assert s.trading.mode == MER
    assert s.strategies.enabled == ["expected_opportunity"]
    assert s.ui.simple_mode is True


def test_both_modes_keep_the_safety_rails():
    for m in TradingMode:
        p = preset_profile(m)
        assert 0 < p.risk.max_daily_loss_pct <= 10
        assert 0 < p.risk.stop_loss_pct <= 10
        assert p.plan.end_buffer_minutes >= 10  # always exits before the close
        assert p.risk.max_position_pct <= 50  # neither mode can concentrate the whole account
    a, m = preset_profile(AGG), preset_profile(MER)
    assert a.risk.max_position_pct > m.risk.max_position_pct  # aggressive = a different sizing policy
    assert a.objective.w_upside > m.objective.w_upside


def test_switching_modes_round_trips_and_keeps_capital():
    s = settings_for(starting_capital=75_000)
    agg = s.with_mode(AGG)
    assert agg.trading.mode == AGG and agg.risk.max_position_pct == preset_profile(AGG).risk.max_position_pct
    assert agg.risk.starting_capital == 75_000
    back = agg.with_mode(MER)
    assert back.risk == s.risk and back.trading.mode == MER
    assert s.trading.mode == MER  # with_mode never mutates the original


def test_profile_for_returns_each_modes_own_settings():
    s = settings_for()
    assert s.profile_for(AGG).risk.max_position_pct == preset_profile(AGG).risk.max_position_pct
    assert s.profile_for().mode == MER


# ------------------------------------------------------------------ switching with open positions
def test_mode_switch_keeps_history_and_open_positions_with_their_original_plan(sess):
    s = settings_for()
    switched = []
    snap = {}

    def hook(runner, bot):
        if bot.broker.positions and not switched:
            pos = next(iter(bot.broker.positions.values()))
            snap.update(
                stop=pos.stop_price, target=pos.take_profit_price, mode=pos.mode, deadline=pos.deadline
            )
            bot.apply_settings(s.with_mode(AGG))
            switched.append(True)

    bot, broker, _ = run(
        {"TST": flat_after_entry(sess)}, settings=s, hook=hook, forecaster=ScriptedForecaster(3.0)
    )
    assert switched and snap["mode"] == MER.value
    assert snap["stop"] and snap["target"] and snap["deadline"]
    assert broker.trades  # the trade completed and is in history
    assert broker.trades[0].strategy == "expected_opportunity"
    assert any("Mode changed" in n["message"] for n in bot.decision_log)
    assert bot.settings.trading.mode == AGG


def test_new_entries_after_a_switch_use_the_new_modes_sizing(sess):
    big, small = {}, {}

    def make_hook(store):
        def hook(runner, bot):
            for p in bot.broker.positions.values():
                store.setdefault("value", p.quantity * p.avg_cost)
                store["equity"] = bot.stats.start_equity

        return hook

    _, _, _ = run(
        {"TST": flat_after_entry(sess)},
        settings=settings_for(),
        hook=make_hook(small),
        forecaster=ScriptedForecaster(3.0),
    )
    _, _, _ = run(
        {"TST": flat_after_entry(sess)},
        settings=settings_for().with_mode(AGG),
        hook=make_hook(big),
        forecaster=ScriptedForecaster(3.0),
    )
    assert small and big
    assert big["value"] / big["equity"] > small["value"] / small["equity"]
    assert big["value"] / big["equity"] <= preset_profile(AGG).risk.max_position_pct / 100 + 0.01
    assert small["value"] / small["equity"] <= preset_profile(MER).risk.max_position_pct / 100 + 0.01


def test_tighten_only_policy_never_loosens_an_open_position(sess):
    s = settings_for()
    s.trading.existing_positions_policy = "tighten_only"
    seen = {}

    def hook(runner, bot):
        if bot.broker.positions and "before" not in seen:
            pos = next(iter(bot.broker.positions.values()))
            seen["before"] = (pos.stop_price, pos.trailing_stop_pct)
            looser = s.with_mode(AGG)
            looser.risk.stop_loss_pct = 9.0
            looser.trading.profiles[AGG.value].plan.trailing_pct = 9.0
            bot.apply_settings(looser)
            seen["after"] = (pos.stop_price, pos.trailing_stop_pct)

    run({"TST": flat_after_entry(sess)}, settings=s, hook=hook, forecaster=ScriptedForecaster(3.0))
    b_stop, b_trail = seen["before"]
    a_stop, a_trail = seen["after"]
    assert a_stop is not None and b_stop is not None and a_stop >= b_stop
    assert (a_trail or 0) <= (b_trail or 99) or b_trail is None


# ------------------------------------------------------------------ controls
def test_pause_blocks_entries_but_not_exits_and_resume_restores_them(sess):
    state = {}

    def hook(runner, bot):
        if bot.broker.positions and "paused" not in state:
            bot.pause()
            state["paused"] = bot.state

    bot, broker, _ = run({"TST": flat_after_entry(sess)}, hook=hook, forecaster=ScriptedForecaster(3.0))
    assert state["paused"] == BotState.PAUSED
    assert broker.trades and not broker.positions  # the open position was still closed while paused
    n_orders = len([o for o in broker.orders.values() if o.side.value == "buy"])
    assert n_orders == 1  # and nothing new was bought during the pause


def test_pause_then_resume_allows_a_later_entry(sess):
    def hook(runner, bot):
        if runner.now == at_minute(sess, 5) and bot.state == BotState.RUNNING:
            bot.pause()
        if runner.now == at_minute(sess, 35):
            bot.resume()

    bot, broker, _ = run({"TST": flat_after_entry(sess)}, hook=hook, forecaster=ScriptedForecaster(3.0))
    assert broker.orders  # entered only after the resume
    first = min(o.created_at for o in broker.orders.values())
    assert first >= at_minute(sess, 35)


def at_minute(sess, m):
    from datetime import timedelta

    return sess.open_utc + timedelta(minutes=m)


def test_stop_ends_new_trades_but_keeps_managing_the_open_position(sess):
    done = {}

    def hook(runner, bot):
        if bot.broker.positions and not done:
            bot.stop()
            done["t"] = runner.now

    bot, broker, _ = run({"TST": flat_after_entry(sess)}, hook=hook, forecaster=ScriptedForecaster(3.0))
    assert done and not broker.positions and broker.trades  # monitoring/risk management continued to flat
    assert bot.state == BotState.STOPPED
    assert len([o for o in broker.orders.values() if o.side.value == "buy"]) == 1


@pytest.mark.parametrize("behavior,expect_flat_early", [("flatten_all", True), ("block_entries_only", False)])
def test_emergency_stop_behaviour_is_the_configured_one(sess, behavior, expect_flat_early):
    s = settings_for()
    s.trading.emergency_behavior = behavior
    out = {}

    def hook(runner, bot):
        if bot.broker.positions and "t" not in out:
            bot.emergency_stop()
            out["t"] = runner.now

    bot, broker, _ = run(
        {"TST": flat_after_entry(sess)}, settings=s, hook=hook, forecaster=ScriptedForecaster(3.0)
    )
    assert bot.state == BotState.EMERGENCY_STOP
    t = broker.trades[0]
    if expect_flat_early:
        assert classify_exit(t.exit_reason) == "emergency"
    else:
        assert classify_exit(t.exit_reason) != "emergency"  # the normal exit rules took it out instead
    assert len([o for o in broker.orders.values() if o.side.value == "buy"]) == 1  # nothing bought afterwards
    assert any(a["code"] == "emergency_stop" for a in bot.alerts)


def test_emergency_stop_must_be_cleared_before_restarting():
    from mwbot.bot.engine import TradingBot
    from mwbot.trading.broker import PaperBroker

    s = settings_for()
    bot = TradingBot(s, PaperBroker(s.risk.starting_capital, s.costs))
    bot.emergency_stop()
    with pytest.raises(RuntimeError):
        bot.start()
    bot.reset_emergency()
    assert bot.state == BotState.STOPPED
    bot.start()
    assert bot.state == BotState.RUNNING


# ------------------------------------------------------------------ risk limits and game rules
def test_position_size_never_exceeds_the_configured_cap(sess):
    s = settings_for(max_position_pct=10.0)
    peak = {}

    def hook(runner, bot):
        for p in bot.broker.positions.values():
            peak["v"] = max(peak.get("v", 0), p.quantity * p.avg_cost)

    run({"TST": flat_after_entry(sess)}, settings=s, hook=hook, forecaster=ScriptedForecaster(3.0))
    assert 0 < peak["v"] <= 0.10 * 100_000 * 1.01


def test_game_position_limit_tightens_the_users_limit(sess):
    s = settings_for(max_position_pct=40.0)
    s.game.max_position_pct = 5.0
    assert s.effective_risk().max_position_pct == 5.0 and s.risk.max_position_pct == 40.0
    peak = {}

    def hook(runner, bot):
        for p in bot.broker.positions.values():
            peak["v"] = max(peak.get("v", 0), p.quantity * p.avg_cost)

    run({"TST": flat_after_entry(sess)}, settings=s, hook=hook, forecaster=ScriptedForecaster(3.0))
    assert 0 < peak["v"] <= 0.05 * 100_000 * 1.01


def test_game_that_only_allows_limit_orders_gets_marketable_limit_orders(sess):
    s = settings_for()
    s.game.supported_order_types = ["limit"]
    bot, broker, _ = run({"TST": flat_after_entry(sess)}, settings=s, forecaster=ScriptedForecaster(3.0))
    assert broker.orders and all(o.order_type == OrderType.LIMIT for o in broker.orders.values())
    assert broker.trades  # and they still filled (marketable) and completed a round trip


def test_game_commissions_are_charged(sess):
    s = settings_for()
    s.game.commission_per_trade = 4.95
    bot, broker, _ = run({"TST": flat_after_entry(sess)}, settings=s, forecaster=ScriptedForecaster(3.0))
    assert broker.trades and broker.trades[0].commissions >= 2 * 4.95 - 1e-6


def test_daily_loss_limit_flattens_and_blocks_further_entries(sess):
    s = settings_for(max_daily_loss_pct=0.2, max_position_pct=40.0)
    bot, broker, _ = run(
        {"TST": drift_then_drop(sess, 40, 10.1, 9.0)}, settings=s, forecaster=ScriptedForecaster(3.0)
    )
    assert bot.daily_halted or broker.trades
    assert not broker.positions
    assert any(a["code"] == "daily_loss_limit" for a in bot.alerts) or any(
        classify_exit(t.exit_reason) in ("risk", "limit") for t in broker.trades
    )


# ------------------------------------------------------------------ live settings
def test_changing_a_threshold_while_running_applies_to_the_next_decision(sess):
    s = settings_for()
    changed = []

    def hook(runner, bot):
        if runner.now == at_minute(sess, 20) and not changed:
            tougher = s.model_copy(deep=True)
            tougher.trading.profiles[
                MER.value
            ].gates.min_expected_net_return_pct = 9.0  # nothing can clear it
            tougher.risk = tougher.risk.model_copy()
            bot.apply_settings(tougher)
            changed.append(True)

    bot, broker, _ = run(
        {"TST": flat_after_entry(sess)}, settings=s, hook=hook, forecaster=ScriptedForecaster(3.0)
    )
    assert changed and not broker.orders  # the new, stricter rule blocked what the old rule would have bought


def test_cooldown_blocks_an_immediate_reentry_after_a_stop_out(sess):
    s = settings_for()
    s.trading.profiles[MER.value].turnover.cooldown_minutes = 600  # effectively the rest of the day
    bars = build_day(
        sess,
        10.0,
        [
            (int(PRE + 40), 10.0, 60_000),
            (6, 10.1, 120_000),
            (12, 9.2, 150_000),  # stopped out
            (30, 10.0, 150_000),  # tempting rebound afterwards
        ],
        base_volume=60_000,
    )
    bot, broker, _ = run({"TST": bars}, settings=s, forecaster=ScriptedForecaster(3.0))
    buys = [o for o in broker.orders.values() if o.side.value == "buy"]
    assert len(broker.trades) == 1 and len(buys) == 1
