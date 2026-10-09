# Cloud → local hand-off

Written at the end of the cloud session. Statuses are strict: **tested** means an automated test or a command I ran in the cloud sandbox exercised it; **not verified** means no check was possible there.

## 1. Implemented

| Area | What exists |
|---|---|
| Project | Backend `backend/mwbot`, dashboard `frontend/`, docs, Windows scripts, CI workflow, `.env.example`, secret-safe `.gitignore` |
| Data modes | Demo (deterministic synthetic market) · Historical (CSV/CSV.GZ replay with validation) · Connected (Massive/Polygon-style provider with retry/backoff/rate-limit/subscription handling) |
| Scanner | All requested criteria; 3 presets; dashboard-editable filters; per-component score breakdown + reasons/cautions/blockers; missing-data policy |
| MarketWatch eligibility | Three states, dated SQLite history, CSV import/export, game rules, point-in-time use in replays/backtests, holdings never trapped, verifier *interface* (skeleton only). `docs/ELIGIBILITY.md` |
| Predictive strategy | `expected_opportunity` (default): features → forecast → per-trade target/stop/deadline → gates → ranking; smart exits; rotation + turnover controls; **Maximum Expected Return** (default) and **Aggressive Upside** presets with live switching. `docs/PREDICTIVE_STRATEGY.md` |
| Forecast framework | Rule-based baseline, ridge regression, optional GBM, isotonic calibration, walk-forward evaluation with strict "validated" criteria, baselines; labelled estimates everywhere |
| Strategies | Classic A Opening Momentum, B Gap Continuation, C Momentum Breakout, D Volume Surge, E Custom Weighted (kept for comparison; also eligibility-gated) |
| Virtual broker | Market/limit, partial fills, IOC/day, halts, spread/slippage/impact/latency/commission, idempotent ids, state machine, sizing, stop/target/trailing/time exits, daily-loss limit, position caps, entry cutoff, EOD liquidation with honest failure reporting, pause + emergency stop |
| Calendar/schedule | Real NYSE calendar, DST/early-close handling, all milestone times configurable, Chicago display, APScheduler live-clock loop (opt-in) |
| Backtester | Same-code-path replay, next-bar fills, costs, OOS split, warnings, reproducible hash, CSV export, comparison, saved runs; point-in-time eligibility, exit mix, forecast accuracy, mode comparison + baselines, walk-forward model check |
| Persistence | SQLite (orders, fills, positions, trades, equity, decisions, reports, backtests, settings), restart recovery |
| Dashboard | Dashboard · Opportunities · Portfolio · Strategy Lab · Trade History · Settings; explanation drawers; Start/Pause/Stop/Emergency; mode switch; settings schema UI (validation both sides, preview of effects, reset to recommended, import/export, saved indicators); dark/light; desktop/tablet/phone; loading/empty/error states; confirmations |
| Execution adapters | `PaperBroker` (full), `MockBrowserAdapter` + mock trading website (full, test-only), `MarketWatchAdapter` (skeleton that refuses to do anything) |
| Safeguards | Real-money latch, honest feed labels, stale-data entry block, delayed-feed policy, duplicate prevention, alerts, mirror-failure pause, reconciliation |

## 2. Tested (cloud sandbox, Linux, Python 3.13 / Node 22)

_Latest numbers (after the eligibility / predictive / two-mode / dashboard work) are in the table below; the older bullet list after it describes the first build and is still true for the parts it names._

| Check | Result |
|---|---|
| `ruff check` / `ruff format --check` / `mypy mwbot` (47 files) | clean |
| Backend tests | **383 passed** (356 non-browser + 27 Playwright browser tests incl. the new dashboard E2E), ≈15 min |
| Frontend `tsc --noEmit` (strict) / `vitest` / `vite build` | clean / **38 passed** / built |
| `ruff check` / `ruff format --check` / `mypy mwbot` (64 files) | clean |
| Real `mwbot serve` | started; served the built dashboard; a full instant synthetic day ran over HTTP |
| End-to-end demo (below) | completed; HTTP-driven day equals headless CLI day (equity $99,765.15, 9 trades) |
| Backtest reproducibility | two CLI runs ⇒ identical result hash `7fd8f6b000070c52…` and byte-identical trade CSVs |
| Look-ahead | scrambling all future bars leaves scanner output and all five strategies' evaluations unchanged (tests) |
| Browser adapter | login, prepare-without-submit, buy/sell, limit/cancel, delayed fills, rejections, market closed, session expiry + recovery, idempotent resubmit, unknown-outcome recovery, reconciliation drift, cross-thread use, full bot→mock-venue mirroring |
| Dashboard in a real browser | full workflow incl. scanner presets, stock chart, history reasons, Strategy Lab backtest, settings validation, emergency-stop confirm; phone-width layout with no horizontal scroll |
| Wall-clock scheduler | fake-clock run through a whole session day; ignores weekends/holidays |
| CI (GitHub Actions, Python 3.12) | at time of writing: ruff, format, mypy and the frontend job **passed**; backend tests, Windows smoke and browser jobs were still running — check PR #1 for the final result |

Bugs found *by* these tests during the session and fixed: schedule clamp overriding the configured liquidation time; pandas-3 µs-vs-ns index units silently freezing replay slices; numpy bools breaking JSON; `slots` dataclass `asdict` use in backtest serialisation; stale marks after restart; Playwright thread-binding when the service created the adapter on a different thread; `os.geteuid` missing on Windows.

### End-to-end demonstration (synthetic data — fabricated prices)

