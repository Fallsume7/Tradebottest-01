# Architecture

## 1. Shape of the system

```
                    ┌──────────────────────────── React dashboard (frontend/) ───────────────────────────┐
                    │ Overview · Scanner · Stock · Positions · History · Strategy Lab · Performance · Settings │
                    └───────────────────────────────────────────┬───────────────────────────────────────┘
                                                                │ JSON (polling)         /api/*
┌───────────────────────────────────────────────────────────────▼───────────────────────────────────────┐
│ FastAPI (mwbot/api/app.py)  ──►  AppService (mwbot/service.py)                                          │
│   settings · provider selection · session thread · APScheduler live loop · backtest jobs · read models   │
└───────┬───────────────────────────┬───────────────────────────┬───────────────────────┬────────────────┘
        │                           │                           │                       │
 MarketDataProvider         TradingBot (bot/engine.py)      SqlStore (db/)         Backtester
 ├ SyntheticProvider        tick = fills → data → marks     SQLite/SQLAlchemy      (backtest/engine.py)
 ├ CsvHistoricalProvider      → scan → exits → entries      account, orders,       runs the SAME
 └ MassiveProvider                 │      │                 fills, positions,      TradingBot +
        │                          │      └─ RiskManager    trades, equity,        SessionRunner
   MarketDataSource                │         (trading/risk)  decisions, reports,    per historical day
   (one opened session)            │                         backtests
   contexts_at(t): only      Scanner (scanner/)  Strategies (strategies/)
   COMPLETE bars ≤ t         metrics → filters → score     5 strategies, common interface
                                   │
                             ExecutionAdapter (execution/base.py)
                             ├ PaperBroker      (trading/broker.py)  ← always the source of truth
                             ├ MockBrowserAdapter (Playwright → mocksite/)  ← optional "mirror"
                             └ MarketWatchAdapter  (skeleton, does nothing)
```

Everything runs in one Python process (plus an optional headless Chromium for the mock-browser adapter). SQLite is the only datastore. There is deliberately no queue, container orchestration or microservice.

## 2. Modules

| Module | Responsibility |
|---|---|
| `domain.py` | Dataclasses and enums shared everywhere: `Order` (strict state machine), `Position`, `Trade`, `StockContext`, `FeedInfo`/`FeedKind`. UTC-only timestamps (`as_utc` rejects naive datetimes) |
| `config.py` | Pydantic `EnvSettings` (environment/.env), `AppSettings` (dashboard-editable, persisted), scanner presets. The real-money latch lives here |
| `calendar.py` | `exchange_calendars` XNYS wrapper; `SessionSchedule` resolves the configurable Chicago-time milestones per date, clamping them on early-close days; `Phase` enum |
| `data/base.py` | `MarketDataProvider` (opens a session) / `MarketDataSource` (answers "what was knowable at *t*") |
| `data/synthetic.py` | Deterministic synthetic market with named regimes (gap-and-run, gap-and-fade, breakout, false breakout, volume surge, illiquid spike, halt, downtrend). Seeded per (seed, date, symbol); days chain overnight |
| `data/csv_provider.py` | Strict CSV loader: tz handling, OHLC validation, duplicate removal, split detection, no-look-ahead derivation of `prev_close`/ADV |
| `data/massive.py`, `data/http.py` | Massive/Polygon-style REST client: bearer-key header, bounded exponential backoff, `Retry-After`, rate limiter, subscription (403) degradation, dataset downloader |
| `data/replay.py`, `data/arrays.py` | `ReplaySource` (look-ahead-safe slicing) and NumPy prefix arrays for fast metrics |
| `scanner/` | `compute_metrics` (pure) → `Scanner` filters + weighted score with per-component explanations |
| `strategies/` | `Strategy` interface (`evaluate_entry` → `Evaluation` with every condition; `evaluate_exit`) and five built-ins |
| `trading/fills.py` | The fill model (rules below) |
| `trading/broker.py` | `PaperBroker`: orders, fills, positions, cash, round-trip ledger, idempotency, expiry, `force_flatten` |
| `trading/risk.py` | Position sizing, hard exit rules, `RiskManager.check_entry` gate, `BotState` |
| `bot/engine.py` | `TradingBot.tick`, alerts, decision log, mirror hook, end-of-session bookkeeping |
| `bot/runner.py`, `bot/report.py` | Minute-stepping `SessionRunner`; daily report (dict + markdown) |
| `backtest/` | `run_backtest`, `compare_strategies`, metrics, CSV export |
| `execution/` | Adapter interface + implementations + factory with the paper-only latch |
| `mocksite/` | Fake virtual-trading website (FastAPI, server-rendered HTML) used to test the browser adapter |
| `db/` | SQLAlchemy models (`UTCDateTime` type) and `SqlStore` (persist/restore/reset, reports, backtests) |
| `service.py` | `AppService`: owns settings, provider, broker+bot, session thread, scheduler, backtest jobs; produces dashboard read-models |
| `api/app.py` | REST routes; serves `frontend/dist` if built |

