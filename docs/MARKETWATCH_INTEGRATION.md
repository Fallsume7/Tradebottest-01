# MarketWatch integration — local hand-off plan

**Status: not integrated, by design.** The cloud session did not touch your Chrome profile, log into any account, or send any order to MarketWatch. What exists is the *seam*: an `ExecutionAdapter` interface, a fully working local virtual broker, and a Playwright adapter proven against a **mock** trading site. `mwbot/execution/marketwatch.py` is a skeleton whose methods raise `NotConfiguredError` and whose `check_session()` honestly reports "not configured". It contains **no selectors, no endpoints, no CAPTCHA handling and no private-API calls**, and none were invented.

The rest of the bot works without MarketWatch. This document lists exactly what remains.

## 0. Gate: permission and rules (do this first, on your side)

1. **Read the MarketWatch / Dow Jones terms of use** and the rules of *your* game. Automated or scripted interaction may be restricted. If it is unclear, ask the game's organiser and, where required, the site owner for permission. If the answer is "no", stop here — keep using the local paper engine and place orders by hand using the dashboard's suggestions.
2. **Never** circumvent CAPTCHAs, rate limits, bot detection or login protections, and never call undocumented/private endpoints. The adapter must stop and alert when it meets a challenge; a human resolves it.
3. Record the permission you obtained (who, when, scope) in the repo's private notes, not in a public commit.

## 1. Facts to research from the game itself (unknown to the cloud session)

Per-game settings are configured by the game's creator and change between games, so each item must be read from *your* game:

| Question | Why it matters to the bot |
|---|---|
| Order types offered (market, limit, stop, trailing stop?), time-in-force, short selling / buy-to-cover | Maps to `OrderType`/`TimeInForce`; the bot is long-only today |
| When and at what price do orders execute (immediately at a quote? next open? delayed?) | Calibrates `CostConfig` (latency, slippage); decides whether intraday exits are even possible |
| Are the game's quotes real-time or delayed, and by how much? | Our data feed and the game's prices may differ; the bot must not trade on a faster feed than the game fills on without accounting for it |
| Trading hours the game honours (premarket/after-hours?) | Schedule and cutoffs |
| Position limits, commission, cash/margin rules, penny-stock restrictions | `RiskConfig`, scanner price band |
| Minimum/maximum order sizes, fractional shares | Sizing rounding |
| Pending-order and cancel behaviour, partial fills, rejections and their wording | Order-state mapping and error handling |
| Session timeouts, re-authentication, two-factor prompts | Session recovery logic |

## 2. Authentication (user-controlled)

* **You log in yourself.** The bot never stores your MarketWatch password, cookies or tokens in the repo, `.env.example`, logs or screenshots. `.gitignore` already excludes `browser_profile/`, `chrome_profile/`, `cookies*.json`, `storage_state*.json`.
* Recommended pattern for a local run: a **dedicated Chrome profile** (not your everyday one) that you sign into manually, which Playwright attaches to (e.g. launch Chrome with `--user-data-dir=<dedicated dir> --remote-debugging-port=9222` and connect with `playwright.chromium.connect_over_cdp`). Recent Chrome versions refuse remote debugging on the *default* profile directory — verify for your installed version. Remote debugging gives local processes full control of that profile, so keep the port bound to localhost and the profile dedicated.
* Alternative: Playwright's own persistent context (`launch_persistent_context`) with a profile directory under an ignored path.
* On a login prompt, expired session or CAPTCHA the adapter must raise `SessionExpiredError`/`ExecutionError`; the bot already turns mirror failures into a critical alert and **pauses** (`pause_on_mirror_failure`). Do not auto-retry logins in a loop.

## 3. What to build (`mwbot/execution/marketwatch.py`)

Implement `ExecutionAdapter` (see `execution/base.py`) in the same shape as `MockBrowserAdapter` (read it — it is the template, including the dedicated Playwright worker thread, the stage-then-confirm flow, the idempotency ledger and the unknown-outcome recovery):

