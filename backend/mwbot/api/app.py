"""FastAPI application: JSON API under /api, built dashboard served at /."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from mwbot import __version__
from mwbot.backtest.engine import BacktestConfig
from mwbot.config import PRESET_FILTERS, EnvSettings
from mwbot.logging_setup import configure_logging
from mwbot.service import AppService, ServiceError, SettingsError

log = logging.getLogger(__name__)


class StartSession(BaseModel):
    date: dt.date | None = None
    speed: float | None = Field(None, gt=0, le=1000)
    instant: bool = False


class StepRequest(BaseModel):
    n: int = Field(1, ge=1, le=120)


class OrderIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=10)
    side: str = Field(pattern="^(buy|sell)$")
    quantity: int = Field(gt=0, le=10_000_000)
    order_type: str = Field("market", pattern="^(market|limit)$")
    limit_price: float | None = Field(None, gt=0)
    client_order_id: str | None = None


class ChangesIn(BaseModel):
    changes: dict[str, Any]


class ResetIn(BaseModel):
    category: str | None = None


class ModeIn(BaseModel):
    mode: str


class ImportIn(BaseModel):
    payload: dict[str, Any]
    apply: bool = True


class EligibilityIn(BaseModel):
    status: str
    name: str = ""
    exchange: str = ""
    rejection_reason: str = ""
    restrictions: str = ""
    effective_from: dt.date | None = None


class CsvIn(BaseModel):
    csv: str = Field(max_length=5_000_000)
    dry_run: bool = False


class CompareIn(BaseModel):
    dataset: str | None = None
    start: dt.date | None = None
    end: dt.date | None = None
    dates: list[dt.date] | None = None
    forecaster: str = Field("heuristic", pattern="^(heuristic|statistical)$")
    eligibility: str = Field("point_in_time", pattern="^(point_in_time|none)$")
    oos_fraction: float = Field(0.0, ge=0.0, le=0.8)


class ModelCheckIn(BaseModel):
    dataset: str
    max_days: int | None = Field(None, ge=4, le=500)


class BacktestRequest(BaseModel):
    name: str = "backtest"
    dataset: str | None = None
    mode: str = "max_expected_return"
    eligibility: str = Field("point_in_time", pattern="^(point_in_time|none)$")
    forecaster: str = Field("heuristic", pattern="^(heuristic|statistical)$")
    strategies: list[str] = Field(min_length=1)
    strategy_params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    compare: bool = False
    start: dt.date | None = None
    end: dt.date | None = None
    dates: list[dt.date] | None = None
    initial_capital: float = Field(100_000.0, gt=0)
    oos_fraction: float = Field(0.0, ge=0.0, le=0.8)
    risk: dict[str, Any] = Field(default_factory=dict)
    costs: dict[str, Any] = Field(default_factory=dict)


def create_app(service: AppService | None = None, env: EnvSettings | None = None) -> FastAPI:
    env = env or EnvSettings()
    owned = service is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if owned:
            configure_logging(env.log_level, env.log_json)
            app.state.service = AppService(env)
        app.state.service.start_scheduler()
        yield
        if owned:
            app.state.service.close()

    app = FastAPI(title="MarketWatchBot", version=__version__, lifespan=lifespan)
    if service is not None:
        app.state.service = service
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def svc(request: Request) -> AppService:
        s: AppService = request.app.state.service
        return s

    @app.exception_handler(SettingsError)
    async def _settings_error(_: Request, exc: SettingsError) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "errors": exc.errors}, status_code=400)

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "real_money": False}

    @app.get("/api/status")
    def status(request: Request) -> dict[str, Any]:
        return svc(request).status()

    # ---- session / bot control
    @app.post("/api/session/start")
    def start(request: Request, body: StartSession | None = None) -> dict[str, Any]:
        b = body or StartSession()
        return svc(request).start_session(b.date, b.speed, b.instant)

    @app.post("/api/session/step")
    def step(request: Request, body: StepRequest | None = None) -> dict[str, Any]:
        return svc(request).step_session((body or StepRequest()).n)

    @app.post("/api/session/pause-stream")
    def pause_stream(request: Request) -> dict[str, str]:
        svc(request).pause_stream()
        return {"status": "paused_stream"}

    @app.post("/api/session/resume-stream")
    def resume_stream(request: Request) -> dict[str, str]:
        svc(request).resume_stream()
        return {"status": "running"}

    @app.post("/api/session/stop")
    def stop(request: Request) -> dict[str, str]:
        svc(request).stop_session()
        return {"status": "idle"}

    @app.post("/api/bot/{action}")
    def bot_action(request: Request, action: str) -> dict[str, Any]:
        return svc(request).control(action)

    @app.post("/api/account/reset")
    def reset(request: Request) -> dict[str, str]:
        svc(request).reset_account()
        return {"status": "reset"}

    # ---- read models
    @app.get("/api/portfolio/positions")
    def positions(request: Request) -> list[dict[str, Any]]:
        return svc(request).positions()

    @app.get("/api/portfolio/orders")
    def orders(request: Request) -> list[dict[str, Any]]:
        return svc(request).orders()

    @app.get("/api/portfolio/trades")
    def trades(request: Request) -> list[dict[str, Any]]:
        return svc(request).trades()

    @app.get("/api/portfolio/equity")
    def equity(request: Request, range: str | None = None) -> Any:  # noqa: A002 - query name is part of the API
        if range:
            return svc(request).equity_series(range)
        return svc(request).equity_history()

    @app.post("/api/positions/{symbol}/close")
    def close_position(request: Request, symbol: str) -> dict[str, Any]:
        return svc(request).close_position(symbol)

    @app.get("/api/activity")
    def activity(request: Request, limit: int = 100) -> list[dict[str, Any]]:
        return svc(request).activity(limit)

    # ---- opportunities
    @app.get("/api/opportunities")
    def opportunities(request: Request) -> dict[str, Any]:
        return svc(request).opportunities()

    @app.get("/api/opportunities/{symbol}")
    def opportunity(request: Request, symbol: str) -> dict[str, Any]:
        return svc(request).opportunity(symbol)

    # ---- MarketWatch eligibility
    @app.get("/api/eligibility")
    def eligibility_list(
        request: Request, q: str = "", status: str = "all", limit: int = 500
    ) -> dict[str, Any]:
        s = svc(request)
        return {"summary": s.eligibility_summary(), "rows": s.eligibility_list(q, status, min(limit, 2000))}

    @app.get("/api/eligibility/summary")
    def eligibility_summary(request: Request) -> dict[str, Any]:
        return svc(request).eligibility_summary()

    @app.get("/api/eligibility/export.csv")
    def eligibility_export(request: Request) -> PlainTextResponse:
        return PlainTextResponse(
            svc(request).eligibility_export_csv(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="marketwatch_eligibility.csv"'},
        )

    @app.post("/api/eligibility/import")
    def eligibility_import(request: Request, body: CsvIn) -> dict[str, Any]:
        return svc(request).eligibility_import_csv(body.csv, body.dry_run)

    @app.post("/api/eligibility/seed-demo")
    def eligibility_seed(request: Request) -> dict[str, int]:
        return {"added": svc(request).eligibility_seed_demo()}

    @app.delete("/api/eligibility")
    def eligibility_clear(request: Request) -> dict[str, str]:
        svc(request).eligibility_clear()
        return {"status": "cleared"}

    @app.get("/api/eligibility/{ticker}/history")
    def eligibility_history(request: Request, ticker: str) -> list[dict[str, Any]]:
        return svc(request).eligibility_history(ticker)

    @app.put("/api/eligibility/{ticker}")
    def eligibility_set(request: Request, ticker: str, body: EligibilityIn) -> dict[str, Any]:
        return svc(request).eligibility_set(
            ticker,
            body.status,
            name=body.name,
            exchange=body.exchange,
            rejection_reason=body.rejection_reason,
            restrictions=body.restrictions,
            effective_from=body.effective_from,
        )

    @app.delete("/api/eligibility/{ticker}")
    def eligibility_delete(request: Request, ticker: str) -> dict[str, int]:
        return {"deleted": svc(request).eligibility_delete(ticker)}

    @app.get("/api/performance")
    def performance(request: Request) -> dict[str, Any]:
        return svc(request).performance()

    @app.get("/api/decisions")
    def decisions(request: Request, limit: int = 200) -> list[dict[str, Any]]:
        return svc(request).decisions(min(limit, 1000))

    @app.get("/api/reports")
    def reports(request: Request) -> list[dict[str, Any]]:
        return svc(request).reports()

    @app.get("/api/logs")
    def logs(request: Request) -> list[dict[str, Any]]:
        return svc(request).logs()

    # ---- scanner / stocks
    @app.get("/api/scanner")
    def scanner(request: Request) -> dict[str, Any]:
        return svc(request).scan()

    @app.get("/api/scanner/presets")
    def presets() -> dict[str, Any]:
        return PRESET_FILTERS

    @app.post("/api/scanner/preset/{name}")
    def apply_preset(request: Request, name: str) -> dict[str, Any]:
        return svc(request).apply_scanner_preset(name).model_dump(mode="json")

    @app.get("/api/stocks/{symbol}")
    def stock(request: Request, symbol: str) -> dict[str, Any]:
        return svc(request).stock(symbol)

    @app.post("/api/orders")
    def place_order(request: Request, body: OrderIn) -> dict[str, Any]:
        return svc(request).manual_order(
            body.symbol, body.side, body.quantity, body.order_type, body.limit_price, body.client_order_id
        )

    @app.delete("/api/orders/{order_id}")
    def cancel_order(request: Request, order_id: str) -> dict[str, bool]:
        return {"canceled": svc(request).cancel_order(order_id)}

    # ---- strategies / settings
    @app.get("/api/strategies")
    def strategies(request: Request) -> list[dict[str, Any]]:
        return svc(request).strategies()

    @app.get("/api/settings")
    def get_settings(request: Request) -> dict[str, Any]:
        s = svc(request)
        return {
            "settings": s.settings.model_dump(mode="json"),
            "env": {
                "data_mode_default": s.env.data_mode,
                "execution_mode": s.env.execution_mode,
                "massive_key_configured": bool(s.env.massive_api_key),
                "massive_feed": s.env.massive_feed,
                "timezone": "America/Chicago",
                "real_money": False,
            },
        }

    @app.put("/api/settings")
    def put_settings(request: Request, patch: dict[str, Any]) -> dict[str, Any]:
        return svc(request).update_settings(patch).model_dump(mode="json")

    @app.get("/api/settings/schema")
    def settings_schema(request: Request) -> dict[str, Any]:
        return svc(request).settings_schema()

    @app.post("/api/settings/changes")
    def settings_changes(request: Request, body: ChangesIn) -> dict[str, Any]:
        return svc(request).update_changes(body.changes)

    @app.post("/api/settings/preview")
    def settings_preview(request: Request, body: ChangesIn) -> dict[str, Any]:
        return svc(request).preview_changes(body.changes)

    @app.post("/api/settings/reset")
    def settings_reset(request: Request, body: ResetIn | None = None) -> dict[str, Any]:
        return svc(request).reset_settings((body or ResetIn()).category)

    @app.get("/api/settings/export")
    def settings_export(request: Request) -> JSONResponse:
        return JSONResponse(
            svc(request).export_settings(),
            headers={"Content-Disposition": 'attachment; filename="marketwatchbot_settings.json"'},
        )

    @app.post("/api/settings/import")
    def settings_import(request: Request, body: ImportIn) -> dict[str, Any]:
        return svc(request).import_settings(body.payload, body.apply)

    @app.post("/api/mode")
    def set_mode(request: Request, body: ModeIn) -> dict[str, Any]:
        return svc(request).set_mode(body.mode)

    # ---- strategy lab
    @app.get("/api/strategy-lab/latest")
    def strategy_lab_latest(request: Request) -> dict[str, Any]:
        return svc(request).strategy_lab_latest()

    @app.post("/api/compare-modes")
    def compare_modes_route(request: Request, body: CompareIn) -> dict[str, str]:
        s = svc(request)
        cfg = BacktestConfig(
            name="Mode comparison",
            dates=body.dates,
            start=body.start,
            end=body.end,
            initial_capital=s.settings.risk.starting_capital,
            costs=s.settings.effective_costs(),
            scanner=s.settings.scanner,
            game=s.settings.game.model_copy(
                update={
                    "eligibility_mode": "point_in_time"
                    if body.eligibility == "point_in_time"
                    else "current_list"
                }
            ),
            eligibility=body.eligibility,
            forecaster=body.forecaster,
            oos_fraction=body.oos_fraction,
        )
        return {"job_id": s.start_compare_modes(cfg, body.dataset)}

    @app.post("/api/model-check")
    def model_check(request: Request, body: ModelCheckIn) -> dict[str, str]:
        return {"job_id": svc(request).start_model_check(body.dataset, body.max_days)}

    # ---- backtests
    @app.get("/api/datasets")
    def datasets(request: Request) -> list[dict[str, Any]]:
        return svc(request).datasets()

    @app.post("/api/backtests")
    def run_backtest(request: Request, body: BacktestRequest) -> dict[str, str]:
        s = svc(request)
        costs = s.settings.effective_costs().model_copy(update=body.costs)
        try:
            from mwbot.config import TradingMode

            mode = TradingMode(body.mode)
        except ValueError as exc:
            raise ServiceError("unknown trading mode") from exc
        cfg = BacktestConfig(
            name=body.name,
            mode=mode,
            eligibility=body.eligibility,
            forecaster=body.forecaster,
            game=s.settings.game.model_copy(
                update={
                    "eligibility_mode": "point_in_time"
                    if body.eligibility == "point_in_time"
                    else "current_list"
                }
            ),
            strategies=body.strategies,
            strategy_params=body.strategy_params,
            dates=body.dates,
            start=body.start,
            end=body.end,
            initial_capital=body.initial_capital,
            risk=s.settings.risk.model_copy(update=body.risk) if body.risk else None,
            costs=costs,
            scanner=s.settings.scanner,
            oos_fraction=body.oos_fraction,
        )
        job = s.start_backtest(cfg, body.dataset, compare=body.strategies if body.compare else None)
        return {"job_id": job}

    @app.get("/api/backtests")
    def list_backtests(request: Request) -> list[dict[str, Any]]:
        return svc(request).store.list_backtests()

    @app.get("/api/backtests/jobs/{job_id}")
    def job(request: Request, job_id: str) -> dict[str, Any]:
        return svc(request).job(job_id)

    @app.get("/api/backtests/{bt_id}")
    def get_backtest(request: Request, bt_id: str) -> dict[str, Any]:
        r = svc(request).store.get_backtest(bt_id)
        if r is None:
            raise ServiceError("backtest not found")
        return r

    @app.get("/api/backtests/{bt_id}/export/{kind}.csv")
    def export(request: Request, bt_id: str, kind: str) -> PlainTextResponse:
        if kind not in ("trades", "summary"):
            raise ServiceError("kind must be trades or summary")
        return PlainTextResponse(
            svc(request).backtest_csv(bt_id, kind),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="backtest_{bt_id}_{kind}.csv"'},
        )

    # ---- static dashboard
    dist = (
        Path(env.frontend_dist)
        if env.frontend_dist
        else Path(__file__).resolve().parents[3] / "frontend" / "dist"
    )
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> FileResponse:
            f = dist / full_path
            return FileResponse(f if full_path and f.is_file() else dist / "index.html")

    return app


def app_factory() -> FastAPI:  # for `uvicorn mwbot.api.app:app_factory --factory`
    return create_app()
