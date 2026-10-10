"""The predictive (expected-opportunity) bot on scripted days: the scenarios the product must get right.

Most tests inject a ``ScriptedForecaster`` so they exercise the *engine* (gates, plans, exits, eligibility,
turnover) deterministically. The ranking-quality tests use the real heuristic forecaster instead.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest

from mwbot.bot.engine import TradingBot, classify_exit
from mwbot.bot.runner import SessionRunner
from mwbot.calendar import build_schedule, session_for
from mwbot.config import AppSettings, StrategySelection
from mwbot.eligibility.models import EligibilityRecord, EligibilityStatus
from mwbot.eligibility.universe import EligibilityUniverse
from mwbot.forecast.features import Features, market_state
from mwbot.forecast.models import Forecast, Forecaster, norm_cdf, sigma_horizon
from mwbot.forecast.ranking import OpportunityRanker
from mwbot.scanner.scanner import Scanner
from mwbot.trading.broker import PaperBroker

from .conftest import TEST_DAY, at, build_day, make_source, meta, relaxed_settings

T = EligibilityStatus.CONFIRMED_TRADABLE
U = EligibilityStatus.CONFIRMED_UNAVAILABLE
V = EligibilityStatus.UNVERIFIED
PRE = 330  # minutes of premarket before the regular open in build_day()


class ScriptedForecaster(Forecaster):
    """Returns a chosen gross expected return (%) per symbol / per moment; deliberately NOT a real model."""

    name = "scripted-test"
    kind = "heuristic"

    def __init__(self, mu: dict[str, float | Callable[[Features], float]] | float = 1.5) -> None:
        self.mu = mu

    def predict(self, f: Features, horizon: float, cost_pct: float) -> Forecast:
        v = self.mu.get(f.symbol, 0.0) if isinstance(self.mu, dict) else self.mu
        mu = v(f) if callable(v) else v
        sh = sigma_horizon(f, horizon)
        return Forecast(
            symbol=f.symbol,
            horizon_minutes=horizon,
            expected_return_pct=mu,
            expected_net_return_pct=mu - cost_pct,
            sigma_pct=sh,
            p_positive=norm_cdf((mu - cost_pct) / sh),
            upside_pct=mu + 1.28 * sh,
            downside_pct=mu - 1.28 * sh,
            cost_pct=cost_pct,
            method=self.name,
            kind=self.kind,
            calibrated=False,
            confidence=None,
        )


def universe(*pairs: tuple[str, EligibilityStatus]) -> EligibilityUniverse:
    return EligibilityUniverse([EligibilityRecord(t, s) for t, s in pairs])


def settings_for(**risk: object) -> AppSettings:
    s = relaxed_settings(**risk)
    s.strategies = StrategySelection(enabled=["expected_opportunity"], params={})
    s.schedule.scanner_refresh_minutes = 2
    s.schedule.monitor_minutes = 1
    return s


def run(
    bars: dict[str, object],
    *,
    elig: EligibilityUniverse | None = None,
    forecaster: Forecaster | None = None,
    settings: AppSettings | None = None,
    hook: Callable[[SessionRunner, TradingBot], None] | None = None,
    halts: dict | None = None,
    stop_at_minute: float | None = None,
    mirror: object | None = None,
):
    settings = settings or settings_for()
    sess = session_for(TEST_DAY)
    assert sess is not None
    src = make_source(sess, {s: (meta(s), b) for s, b in bars.items()}, halts=halts)
    broker = PaperBroker(settings.risk.starting_capital, settings.costs)
    uni = elig or universe(*[(s, T) for s in bars])
    bot = TradingBot(
        settings,
        broker,
        mirror=mirror,
        eligibility=uni,
        forecaster=forecaster or ScriptedForecaster(),  # type: ignore[arg-type]
    )
    bot.start()
    sched = build_schedule(TEST_DAY, settings.schedule)
    runner = SessionRunner(bot, src, sched)
    while not runner.finished:
        if stop_at_minute is not None and runner.now >= at(sess, stop_at_minute):
            break
        if hook:
            hook(runner, bot)
        runner.step()
    return bot, broker, runner


def drift_up(sess, start_min: float, to: float, minutes: int = 30, volume: float = 60_000, p0: float = 10.0):
    """Flat at p0 until `start_min` minutes after the open, then ramps to `to`."""
    return build_day(
        sess, p0, [(int(PRE + start_min), p0, volume), (minutes, to, volume * 2)], base_volume=volume
    )


def exits(bot: TradingBot):
    return [n for n in bot.decision_log if n["kind"] == "trade"]


# ------------------------------------------------------------------ 1+2: ranking is predictive, not "top gainer"
def _rank_at(sess, bars: dict[str, object], minute: float, forecaster=None):
    src = make_source(sess, {s: (meta(s), b) for s, b in bars.items()})
    ctxs = src.contexts_at(at(sess, minute))
    uni = universe(*[(s, T) for s in bars])
    scans = Scanner().scan(ctxs, uni, None)
    mkt = market_state([r.metrics for r in scans])
    s = settings_for()
    ranker = OpportunityRanker(forecaster)
    return {
        o.symbol: o for o in ranker.rank(ctxs, scans, mkt, s.profile_for(), s.effective_costs(), held=set())
    }


def _stalled_winner(sess):
    """Gapped +30% on yesterday's news, ran hard, and is now rolling over below its high."""
    return build_day(
        sess,
        13.0,
        [
            (int(PRE + 5), 14.6, 400_000),
            (25, 15.4, 300_000),
            (40, 14.7, 120_000),
            (40, 14.0, 90_000),
        ],
        base_volume=150_000,
    )