| Method | Requirement |
|---|---|
| `check_session()` | Detect "logged in to the right game", never navigate away from a staged order |
| `get_buying_power()` | Parse the game's cash / buying power; fail loudly on parse errors (never return 0 silently) |
| `get_holdings()` | Parse the portfolio table into `Holding`s |
| `prepare_order(req)` | Fill the trade form up to — **not including** — the final confirmation; return estimated price/cost and warnings |
| `submit_order(prepared)` | Click the final confirmation only for a prepared order; **idempotent on `client_order_id`**; if the outcome is uncertain (timeout) look the order up before any retry (see `_new_order_since`) |
| `get_order_status(id)` / `cancel_order(id)` | Parse pending/completed order tables; map the game's wording to `OrderStatus` |
| `reconcile(internal)` | Inherited; compares holdings with the bot's positions |

Page objects: keep **all selectors in one module** (a dataclass/dict) so a site redesign is a one-file fix, prefer role/label/text locators over brittle CSS, and add screenshot-on-failure to a git-ignored folder.

Gaps in the mock adapter that the real one must close:

* The idempotency ledger (`client_order_id → venue id`) is **in memory**. Persist it (the internal `Order.meta["external_id"]` is already stored in SQLite; read it back after restart) before trusting unattended runs.
* Multi-account/game selection, pagination of order tables, locale/currency formats and time zones on the site.
* Rate limiting / human-like pacing as the *game's rules* require (not to evade detection — to stay within what was permitted).

Then enable it: add `marketwatch` to `EnvSettings.execution_mode` and `create_adapter`, keeping `assert_virtual_only` (the game is play-money, so `is_real_money=False` is correct; the latch must still reject anything that is not).

## 4. Staged rollout (each stage needs your explicit go-ahead)

| Stage | Behaviour | Exit criterion |
|---|---|---|
| S0 | Paper only (today) | — |
| S1 | **Read-only**: adapter logs in (by you), reads buying power + holdings, reconciles against a manual paper position | Parsers correct on real pages; screenshots reviewed |
| S2 | **Prepare-only**: fills the form for each bot decision, screenshots it, never confirms | 100 % of prepared orders match intent |
| S3 | **Shadow**: you place the orders by hand following the bot; compare your fills with paper fills; calibrate `CostConfig` | Slippage/latency assumptions within tolerance |
| S4 | **Supervised submit**: tiny size, you watch, kill switch at hand | A full day with clean reconciliation |
| S5 | **Unattended** within permission: auto-start via Task Scheduler, alerts to your phone, daily reconciliation, hard max loss | You are comfortable; failures are loud |

At every stage: the bot's **internal paper account stays the record of intent**; the mirror's failure never silently continues (pause + alert); end-of-day reconciliation mismatches raise `reconcile_mismatch`.

## 5. Test procedure for the real adapter

1. Keep the mock-site tests; copy their structure for the real pages using **saved HTML snapshots** or Playwright HAR/trace fixtures from your own game (strip personal data before committing — or don't commit them).
2. Unit-test parsers against snapshots (money formats, empty portfolio, pending vs filled, error banners).
3. Live S1/S2 checks run by you locally, never in CI.
4. Failure drills: expired session, site timeout mid-confirm (must not double-submit), rejected order, market closed, game ended, CAPTCHA (must stop and alert).

## 6. Outstanding checklist

- [ ] Permission/terms confirmed and recorded
- [ ] Game rules answered (table in §1)
- [ ] Authentication approach chosen and documented (dedicated profile + CDP, or persistent context)
- [ ] Selectors/page objects written and snapshot-tested
- [ ] Idempotency ledger persisted across restarts
- [ ] `execution_mode=marketwatch` wired through `EnvSettings`/factory behind `assert_virtual_only`
- [ ] Short selling / stop orders added to the engine if the game offers them and you want them
- [ ] Price-feed vs game-price alignment decided (and delayed-feed policy confirmed)
- [ ] S1 → S5 rollout completed with sign-off at each stage
