# Backtesting

## How to run

**Dashboard** — *Strategy Lab → Run a backtest*: choose a dataset (the bundled `synthetic-sample`, any folder you add, or the "demo generator" for the last 5 trading days), pick dates, strategies, capital, slippage and an out-of-sample hold-out. Tick *compare* to run each strategy alone plus all together under identical data and costs. Progress is shown while the job runs; results can be exported as CSV and are saved in SQLite.

**CLI**

```bash
mwbot backtest --dataset synthetic_sample --strategies opening_momentum gap_continuation --oos 0.3 --out reports/
```

**Python**

```python
from mwbot.backtest.engine import BacktestConfig, run_backtest
from mwbot.data.csv_provider import CsvHistoricalProvider
res = run_backtest(CsvHistoricalProvider("backend/sample_data/synthetic_sample"),
                   BacktestConfig(strategies=["opening_momentum"], oos_fraction=0.3))
print(res.summary["total_return_pct"], res.result_hash)
```

## Datasets

A dataset is a folder: `manifest.json` (**timezone** and **adjusted** are mandatory, `synthetic` flag), `bars.csv[.gz]` (`timestamp,symbol,open,high,low,close,volume`, 1-minute bars labelled by open time, premarket included if you have it), optional `meta.csv` (`date,symbol,prev_close,avg_daily_volume,market_cap,float_shares`) and `halts.csv` (`symbol,start,end`).

* Real data: `mwbot download-dataset --symbols AAA BBB --start 2026-09-01 --end 2026-09-30 --out data/mydata` (uses *your* Massive key; adjusted aggregates; writes a manifest with `synthetic:false`). Then set the dataset name in Settings or the Strategy Lab form. This command has not been exercised against the live service (see the handoff doc).
* Synthetic: `mwbot make-sample-data` regenerates `backend/sample_data/synthetic_sample` (fabricated; labelled in the manifest and everywhere in the UI).

## How a backtest executes

For each date: open the session → `SessionRunner` steps minute by minute from the scanner start to the close, calling the **same `TradingBot.tick`** the live loop uses (scan → exits → entries → fills) → the daily report. Capital compounds across days by default (`carry_capital`). Anything still open after the close is **force-closed at the last mark with costs and flagged** in the day's record, the trade's exit reason and the warnings — so one day's failure to liquidate cannot leak silently into the next.

## Protections against common errors

| Risk | Protection | Test |
|---|---|---|
| **Look-ahead bias** | `contexts_at(t)` exposes only bars with `label + 1 min ≤ t`; derived fields (`prev_close`, ADV) use strictly earlier sessions | `test_lookahead.py` scrambles all future bars and asserts identical scans and signals |
| **Same-bar fills** | Orders fill on a bar opening at/after `t + latency`; decision at *t* ⇒ earliest fill is the bar labelled *t + 1 min* | `test_never_fills_on_the_signal_bar`, `test_entries_never_precede_observation_period_plus_one_bar` |
| **Unrealistic costs** | Half-spread + slippage + size impact + participation caps + commissions; stops fill on the *next* bar so gaps hurt | `test_higher_costs_reduce_profit`, fill-model tests |
| **Time-zone / DST errors** | tz-aware UTC only (naive datetimes refused); CSV naive timestamps need a declared manifest timezone; calendar supplies DST-correct sessions | DST tests in `test_calendar.py`, `test_data_providers.py` |
| **Split-adjustment mistakes** | Manifest must declare `adjusted`; overnight ratios near N:1 / 1:N are flagged as possible unadjusted splits | `test_split_detector`, unadjusted-manifest warning |
| **Mixing delayed/real-time/synthetic** | Every result carries the feed kind; live providers cannot be backtested; synthetic results are labelled and warned | `test_backtest_requires_replayable_dataset`, label tests |
| **Overfitting** | Chronological in-sample / out-of-sample split (refused below 4 days); sample-size and "tuning on this data is overfitting" warnings; ratios withheld on short samples | `test_out_of_sample_split_is_chronological`, `test_no_fake_split_for_tiny_samples` |
| **Survivorship bias** | Cannot be solved by code alone. Every non-synthetic result carries a survivorship warning unless you know the dataset is point-in-time. Include delisted/halted names in your data | warning test |
| **Non-reproducibility** | Deterministic simulation; `result_hash` over config + trades + dataset id; identical runs give identical hashes and CSVs | `test_backtest_is_reproducible` (and a CLI double-run during the cloud demo) |

## Reading the results

* **Total return** vs **Benchmark**: the benchmark is the *equal-weight average open→close return of the whole simulated universe* compounded per day. It is a crude baseline ("did the bot beat just holding everything?"), not an index.
* **Win rate / profit factor / expectancy / payoff**: trade-level. With fewer than ~30 trades these are noise; the UI says so.
* **Max drawdown**: from the intraday equity path (every tick), not just daily closes.
* **Sharpe / Sortino**: computed on daily returns only with ≥ 20 days, annualised with √252 and a 0 % risk-free rate; otherwise shown as *withheld*.
* **In-sample vs out-of-sample**: if OOS performance collapses relative to in-sample, the parameters were probably fitted to noise.
* **Warnings** are part of the result — read them.

## Known limitations

1. 1-minute bars: no intrabar path. Stop/target checks use the bar's low/high; if both are touched in one bar the stop is assumed first (conservative).
2. No queue-position model, hidden liquidity, short-sale constraints, locates, or exchange-specific halts (halts only where the dataset lists them).
3. Spread comes from synthetic quotes or the configured assumption — real minute aggregates carry no quotes.
4. RVOL is estimated from ADV and a fixed intraday curve unless the dataset supplies better.
5. Fills ignore your own market impact beyond the participation/impact heuristic.
6. Parameter sweeps are not provided on purpose (they invite overfitting). If you add one, hold out untouched data.
7. The synthetic dataset's regimes were authored by hand; momentum often "works" in it by construction (half the setups continue). **Do not read its returns as evidence about any strategy.**
