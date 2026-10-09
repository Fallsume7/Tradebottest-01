import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../App";
import { errorResponse, feed, mockApi, oppResp, position, schemaResp, status, withBot } from "./fixtures";
import type { Trade } from "../types";

const trade = (o: Partial<Trade> = {}): Trade => ({
  id: "T1", symbol: "AAA", strategy: "expected_opportunity", quantity: 100, entry_time: "2026-10-08T14:00:00+00:00", entry_price: 5, exit_time: "2026-10-08T15:00:00+00:00",
  exit_price: 5.3, gross_pnl: 30, commissions: 0, net_pnl: 30, return_pct: 6, hold_minutes: 60, entry_reason: "Expected +0.9% after costs", exit_reason: "Profit target reached (high 5.30 >= 5.28)",
  session_date: "2026-10-08", mode: "max_expected_return", predicted_net_return_pct: 0.9, predicted_p_positive: 0.58, predicted_p_target: 0.41, planned_target_price: 5.28, planned_stop_price: 4.85, forecast_calibrated: false, ...o,
});

const equity = { range: "1D", points: [], start_equity: 100000, end_equity: 100000, change: 0, change_pct: 0 };
const base = (over: Record<string, unknown> = {}) => ({
  "GET /status": withBot("running"),
  "GET /opportunities": oppResp(),
  "GET /portfolio/positions": [],
  "GET /portfolio/orders": [],
  "GET /portfolio/trades": [],
  "GET /activity": [{ ts: "2026-10-08T15:00:00+00:00", ts_chicago: "10:00", kind: "entry", symbol: "AAA", message: "Opening a simulated position: buying 100 AAA.", exit_category: null }],
  "GET /portfolio/equity": equity,
  "GET /settings/schema": schemaResp(),
  ...over,
});
const go = (route: string) => { window.location.hash = `/${route}`; };

describe("shell, honesty labels and navigation", () => {
  it("has exactly the six navigation items", async () => {
    mockApi(base());
    render(<App />);
    await screen.findByTestId("stat-equity");
    const nav = within(screen.getByRole("complementary", { name: "Primary" }));
    expect(nav.getAllByRole("link").map((l) => l.getAttribute("aria-label"))).toEqual(["Dashboard", "Opportunities", "Portfolio", "Strategy Lab", "Trade History", "Settings"]);
  });

  it("shows paper-only, the data kind and an 'estimate - not validated' label (never 'real-time' for synthetic)", async () => {
    mockApi(base());
    render(<App />);
    expect(await screen.findByText("SYNTHETIC")).toBeInTheDocument();
    expect(screen.getByText("PAPER ONLY - no real money")).toBeInTheDocument();
    expect((await screen.findAllByText("Estimate - not validated")).length).toBeGreaterThan(0);
    expect(screen.queryByText(/REAL-TIME/)).not.toBeInTheDocument();
  });

  it("labels delayed feeds honestly", async () => {
    mockApi(base({ "GET /status": withBot("running", { feed: feed({ kind: "delayed", provider: "massive", delay_seconds: 900, is_realtime: false, label: "DELAYED" }) }) }));
    render(<App />);
    expect(await screen.findByText("DELAYED ~15 MIN")).toBeInTheDocument();
  });

  it("shows a clear error when the backend is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("network"); }));
    render(<App />);
    expect(await screen.findByText(/Cannot reach the backend/)).toBeInTheDocument();
  });

  it("shows the top bar summary and the market status", async () => {
    mockApi(base({ "GET /status": withBot("running", { market: { phase: "trading", label: "Open (simulated clock)", open: true, simulated: true } }) }));
    render(<App />);
    expect(await screen.findByTestId("stat-equity")).toHaveTextContent("$100,000.00");
    for (const id of ["stat-daily", "stat-return", "stat-bp", "stat-positions"]) expect(screen.getByTestId(id)).toBeInTheDocument();
    expect(screen.getByText("Open (simulated clock)")).toBeInTheDocument();
  });
});

