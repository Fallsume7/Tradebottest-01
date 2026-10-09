# CLAUDE.md — MarketWatchBot

Guidance for future Claude Code sessions. Read this first, then `docs/CLOUD_TO_LOCAL_HANDOFF.md` for status.

## What this is
A **paper-trading** system for a friends' virtual-stock competition: scanner → strategies → virtual broker → dashboard, plus a backtester and an adapter seam for a *future, authorised* MarketWatch Virtual Stock Exchange integration. Python 3.12+ / FastAPI / SQLAlchemy+SQLite / pandas+NumPy backend (`backend/mwbot`), React+TypeScript+Vite+Tailwind dashboard (`frontend`).

## Non-negotiable invariants
1. **No real money, ever.** Do not add brokerage adapters. `ExecutionAdapter.is_real_money` stays `False`; `EnvSettings.allow_real_money` must keep rejecting `true`; execution modes stay `paper | mock_browser` (add `marketwatch` only behind `assert_virtual_only`, after the permission gate in `docs/MARKETWATCH_INTEGRATION.md`).
2. **No look-ahead.** Strategies/scanner get only `MarketDataSource.contexts_at(t)` (bars with `label + 1min <= t`). Never pass `bar_at`/full-day frames to them. `tests/test_lookahead.py` must stay green. Orders never fill on the signal bar.
3. **Never fabricate market data.** Unavailable ⇒ `None` + scanner policy (`ignore`/`exclude`), shown as "-" in the UI. Estimates (RVOL) are labelled.
4. **Honest feed labels.** `FeedKind` ∈ synthetic/historical/delayed/realtime. Only a feed explicitly configured real-time may be called real-time. Delayed feeds block entries unless opted in.
5. **Never claim success that didn't happen.** Unfilled liquidation ⇒ `liquidation_complete=false` + critical alerts. Failed mirror orders ⇒ alert + pause.
6. **Timestamps are tz-aware UTC internally**, Chicago only for display. Naive datetimes are rejected.
7. **No secrets in git.** Keys live in `.env` (ignored). Never log query strings with keys (`_safe` in `data/http.py`). The dashboard must not ask for or store keys.
8. Do not claim a strategy is profitable. Synthetic results are labelled as such everywhere.

## Commands (repo root; venv active)
```bash
pip install -e "backend[dev]" && python -m playwright install chromium
ruff check backend && ruff format --check backend && (cd backend && mypy mwbot)
(cd backend && python -m pytest -m "not browser")     # ~4 min
(cd backend && python -m pytest -m browser)           # needs frontend/dist built + chromium
(cd frontend && npm ci && npm run typecheck && npm test && npm run build)
mwbot serve                                           # API + dashboard on :8000
mwbot demo-day --date 2026-10-08                      # headless synthetic day
mwbot backtest --dataset synthetic_sample --oos 0.3 --out reports/
MWBOT_SCREENSHOTS=1 python -m pytest backend/tests/test_dashboard_e2e.py   # regenerate docs/screenshots
```
Run ruff format + ruff check + mypy before committing (CI enforces them).

## Map
`domain.py` types · `config.py` settings · `calendar.py` NYSE sessions/schedule · `data/` providers (synthetic, csv, massive) · `scanner/` metrics+score · `strategies/` (A–E) · `trading/` fills, broker, risk · `bot/` engine (tick), runner, report · `backtest/` · `execution/` adapters (+`marketwatch.py` skeleton) · `mocksite/` fake venue · `db/` · `service.py` (AppService) · `api/app.py` · `cli.py`. Full detail: `docs/ARCHITECTURE.md`.

## Conventions
* Type-hinted, `from __future__ import annotations`, ruff (line length 110) + mypy clean. Pydantic for config/API, dataclasses (often `slots=True`) for hot-path domain objects.
* Tests live in `backend/tests`; build price paths with `tests/conftest.py::build_day` and `tests/scenarios.py`; a new strategy gets positive/negative/exit tests plus the automatic "tiny history" parametrised test.
* Frontend: strict TS, Tailwind tokens (`bg-panel`, `text-muted`, `text-up/down`), data via `src/api.ts`; mock fetch with `src/test/fixtures.ts::mockApi` (unmocked calls fail the test).
* Keep the UI honest: show "-" for missing values, label feed kind, show errors from the API (`ErrorBanner`).

## Gotchas learned the hard way
* **pandas 3** datetime indexes may use µs units: use `data/arrays.py::index_ns` (never raw `.asi8` vs `Timestamp.value`).
* A pydantic field named `date` shadows `datetime.date` under postponed annotations — import `datetime as dt` (see `api/app.py`).
* `@dataclass(slots=True)` objects have no `__dict__`: use `dataclasses.asdict`.
* numpy scalars (`np.bool_`, `np.float64`) are not JSON-serialisable by FastAPI: cast at the source (`float()`, `bool()`).
* **Playwright's sync API is thread-bound**: `MockBrowserAdapter` routes every call through its own worker thread (`@_on_worker`). Keep that for any new browser adapter.
* `.gitignore` uses anchored `/data/` so the `mwbot/data` package isn't ignored.
* Orders submitted at tick *T* first fill on bar *T+1min* (latency > 0 ⇒ next bar). This is intentional.
* The mock site and mock adapter are generic test doubles — they are **not** MarketWatch and share none of its markup.

## Status (see handoff doc for the full table)
Implemented & tested in the cloud: everything listed in the README. **Not verified**: Massive provider against the live service; anything on Windows locally; the wall-clock scheduler over real trading days; any MarketWatch interaction (intentionally absent).

## Priorities for next sessions
1. Local Windows bring-up (`docs/LOCAL_SETUP.md`), fix whatever differs.
2. Verify the Massive provider with the user's key; adjust parsing; add per-minute historical RVOL if the plan allows.
3. Collect real minute data (`mwbot download-dataset`) and re-run backtests with the out-of-sample split; do **not** tune on a single window.
4. After the user confirms permission: MarketWatch adapter per `docs/MARKETWATCH_INTEGRATION.md` (S1 read-only first).
5. Nice-to-haves: persist the adapter idempotency ledger, shorting, stop orders, websocket push to the UI, alert notifications (email/push), strategy walk-forward evaluation.
