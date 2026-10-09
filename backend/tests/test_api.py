"""HTTP API against a real AppService with a temp SQLite DB (demo mode, no credentials)."""

import time

import pytest
from fastapi.testclient import TestClient

from mwbot.api.app import create_app
from mwbot.config import EnvSettings
from mwbot.service import AppService

from .conftest import SAMPLE_DIR


@pytest.fixture()
def svc(tmp_env):
    s = AppService(tmp_env)
    yield s
    s.close()


@pytest.fixture()
def client(svc):
    app = create_app(svc)
    with TestClient(app) as c:
        yield c


def run_instant_day(client, svc, **body):
    r = client.post("/api/session/start", json={"instant": True, **body})
    assert r.status_code == 200, r.text
    svc.wait_for_session(180)
    return client.get("/api/status").json()


def test_health_and_default_status_is_demo_and_labelled(client):
    assert client.get("/api/health").json() == {"ok": True, "version": "0.1.0", "real_money": False}
    st = client.get("/api/status").json()
    assert st["data_mode"] == "demo" and st["real_money"] is False
    assert st["feed"]["kind"] == "synthetic" and "SYNTHETIC" in st["feed"]["label"] and st["feed"]["is_realtime"] is False
    assert st["portfolio"]["equity"] == 100_000 and st["session_status"] == "idle" and st["bot_state"] == "stopped"


def test_full_demo_day_through_the_api(client, svc):
    st = run_instant_day(client, svc)
    assert st["session_status"] == "finished" and st["phase"] == "post_close"
    assert st["session_date"] and st["schedule"]["close"] == "15:00"
    for ep in ["portfolio/positions", "portfolio/orders", "portfolio/trades", "portfolio/equity", "performance",
               "decisions", "reports", "scanner", "strategies", "settings", "datasets", "backtests", "logs"]:
        r = client.get(f"/api/{ep}")
        assert r.status_code == 200, (ep, r.text[:200])
    trades = client.get("/api/portfolio/trades").json()
    report = client.get("/api/reports").json()[-1]
    assert report["n_trades"] == len([t for t in trades if t["session_date"] == report["date"]])
    assert report["liquidation_complete"] in (True, False) and report["benchmark_return_pct"] is not None
    perf = client.get("/api/performance").json()
    assert perf["equity"] == pytest.approx(st["portfolio"]["equity"]) and perf["daily"][0]["date"] == report["date"]
    assert client.get("/api/portfolio/equity").json()
    scan = client.get("/api/scanner").json()
    assert scan["results"] and {"score", "components", "reasons", "metrics"} <= set(scan["results"][0])
    # the persisted report file the user can open
    assert any(p.name.startswith("daily_") for p in (svc.env.data_dir and __import__("pathlib").Path(svc.env.data_dir) / "reports").iterdir())


def test_second_session_advances_to_next_trading_day(client, svc):
    d1 = run_instant_day(client, svc)["session_date"]
    d2 = run_instant_day(client, svc)["session_date"]
    assert d2 > d1


def test_cannot_start_two_sessions_or_change_settings_mid_session(client, svc):
    assert client.post("/api/session/start", json={"speed": 1}).status_code == 200
    try:
        r = client.post("/api/session/start", json={})
        assert r.status_code == 400 and "already running" in r.json()["detail"]
        r = client.put("/api/settings", json={"risk": {"starting_capital": 5}})
        assert r.status_code == 400 and "Stop the running session" in r.json()["detail"]
    finally:
        client.post("/api/session/stop")
    assert client.get("/api/status").json()["session_status"] == "idle"


def test_step_through_a_session_manually(client, svc):
    client.post("/api/session/start", json={"speed": 1})
    client.post("/api/session/pause-stream")
    t0 = client.get("/api/status").json()["sim_time_utc"]
    st = client.post("/api/session/step", json={"n": 5}).json()
    assert st["sim_time_utc"] > t0 and st["session_status"] == "paused_stream"
    stock = client.get("/api/stocks/" + client.get("/api/scanner").json()["results"][0]["symbol"]).json()
    assert stock["bars"] and stock["evaluations"] and len(stock["evaluations"]) == 5 and "conditions" in stock["evaluations"][0]
    assert client.get("/api/stocks/NOPE").status_code == 400