describe("dashboard", () => {
  it("shows the top 5 with eligibility, expected return and a 'ready / watching' status", async () => {
    mockApi(base());
    render(<App />);
    const table = await screen.findByTestId("top5");
    expect(within(table).getByText("AAA")).toBeInTheDocument();
    expect(within(table).getByText("Ready to buy")).toBeInTheDocument();
    expect(within(table).getByText("Watching")).toBeInTheDocument();
    expect(within(table).getAllByText("Available").length).toBe(2);
  });

  it("explains why nothing is bought when no opportunity qualifies", async () => {
    mockApi(base({ "GET /status": withBot("running", { session_status: "running" }), "GET /opportunities": oppResp({ top: [], ready: [], watching: [], waiting: "Waiting for a better setup: the best candidate AAA does not qualify yet" }) }));
    render(<App />);
    expect(await screen.findByText(/Waiting for a better setup/)).toBeInTheDocument();
  });

  it("renders active positions with progress and opens an explanation drawer", async () => {
    mockApi(base({ "GET /portfolio/positions": [position("AAA")] }));
    render(<App />);
    const user = userEvent.setup();
    await screen.findByText(/50% of the way to target/);
    await user.click(screen.getByText(/50% of the way to target/));
    const dlg = await screen.findByRole("dialog", { name: "Details" });
    expect(within(dlg).getByText("Why it was bought")).toBeInTheDocument();
    expect(within(dlg).getByText(/Expected \+0.9% after costs/)).toBeInTheDocument();
    expect(within(dlg).getByText("What would close it")).toBeInTheDocument();
    expect(within(dlg).getByText("Estimate - not validated")).toBeInTheDocument();
  });

  it("flags a held stock whose eligibility changed but never blocks managing it", async () => {
    mockApi(base({ "GET /portfolio/positions": [position("AAA", { eligibility: "confirmed_unavailable", eligibility_label: "Confirmed unavailable", eligibility_flag: true })] }));
    render(<App />);
    expect(await screen.findByText("Confirmed unavailable")).toBeInTheDocument();
  });

  it("switches mode after confirmation and tells you what happens to open positions", async () => {
    const api = mockApi(base({
      "GET /status": withBot("running", { portfolio: { ...status().portfolio, open_positions: 2 } }),
      "POST /mode": { mode: { from: "max_expected_return", to: "aggressive_upside", open_positions: 2, message: "Now trading in Aggressive Upside mode. Your 2 open position(s) keep the plan they were opened with." } },
    }));
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("radio", { name: /Aggressive Upside/ }));
    const dlg = await screen.findByRole("dialog");
    expect(within(dlg).getByText(/2 open position\(s\) keep the plan they were opened with/)).toBeInTheDocument();
    await user.click(within(dlg).getByRole("button", { name: "Switch mode" }));
    await waitFor(() => expect(api.called("POST /mode")[0].body).toEqual({ mode: "aggressive_upside" }));
    expect(await screen.findByText(/Now trading in Aggressive Upside mode/)).toBeInTheDocument();
  });

  it("cancelling the mode switch changes nothing", async () => {
    const api = mockApi(base());
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("radio", { name: /Aggressive Upside/ }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(api.called("POST /mode")).toHaveLength(0);
  });
});