## 3. Time and the no-look-ahead guarantee

* A 1-minute bar is **labelled by its open time**. The bar labelled *t* is complete — and visible — at *t + 1 minute*.
* `MarketDataSource.contexts_at(as_of)` returns only bars with `label + 1min ≤ as_of`. Scanner metrics and strategies receive nothing else.
* Tests prove it three ways: bar-visibility boundary, "scramble every bar at/after *t* and assert the context, scanner output and all five strategies' evaluations at *t* are byte-identical", and the same on a full synthetic day.
* The **tape interface** (`bar_at`, `is_halted`, `spread_bps`) exists only so the simulated broker can price fills. It deliberately lives on the source but is never passed to strategies.

## 4. One tick

`TradingBot.tick(source, schedule, now)`:

1. **Fills** — `broker.process()` looks at bars that *completed* since the last tick and fills/expires open orders.
2. **Phase** — `schedule.phase_at(now)` ∈ closed · premarket_scan · observing_open · trading · no_new_entries · liquidating · final_check · post_close.
3. **Data** — `source.contexts_at(now)`; a `DataUnavailableError` becomes a critical alert, never a crash. Feed staleness (`feed_age_seconds`) blocks entries.
4. **Marks** — held symbols are always priced (fetched individually if absent from the universe).
5. **Scan** — filters + scores. (`bot.fast=True` in backtests skips scanning outside the entry phase.)
6. **Exits** (run in *every* phase, even paused or stale — exits only reduce risk), in priority order: emergency-stop flatten → daily-loss flatten → end-of-day liquidation → hard exits (stop/trailing/target/time, evaluated on the last complete bar's low/high) → the strategy's own exit rule.
7. **Entries** (only in the `trading` phase, bot `running`, feed permits, loss limit not hit): evaluate each enabled strategy on each tradable symbol; rank passing signals by `strength × (0.5 + score/200)`; pass each through `RiskManager.check_entry`; size; submit.
8. **Persist** — the broker writes account/orders/fills/positions; decisions are logged and stored.

## 5. The fill model (deterministic, documented)

1. An order is eligible on bars opening at/after `submit_time + latency` (default 5 s ⇒ in practice the **next minute bar**). A decision at *T* is therefore never filled using the bar still forming at *T*.
2. No fill on a halted minute, on a minute with no print, or on a bar with volume `< min_fill_volume`.
3. Per-bar fill size ≤ `max_participation × bar volume`; the rest stays pending (partial fills).
4. **Market** orders fill at the bar **open** adjusted against the trader by `½ spread + slippage + impact × participation`, clamped to the bar's `[low, high]`. Spread comes from the feed's quote when available, else `assumed_spread_bps`.
5. **Limit** orders fill only when the bar trades *through* the limit (touching is not enough); a bar that opens better than the limit fills at the open; limit fills pay no spread/slippage.
6. Commissions: per share + per order, with a per-order minimum.
7. Cash account: a buy that can't be afforded at fill time is reduced or left unfilled; sells can't exceed shares held (no shorting).

Order states: `new → pending → partially_filled → filled | canceled | expired`, or `new → rejected`; illegal transitions raise `InvalidTransition`. Duplicate protection is layered: a deterministic client order id per (date, strategy, symbol), the broker's idempotent `client_order_id` map, the risk gate's "already holding/buying" check, and a per-day `(strategy, symbol)` set.

## 6. Safety design

* **Paper-only latch**: `EnvSettings.allow_real_money` raises if true; `ExecutionAdapter.is_real_money` is `False`; `assert_virtual_only()` guards the factory. Execution mode is an enum of `paper | mock_browser` — there is no value that could select a brokerage.
* **Honest feeds**: `FeedKind` is `synthetic | historical | delayed | realtime`; the UI badge and `FeedInfo.to_dict()["is_realtime"]` are `true` only for a feed explicitly configured as real-time. A *delayed* feed blocks automated entries unless `allow_delayed_feed_trading` is switched on.
* **Stale data**: entries blocked when `feed_age > max_data_age_seconds`; exits still allowed (documented trade-off: reducing risk beats freezing).
* **Liquidation honesty**: orders that cannot fill (e.g. halt through the close) expire; the daily report sets `liquidation_complete=false`, lists unresolved positions, and raises `unresolved_positions` / `overnight_risk` critical alerts. Nothing is marked closed that wasn't.
* **Restart recovery**: broker state (cash, positions with stops, open orders, counters, marks) is persisted every tick and restored at start-up; a restart with open exposure raises a `restart_recovery` alert. A restart cannot resume an in-memory replay — it says so rather than pretending.
* **Failure visibility**: alerts (warning/critical) surface as red dashboard banners; the session loop catches crashes into `session_error`; mirror-venue failures pause the bot (configurable); reconciliation mismatches alert.

## 7. Mirror mode (path to MarketWatch)

The internal `PaperBroker` always decides and records. With `MWBOT_EXECUTION_MODE=mock_browser`, each accepted order is *also* `prepare`d and `submit`ted through an `ExecutionAdapter`; the venue's order id is stored on the internal order; at the close the venue's holdings are reconciled against the bot's. A future MarketWatch adapter plugs into exactly this seam. The browser adapter funnels all Playwright calls to one dedicated worker thread because Playwright's sync API is thread-bound.

## 8. Persistence

Tables: `kv` (settings, flags), `account`, `orders`, `fills`, `positions`, `trades`, `equity`, `decisions`, `daily_reports`, `backtests`. Datetimes are stored as naive UTC and always returned tz-aware (`UTCDateTime`; naive input is refused). WAL mode is enabled. `SqlStore.reset_account()` wipes trading state but not settings.

## 9. Frontend

Vite + React 18 + TypeScript (strict) + Tailwind 4. No router library (hash routes), no state library: `usePoll` hooks against the typed `api.ts`. Charts: `lightweight-charts` (candles/volume/VWAP, equity) and Recharts (daily returns). CSS-variable tokens implement dark (default) and light themes. Layout: sidebar on desktop, scrolling bottom navigation on phones.

## 10. Key decisions and trade-offs

| Decision | Why |
|---|---|
| Backtests reuse the live `TradingBot`/`SessionRunner` | One code path ⇒ no "backtest-only" optimism; the live demo *is* a backtest at human speed |
| 1-minute bars + next-bar fills | Honest about what minute data can support; no intrabar guessing beyond stop/target checks on bar extremes |
| Hard exits checked on bar low/high, filled on the *next* bar | A gap through a stop yields realistic slippage instead of a fill at the stop price |
| RVOL from ADV × U-curve, flagged "estimated" | Real per-minute volume history is not universally available; the estimate is labelled everywhere |
| Missing data ⇒ `None` + policy (`ignore`/`exclude`) | Never fabricate signals; weights renormalise over available components |
| Long-only cash account | Matches a safe default; shorting rules differ by venue and are a later, explicit decision |
| Polling instead of websockets in the UI | Simpler and sufficient at 1-minute decision cadence |
| Playwright adapter on its own thread | The sync API is thread-bound (found by a test; regression-tested) |