def test_stock_detail_requires_a_session(client):
    r = client.get("/api/stocks/ZNTR")
    assert r.status_code == 400 and "Start a session" in r.json()["detail"]


def test_settings_validation_and_persistence(client, svc, tmp_env):
    ok = client.put("/api/settings", json={"risk": {"starting_capital": 50_000, "max_positions": 3}})
    assert ok.status_code == 200 and ok.json()["risk"]["starting_capital"] == 50_000
    assert client.get("/api/status").json()["portfolio"]["equity"] == 50_000
    assert client.put("/api/settings", json={"risk": {"starting_capital": -5}}).status_code == 400
    assert client.put("/api/settings", json={"schedule": {"last_entry": "15:30", "liquidation_start": "14:45"}}).status_code == 400
    assert client.put("/api/settings", json={"strategies": {"enabled": ["nonsense"]}}).status_code == 400
    assert client.put("/api/settings", json={"strategies": {"params": {"opening_momentum": {"stop_loss_pct": -1}}}}).status_code == 400
    # persisted for the next process
    again = AppService(tmp_env)
    assert again.settings.risk.max_positions == 3
    again.close()


def test_scanner_presets(client):
    p = client.get("/api/scanner/presets").json()
    assert set(p) == {"penny_stocks", "small_cap_momentum", "broad_us_equities"}
    r = client.post("/api/scanner/preset/penny_stocks")
    assert r.status_code == 200 and r.json()["scanner"]["filters"]["max_price"] == 5.0
    assert client.post("/api/scanner/preset/bogus").status_code == 400


def test_manual_orders_idempotency_and_rejections(client, svc):
    client.post("/api/session/start", json={"speed": 1})
    client.post("/api/session/pause-stream")
    client.post("/api/session/step", json={"n": 60})
    sym = client.get("/api/scanner").json()["results"][0]["symbol"]
    body = {"symbol": sym, "side": "buy", "quantity": 10, "client_order_id": "ui-1"}
    a, b = client.post("/api/orders", json=body).json(), client.post("/api/orders", json=body).json()
    assert a["id"] == b["id"] and a["status"] == "pending"
    client.post("/api/session/step", json={"n": 3})
    orders = client.get("/api/portfolio/orders").json()
    filled = next(o for o in orders if o["id"] == a["id"])
    assert filled["status"] == "filled" and filled["filled_quantity"] == 10 and filled["strategy"] == "manual"
    # the bot manages manual positions with the same hard exits, so it may already be closed again: either is valid
    assert any(p["symbol"] == sym for p in client.get("/api/portfolio/positions").json()) or \
        any(t["symbol"] == sym and t["strategy"] == "manual" for t in client.get("/api/portfolio/trades").json())
    over = client.post("/api/orders", json={"symbol": sym, "side": "sell", "quantity": 999_999}).json()
    assert over["status"] == "rejected" and "no shorting" in over["reject_reason"]
    assert client.post("/api/orders", json={"symbol": sym, "side": "hold", "quantity": 1}).status_code == 422
    assert client.post("/api/orders", json={"symbol": sym, "side": "buy", "quantity": 0}).status_code == 422
    client.post("/api/session/stop")


def test_bot_controls_and_emergency_stop(client):
    assert client.post("/api/bot/start").json()["state"] == "running"
    assert client.post("/api/bot/pause").json()["state"] == "paused"
    assert client.post("/api/bot/resume").json()["state"] == "running"
    assert client.post("/api/bot/emergency_stop").json()["state"] == "emergency_stop"
    assert client.post("/api/session/start", json={}).status_code == 400    # blocked while e-stop is active
    assert client.post("/api/bot/clear_emergency").json()["state"] == "stopped"
    assert client.post("/api/bot/explode").status_code == 400


def test_account_reset(client, svc):
    run_instant_day(client, svc)
    assert client.post("/api/account/reset").status_code == 200
    st = client.get("/api/status").json()
    assert st["portfolio"]["equity"] == 100_000 and client.get("/api/portfolio/trades").json() == []
    assert client.get("/api/reports").json() == []