def _quiet_turner(sess):
    """Flat for an hour, then starting to lift on rising volume with a clean breakout."""
    return build_day(
        sess,
        10.0,
        [(int(PRE + 50), 10.0, 40_000), (25, 10.22, 70_000), (20, 10.34, 95_000)],
        base_volume=40_000,
    )


def test_yesterdays_biggest_winner_is_a_poor_current_opportunity(sess):
    bars = {"WINR": _stalled_winner(sess), "TURN": _quiet_turner(sess)}
    ops = _rank_at(sess, bars, 100)
    w, t = ops["WINR"], ops["TURN"]
    assert w.scan.metrics.pct_change is not None and w.scan.metrics.pct_change > 20  # it IS the top gainer...
    assert w.forecast is not None and t.forecast is not None
    assert t.forecast.expected_net_return_pct > w.forecast.expected_net_return_pct  # ...but not the best bet
    assert t.utility > w.utility
    assert not w.forecast.calibrated  # and the model says plainly that it is an uncalibrated heuristic
    assert "heuristic" in w.forecast.label


def test_flat_stock_can_have_stronger_predicted_upside_than_the_top_gainer(sess):
    bars = {"HOT": _stalled_winner(sess), "FLAT": _quiet_turner(sess)}
    ops = _rank_at(sess, bars, 100)
    order = sorted(ops.values(), key=lambda o: -o.utility)
    assert order[0].symbol == "FLAT"
    flat = ops["FLAT"]
    assert abs(flat.scan.metrics.pct_change or 0) < 5  # barely moved on the day
    assert flat.forecast.expected_return_pct > ops["HOT"].forecast.expected_return_pct


def test_ranking_does_not_depend_on_daily_percent_gain(sess):
    """Same recent path, different overnight gap: the forecast must not reward the bigger daily gain."""
    base = _quiet_turner(sess)
    src_a = make_source(sess, {"A": (meta("A", prev_close=10.0), base)})
    src_b = make_source(sess, {"B": (meta("B", prev_close=7.0), base)})  # looks like +40% on the day
    ctx_a, ctx_b = (s.contexts_at(at(sess, 100))[0] for s in (src_a, src_b))
    scanner = Scanner()
    uni = universe(("A", T), ("B", T))
    ra, rb = scanner.scan_one(ctx_a, uni, None), scanner.scan_one(ctx_b, uni, None)
    assert (rb.metrics.pct_change or 0) > 30 > (ra.metrics.pct_change or 0)
    s = settings_for()
    ranker = OpportunityRanker()
    mk = market_state([])
    oa = ranker.evaluate(ctx_a, ra, mk, s.profile_for(), s.effective_costs())
    ob = ranker.evaluate(ctx_b, rb, mk, s.profile_for(), s.effective_costs())
    assert oa.forecast is not None and ob.forecast is not None
    assert ob.forecast.expected_return_pct <= oa.forecast.expected_return_pct + 1e-9


# ------------------------------------------------------------------ 3: target reached early
def test_profit_target_closes_the_trade_early(sess):
    bot, broker, _ = run({"TST": drift_up(sess, 40, 11.2, 60)}, forecaster=ScriptedForecaster(1.5))
    ts = broker.trades
    assert ts, [n["message"] for n in bot.decision_log][-15:]
    t = ts[0]
    assert classify_exit(t.exit_reason) == "target" and t.net_pnl > 0
    sched = build_schedule(TEST_DAY, bot.settings.schedule)
    assert t.exit_time < sched.liquidation_start  # sold on the target, not held into the close
    assert t.exit_time < t.entry_time + timedelta(minutes=120)


