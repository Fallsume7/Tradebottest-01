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
from mwbot.service import AppService, ServiceError

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


class BacktestRequest(BaseModel):
    name: str = "backtest"
    dataset: str | None = None
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
    def equity(request: Request) -> list[dict[str, Any]]:
        return svc(request).equity_history()

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

    # ---- backtests
    @app.get("/api/datasets")
    def datasets(request: Request) -> list[dict[str, Any]]:
        return svc(request).datasets()

    @app.post("/api/backtests")
    def run_backtest(request: Request, body: BacktestRequest) -> dict[str, str]:
        s = svc(request)
        risk = s.settings.risk.model_copy(update=body.risk)
        costs = s.settings.costs.model_copy(update=body.costs)
        cfg = BacktestConfig(
            name=body.name,
            strategies=body.strategies,
            strategy_params=body.strategy_params,
            dates=body.dates,
            start=body.start,
            end=body.end,
            initial_capital=body.initial_capital,
            risk=risk,
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