def test_historical_mode_replays_dataset_in_order(client, svc):
    assert client.put("/api/settings", json={"data_mode": "historical"}).status_code == 200
    st = client.get("/api/status").json()
    assert st["feed"]["available"] and st["feed"]["kind"] == "synthetic"   # the bundled sample is synthetic and says so
    s1 = run_instant_day(client, svc)
    assert s1["session_date"] == "2026-09-14"
    assert run_instant_day(client, svc)["session_date"] == "2026-09-15"


def test_connected_mode_without_key_fails_clearly_and_safely(client):
    assert client.put("/api/settings", json={"data_mode": "connected"}).status_code == 200
    st = client.get("/api/status").json()
    assert st["feed"]["available"] is False and "API_KEY" in st["provider_error"]
    r = client.post("/api/session/start", json={})
    assert r.status_code == 400 and "API_KEY" in r.json()["detail"]


def test_backtest_job_lifecycle_and_csv_export(client, svc):
    body = {"name": "api-bt", "dataset": "synthetic_sample", "strategies": ["opening_momentum"],
            "dates": ["2026-09-14", "2026-09-15"]}
    job = client.post("/api/backtests", json=body).json()["job_id"]
    for _ in range(600):
        j = client.get(f"/api/backtests/jobs/{job}").json()
        if j["status"] in ("done", "error"):
            break
        time.sleep(0.2)
    assert j["status"] == "done", j
    bt_id = j["result_ids"][0]
    res = client.get(f"/api/backtests/{bt_id}").json()
    assert res["summary"]["n_days"] == 2 and res["result"]["warnings"] and res["result"]["feed"]["kind"] == "synthetic"
    assert any(b["id"] == bt_id for b in client.get("/api/backtests").json())
    csv_trades = client.get(f"/api/backtests/{bt_id}/export/trades.csv")
    assert csv_trades.status_code == 200 and csv_trades.text.startswith("id,session_date,symbol")
    assert client.get(f"/api/backtests/{bt_id}/export/summary.csv").text.startswith("metric,value")
    assert client.get(f"/api/backtests/{bt_id}/export/other.csv").status_code == 400
    assert client.get("/api/backtests/zzz").status_code == 400
    assert client.get("/api/backtests/jobs/zzz").status_code == 400


def test_backtest_rejects_unknown_strategy_and_dataset(client):
    assert client.post("/api/backtests", json={"strategies": ["magic"], "dataset": "synthetic_sample"}).status_code == 400
    assert client.post("/api/backtests", json={"strategies": ["opening_momentum"], "dataset": "missing"}).status_code == 400
    assert client.post("/api/backtests", json={"strategies": []}).status_code == 422


def test_datasets_listing_marks_synthetic(client):
    ds = client.get("/api/datasets").json()
    assert ds and ds[0]["id"] == "synthetic_sample" and ds[0]["synthetic"] is True


def test_real_money_flag_is_rejected_at_startup(monkeypatch):
    monkeypatch.setenv("MWBOT_ALLOW_REAL_MONEY", "true")
    with pytest.raises(Exception, match="no real-money"):
        EnvSettings(_env_file=None)


def test_session_loop_crash_is_surfaced_not_swallowed(client, svc, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaput")

    monkeypatch.setattr(svc.bot, "tick", boom)
    client.post("/api/session/start", json={"instant": True})
    svc.wait_for_session(30)
    st = client.get("/api/status").json()
    assert st["session_status"] == "error" and "kaput" in st["session_error"]
    assert st["alerts_critical"] >= 1


def test_restart_recovery_flags_open_exposure(tmp_env):
    s1 = AppService(tmp_env)
    app = create_app(s1)
    with TestClient(app) as c:
        c.post("/api/session/start", json={"speed": 1})
        c.post("/api/session/pause-stream")
        c.post("/api/session/step", json={"n": 70})
        sym = c.get("/api/scanner").json()["results"][0]["symbol"]
        c.post("/api/orders", json={"symbol": sym, "side": "buy", "quantity": 5})
        c.post("/api/session/step", json={"n": 3})
        assert c.get("/api/portfolio/positions").json()
    s1.close()
    s2 = AppService(tmp_env)         # simulated restart
    try:
        assert s2.restored and s2.broker.positions
        assert any(a["code"] == "restart_recovery" for a in s2.bot.alerts)
        assert s2.status()["session_status"] == "idle"
    finally:
        s2.close()
