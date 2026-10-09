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


def _errors(page):
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    return errors


def _run_to_midday(svc, steps=90):
    svc.start_session(speed=1)
    svc.pause_stream()
    svc.step_session(steps)


def test_dashboard_first_run_just_works_and_is_honest(server, browser):
    from playwright.sync_api import expect

    url, svc = server
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = _errors(page)
    page.goto(url)
    expect(page.get_by_text("PAPER ONLY - no real money")).to_be_visible()
    expect(page.get_by_text("SYNTHETIC", exact=True).first).to_be_visible()
    expect(page.get_by_text("Welcome - nothing to configure")).to_be_visible()
    expect(page.get_by_text("Estimate - not validated").first).to_be_visible()
    nav = page.get_by_role("complementary", name="Primary").get_by_role("link")
    assert [nav.nth(i).get_attribute("aria-label") for i in range(nav.count())] == [
        "Dashboard",
        "Opportunities",
        "Portfolio",
        "Strategy Lab",
        "Trade History",
        "Settings",
    ]
    shot(page, "dashboard_idle")

    # one click: Start -> a session streams, the bot is running, activity appears
    page.get_by_role("button", name="Start", exact=True).click()
    expect(page.get_by_text("Bot: Running")).to_be_visible()
    svc.pause_stream()
    svc.step_session(60)
    expect(page.get_by_test_id("top5")).to_be_visible(timeout=15000)
    expect(page.get_by_test_id("activity-feed")).to_be_visible()
    shot(page, "dashboard_running")
    assert errors == [], errors


def test_modes_controls_and_emergency_stop(server, browser):
    from playwright.sync_api import expect

    url, svc = server
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = _errors(page)
    page.goto(url)
    _run_to_midday(svc, 40)
    svc.control("start")
    page.reload()

    # switch mode through the top bar, with a confirmation
    page.get_by_label("Trading mode").first.select_option("aggressive_upside")
    page.get_by_role("dialog").get_by_role("button", name="Switch mode").click()
    expect(page.get_by_text("Now trading in Aggressive Upside mode")).to_be_visible()
    assert svc.settings.trading.mode.value == "aggressive_upside"
    page.get_by_label("Trading mode").first.select_option("max_expected_return")
    page.get_by_role("dialog").get_by_role("button", name="Switch mode").click()
    expect(page.get_by_text("Now trading in Maximum Expected Return mode")).to_be_visible()

    # pause / resume / stop
    page.get_by_role("button", name="Pause", exact=True).click()
    expect(page.get_by_text("Bot: Paused")).to_be_visible()
    page.get_by_role("button", name="Resume").click()
    expect(page.get_by_text("Bot: Running")).to_be_visible()

    # emergency stop needs a confirmation and then blocks everything until cleared
    page.get_by_role("button", name="Emergency stop").click()
    page.get_by_role("dialog").get_by_role("button", name="Emergency stop").click()
    expect(page.get_by_role("alert").get_by_text("EMERGENCY STOP ACTIVE")).to_be_visible()
    page.get_by_role("button", name="Clear emergency stop").click()
    expect(page.get_by_text("Bot: Stopped")).to_be_visible()
    assert errors == [], errors


def test_settings_workflow_in_a_real_browser(server, browser):
    from playwright.sync_api import expect

    url, svc = server
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = _errors(page)
    page.goto(url + "/#/settings")
    expect(page.get_by_text("You are using the recommended settings")).to_be_visible()
    shot(page, "settings")
    page.get_by_role("button", name="Risk Management").click()

    # frontend validation blocks an impossible value
    box = page.get_by_label("Largest single position (type a value)")
    box.fill("500")
    expect(page.get_by_text("Must be at most 100%")).to_be_visible()
    expect(page.get_by_role("button", name="Save changes")).to_be_disabled()

    # a valid edit shows its effect, saves, persists in the database
    box.fill("8")
    expect(page.get_by_test_id("effects")).to_contain_text("new trades only", timeout=5000)
    page.get_by_role("button", name="Save changes").click()
    expect(page.get_by_text("Settings saved")).to_be_visible()
    assert svc.settings.risk.max_position_pct == 8
    expect(page.get_by_text("setting(s) differ from the recommended defaults")).to_be_visible()

    # reset to recommended (with confirmation)
    page.get_by_role("button", name="Reset all to recommended").click()
    page.get_by_role("dialog").get_by_role("button", name="Reset to recommended").click()
    expect(page.get_by_text("You are using the recommended settings")).to_be_visible()
    assert svc.settings.risk.max_position_pct == 10

    # advanced settings are hidden until requested
    page.get_by_role("button", name="Trading Strategy").click()
    assert page.get_by_text("Minimum reward-to-risk").count() == 0
    page.get_by_role("switch", name="Show advanced settings").click()
    expect(page.get_by_text("Minimum reward-to-risk")).to_be_visible()
    assert errors == [], errors


