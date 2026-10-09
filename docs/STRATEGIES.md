# Strategies

> **No strategy here is claimed to make money.** They are *illustrative, rule-based momentum screens* whose purpose is to exercise the scanner, risk engine, simulator and backtester. Past or simulated results do not predict future results. The bundled sample data is fabricated.

All strategies share one interface (`mwbot/strategies/base.py`):

```python
class Strategy:
    def evaluate_entry(self, inp: SignalInput) -> Evaluation   # every condition with pass/fail + detail
    def evaluate_exit(self, inp: SignalInput, position) -> ExitDecision | None
    def describe(self) -> dict                                 # rules, parameter schema, current values
```

* A strategy only ever sees a `SignalInput` built from **complete bars up to "now"** (see ARCHITECTURE §3), the scanner metrics and the scanner result. It cannot see the future.
* `evaluate_entry` returns *all* conditions — not just the first failure — so the dashboard's Stock page can show exactly why a symbol did or did not qualify, and the trade record stores the passing conditions as the "why bought" text.
* Hard risk exits (stop, trailing stop, profit target, time stop, end-of-day liquidation, daily-loss and emergency flatten) are applied by the bot for **every** strategy; `evaluate_exit` adds the strategy's own discretionary exit on top.
* **Sizing** is common: `qty = min(risk-based, allocation-based, cash-based)` where risk-based = `equity × risk_per_trade% ÷ (price × stop%)`, allocation-based = `equity × max_position% ÷ price`, cash-based = buying power with a 1 % buffer.
* All parameters are validated Pydantic fields (ranges enforced), editable in **Strategy Lab**, persisted in settings.

Default entry gate for every strategy: the symbol must be **tradable** per the scanner (price band, volume, dollar volume, RVOL, spread, history, freshness, halt status, abnormal-data checks) and meet a minimum scanner score.

---

## A — Opening Momentum (`opening_momentum`)

*Buy a strong early-session drive that is still making progress.*

**Entry** (all must hold): within `[start_minute, end_minute]` after the open · up ≥ `min_open_drive_pct` from the regular-session open · price within `or_tolerance_pct` of the opening-range high · not already up more than `max_extension_pct` from the open · RVOL ≥ `min_rvol` (check *skipped, and said so*, if RVOL is unavailable) · price above VWAP · short-term momentum ≥ `min_momentum_pct` · scanner score ≥ `min_scan_score`.

**Exit**: price more than `exit_vwap_tolerance_pct` below VWAP *and* negative short-term momentum ("momentum lost"); plus hard exits.

| Parameter | Default | Meaning |
|---|---|---|
| `start_minute` / `end_minute` | 10 / 60 | Entry window, minutes after open |
| `min_open_drive_pct` | 2.0 | Min % gain from the regular open |
| `min_rvol` | 2.0 | Min relative volume |
| `min_momentum_pct` | 0.2 | Min % gain over the short momentum window (5 min) |
| `max_extension_pct` | 30 | Skip over-extended names |
| `or_tolerance_pct` | 1.0 | Distance allowed below opening-range high |
| `min_scan_score` | 35 | Scanner score floor |
| `stop_loss_pct` / `take_profit_pct` / `trailing_stop_pct` / `max_hold_minutes` | 2.5 / 5 / 2 / 90 | Risk exits (`null` disables an exit) |
| `exit_vwap_tolerance_pct` | 0.3 | VWAP tolerance for "momentum lost" |

**Assumptions/limits**: the "opening range" is the first `max(start_minute, 5)` regular-session bars; a stock that spikes then consolidates tightly can pass while already stale; thin names with exaggerated RVOL are guarded only by the scanner's liquidity filters.

## B — Premarket Gap Continuation (`gap_continuation`)

*After a large premarket gap, buy only if the gap holds and buyers keep pushing after the open.*

**Entry**: within `[10, 75]` min · gap vs previous close ≥ `min_gap_pct` (uses the regular open gap, else the premarket move) · premarket volume ≥ `min_premarket_volume` · total move ≤ `max_total_change_pct` · price holds the regular open (`hold_open_tolerance_pct`) · price within `pm_high_tolerance_pct` of the premarket high · above VWAP · momentum ≥ `min_momentum_pct` · scanner score ≥ `min_scan_score`.

