"""The 'most money the bot may use' cap, exercised through a whole scripted trading day."""

from __future__ import annotations

from mwbot.config import AppSettings

from .test_opportunity_bot import ScriptedForecaster, drift_up, run, settings_for


def _settings(budget: float | None) -> AppSettings:
    s = settings_for(max_position_pct=100, risk_per_trade_pct=10, capital_budget=budget)
    return s


def _peak_invested(sess, budget: float | None) -> tuple[float, int]:
    """Highest cost basis (positions + pending buys) over the day, and the number of trades."""
    peak = [0.0]

    def hook(_runner, bot) -> None:
        b = bot.broker
        basis = sum(p.quantity * p.avg_cost for p in b.positions.values()) + b.reserved_cash()
        peak[0] = max(peak[0], basis)

    bars = {"AAA": drift_up(sess, 40, 11.2, 60), "BBB": drift_up(sess, 45, 11.4, 60, p0=12.0)}
    _bot, broker, _ = run(bars, settings=_settings(budget), forecaster=ScriptedForecaster(1.5), hook=hook)
    return peak[0], len(broker.trades)


def test_bot_never_invests_more_than_its_budget(sess):
    free_peak, free_trades = _peak_invested(sess, None)
    assert free_trades >= 1 and free_peak > 20_000  # sanity: without a cap it deploys real money

    capped_peak, capped_trades = _peak_invested(sess, 5_000)
    assert capped_trades >= 1, "a $5,000 budget should still allow trades"
    assert 0 < capped_peak <= 5_000 + 1e-6


def test_a_budget_does_not_sell_what_is_already_open(sess):
    """Lowering the budget below what is invested only blocks NEW entries; the position keeps its plan."""
    bars = {"AAA": drift_up(sess, 40, 11.2, 60)}
    s = settings_for(max_position_pct=50, risk_per_trade_pct=10)
    seen = {"lowered": False, "held_after": False}

    def hook(runner, bot) -> None:
        if bot.broker.positions and not seen["lowered"]:
            bot.settings.risk.capital_budget = 100.0
            bot.risk.cfg.capital_budget = 100.0
            seen["lowered"] = True
        elif seen["lowered"] and bot.broker.positions:
            seen["held_after"] = True

    _bot, broker, _ = run(bars, settings=s, forecaster=ScriptedForecaster(1.5), hook=hook)
    assert seen["lowered"] and seen["held_after"]
    assert len(broker.trades) == 1  # exited by its own plan (target/stop/close), no extra entries