`mwbot demo-day --date 2026-10-08 --seed 42` → 9 trades, win rate 33 %, P/L **−$234.85 (−0.23 %)**, liquidation complete (full report: `docs/demo/demo_day_2026-10-08.md`). It exercised scanner ranking → strategy entries (opening momentum, gap continuation, breakout) → exits by trailing stop, "momentum lost", failed breakout, time stop → cash/equity reconciliation.

`mwbot backtest --dataset synthetic_sample --oos 0.3` (10 sessions × 30 fabricated symbols, 4 strategies) → 60 trades, +6.2 % vs −5.0 % universe benchmark, max drawdown 0.74 %, Sharpe withheld (10 days). Exports: `docs/demo/backtest_synthetic_sample_*.csv`.

**Do not read either result as evidence that a strategy works.** The synthetic regimes were written by hand; roughly half of each setup type continues by construction.

## 3. Not finished / known limitations

* Long-only cash account; no shorting, stop orders or margin.
* RVOL is an estimate (ADV × standard curve). Spread is unavailable on bar-only data (a configured assumption is used and said so).
* The live provider polls "top gainers" (≤ ~30 symbols); it is not a full-market scanner.
* No websocket streaming (polling at 1-minute cadence), no mobile push/email alerts, no authentication on the local API (binds to 127.0.0.1 — do not expose it).
* Mock adapter's idempotency ledger is in-memory (see MarketWatch doc §3).
* Survivorship/point-in-time universe cannot be solved in code; real datasets need care.
* A restart cannot resume an in-memory replay session; it restores the account and warns about open exposure.
* Strategy defaults are illustrative, untuned and not validated on real data.

## 4. Requires your Windows computer

* Running the app and tests there (`docs/LOCAL_SETUP.md`); CI has a Windows smoke job for the backend core but the dashboard/browser flow was only run on Linux.
* Leaving the app running through a real session (sleep settings, Task Scheduler, auto-schedule).
* Anything involving your Chrome profile or a logged-in browser.

## 5. Requires provider API keys

* Connected mode (`MWBOT_MASSIVE_API_KEY`). **The Massive provider has not been run against the live service**: the sandbox's network policy blocked the docs and API, so field names follow the documented Polygon-compatible schema (`/v2/snapshot/locale/us/markets/stocks/gainers`, `/v2/aggs/ticker/{t}/range/1/minute/...`, bearer-key auth) and are tested only against mocked responses. Expect to adjust parsing on first contact; plan tier decides real-time vs delayed and which endpoints return 403.
* Real historical minute data for credible backtests (`mwbot download-dataset`).

## 6. Requires external authorisation

* Any automation of MarketWatch (terms of use / organiser permission) — see `docs/MARKETWATCH_INTEGRATION.md`. Nothing in the repo contacts MarketWatch.

## 7. First steps on your PC

1. Follow `docs/LOCAL_SETUP.md`; run `scripts\test.ps1 -SkipBrowser`, then `scripts\run.ps1` and run a demo day.
2. Tell Claude Code locally about any Windows-specific failures.
3. Add your Massive key, switch to *Connected*, watch the logs, fix parsing differences; keep *Allow trading on delayed feed* **off** unless your plan is real-time or you accept stale prices.
4. Download real minute data for a handful of past weeks, backtest with the OOS split, and treat the results sceptically.
5. Decide on MarketWatch (permission first), then follow the S1→S5 rollout.

## 8. Continuing with Claude Code locally

```powershell
cd path\to\Tradebottest-01
git checkout claude/marketwatchbot-build   # or main after merging PR #1
claude                                      # Claude Code reads CLAUDE.md automatically
```

Suggested first prompt: *"Read CLAUDE.md and docs/CLOUD_TO_LOCAL_HANDOFF.md. Run scripts\test.ps1 -SkipBrowser and fix anything Windows-specific. Then help me verify the Massive provider with my key (in .env)."* Keep secrets in `.env`; never paste keys into chat or commit them.

## 9. Eligibility / predictive / two-mode / dashboard work: status

**Tested** (automated): eligibility states, point-in-time lookups, CSV, persistence, scanner gating (unavailable/unverified can never become actionable), holdings still sold when eligibility changes, best-ranked-stock-unavailable, yesterday's-winner-vs-flat-stock ranking, profit target, stop-loss, forecast change during the session and forecast exits, no entries near the close, unfilled entry, mode presets/switching with open positions, `tighten_only`, pause/resume/stop/emergency, risk limits, game order types/commissions, live settings changes, settings validation/preview/reset/import/export through the API, forecast features/calibration/walk-forward (no model can be called validated on the sample), backtest eligibility handling, mode comparison, and the real-browser dashboard workflow (desktop, tablet, phone widths, no horizontal scroll).

**Honest caveats**
* On the bundled synthetic sample (bar-only, assumed 40 bps spread) the default mode takes **no trades**: the modelled cost exceeds the rule-based model's edge. On the live demo market (quotes available) it trades roughly once a day with mixed results. Neither says anything about real markets.
* No forecast model is validated. The Strategy Lab model check is the way to find out on real data you download; it needs 10+ out-of-sample sessions before any model may be labelled validated.
* Eligibility must be maintained by you (dashboard or CSV). Automatic checking against MarketWatch is **not implemented** by design.
* The optional gradient-boosted model needs `pip install -e "backend[ml]"` and has only been exercised on synthetic data.
* Stop (bot) keeps managing open positions until flat; there is deliberately no "stop monitoring while holding" option.
