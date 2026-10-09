# Predictive strategy: Expected Opportunity ranking

The default strategy (`expected_opportunity`) does **not** chase stocks that already rose the most. It asks, for each stock your game offers: *given what is happening right now and the time left today, what is the expected return after costs, and how risky is it?* Yesterday's biggest winner is usually a poor answer (stretched, fading); a flat or recovering stock with improving momentum and volume can be a better one. `test_opportunity_bot.py` pins that behaviour.

> **No forecast here is claimed to be reliable.** The default model is a transparent rule-of-thumb. Its probabilities are labelled "Estimate - not validated" everywhere unless an out-of-sample test passed (see *Honesty* below). No language model makes predictions.

## Pipeline

1. **Universe**: stocks with usable data (price band, liquidity, fresh quotes, not halted) **and** confirmed tradable in your game (see `ELIGIBILITY.md`). Unverified names are ranked separately as research only.
2. **Features** (`forecast/features.py`, volatility-normalised, no look-ahead): short-term momentum and acceleration, relative/unusual volume, volatility, price vs VWAP and moving averages, breakout / recovery / extension-exhaustion, strength vs the market, liquidity and spread, news/catalyst if the feed has one, same-time-of-day history from **earlier sessions only**, and time remaining.
3. **Forecast** (`forecast/models.py`): expected gross return over the planning horizon, uncertainty (σ), probability of a gain *after costs*, 10th/90th percentile range. Costs come from the same model the simulator uses (half-spread + slippage + size impact + commissions).
4. **Plan** (`forecast/plan.py`): target = where the estimated chance of *touching* it is about 45 % (reflection principle with drift), capped under recent resistance; stop = a multiple of the stock's typical 20-minute adverse swing; maximum hold = the horizon, never later than the end-of-day buffer. The **target is a plausible upside level, not the expected return**; both are shown.
5. **Gates + score** (`forecast/ranking.py`): enough time before the close, expected return after costs, chance of a gain, chance of reaching the target, reward-vs-risk, not too volatile for the mode, trading cost, room under resistance. The score ranks by a configurable utility (return, probability, liquidity, downside, uncertainty, upside weights). Daily % gain is *not* an input to the ranking.

## Time horizon

`horizon = min(longest plan, usable remaining time × fraction)`, where usable time excludes the end-of-day buffer, and the calendar knows early closes. At 8:45 CT the bot may plan ~2 hours; at 2:30 CT it will not open positions at all (no new trades in the last N minutes, configurable).

## Exits (smart exits)

* **Profit target** reached ⇒ sell early, don't wait for the close.
* **Risk exits**: the plan's stop, a trailing stop once in profit, and a breakeven stop after part of the way to target.
* **Forecast exit**: every monitoring cycle the position is re-forecast for the *remaining* time; if the outlook stays poor for several checks in a row (and the minimum hold has passed) it is sold.
* **Deadline / end of day**: exit by the plan's deadline; liquidation always before the close, with retries, honest failure reporting and early-close handling.
* **Rotation**: the weakest holding may be swapped for a clearly better idea only if the advantage beats costs by a margin, outside cooldowns and the entries-per-hour brake.
* Eligibility changes never block an exit.

## Continuous re-evaluation

Every `scanner_refresh_minutes` (default 2) the universe is rescanned and re-ranked; every `monitor_minutes` held positions get a new forecast. Turnover controls: cooldown before re-buying a stock, minimum hold, entries per hour, minimum advantage to rotate, no re-entry after a stop-out. If nothing qualifies, the activity feed says why ("Waiting for a better setup: the best candidate X does not qualify yet - expected return after costs: -0.16% (need >= 0.25%)").

## The two modes

Both modes are presets of the **same** engines (`ModeProfile`): sizing, entry gates, plan parameters, turnover, objective weights.

| | **Maximum Expected Return** (recommended default) | **Aggressive Upside** |
|---|---|---|
| Idea | Best risk-adjusted expected net return | More variance for more potential upside |
| Position size | ≤ 10 % of account, risk 0.5 %/trade | ≤ 30 %, risk 1.5 %/trade, fewer positions |
| Targets / stops | Nearer target (~45 % touch chance), tighter stops | Farther targets, wider range of stocks, wider stops |
| Safety | daily loss limit 3 %, stops, end-of-day exits | daily loss limit 6 %, stops, end-of-day exits - **never disabled** |

Aggressive Upside does not assume small-cap or volatile stocks are better; it only changes sizing and how much variance the gates accept.

**Switching modes** (dashboard top bar, Dashboard, or Settings) is a live change: new trades use the new mode; **open positions keep the plan they were opened with** (each position stores its own target, stop, deadline and mode) unless *Settings → "When settings change, open positions…" = adopt tighter stops only*, which can tighten but never loosen them. Trade history is never touched. Each mode's custom risk settings are remembered while you use the other.

## Defaults: it just works

A fresh install runs Maximum Expected Return with the recommended values - nothing to configure. *Settings* shows "You are using the recommended settings"; **Reset to recommended** restores them per section or everywhere (your mode and data source are kept). Advanced controls are hidden until you turn them on.

## Honesty about forecasts

* `HeuristicForecaster` (default): hand-written rules; its edge is deliberately modest; its probabilities are **never** labelled calibrated.
* `RidgeForecaster` (statistical) and optional `GradientBoostedForecaster` (`pip install -e "backend[ml]"`): trained **walk-forward** only on earlier sessions; probabilities calibrated on the sessions just before the test day.
* A model is called **validated** only if, out-of-sample, it has ≥ 300 samples **over ≥ 10 separate sessions**, calibration error ≤ 0.08, AUC ≥ 0.55 and a Brier score clearly better than always predicting the base rate. The *Strategy Lab → Model check* runs this and shows ranking skill, top-pick edge vs average, error, and baselines (momentum, random). On the bundled synthetic sample nothing passes - correctly.
* The dashboard shows the label next to every forecast, and the data kind (synthetic / historical / delayed / real-time) next to every chart and result.

## Known limits

* Costs on **bar-only data** are an assumption (`assumed_spread_bps`, default 40): bar data cannot show the bid/ask spread. With that default the modelled edge rarely beats costs, so the default mode trades rarely on datasets without quotes (the bundled sample: 0 trades). If your real data has tight spreads, lower it in *Settings → Market Data*; do not lower it just to see trades.
* On synthetic data results demonstrate machinery only. Backtests of 10 days are statistically meaningless; the Strategy Lab says so.
