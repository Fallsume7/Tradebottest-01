# Hands-off MarketWatch trading (play money)

The bot can mirror every order it decides on into **your** MarketWatch Virtual Stock Exchange game, and run a whole trading day by itself. This page is the how-to. The design rules behind it are in `MARKETWATCH_INTEGRATION.md`.

> **Status:** the plumbing is built and tested (connection, safety stages, order ledger, budget cap, autopilot, launchers). The part that knows what MarketWatch's pages look like is **not written yet**, because nobody can guess it: it needs pages you save from your own game (step 2). Until then the adapter says "not calibrated" and does nothing.

## What you control

| Thing | Where |
|---|---|
| Most virtual money the bot may have invested at once | Settings → Risk Management → **Most money the bot may use** (empty = whole account) |
| Starting capital of the bot's own account (set it equal to your game's cash) | Settings → Risk → Starting capital |
| How far automation goes | `MWBOT_MARKETWATCH_STAGE` in `.env`: `read_only` → `prepare_only` → `live` |
| Emergency stop | **E-STOP** in the dashboard (sells everything, blocks orders) |

## The rules that never change
* **Your password is never given to, typed by, or stored by the bot.** You log in yourself in a separate Chrome window (a dedicated profile in `chrome_profile\`, git-ignored). The bot attaches to that window over a port that only this computer can reach.
* If the login expires, or MarketWatch shows a CAPTCHA, the bot **stops and alerts**; it does not log in again and does not try to get past a check. You fix it in the Chrome window and resume.
* It only ever opens `https://…marketwatch.com` pages and refuses anything else.
* It will **not send orders decided on demo or replayed data**. Connected mode (a real price feed) is required, otherwise the bot would be buying made-up stocks in your real game.
* An order is never confirmed twice: each has a unique id kept in `data\marketwatch_ledger.json`. If a click's outcome is unclear, the bot looks the order up on the orders page; if it still cannot tell, it pauses and asks you to check by hand.
* Stages: `read_only` (reads cash and holdings, sends nothing), `prepare_only` (fills the order form and takes a screenshot into `data\marketwatch_shots\`, never presses confirm), `live` (presses confirm). Move up one stage only after watching the previous one behave.

## Step by step

### 1. Real market data (needed for anything live)
Put your Massive/Polygon key in `.env` as `MWBOT_MASSIVE_API_KEY`, set `MWBOT_MASSIVE_FEED` to what your plan really provides (`realtime` or `delayed`), and choose **Settings → Data mode → Connected**. On a delayed feed the bot will not open positions unless you turn on "allow trading on a delayed feed" (not recommended).

### 2. Save your game's pages (so the bot can be taught what they look like)
Start the bot's Chrome and log in:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\marketwatch_chrome.ps1
```

In that window, open each page below and press **Ctrl+S → "Webpage, Complete"**, saving into `E:\TradeBot\Tradebottest-01\marketwatch_snapshots\` with these names. The folder is git-ignored; the files contain your account details, so keep them local.

| File name | Page |
|---|---|
| `01_portfolio.html` | Your game's portfolio (cash / buying power and the holdings table) |
| `02_trade_form_buy.html` | The trade page for any stock, set to **Buy**, form visible, **not submitted** |
| `03_trade_form_sell.html` | Same for a stock you hold, set to **Sell** (skip if you hold nothing yet) |
| `04_trade_review.html` | The "review / confirm" step after filling the form, if the game has one (stop there; do not confirm) |
| `05_orders_pending.html` | The pending/open orders page (any state is fine, even empty) |
| `06_orders_history.html` | The completed/transactions page |
| `07_game_rules.html` | The game's details / rules / settings page (order types, limits, hours) |
| `08_logged_out.html` | (optional) the same portfolio address opened in a private window while logged out |

Also copy your game's address bar URL (the one for your game's home/portfolio) into `.env` as `MWBOT_MARKETWATCH_GAME_URL`.

Then tell Claude the files are saved; it builds and tests the page-reading layer (`mwbot/execution/marketwatch_pages.py`) against them.

### 3. Turn the stages on, one at a time
In `.env`:

```
MWBOT_EXECUTION_MODE=marketwatch
MWBOT_MARKETWATCH_GAME_URL=https://www.marketwatch.com/games/<your game>
MWBOT_MARKETWATCH_STAGE=read_only
```

1. **read_only**: the dashboard should show your real cash and holdings matching MarketWatch. Nothing is sent.
2. **prepare_only** for a day: check the screenshots in `data\marketwatch_shots\` show the order the bot meant.
3. **live** with a small *Most money the bot may use*. Watch a full day.

### 4. Run it hands-off
```powershell
powershell -ExecutionPolicy Bypass -File scripts\autopilot.ps1
```
This opens the bot's Chrome window if needed and starts the app with autopilot on: it follows the real exchange calendar (holidays and early closes included), scans, buys, sells on target/stop and clears everything before the close, with no clicks. Your computer must be awake and logged in, with that Chrome window and the autopilot window left open. Starting it automatically at logon (Task Scheduler) is a separate step to take only after a few clean supervised days.

## If something goes wrong
| Alert | Meaning | What to do |
|---|---|---|
| `marketwatch_session` | Not logged in / Chrome not reachable / not calibrated | Run `marketwatch_chrome.ps1`, log in |
| `mirror_failed` (critical) | An order could not be sent or its result is unknown; the bot paused | Open MarketWatch, check what actually happened, then resume |
| `reconcile_mismatch` | End of day: the game's holdings differ from the bot's | Compare by hand before the next session |
| "refusing to send orders" in the activity feed | The data feed is demo/historical | Switch to Connected mode |