**Exit**: loses the regular open by more than the tolerance + 0.5 % ("gap failed"), or below VWAP with negative momentum; plus hard exits.

Defaults: gap ≥ 8 %, premarket volume ≥ 100 000, max total change 80 %, stop 4 %, target 8 %, trail 3 %, max hold 120 min (wider stops/targets because gappers are volatile).

**Assumptions/limits**: requires `prev_close`; without it the gap condition fails (never guessed). Premarket data on live feeds can be sparse — the strategy treats missing premarket bars as "no premarket volume".

## C — Momentum Breakout (`momentum_breakout`)

*Buy a volume-confirmed break above a recent intraday high out of a contained base.*

**Entry**: window `[15, 240]` · the last complete bar **closes above the highest high of the prior `lookback_minutes` (30) bars** by `breakout_buffer_pct` (0.15 %) · that bar's volume ≥ `volume_multiple` (2.5×) the lookback average · lookback range ≤ `max_range_pct` (12 %) so it is a base and not a vertical spike · above VWAP · up ≥ `min_pct_change` (2 %) on the day · scanner score ≥ 30.

**Exit**: after ≥ 2 minutes, price closes more than `failed_breakout_pct` (0.5 %) back below the broken level ("failed breakout"; the level is stored on the position); plus hard exits. Defaults: stop 2.5 %, target 6 %, trail 2.5 %, hold 90 min.

**Assumptions/limits**: the breakout bar is excluded from the reference-high window (otherwise every new high would "break out" of itself). Entry fills on the *following* bar, so part of the move is usually already gone — modelled, not hidden.

## D — Volume Surge (`volume_surge`)

*Buy abnormal volume that is actually moving price, while rejecting thin one-print spikes.*

**Entry**: window `[10, 300]` · per-minute volume over the last `surge_window` (3) minutes ≥ `surge_multiple` (5×) the prior `baseline_window` (30) minutes' rate · price moved ≥ `min_move_pct` (1.5 %) across the window · dollar volume in the window ≥ `min_surge_dollar_volume` ($150 000) · at least `min_active_bars` (2) distinct bars traded · above VWAP · scanner score ≥ 25.

**Exit**: after ≥ 3 minutes, volume falls back to ≤ `fade_volume_ratio` × baseline *and* momentum ≤ 0 ("surge faded"); plus hard exits with a short `max_hold_minutes` (45), stop 2.5 %, target 5 %, trail 2 %.

**Limits**: surges often mean-revert; the anti-illiquidity filters (dollar volume, active bars, scanner spread/volume checks) reduce but do not remove that risk.

## E — Custom Weighted (`custom_weighted`)

*Combine normalised signals with your own weights and thresholds.*

Score = weighted average (renormalised over *available* signals) of: % change, RVOL, 5-min momentum, VWAP distance, nearness to day high, liquidity; each normalised 0–1 with the same documented curves as the scanner. **Enter** when score ≥ `entry_threshold` (62) (and, optionally, momentum > 0) within `[10, 180]` minutes; **exit** when score < `exit_threshold` (30) — the gap between the two gives hysteresis so the bot doesn't churn. Weights `w_*` ∈ [0, 10]. Hard exits: stop 3 %, target 6 %, trail 2.5 %, hold 120 min.

**Limits**: with many free weights it is easy to overfit; use Strategy Lab's out-of-sample split and fresh dates, and treat tiny samples (< 30 trades) as noise.

---

## Adding a strategy

1. Create a Pydantic params model with bounded fields (they become the form in Strategy Lab) and a `Strategy` subclass with `id`, `name`, `summary`, `entry_rules`, `exit_rules`, `risk_rules`, `ParamsModel`.
2. Implement `evaluate_entry` returning an `Evaluation` built through `self._eval(inp, conditions, strength)`; implement `evaluate_exit`.
3. Register it in `STRATEGY_CLASSES` (`strategies/builtin.py`).
4. Add tests with a representative price sequence from `backend/tests/scenarios.py` (positive case, each rejection, exit, and the generic "survives empty/tiny history" parametrised test, which picks it up automatically).

## What would make these better (not done)

Per-minute historical RVOL, float/short-interest data, news/catalyst awareness, sector context, multi-timeframe confirmation, stop placement from ATR/structure, scaling in/out, and walk-forward parameter selection. None of these is implemented.