# ------------------------------------------------------------------ 4: stop-loss
def drift_then_drop(sess, start_min: float, up_to: float, down_to: float, p0: float = 10.0):
    return build_day(
        sess,
        p0,
        [(int(PRE + start_min), p0, 60_000), (6, up_to, 120_000), (30, down_to, 150_000)],
        base_volume=60_000,
    )


def test_stop_loss_exits_a_trade_that_goes_wrong(sess):
    bot, broker, _ = run(
        {"TST": drift_then_drop(sess, 40, 10.1, 9.2)}, forecaster=ScriptedForecaster({"TST": 1.5})
    )
    assert broker.trades, [n["message"] for n in bot.decision_log][-12:]
    t = broker.trades[0]
    assert classify_exit(t.exit_reason) in ("risk", "forecast") and t.net_pnl < 0
    assert t.exit_price > 9.0  # the loss was cut early, not carried to the close
    sched = build_schedule(TEST_DAY, bot.settings.schedule)
    assert t.exit_time < sched.liquidation_start


# ------------------------------------------------------------------ 5: predictions change during the session
def test_forecast_changes_as_the_session_unfolds(sess):
    bars = {"TST": drift_then_drop(sess, 60, 10.5, 9.9)}
    early = _rank_at(sess, bars, 50)["TST"]
    later = _rank_at(sess, bars, 110)["TST"]
    assert early.forecast and later.forecast
    assert early.forecast.expected_return_pct != pytest.approx(later.forecast.expected_return_pct)
    assert early.minutes_to_close > later.minutes_to_close
    assert (early.horizon_minutes or 0) >= (later.horizon_minutes or 0)


def test_deteriorating_forecast_triggers_a_forecast_exit_after_confirmations(sess):
    flip_min = 55

    def mu(f: Features) -> float:
        # a model whose view turns negative once the session reaches +55 minutes
        return 1.5 if f.minutes_since_open < flip_min else -2.0

    flat_after_entry = build_day(
        sess,
        10.0,
        [(int(PRE + 40), 10.0, 60_000), (10, 10.12, 150_000), (340, 10.12, 90_000)],
        base_volume=60_000,
    )
    bot, broker, _ = run({"TST": flat_after_entry}, forecaster=ScriptedForecaster({"TST": mu}))
    assert broker.trades, [n["message"] for n in bot.decision_log][-12:]
    t = broker.trades[0]
    flip_at = at(sess, flip_min)
    assert t.entry_time < flip_at
    assert classify_exit(t.exit_reason) == "forecast", t.exit_reason
    assert t.exit_time >= flip_at  # never before the model changed its mind
    # hysteresis: needs several consecutive bad checks, so not on the very first one
    assert t.exit_time >= flip_at + timedelta(minutes=bot.profile.forecast_exit.confirm_evaluations - 1)


# ------------------------------------------------------------------ 6: approaching the close
def _signal_after(minute: float):
    return lambda f: 2.0 if f.minutes_since_open >= minute else -1.0


def test_no_new_entries_close_to_the_close(sess):
    # the model only turns bullish 40 minutes before the close - inside the 45-minute no-new-trades window
    late = {"TST": drift_up(sess, 350, 10.6, 30)}
    bot, broker, _ = run(late, forecaster=ScriptedForecaster({"TST": _signal_after(350)}))
    assert not broker.trades and not broker.positions
    assert not [n for n in bot.decision_log if n["kind"] == "entry"]
    ops = [o for o in bot.opportunities if o.symbol == "TST"]
    assert ops and any(not g.passed and "time" in g.label.lower() for g in ops[-1].gates)
    # positive control: the very same signal earlier in the day IS traded, so the test is not vacuous
    early = {"TST": drift_up(sess, 200, 10.6, 30)}
    _, broker2, _ = run(early, forecaster=ScriptedForecaster({"TST": _signal_after(200)}))
    assert broker2.orders


def test_horizon_and_deadline_shrink_toward_the_close(sess):
    bars = {"TST": drift_up(sess, 30, 10.3, 200)}
    early = _rank_at(sess, bars, 45, ScriptedForecaster(1.0))["TST"]
    late = _rank_at(sess, bars, 300, ScriptedForecaster(1.0))["TST"]
    assert early.plan and late.plan
    assert early.plan.max_hold_minutes > late.plan.max_hold_minutes
    prof = settings_for().profile_for()
    assert late.plan.max_hold_minutes <= late.minutes_to_close - prof.plan.end_buffer_minutes + 1e-6