def test_opportunities_eligibility_and_explanations(server, browser):
    from playwright.sync_api import expect

    url, svc = server
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = _errors(page)
    _run_to_midday(svc, 90)
    svc.control("start")
    page.goto(url + "/#/opportunities")
    table = page.get_by_test_id("opp-table")
    expect(table).to_be_visible(timeout=15000)
    expect(table.get_by_text("Eligibility")).to_be_visible()
    shot(page, "opportunities")

    # filters
    page.get_by_role("button", name="Unverified (research)").click()
    assert page.get_by_test_id("opp-table").get_by_text("Unverified").count() >= 1
    page.get_by_role("button", name=re.compile(r"^All")).first.click()

    # explanation drawer for the first row: plan, checks, honest estimate label
    page.get_by_test_id("opp-table").locator("td[aria-label^='Explain']").first.click()
    dlg = page.get_by_role("dialog", name="Details")
    expect(dlg.get_by_text("Checks before buying")).to_be_visible(timeout=10000)
    expect(dlg.get_by_text("Estimate - not validated")).to_be_visible()
    expect(dlg.get_by_text("Your game list")).to_be_visible()
    shot(page, "explain_drawer")
    page.keyboard.press("Escape")

    # mark the first listed stock unavailable: it disappears from recommendations
    row = page.get_by_test_id("opp-table").locator("tbody tr").first
    sym = row.locator("td").first.locator("div").first.inner_text()
    row.get_by_role("button", name=f"Mark {sym} unavailable").click()
    page.get_by_role("dialog").get_by_role("button", name="Mark unavailable").click()
    expect(page.get_by_text(f"{sym} marked unavailable")).to_be_visible()
    assert svc.bot.universe.record_on(sym, None).status.value == "confirmed_unavailable"
    assert errors == [], errors


def test_every_page_renders_and_layouts_fit_tablet_and_phone(server, browser):
    from playwright.sync_api import expect

    url, svc = server
    _run_to_midday(svc, 100)
    svc.control("start")
    for name, size in (("desktop", (1440, 900)), ("tablet", (820, 1100)), ("phone", (390, 800))):
        page = browser.new_page(viewport={"width": size[0], "height": size[1]})
        errors = _errors(page)
        for route, marker in (
            ("dashboard", "Top 5 opportunities"),
            ("opportunities", "Your MarketWatch stock list"),
            ("portfolio", "Active positions"),
            ("lab", "Maximum Expected Return vs Aggressive Upside"),
            ("history", "Trades"),
            ("settings", "Trading Strategy"),
        ):
            page.goto(f"{url}/#/{route}")
            expect(page.get_by_text(marker, exact=False).first).to_be_visible(timeout=15000)
            overflow = page.evaluate(
                "document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            assert overflow <= 1, f"{name} {route}: horizontal page scroll of {overflow}px"
            if name == "tablet":
                shot(page, f"tablet_{route}")
        assert errors == [], (name, errors)
        page.close()


def test_strategy_lab_runs_the_mode_comparison(server, browser):
    from playwright.sync_api import expect

    url, svc = server
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = _errors(page)
    page.goto(url + "/#/lab")
    page.get_by_role("button", name="Compare the two modes").click()
    expect(page.get_by_test_id("compare-table")).to_be_visible(timeout=240000)
    expect(page.get_by_test_id("verdict")).to_be_visible()
    expect(page.get_by_test_id("compare-warnings")).to_contain_text("SYNTHETIC")
    expect(page.get_by_text("SYNTHETIC DATA").first).to_be_visible()
    shot(page, "strategy_lab")
    assert errors == [], errors