describe("bot controls", () => {
  it("pause and resume call the right endpoints", async () => {
    const api = mockApi(base({ "POST /bot/pause": { state: "paused" }, "POST /bot/resume": { state: "running" } }));
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Pause" }));
    await waitFor(() => expect(api.called("POST /bot/pause")).toHaveLength(1));
  });

  it("shows Resume when paused", async () => {
    const api = mockApi(base({ "GET /status": withBot("paused"), "POST /bot/resume": { state: "running" } }));
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Resume" }));
    await waitFor(() => expect(api.called("POST /bot/resume")).toHaveLength(1));
  });

  it("start begins a session when none is active", async () => {
    const api = mockApi(base({ "GET /status": withBot("stopped"), "POST /session/start": { date: "2026-10-08" } }));
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Start" }));
    await waitFor(() => expect(api.called("POST /session/start")[0].body).toEqual({ speed: 10 }));
  });

  it("stop explains that open positions keep being managed, and needs confirmation", async () => {
    const api = mockApi(base({ "GET /status": withBot("running", { portfolio: { ...status().portfolio, open_positions: 1 } }), "POST /bot/stop": { state: "stopped" } }));
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Stop" }));
    const dlg = await screen.findByRole("dialog");
    expect(within(dlg).getByText(/keep being managed/)).toBeInTheDocument();
    expect(api.called("POST /bot/stop")).toHaveLength(0);
    await user.click(within(dlg).getByRole("button", { name: "Stop the bot" }));
    await waitFor(() => expect(api.called("POST /bot/stop")).toHaveLength(1));
  });

  it("emergency stop needs confirmation and describes the configured behaviour", async () => {
    const api = mockApi(base({ "POST /bot/emergency_stop": { state: "emergency_stop" } }));
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Emergency stop" }));
    const dlg = await screen.findByRole("dialog");
    expect(within(dlg).getByText(/sells ALL open virtual positions/)).toBeInTheDocument();
    await user.click(within(dlg).getByRole("button", { name: "Emergency stop" }));
    await waitFor(() => expect(api.called("POST /bot/emergency_stop")).toHaveLength(1));
  });

  it("an active emergency stop shows a banner and a way to clear it", async () => {
    mockApi(base({ "GET /status": withBot("emergency_stop") }));
    render(<App />);
    expect(await screen.findByText(/EMERGENCY STOP ACTIVE/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear emergency stop" })).toBeInTheDocument();
  });
});

describe("opportunities and eligibility", () => {
  const withBoth = () => base({ "GET /eligibility": { summary: { applied: true, n_tickers: 3, counts: { confirmed_tradable: 1, confirmed_unavailable: 1, unverified: 1 }, source: "your list", note: "A stock is only ever bought if it is marked Confirmed tradable here.", verifier: { name: "marketwatch", available: false, message: "Automatic verification is not implemented." } }, rows: [] } });

  it("has an Eligibility column and filters for available / unverified / unavailable", async () => {
    mockApi(withBoth());
    go("opportunities");
    render(<App />);
    const table = await screen.findByTestId("opp-table");
    expect(within(table).getByText("Eligibility")).toBeInTheDocument();
    expect(within(table).getByText("RSCH")).toBeInTheDocument();
    expect(within(table).getByText("GONE")).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /^Unverified/ }));
    expect(within(screen.getByTestId("opp-table")).queryByText("AAA")).not.toBeInTheDocument();
    expect(within(screen.getByTestId("opp-table")).getByText("RSCH")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^Unavailable/ }));
    expect(within(screen.getByTestId("opp-table")).getByText("GONE")).toBeInTheDocument();
  });

  it("marks a stock available in one click and unavailable after confirmation", async () => {
    const put = vi.fn(() => ({ ticker: "RSCH", status: "confirmed_tradable" }));
    const api = mockApi({ ...withBoth(), "PUT /eligibility/RSCH": put, "PUT /eligibility/AAA": put });
    go("opportunities");
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Mark RSCH available" }));
    await waitFor(() => expect(api.called("PUT /eligibility/RSCH")[0].body).toMatchObject({ status: "confirmed_tradable" }));
    await user.click(screen.getByRole("button", { name: "Mark AAA unavailable" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Mark unavailable" }));
    await waitFor(() => expect(api.called("PUT /eligibility/AAA")[0].body).toMatchObject({ status: "confirmed_unavailable" }));
  });

  it("explains a research candidate as 'not verified - will not be bought'", async () => {
    const detail = { symbol: "RSCH", name: "Research Corp", price: 5, score: 50, actionable: false, research_only: true, eligibility: { status: "unverified", status_label: "Unverified", rejection_reason: "", restrictions: "", source: "default", verified_on: null, violations: [], applied: true }, forecast: null, plan: null, gates: [], explanation: {}, minutes_to_close: 200, day_change_pct: 12 };
    mockApi({ ...withBoth(), "GET /opportunities/RSCH": detail, "GET /stocks/RSCH": errorResponse(400, "no session") });
    go("opportunities");
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByLabelText("Explain RSCH"));
    expect(await screen.findByText(/research candidate. The bot will not buy it/)).toBeInTheDocument();
    expect(screen.getByText(/not used to rank/)).toBeInTheDocument();
  });

  it("explanation of a ready stock shows plan, checks and says the estimate is not validated", async () => {
    const detail = {
      symbol: "AAA", name: "AAA", price: 5, score: 62, actionable: true, research_only: false,
      eligibility: { status: "confirmed_tradable", status_label: "Confirmed tradable", rejection_reason: "", restrictions: "", source: "csv", verified_on: "2026-10-01", violations: [], applied: true },
      forecast: { horizon_minutes: 90, expected_return_pct: 1.3, expected_net_return_pct: 0.8, uncertainty_pct: 2.1, p_positive: 0.58, upside_pct: 3.9, downside_pct: -2.4, cost_pct: 0.5, method: "heuristic-v1", calibrated: false, label: "heuristic", contributions: [{ name: "momentum", label: "Short-term momentum", value: 0.4, points: 0.12 }], warnings: [] },
      plan: { entry_price: 5.01, target_price: 5.3, target_return_pct: 6, target_net_return_pct: 5.5, p_target: 0.42, stop_price: 4.85, stop_pct: 3.2, max_hold_minutes: 80, reward_risk: 1.6, notes: ["stop 3.20% below entry"], blockers: [] },
      gates: [{ label: "Expected return after costs", passed: true, detail: "+0.80%" }, { label: "Reward vs risk", passed: false, detail: "0.9 (need >= 1)" }],
      explanation: { summary: "Expected +0.8% after costs over ~90 min.", drivers: ["Short-term momentum (+0.12 pts)"], invalidation: ["Price hits the risk exit at $4.85"] }, minutes_to_close: 240, day_change_pct: 1.2,
    };
    mockApi({ ...withBoth(), "GET /opportunities/AAA": detail, "GET /stocks/AAA": errorResponse(400, "no session") });
    go("opportunities");
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByLabelText("Explain AAA"));
    const dlg = await screen.findByRole("dialog", { name: "Details" });
    expect(await within(dlg).findByText("The plan if bought")).toBeInTheDocument();
    expect(within(dlg).getByText(/NOT been validated/)).toBeInTheDocument();
    expect(within(dlg).getByText("What would change the picture")).toBeInTheDocument();
    expect(within(dlg).getByLabelText("passed")).toBeInTheDocument();
    expect(within(dlg).getByLabelText("not passed")).toBeInTheDocument();
  });
});

