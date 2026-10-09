# MarketWatch stock eligibility

MarketWatch Virtual Stock Exchange does **not** offer every listed U.S. stock, and what a given game offers can change. So the bot never assumes a stock can be bought because it trades on NYSE/Nasdaq or because a data provider has prices for it. A stock is tradable **for this bot** only if somebody (you, or a future authorised verifier) has marked it so.

This is a core rule, not a feature flag: the scanner, the opportunity ranking, the bot, the simulator and the backtester all go through it.

## The three states

| State | Meaning | What the bot does |
|---|---|---|
| **Confirmed tradable** ("Available") | You confirmed it exists in your game | May rank, recommend and buy it (if it also passes your game's rules and all trade checks) |
| **Confirmed unavailable** | You confirmed it is *not* in your game | Never recommended, never bought. Skipped quietly; at most one note ("Skipping XYZ: it scores well but is marked unavailable") |
| **Unverified** | Nobody has checked | Shown only as a **research candidate**, clearly separated, never as a confirmed opportunity, never bought |

A ticker with no record at all is **Unverified**.

## What is stored

SQLite table `eligibility` (append-only, dated): ticker, company name, exchange, security type, status, `effective_from`, `verified_on`, rejection reason, restrictions (free text such as "max 5,000 shares"), and source (`manual`, `csv`, `demo-seed`, a future `verifier:<name>`). Changing a stock adds a new dated statement; history is kept.

## Game rules (Settings → MarketWatch Game)

Minimum price · allowed exchanges · allowed security types · position limits (% of account, $, number of holdings) · commissions · supported order types (if market orders are not supported the bot uses marketable limit orders) · eligibility lookup mode. A *confirmed tradable* stock that breaks a game rule (e.g. price below your game's minimum) is still not actionable, and the reason is shown. The stricter of your own risk limits and the game's limits applies.

## Maintaining the list

* **Dashboard**: *Opportunities* has an Eligibility column, filters (All / Available / Unverified / Unavailable), and one-click **Available / Unavailable** buttons on every row and in every explanation drawer. Marking *unavailable* asks for confirmation.
* **CSV import / export** (*Opportunities → Your MarketWatch stock list*): columns `ticker,status,name,exchange,security_type,rejection_reason,restrictions,effective_from,verified_on`. Column aliases such as `symbol`, `company`, `reason`, `since` and status words such as `available`, `tradable`, `yes`/`no`, `rejected` are accepted. Import shows a preview and lists bad rows with line numbers; good rows are still imported; existing history is kept.
* **Demo mode**: a first run is seeded with a clearly labelled, made-up list so the demo works out of the box ("demo list (made-up, for demo mode)"). Replace it with yours any time.
* **Future automatic verification**: `mwbot/eligibility/verifier.py` defines the interface (`EligibilityVerifier.verify(ticker) -> VerificationResult`). The shipped `MarketWatchVerifier` is a skeleton that refuses to run: checking MarketWatch needs your permission and your own logged-in browser on your own computer (see `MARKETWATCH_INTEGRATION.md`). Nothing in this repository contacts MarketWatch. `apply_verification` is the single place results become dated, audited records ("could not determine" is never recorded as a fact).

## Holdings are never trapped

If a stock you already hold stops being "confirmed tradable", the bot **flags it** (a warning alert, a badge on the position) but keeps managing it and sells it by its normal exit rules. Eligibility only gates *new entries*; it never blocks an exit.

## Simulator and backtester: point-in-time

* Live/demo sessions use the list as it stands today (`current_list`).
* Replays and backtests use the list **as it was on each date** (`point_in_time`): the latest statement with `effective_from <= date`; no statement ⇒ Unverified. Today's knowledge never leaks into the past.
* A backtest on a dataset with no eligibility history **refuses to run** instead of silently trading everything. You can explicitly choose "eligibility not applied"; every result is then labelled that way (`summary.eligibility.applied = false`, and a warning at the top).
* The bundled synthetic sample ships a dated `eligibility.csv` (including mid-period changes) to exercise this.

## Tests that guard it

`test_eligibility.py` (states, point-in-time, CSV, persistence, verifier interface, demo seed), `test_scanner_eligibility.py` (unavailable/unverified can never become actionable; replays use the list as of the date), `test_opportunity_bot.py` (best-ranked stock unavailable ⇒ the next one is bought; unverified is research only; held stock that becomes unavailable is still sold and flagged), `test_backtest_predictive.py` (traded symbols were confirmed tradable on the trade date; no list ⇒ refusal).
