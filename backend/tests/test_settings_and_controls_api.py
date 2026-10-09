"""Settings workflow, mode switching, bot controls, eligibility and opportunities through the HTTP API."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from mwbot.api.app import create_app
from mwbot.service import AppService


@pytest.fixture()
def svc(tmp_env):
    s = AppService(tmp_env)
    yield s
    s.close()


@pytest.fixture()
def client(svc):
    with TestClient(create_app(service=svc)) as c:
        yield c


def changes(client, **kv):
    return client.post(
        "/api/settings/changes", json={"changes": {k.replace("__", "."): v for k, v in kv.items()}}
    )


def schema(client):
    return client.get("/api/settings/schema").json()


def field(client, path):
    return next(f for f in schema(client)["fields"] if f["path"] == path)


# ------------------------------------------------------------------ schema, defaults, validation
def test_default_works_out_of_the_box_and_schema_describes_every_category(client):
    sc = schema(client)
    assert [c["label"] for c in sc["categories"]] == [
        "Trading Strategy",
        "Risk Management",
        "Trading Schedule",
        "MarketWatch Game",
        "Market Data",
        "General",
    ]
    assert sc["mode"]["active"] == "max_expected_return"
    assert next(m for m in sc["mode"]["modes"] if m["id"] == "max_expected_return")["recommended"]
    cats = {f["category"] for f in sc["fields"]}
    assert cats == {c["id"] for c in sc["categories"]}
    for f in sc[
        "fields"
    ]:  # everything has plain-language help, a control, a recommended value and an effect note
        assert f["help"] and f["control"] and f["effect_text"] and "recommended" in f and "value" in f
    assert all(f["is_recommended"] for f in sc["fields"]), (
        "a fresh install must be on the recommended defaults"
    )


def test_changes_are_saved_persisted_and_reported(client, svc, tmp_env):
    r = changes(client, risk__max_positions=3, mode__gates__min_p_positive=0.6)
    assert r.status_code == 200
    body = r.json()
    assert body["saved_at"] and {e["path"] for e in body["effects"]} == {
        "risk.max_positions",
        "mode.gates.min_p_positive",
    }
    assert field(client, "risk.max_positions")["value"] == 3
    assert not field(client, "risk.max_positions")["is_recommended"]
    # persisted: a brand-new service on the same database sees it
    again = AppService(tmp_env)
    try:
        assert again.settings.risk.max_positions == 3
        assert again.settings.profile_for().gates.min_p_positive == 0.6
    finally:
        again.close()


@pytest.mark.parametrize(
    "path,value,fragment",
    [
        ("risk.max_position_pct", 500, "at most"),
        ("risk.max_positions", 0, "at least"),
        ("risk.max_positions", "many", "number"),
        ("schedule.last_entry", "25:99", "HH:MM"),
        ("trading.mode", "yolo", "mode"),
        ("game.supported_order_types", [], "at least one"),
        ("ui.theme", "neon", "Pick one"),
        ("not.a.setting", 1, "Unknown"),
    ],
)
def test_server_rejects_bad_values_with_field_level_messages(client, path, value, fragment):
    r = client.post("/api/settings/changes", json={"changes": {path: value}})
    assert r.status_code == 400
    j = r.json()
    assert j["errors"] and j["errors"][0]["path"] == path and fragment.lower() in j["detail"].lower()


def test_cross_field_rules_are_enforced_by_the_backend(client):
    r = changes(client, schedule__last_entry="15:00", schedule__liquidation_start="14:30")
    assert r.status_code == 400 and "last_entry" in r.json()["detail"]
    assert field(client, "schedule.last_entry")["value"] == "14:15"  # nothing half-saved


def test_preview_explains_effects_without_saving(client):
    r = client.post("/api/settings/preview", json={"changes": {"risk.max_positions": 2}})
    j = r.json()
    assert j["valid"] and j["effects"][0]["message"] and field(client, "risk.max_positions")["value"] == 5
    bad = client.post("/api/settings/preview", json={"changes": {"risk.max_positions": 0}}).json()
    assert not bad["valid"] and bad["errors"]


def test_reset_to_recommended_per_category_and_all(client):
    changes(client, risk__max_positions=2, ui__theme="light", mode__gates__min_p_positive=0.7)
    client.post("/api/settings/reset", json={"category": "risk"})
    assert field(client, "risk.max_positions")["is_recommended"]
    assert field(client, "ui.theme")["value"] == "light"  # other categories untouched
    client.post("/api/settings/reset", json={})
    assert all(f["is_recommended"] for f in schema(client)["fields"])


def test_reset_keeps_your_mode_and_data_choices(client):
    client.post("/api/mode", json={"mode": "aggressive_upside"})
    client.post("/api/settings/reset", json={})
    assert schema(client)["mode"]["active"] == "aggressive_upside"
    # every other setting is back on the recommended value FOR that mode (only the mode choice itself differs)
    assert all(f["is_recommended"] for f in schema(client)["fields"] if f["path"] != "trading.mode")


def test_export_import_round_trip_and_rejects_foreign_files(client):
    changes(client, risk__max_positions=4, ui__simple_mode=False)
    exported = client.get("/api/settings/export").json()
    assert exported["app"] == "MarketWatchBot" and "api_key" not in str(exported).lower()
    client.post("/api/settings/reset", json={})
    assert field(client, "risk.max_positions")["value"] == 5
    dry = client.post("/api/settings/import", json={"payload": exported, "apply": False}).json()
    assert (
        dry["applied"] is False
        and dry["n_changes"] >= 2
        and field(client, "risk.max_positions")["value"] == 5
    )
    done = client.post("/api/settings/import", json={"payload": exported}).json()
    assert done["applied"] and field(client, "risk.max_positions")["value"] == 4
    assert client.post("/api/settings/import", json={"payload": {"hello": 1}}).status_code == 400
    broken = {**exported, "settings": {**exported["settings"], "risk": {"max_positions": -3}}}
    assert client.post("/api/settings/import", json={"payload": broken}).status_code == 400


# ------------------------------------------------------------------ modes
def test_mode_switch_preserves_each_modes_own_risk_and_history(client, svc):
    changes(client, risk__max_positions=3)  # a custom MER setting
    r = client.post("/api/mode", json={"mode": "aggressive_upside"}).json()
    assert r["mode"]["to"] == "aggressive_upside" and "Trade history is unchanged" in r["mode"]["message"]
    assert svc.settings.risk.max_position_pct == 30.0  # aggressive sizing is active
    client.post("/api/mode", json={"mode": "max_expected_return"})
    assert svc.settings.risk.max_positions == 3  # the custom MER setting survived the round trip
    assert client.post("/api/mode", json={"mode": "bogus"}).status_code == 400


def test_mode_switch_with_open_positions_says_what_happens_to_them(client, svc):
    client.post("/api/session/start", json={"instant": True})
    svc.wait_for_session(300)
    prev = len(svc.broker.trades)
    r = client.post("/api/mode", json={"mode": "aggressive_upside"})
    assert r.status_code == 200 and len(svc.broker.trades) == prev  # history untouched
    pre = client.post(
        "/api/settings/preview", json={"changes": {"trading.mode": "max_expected_return"}}
    ).json()
    assert any(e["path"] == "trading.mode" for e in pre["effects"])


# ------------------------------------------------------------------ live changes + locked changes
def test_settings_change_live_while_a_session_streams(client, svc):
    assert client.post("/api/session/start", json={"speed": 1}).status_code == 200
    try:
        r = changes(client, risk__max_positions=2, mode__gates__min_expected_net_return_pct=1.0)
        assert r.status_code == 200
        assert svc.bot.risk.cfg.max_positions == 2
        assert svc.bot.profile.gates.min_expected_net_return_pct == 1.0
        # the data source, however, cannot change under a running session
        bad = changes(client, data_mode="historical")
        assert bad.status_code == 400 and "Stop the session" in bad.json()["detail"]
    finally:
        client.post("/api/session/stop")


# ------------------------------------------------------------------ controls
def test_controls_pause_resume_stop_and_emergency(client, svc):
    assert client.post("/api/bot/start").json()["state"] == "running"
    assert client.post("/api/bot/pause").json()["state"] == "paused"
    assert client.post("/api/bot/resume").json()["state"] == "running"
    assert client.post("/api/bot/stop").json()["state"] == "stopped"
    assert client.post("/api/bot/emergency_stop").json()["state"] == "emergency_stop"
    assert client.get("/api/status").json()["bot"]["entries_allowed"] is False
    assert (
        client.post("/api/session/start", json={"instant": True}).status_code == 400
    )  # blocked until cleared
    assert client.post("/api/bot/clear_emergency").json()["state"] == "stopped"
    assert client.post("/api/bot/flatten").status_code == 400  # nothing to close
    assert client.post("/api/bot/explode").status_code == 400


def test_status_has_everything_the_top_bar_needs(client):
    st = client.get("/api/status").json()
    for k in ("equity", "daily_pnl", "total_return_pct", "buying_power", "open_positions"):
        assert k in st["portfolio"]
    assert st["mode"]["label"] and st["market"]["label"] and st["bot"]["state"] and st["feed"]["kind"]
    assert st["forecaster"]["calibrated"] is False and "not" in st["forecaster"]["label"].lower()


# ------------------------------------------------------------------ eligibility
def test_eligibility_manual_marking_and_dated_history(client, svc):
    assert client.get("/api/eligibility").json()["summary"]["source"].startswith("demo list")
    r = client.put(
        "/api/eligibility/ZZZZ", json={"status": "confirmed_unavailable", "rejection_reason": "not in game"}
    )
    assert r.status_code == 200 and r.json()["status"] == "confirmed_unavailable"
    rows = client.get("/api/eligibility", params={"q": "ZZZ"}).json()["rows"]
    assert rows and rows[0]["rejection_reason"] == "not in game"
    client.put("/api/eligibility/ZZZZ", json={"status": "confirmed_tradable"})
    assert (
        client.get("/api/eligibility", params={"q": "ZZZZ"}).json()["rows"][0]["status"]
        == "confirmed_tradable"
    )
    assert len(client.get("/api/eligibility/ZZZZ/history").json()) >= 1
    assert client.put("/api/eligibility/!!", json={"status": "confirmed_tradable"}).status_code == 400
    assert client.put("/api/eligibility/AAA", json={"status": "maybe"}).status_code == 400
    assert client.delete("/api/eligibility/ZZZZ").json()["deleted"] >= 1


def test_eligibility_csv_import_export_and_dry_run(client):
    csv_text = "ticker,status,name,exchange,rejection_reason\nAAAA,confirmed_tradable,Alpha,NASDAQ,\nBBBB,confirmed_unavailable,Beta,OTC,not offered\n"
    dry = client.post("/api/eligibility/import", json={"csv": csv_text, "dry_run": True}).json()
    assert dry["n_rows"] == 2 and not dry["applied"]
    assert not client.get("/api/eligibility", params={"q": "AAAA"}).json()["rows"]
    ok = client.post("/api/eligibility/import", json={"csv": csv_text}).json()
    assert ok["applied"] and not ok["errors"]
    assert (
        client.get("/api/eligibility", params={"q": "BBBB"}).json()["rows"][0]["status"]
        == "confirmed_unavailable"
    )
    out = client.get("/api/eligibility/export.csv")
    assert out.status_code == 200 and "AAAA" in out.text and "not offered" in out.text
    assert client.post("/api/eligibility/import", json={"csv": "nonsense"}).status_code == 400


def test_nothing_is_tradable_without_a_confirmed_list(client, svc):
    assert client.delete("/api/eligibility").json()["status"] == "cleared"
    assert client.get("/api/eligibility/summary").json()["n_tickers"] == 0
    client.post("/api/session/start", json={"instant": True})
    svc.wait_for_session(300)
    assert not svc.broker.orders  # empty list => no trades, ever
    assert client.get("/api/opportunities").json()["counts"]["ready"] == 0


def test_unavailable_marking_stops_recommendations_and_is_explained(client, svc):
    client.post("/api/session/start", json={"speed": 1})
    client.post("/api/session/pause-stream")
    client.post("/api/session/step", json={"n": 60})
    ops = client.get("/api/opportunities").json()
    assert ops["eligibility_applied"] and ops["forecaster"]["calibrated"] is False
    shown = [r["symbol"] for r in ops["top"] + ops["watching"] + ops["ready"]]
    if shown:
        victim = shown[0]
        client.put(
            f"/api/eligibility/{victim}", json={"status": "confirmed_unavailable", "rejection_reason": "test"}
        )
        client.post("/api/session/step", json={"n": 4})
        after = client.get("/api/opportunities").json()
        assert victim not in [
            r["symbol"] for r in after["top"] + after["watching"] + after["ready"] + after["research"]
        ]
        assert any(u["symbol"] == victim for u in after["unavailable_skipped"])
    client.post("/api/session/stop")


# ------------------------------------------------------------------ opportunities and positions
def test_opportunities_and_activity_read_models(client, svc):
    client.post("/api/session/start", json={"speed": 1})
    client.post("/api/session/pause-stream")
    client.post("/api/session/step", json={"n": 90})
    ops = client.get("/api/opportunities").json()
    assert ops["counts"]["scanned"] > 0 and "NOT validated" in ops["note"]
    for row in ops["top"]:
        assert row["eligibility"] == "confirmed_tradable"
        assert row["expected_net_return_pct"] is not None and row["target_price"] and row["stop_price"]
        assert row["forecast_label"] and "heuristic" in row["forecast_label"].lower()
    assert all(r["eligibility"] == "unverified" and not r["actionable"] for r in ops["research"])
    act = client.get("/api/activity").json()
    assert act and {"ts", "ts_chicago", "kind", "message"} <= set(act[0])
    sym = (ops["top"] or ops["research"] or [{"symbol": None}])[0]["symbol"]
    if sym:
        d = client.get(f"/api/opportunities/{sym}").json()
        assert d["gates"] and d["explanation"]["summary"]
    client.post("/api/session/stop")


def test_equity_ranges(client, svc):
    client.post("/api/session/start", json={"instant": True})
    svc.wait_for_session(300)
    for rng in ("1D", "1W", "1M", "ALL"):
        r = client.get("/api/portfolio/equity", params={"range": rng}).json()
        assert r["range"] == rng and r["points"] and "change_pct" in r
    assert client.get("/api/portfolio/equity", params={"range": "5Y"}).status_code == 400


def test_close_position_and_flatten_controls(client, svc):
    client.post("/api/session/start", json={"speed": 1})
    client.post("/api/session/pause-stream")
    # buy something by hand so there is a position to close
    for _ in range(40):
        client.post("/api/session/step", json={"n": 5})
        scan = client.get("/api/scanner").json()["results"]
        if scan:
            break
    sym = scan[0]["symbol"]
    o = client.post("/api/orders", json={"symbol": sym, "side": "buy", "quantity": 10}).json()
    assert o["status"] in ("pending", "new", "filled", "partially_filled")
    client.post("/api/session/step", json={"n": 3})
    pos = client.get("/api/portfolio/positions").json()
    assert pos, "the manual buy should have filled on the next bar"
    assert {"progress_to_target_pct", "eligibility", "minutes_to_close"} <= set(pos[0])
    assert client.post(f"/api/positions/{sym}/close").status_code == 200
    client.post("/api/session/step", json={"n": 4})
    assert not client.get("/api/portfolio/positions").json()
    trades = client.get("/api/portfolio/trades").json()
    assert any(t["symbol"] == sym and "manual" in t["exit_reason"].lower() for t in trades)
    assert client.post("/api/positions/NOPE/close").status_code == 400
    client.post("/api/session/stop")


# ------------------------------------------------------------------ strategy comparison (jobs)
def wait_job(client, job_id, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/backtests/jobs/{job_id}").json()
        if j["status"] in ("done", "error", "cancelled"):
            return j
        time.sleep(0.5)
    raise AssertionError("job timed out")


def test_compare_modes_job_returns_an_honest_labelled_table(client):
    r = client.post(
        "/api/compare-modes",
        json={"dataset": "synthetic_sample", "start": "2026-09-14", "end": "2026-09-17"},
    )
    j = wait_job(client, r.json()["job_id"])
    assert j["status"] == "done" and j["kind"] == "compare_modes", j
    p = j["payload"]
    assert {x["mode"] for x in p["rows"]} == {
        "max_expected_return",
        "aggressive_upside",
        "baseline_momentum",
        "benchmark",
    }
    assert p["same_data"]["feed"]["kind"] == "synthetic" and p["same_data"]["eligibility"]["applied"]
    assert any("SYNTHETIC" in w for w in p["warnings"]) and p["verdict"]
    latest = client.get("/api/strategy-lab/latest").json()
    assert latest["comparison"]["id"] == p["id"]  # survives a page refresh


def test_model_check_job_never_claims_calibration_on_short_data(client, svc):
    r = client.post("/api/model-check", json={"dataset": "synthetic_sample", "max_days": 6})
    j = wait_job(client, r.json()["job_id"])
    assert j["status"] == "done", j
    ev = j["payload"]
    assert all(not m["calibrated"] for m in ev["models"].values())
    assert "Synthetic" in ev["data"]["note"]
    assert client.get("/api/strategy-lab/latest").json()["model_check"]["id"] == ev["id"]
    assert client.post("/api/model-check", json={"dataset": "demo"}).status_code == 400


def test_backtest_defaults_to_the_predictive_strategy_with_eligibility(client):
    r = client.post(
        "/api/backtests",
        json={
            "dataset": "synthetic_sample",
            "strategies": ["expected_opportunity"],
            "start": "2026-09-14",
            "end": "2026-09-16",
        },
    )
    j = wait_job(client, r.json()["job_id"])
    assert j["status"] == "done", j
    res = client.get(f"/api/backtests/{j['result_ids'][0]}").json()
    assert res["result"]["summary"]["eligibility"]["applied"] is True