describe("settings", () => {
  const settingsApp = (extra: Record<string, unknown> = {}) => {
    const api = mockApi(base({ "POST /settings/preview": { valid: true, errors: [], effects: [{ path: "risk.max_positions", label: "Most positions at once", from: 5, to: 3, effect: "new_trades", message: "Applies to new trades only. Open positions keep the plan they were opened with.", blocked: false }] }, ...extra }));
    go("settings");
    render(<App />);
    return api;
  };

  it("shows the six categories, the recommended banner and the mode cards", async () => {
    settingsApp();
    const nav = within(await screen.findByRole("navigation", { name: "Settings categories" }));
    expect(nav.getAllByRole("button").map((b) => b.textContent?.replace(/\d+$/, ""))).toEqual(["Trading Strategy", "Risk Management", "Trading Schedule", "MarketWatch Game", "Market Data", "General"]);
    expect(screen.getByText(/You are using the recommended settings/)).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Maximum Expected Return/ })).toBeChecked();
  });

  it("simple mode hides advanced controls until asked", async () => {
    settingsApp();
    await screen.findByText("Minimum chance of a gain");
    expect(screen.queryByText("Minimum reward-to-risk")).not.toBeInTheDocument();
  });

  it("editing creates a pending change with its effect, and saving sends only the changes", async () => {
    const api = settingsApp({ "POST /settings/changes": { settings: {}, effects: [], saved_at: "2026-10-08T15:00:00+00:00" } });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Risk Management" }));
    const box = await screen.findByLabelText("Most positions at once (type a value)");
    await user.clear(box);
    await user.type(box, "3");
    expect(await screen.findByText("1 unsaved change(s)", { selector: "span.text-\\[13px\\]" })).toBeInTheDocument();
    expect(await screen.findByTestId("effects")).toHaveTextContent("Applies to new trades only");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.called("POST /settings/changes")[0].body).toEqual({ changes: { "risk.max_positions": 3 } }));
    expect(await screen.findByText("Settings saved")).toBeInTheDocument();
  });

  it("validates in the browser: out-of-range values block saving and explain why", async () => {
    const api = settingsApp();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Risk Management" }));
    const box = await screen.findByLabelText("Largest single position (type a value)");
    await user.clear(box);
    await user.type(box, "500");
    expect(await screen.findByText(/Must be at most 100%/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(api.called("POST /settings/changes")).toHaveLength(0);
  });

  it("shows server-side validation errors on the right field", async () => {
    settingsApp({ "POST /settings/changes": () => new Response(JSON.stringify({ detail: "bad", errors: [{ path: "risk.max_positions", message: "Most positions at once: Must be at least 1." }] }), { status: 400, headers: { "Content-Type": "application/json" } }) });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Risk Management" }));
    const box = await screen.findByLabelText("Most positions at once (type a value)");
    await user.clear(box);
    await user.type(box, "4");
    await user.click(await screen.findByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("Most positions at once: Must be at least 1.")).toBeInTheDocument();
  });

  it("discard throws edits away", async () => {
    settingsApp();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Risk Management" }));
    const box = await screen.findByLabelText("Most positions at once (type a value)");
    await user.clear(box);
    await user.type(box, "2");
    await user.click(await screen.findByRole("button", { name: "Discard" }));
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Most positions at once (type a value)")).toHaveValue(5);
  });

  it("reset to recommended asks first, then calls the server", async () => {
    const custom = schemaResp();
    custom.fields[0] = { ...custom.fields[0], value: 0.7, is_recommended: false };
    const api = settingsApp({ "GET /settings/schema": custom, "POST /settings/reset": { effects: [], reset: "strategy" } });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Reset this section" }));
    expect(api.called("POST /settings/reset")).toHaveLength(0);
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Reset to recommended" }));
    await waitFor(() => expect(api.called("POST /settings/reset")[0].body).toEqual({ category: "strategy" }));
  });

  it("locks start-of-day-only settings while positions are open", async () => {
    settingsApp({ "GET /settings/schema": schemaResp({ open_positions: 1 }) });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Risk Management" }));
    expect(await screen.findByLabelText("Starting capital")).toBeDisabled();
    expect(screen.getByText(/Locked while a session runs or positions are open/)).toBeInTheDocument();
  });

  it("exports settings and previews an import before applying it", async () => {
    const api = settingsApp({ "GET /settings/export": { app: "MarketWatchBot", format: 1, settings: {} }, "POST /settings/import": (b: unknown) => ((b as { apply: boolean }).apply ? { applied: true, n_changes: 1, effects: [] } : { applied: false, n_changes: 1, effects: [{ path: "risk.max_positions", label: "Most positions at once", from: 5, to: 3, effect: "new_trades", message: "Applies to new trades only.", blocked: false }] }) });
    const user = userEvent.setup();
    const input = await screen.findByLabelText("Import settings file");
    await user.upload(input, new File([JSON.stringify({ app: "MarketWatchBot", format: 1, settings: {} })], "s.json", { type: "application/json" }));
    const dlg = await screen.findByRole("dialog");
    expect(within(dlg).getByText(/Apply 1 change\(s\)/)).toBeInTheDocument();
    await user.click(within(dlg).getByRole("button", { name: "Import settings" }));
    await waitFor(() => expect(api.called("POST /settings/import")).toHaveLength(2));
  });
});