def test_open_positions_are_sold_by_the_end_of_day_buffer(sess):
    # price goes sideways after entry: no target, no stop -> the deadline / EOD exit must close it
    flat = build_day(
        sess,
        10.0,
        [(int(PRE + 40), 10.0, 60_000), (10, 10.12, 150_000), (340, 10.12, 90_000)],
        base_volume=60_000,
    )
    bot, broker, runner = run({"TST": flat}, forecaster=ScriptedForecaster({"TST": 1.5}))
    assert broker.trades and not broker.positions
    t = broker.trades[-1]
    assert classify_exit(t.exit_reason) in ("end_of_day", "time", "forecast", "target", "risk")
    sched = build_schedule(TEST_DAY, bot.settings.schedule)
    assert t.exit_time <= sched.close


# ------------------------------------------------------------------ 7: order does not fill
def test_unfilled_entry_is_handled_without_a_phantom_position(sess):
    # the stock halts right after the buy decision, so the order cannot fill
    bars = {"TST": drift_up(sess, 40, 10.4, 30)}
    halts = {"TST": [(at(sess, 38), at(sess, 400))]}
    bot, broker, _ = run(bars, halts=halts, forecaster=ScriptedForecaster({"TST": 1.5}))
    assert not broker.positions and not broker.trades
    assert broker.cash == pytest.approx(broker.starting_cash)  # no money moved
    states = {o.status.value for o in broker.orders.values()}
    assert states <= {"expired", "canceled", "rejected"}, states
    # and the bot kept running: it noted the problem instead of crashing
    assert bot.decision_log


# ------------------------------------------------------------------ 8: best-ranked stock unavailable in MarketWatch
def test_best_ranked_stock_that_is_unavailable_in_the_game_is_never_bought(sess):
    bars = {"BEST": drift_up(sess, 40, 11.0, 40), "NEXT": drift_up(sess, 42, 10.6, 40)}
    fc = ScriptedForecaster({"BEST": 3.0, "NEXT": 1.5})
    uni = EligibilityUniverse(
        [
            EligibilityRecord("BEST", U, rejection_reason="Not offered in this game"),
            EligibilityRecord("NEXT", T),
        ]
    )
    bot, broker, _ = run(bars, elig=uni, forecaster=fc)
    bought = {o.symbol for o in broker.orders.values() if o.side.value == "buy"}
    assert "BEST" not in bought and "NEXT" in bought
    assert all(o.symbol != "BEST" for o in bot.opportunities)  # not even listed as an opportunity
    assert any("BEST" in n["message"] and "unavailable" in n["message"].lower() for n in bot.decision_log)


def test_unverified_stocks_are_research_candidates_never_actionable(sess):
    bars = {"UNK": drift_up(sess, 40, 11.0, 40), "OK": drift_up(sess, 42, 10.5, 40)}
    uni = universe(("OK", T))  # UNK has no record at all -> unverified
    seen: dict[str, list] = {"UNK": [], "OK": []}

    def hook(runner, bot):
        for o in bot.opportunities:
            seen[o.symbol].append(o)

    bot, broker, _ = run(bars, elig=uni, forecaster=ScriptedForecaster({"UNK": 3.0, "OK": 1.5}), hook=hook)
    assert {o.symbol for o in broker.orders.values()} == {"OK"}  # the better-looking UNK was never bought
    assert seen["UNK"] and all(o.research_only and not o.actionable and o.rank == 0 for o in seen["UNK"])
    assert any(o.actionable for o in seen["OK"])


def test_holdings_stay_managed_when_their_eligibility_changes(sess):
    bars = {"TST": drift_then_drop(sess, 40, 10.1, 9.2)}
    uni = universe(("TST", T))
    flipped = []

    def hook(runner, bot):
        if bot.broker.positions and not flipped:
            bot.set_universe(
                EligibilityUniverse([EligibilityRecord("TST", U, rejection_reason="removed from game")])
            )
            flipped.append(True)

    bot, broker, _ = run(bars, elig=uni, forecaster=ScriptedForecaster({"TST": 1.5}), hook=hook)
    assert flipped and broker.trades and not broker.positions  # still sold by the normal rules
    assert any(a["code"] == "eligibility_changed" for a in bot.alerts)  # and the user was told


def test_a_quiet_bot_explains_why_it_is_not_trading(sess):
    bot, broker, _ = run({"TST": drift_up(sess, 200, 10.3, 30)}, forecaster=ScriptedForecaster(-1.0))
    assert not broker.orders
    msgs = [n["message"] for n in bot.decision_log if n["message"].startswith("Waiting")]
    assert msgs and "TST" in msgs[0] and "expected return after costs" in msgs[0]
    assert len(msgs) < 20  # throttled, not one per tick
