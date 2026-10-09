# Local setup on Windows (PowerShell)

The CI workflow includes a Windows smoke job for the backend core; the full browser/dashboard flow was exercised only on Linux in the cloud session, so expect to fix small Windows differences. See *Troubleshooting*, and tell Claude Code (CLAUDE.md explains the project).

## 1. Prerequisites

Open **PowerShell** (not as administrator) and install what is missing:

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id OpenJS.NodeJS.LTS
winget install -e --id Git.Git
winget install -e --id Google.Chrome        # only needed for the future MarketWatch work; tests use Playwright's own Chromium
```

Close and re-open PowerShell, then check:

```powershell
py -3.12 --version     # Python 3.12.x
node --version         # v20+ (22 LTS recommended)
git --version
```

## 2. Get the code

```powershell
cd $HOME\Documents
git clone https://github.com/Fallsume7/Tradebottest-01.git
cd Tradebottest-01
git checkout claude/marketwatchbot-build     # or main, once the PR is merged
```

## 3. One-time setup

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

This creates `.venv`, installs the backend (`pip install -e "backend[dev]"`), downloads Playwright's Chromium, runs `npm ci` and builds the dashboard, and copies `.env.example` to `.env`.

Manual equivalent:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1                  # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -e "backend[dev]"
python -m playwright install chromium
cd frontend; npm ci; npm run build; cd ..
Copy-Item .env.example .env
```

## 4. Run it

```powershell
powershell -File scripts\run.ps1
```

Open <http://127.0.0.1:8000>. Press **Start** (top right) to run a synthetic trading day with the recommended settings (under *Dashboard → Mode & session* choose *Instant* for a few seconds or *Watch it* to stream).

The default is **demo mode — no keys required**. Everything is labelled SYNTHETIC in the header.

Developer mode with hot reload: `powershell -File scripts\dev.ps1` (backend :8000, Vite :5173).

## 5. Run the tests

```powershell
powershell -File scripts\test.ps1               # lint, types, backend tests incl. browser, frontend tests + build
powershell -File scripts\test.ps1 -SkipBrowser  # faster
```

## 6. Using real market data (optional, your own key)

1. Get a market-data key from your provider. The built-in provider speaks the Massive (formerly Polygon.io) REST schema. Which endpoints you can call, and whether data is real-time or 15-minute-delayed, depends on **your subscription**.
2. Edit `.env` (never commit it):
   ```
   MWBOT_MASSIVE_API_KEY=your_key_here
   MWBOT_MASSIVE_FEED=delayed        # or realtime — set what your plan actually provides
   MWBOT_MASSIVE_DELAY_SECONDS=900
   ```
3. Restart, then **Settings → Data mode → Connected**. The header shows *DELAYED ~15 MIN* or *REAL-TIME*, as you configured. On a delayed feed the bot **will not open positions** unless you switch on *Allow trading on a delayed feed*.
4. The first run against the live service is the first time that code meets real responses: watch the log (`logs` card / console) for provider warnings and tell Claude Code about any schema differences.
5. Download real minute bars for backtests: `.\.venv\Scripts\mwbot.exe download-dataset --symbols AAA BBB --start 2026-09-01 --end 2026-09-30 --out data\mydata`, then point *Historical dataset folder* at it (set `MWBOT_HISTORICAL_DIR=.\data`).

## 7. Keeping it running (optional)

* **Stop Windows sleeping while the market is open:** `powercfg /change standby-timeout-ac 0` (revert afterwards).
* **Start at logon:** Task Scheduler → Create Task → trigger *At log on* → action *Start a program*: `powershell.exe`, arguments `-NoProfile -File "C:\path\to\tradebottest-01\scripts\run.ps1"`.
* Turn on **Settings → Run on schedule** to let the live-clock loop work on real trading days (08:00 CT scanner start → report). It only acts while the app is running. The cloud session cannot do this for you.
* Your computer's time zone doesn't matter: the app converts to America/Chicago and takes market hours from the NYSE calendar.

## 8. Where things live

| What | Where |
|---|---|
| Database | `data\mwbot.db` (SQLite; delete to start fresh, or use *Settings → Reset account*) |
| Daily reports | `data\reports\daily_YYYY-MM-DD.md` |
| Settings | in the database; edited through the dashboard |
| Secrets | `.env` only (git-ignored) |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `running scripts is disabled` | Use `powershell -ExecutionPolicy Bypass -File ...`, or `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `py` not found | Re-open PowerShell after installing Python; or use `python` if it is 3.12+ |
| Port 8000 busy | `mwbot serve --port 8010` (or set `MWBOT_PORT`) |
| Dashboard shows "Cannot reach the backend" | Backend not running / different port; with `npm run dev` set `$env:MWBOT_API="http://127.0.0.1:8010"` first |
| Playwright can't find a browser | `python -m playwright install chromium`; or set `MWBOT_CHROMIUM_PATH` to a `chrome.exe` |
| Blank dashboard at `/` | `frontend\dist` missing: `cd frontend; npm run build` |
| Firewall prompt | Allow on *private* networks only; the server binds to 127.0.0.1 by default. The API has **no authentication** — never bind it to 0.0.0.0 / expose it to a network |