describe("trade history and portfolio", () => {
  it("filters trades and explains why one was bought and closed", async () => {
    mockApi(base({ "GET /portfolio/trades": [trade(), trade({ id: "T2", symbol: "BBB", net_pnl: -20, return_pct: -2, exit_reason: "Stop loss hit (low 4.8 <= 4.85)" })], "GET /reports": [] }));
    go("history");
    render(<App />);
    const user = userEvent.setup();
    const table = await screen.findByTestId("trade-table");
    expect(within(table).getByText("Target hit")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Losers" }));
    expect(within(screen.getByTestId("trade-table")).queryByText("AAA")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "All" }));
    await user.click(screen.getByLabelText("Explain AAA trade"));
    const dlg = await screen.findByRole("dialog", { name: "Details" });
    expect(within(dlg).getByText("Why it was closed")).toBeInTheDocument();
    expect(within(dlg).getByText(/sold early rather than holding to the close/)).toBeInTheDocument();
    expect(within(dlg).getByText("Forecast vs result")).toBeInTheDocument();
  });

  it("closes a position only after confirmation", async () => {
    const api = mockApi(base({
      "GET /portfolio/positions": [position("AAA")], "GET /performance": { starting_capital: 100000, equity: 100000, total_return_pct: 0, benchmark_return_pct: null, benchmark_note: "", max_drawdown_pct: 0, max_drawdown_usd: 0, daily: [], trade_stats: { n_trades: 0, win_rate: null, net_pnl: 0, avg_win: null, avg_loss: null, payoff_ratio: null, profit_factor: null, expectancy: null, avg_return_pct: null, avg_hold_minutes: null, best_trade: null, worst_trade: null }, daily_stats: { n_days: 0, mean_daily_pct: null, std_daily_pct: null, sharpe: null, sortino: null, ratios_note: null }, by_strategy: {} },
      "POST /positions/AAA/close": { closing: "AAA" },
    }));
    go("portfolio");
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Close AAA" }));
    expect(api.called("POST /positions/AAA/close")).toHaveLength(0);
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Close position" }));
    await waitFor(() => expect(api.called("POST /positions/AAA/close")).toHaveLength(1));
  });
});

