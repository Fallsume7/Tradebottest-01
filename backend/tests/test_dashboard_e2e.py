"""Real browser against the real backend serving the built dashboard (demo mode, no credentials).

Skipped when the frontend has not been built (`cd frontend && npm run build`).
Set MWBOT_SCREENSHOTS=1 to regenerate docs/screenshots/*.png.
"""

import os
import re
import threading
import time
from pathlib import Path

import pytest

from mwbot.api.app import create_app
from mwbot.execution.mock_browser import find_chromium
from mwbot.service import AppService

from .conftest import _free_port

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
SHOTS = Path(__file__).resolve().parents[2] / "docs" / "screenshots"
pytestmark = [pytest.mark.browser, pytest.mark.skipif(not DIST.exists(), reason="frontend/dist not built")]


@pytest.fixture()
def server(tmp_env, monkeypatch):
    import uvicorn

    monkeypatch.setenv("MWBOT_FRONTEND_DIST", str(DIST))
    env = type(tmp_env)(_env_file=None)
    svc = AppService(env)
    app = create_app(svc, env)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", svc
    srv.should_exit = True
    t.join(timeout=5)
    svc.close()


@pytest.fixture()
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        kwargs = {"headless": True, "args": ["--no-sandbox"]}
        exe = find_chromium()
        if exe:
            kwargs["executable_path"] = exe
        b = pw.chromium.launch(**kwargs)
        yield b
        b.close()


def shot(page, name):
    if os.environ.get("MWBOT_SCREENSHOTS") == "1":
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.wait_for_timeout(500)
        page.screenshot(path=str(SHOTS / f"{name}.png"))


def test_dashboard_full_workflow(server, browser):
    from playwright.sync_api import expect

    url, svc = server
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(url)

    # honest labelling + safety badges
    expect(page.get_by_text("PAPER ONLY - no real money")).to_be_visible()
    expect(page.get_by_text("SYNTHETIC - synthetic-demo")).to_be_visible()
    expect(page.get_by_test_id("stat-equity")).to_contain_text("$100,000.00")

    # run a whole synthetic day instantly
    page.get_by_label("Replay speed").select_option("instant")
    page.get_by_role("button", name="Start session").click()
    expect(page.get_by_role("button", name="Run next trading day")).to_be_visible(timeout=90_000)
    expect(page.get_by_test_id("decision-feed")).to_be_visible()
    shot(page, "overview")
    equity_text = page.get_by_test_id("stat-equity").inner_text()
    assert equity_text != "$100,000.00" or svc.broker.trades == []   # state in the UI reflects the backend

    # scanner: presets + score breakdown
    page.get_by_role("link", name="Scanner").first.click()
    expect(page.get_by_text("Opportunities")).to_be_visible()
    page.get_by_role("button", name="Penny stocks").click()
    expect(page.get_by_text("preset: penny stocks")).to_be_visible(timeout=10_000)
    page.get_by_role("button", name="Small-cap momentum").click()
    expect(page.get_by_text("preset: small cap momentum")).to_be_visible(timeout=10_000)
    first = page.locator("[data-testid^=scan-row-]").first
    expect(first).to_be_visible(timeout=10_000)
    first.click()
    expect(page.get_by_text("Score breakdown")).to_be_visible()
    shot(page, "scanner")

    # stock detail: chart + strategy conditions + trade ticket
    page.locator("[data-testid^=scan-row-]").first.get_by_role("button").first.click()
    expect(page.get_by_test_id("price-chart").locator("canvas").first).to_be_visible(timeout=15_000)
    expect(page.get_by_text("Strategy signals")).to_be_visible()
    expect(page.get_by_test_id("eval-opening_momentum")).to_be_visible(timeout=10_000)
    shot(page, "stock")

    # history: why bought / why sold
    page.get_by_role("link", name="History").first.click()
    if svc.broker.trades:
        row = page.locator("[data-testid^=trade-]").first
        expect(row).to_be_visible(timeout=10_000)
        row.click()
        expect(page.get_by_text("Why bought:")).to_be_visible()
        expect(page.get_by_text("Why sold:")).to_be_visible()
    shot(page, "history")

    # performance
    page.get_by_role("link", name="Performance").first.click()
    expect(page.get_by_text("Equity curve")).to_be_visible()
    shot(page, "performance")

    # strategy lab: run a real backtest through the UI
    page.get_by_role("link", name="Strategy Lab").first.click()
    page.get_by_label("From date").select_option(index=1)
    page.get_by_label("To date").select_option(index=4)
    page.get_by_role("button", name="Run backtest").click()
    expect(page.get_by_test_id("bt-compare")).to_be_visible(timeout=120_000)
    expect(page.get_by_test_id("bt-warnings").first).to_contain_text("SYNTHETIC")
    shot(page, "strategy-lab")

    # settings: validation error from the server is shown, valid save succeeds
    page.get_by_role("link", name="Settings").first.click()
    page.get_by_label("Max simultaneous holdings").fill("7")
    page.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("status").filter(has_text="Saved.")).to_be_visible(timeout=10_000)
    assert svc.settings.risk.max_positions == 7
    page.get_by_label("Begin liquidation").fill("14:00")
    page.get_by_label("No new positions after").fill("14:30")
    page.get_by_role("button", name="Save settings").click()
    expect(page.get_by_role("alert").filter(has_text="last_entry")).to_be_visible(timeout=10_000)
    shot(page, "settings")

    # emergency stop needs confirmation and is reflected in the UI
    page.once("dialog", lambda d: d.dismiss())
    page.get_by_role("button", name="Emergency stop").click()
    page.wait_for_timeout(300)
    assert svc.bot.state.value != "emergency_stop"
    page.once("dialog", lambda d: d.accept())
    page.get_by_role("button", name="Emergency stop").click()
    expect(page.get_by_text("bot: emergency stop")).to_be_visible(timeout=10_000)

    # the only console error allowed is the browser's own line for the deliberate 400 (invalid schedule) above
    unexpected = [e for e in errors if "favicon" not in e and "status of 400" not in e]
    assert not unexpected, unexpected
    assert sum("status of 400" in e for e in errors) == 1


def test_dashboard_is_usable_on_a_phone(server, browser):
    from playwright.sync_api import expect

    url, _ = server
    page = browser.new_page(viewport={"width": 390, "height": 844})
    page.goto(url)
    expect(page.get_by_test_id("stat-equity")).to_be_visible()
    expect(page.get_by_label("Mobile")).to_be_visible()
    expect(page.get_by_label("Primary")).to_be_hidden()
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 1, f"horizontal page scroll of {overflow}px on a 390px screen"
    page.get_by_label("Mobile").get_by_role("link", name="Scanner").click()
    expect(page.get_by_text("Scanner filters")).to_be_visible()
    shot(page, "mobile")


def test_backend_unreachable_shows_a_clear_error(server, browser):
    from playwright.sync_api import expect

    url, _ = server
    page = browser.new_page()
    page.route("**/api/**", lambda r: r.abort())
    page.goto(url)
    expect(page.get_by_text("Connecting to the backend")).to_be_visible()
    expect(page.get_by_role("alert").first).to_contain_text("Cannot reach the backend")