describe("strategy lab", () => {
  const cmp = { id: "j1", created_at: "2026-10-08", dataset: "synthetic_sample", rows: [
    { label: "Maximum Expected Return", mode: "max_expected_return", total_return_pct: -0.4, max_drawdown_pct: 0.9, win_rate: 0.3, avg_return_pct: -0.1, n_trades: 6, target_hit_rate: 0.1, stop_rate: 0.5, end_of_day_rate: 0.3 },
    { label: "Aggressive Upside", mode: "aggressive_upside", total_return_pct: 1.2, max_drawdown_pct: 2.1, win_rate: 0.5, avg_return_pct: 0.3, n_trades: 8, target_hit_rate: 0.2, stop_rate: 0.4, end_of_day_rate: 0.2 },
    { label: "Buy & hold universe (benchmark)", mode: "benchmark", total_return_pct: -0.5, n_trades: null },
  ], verdict: "Too few trades to say which mode is better (6 vs 8; 30+ each needed).", same_data: { dataset: "synthetic_sample", feed: feed(), n_days: 10, eligibility: { applied: true }, forecaster: "heuristic" }, equity_curves: {}, warnings: ["SYNTHETIC data: results show how the machinery behaves on fabricated prices, not how any strategy performs in real markets."] };

  it("shows the comparison with its data label, honest verdict and weak results unhidden", async () => {
    mockApi(base({ "GET /strategies": [], "GET /datasets": [{ id: "synthetic_sample", name: "Synthetic sample", synthetic: true, dates: ["2026-09-14"], description: "" }], "GET /strategy-lab/latest": { comparison: cmp, model_check: null, forecaster: status().forecaster } }));
    go("lab");
    render(<App />);
    expect(await screen.findByTestId("verdict")).toHaveTextContent("Too few trades");
    expect(screen.getByText("SYNTHETIC DATA")).toBeInTheDocument();
    const table = screen.getByTestId("compare-table");
    expect(within(table).getByText("-0.40%")).toBeInTheDocument();
    expect(screen.getByTestId("compare-warnings")).toHaveTextContent("SYNTHETIC data");
  });

  it("runs a comparison job and shows its result", async () => {
    const api = mockApi(base({
      "GET /strategies": [], "GET /datasets": [{ id: "synthetic_sample", name: "Synthetic sample", synthetic: true, dates: ["2026-09-14"], description: "" }],
      "GET /strategy-lab/latest": { comparison: null, model_check: null, forecaster: status().forecaster },
      "POST /compare-modes": { job_id: "j1" }, "GET /backtests/jobs/j1": { id: "j1", kind: "compare_modes", status: "done", progress: 1, message: "", result_ids: [], error: "", payload: cmp },
    }));
    go("lab");
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Compare the two modes" }));
    await waitFor(() => expect(api.called("POST /compare-modes")[0].body).toMatchObject({ forecaster: "heuristic", eligibility: "point_in_time" }));
    expect(await screen.findByTestId("compare-table")).toBeInTheDocument();
  });
});
