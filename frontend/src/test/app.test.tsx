import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "../App";
import { feed, mockApi, order, position, scanRow, scanner, settingsResp, status, strategyInfo, errorResponse } from "./fixtures";

const base = () => ({
  "GET /status": status(),
  "GET /portfolio/equity": [],
  "GET /decisions": [],
  "GET /portfolio/positions": [],
});

describe("shell and overview", () => {
  it("shows safety labelling: paper-only and the data feed kind, never 'real-time' for synthetic data", async () => {
    mockApi(base());
    render(<App />);
    expect(await screen.findByText("SYNTHETIC - synthetic-demo")).toBeInTheDocument();
    expect(screen.getByText("PAPER ONLY - no real money")).toBeInTheDocument();
    expect(screen.queryByText(/REAL-TIME/)).not.toBeInTheDocument();
    expect(await screen.findByTestId("stat-equity")).toHaveTextContent("$100,000.00");
  });

  it("labels delayed and real-time feeds honestly", async () => {
    mockApi({ ...base(), "GET /status": status({ data_mode: "connected", feed: feed({ kind: "delayed", provider: "massive", delay_seconds: 900, is_realtime: false, label: "Massive - DELAYED ~15 min" }) }) });
    const { unmount } = render(<App />);
    expect(await screen.findByText(/DELAYED ~15 MIN - massive/)).toBeInTheDocument();
    expect(screen.queryByText(/REAL-TIME/)).not.toBeInTheDocument();
    unmount();
    mockApi({ ...base(), "GET /status": status({ feed: feed({ kind: "realtime", provider: "massive", is_realtime: true }) }) });
    render(<App />);
    expect(await screen.findByText(/REAL-TIME - massive/)).toBeInTheDocument();
  });

  it("shows a clear error when the backend is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("network"); }));
    render(<App />);
    expect(await screen.findByText(/Cannot reach the backend/)).toBeInTheDocument();
  });

  it("surfaces critical alerts and session errors as banners", async () => {
    mockApi({ ...base(), "GET /status": status({ session_error: "RuntimeError: kaput", alerts: [{ ts: "2026-10-08T15:00:00+00:00", level: "critical", code: "overnight_risk", message: "Positions NOT liquidated at close: AAAx100" }], alerts_critical: 1 }) });
    render(<App />);
    expect(await screen.findByText(/Session error: RuntimeError: kaput/)).toBeInTheDocument();
    expect(screen.getAllByText(/Positions NOT liquidated at close/).length).toBeGreaterThan(0);
  });

  it("starts a session at the chosen speed and instant mode", async () => {
    const api = mockApi({ ...base(), "POST /session/start": { date: "2026-10-08" } });
    render(<App />);
    const user = userEvent.setup();
    await screen.findByTestId("stat-equity");
    await user.click(screen.getByRole("button", { name: "Start session" }));
    await waitFor(() => expect(api.called("POST /session/start")[0].body).toEqual({ speed: 10 }));
    await user.selectOptions(screen.getByLabelText("Replay speed"), "instant");
    await user.click(screen.getByRole("button", { name: "Start session" }));
    await waitFor(() => expect(api.called("POST /session/start")[1].body).toEqual({ instant: true }));
  });

  it("disables starting when no data feed is available and explains why", async () => {
    mockApi({ ...base(), "GET /status": status({ data_mode: "connected", feed: feed({ available: false, kind: "historical", provider: "none", label: "NO DATA FEED", warnings: ["API_KEY missing"] }), provider_error: "DataUnavailableError: MWBOT_MASSIVE_API_KEY is not set" }) });
    render(<App />);
    expect(await screen.findByText(/MWBOT_MASSIVE_API_KEY is not set/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start live session" })).toBeDisabled();
  });

  it("emergency stop asks for confirmation first", async () => {
    const api = mockApi({ ...base(), "POST /bot/emergency_stop": { state: "emergency_stop" } });
    render(<App />);
    const user = userEvent.setup();
    await screen.findByTestId("stat-equity");
    vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    await user.click(screen.getByRole("button", { name: "Emergency stop" }));
    expect(api.called("POST /bot/emergency_stop")).toHaveLength(0);
    vi.spyOn(window, "confirm").mockReturnValueOnce(true);
    await user.click(screen.getByRole("button", { name: "Emergency stop" }));
    await waitFor(() => expect(api.called("POST /bot/emergency_stop")).toHaveLength(1));
  });

  it("shows positions and the bot's reasons on the overview", async () => {
    mockApi({ ...base(), "GET /portfolio/positions": [position("AAA")], "GET /decisions": [{ ts: "2026-10-08T14:01:00+00:00", kind: "entry", symbol: "AAA", strategy: "opening_momentum", message: "BUY 100 AAA (opening_momentum): Open drive 3%", details: {} }] });
    render(<App />);
    const feedList = await screen.findByTestId("decision-feed");
    expect(within(feedList).getByText(/BUY 100 AAA/)).toBeInTheDocument();
    expect(await screen.findByText("+$50.00 (+10.00%)")).toBeInTheDocument();
  });
});

describe("scanner page", () => {
  const routes = () => ({ ...base(), "GET /scanner": scanner(), "POST /scanner/preset/penny_stocks": {}, "PUT /settings": {} });

  it("lists tradable rows ranked, hides filtered-out ones by default and shows why they were filtered", async () => {
    window.location.hash = "#/scanner";
    mockApi(routes());
    render(<App />);
    const user = userEvent.setup();
    expect(await screen.findByTestId("scan-row-AAA")).toBeInTheDocument();
    expect(screen.queryByTestId("scan-row-CCC")).not.toBeInTheDocument();
    await user.click(screen.getByLabelText("show filtered-out"));
    expect(await screen.findByTestId("scan-row-CCC")).toHaveTextContent("price $50.00 outside $1-$20");
  });

  it("sorts by a column and toggles direction", async () => {
    window.location.hash = "#/scanner";
    mockApi(routes());
    render(<App />);
    const user = userEvent.setup();
    await screen.findByTestId("scan-row-AAA");
    const order = () => screen.getAllByTestId(/scan-row-/).map((r) => r.getAttribute("data-testid"));
    expect(order()).toEqual(["scan-row-AAA", "scan-row-BBB"]);
    await user.click(screen.getByRole("button", { name: /Chg %/ }));
    expect(order()).toEqual(["scan-row-AAA", "scan-row-BBB"]); // ascending by pct: AAA 11 < BBB 25
    await user.click(screen.getByRole("button", { name: /Chg %/ }));
    expect(order()).toEqual(["scan-row-BBB", "scan-row-AAA"]);
  });

  it("explains the score: components with points and unavailable data marked as such", async () => {
    window.location.hash = "#/scanner";
    mockApi(routes());
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByTestId("scan-row-AAA"));
    expect(screen.getByText("Score breakdown (0-100)")).toBeInTheDocument();
    expect(screen.getByText("+12.5")).toBeInTheDocument();
    expect(screen.getByText(/unavailable - excluded from score/)).toBeInTheDocument();
    expect(screen.getAllByText("AAA relative volume 3.2x normal").length).toBe(2); // table summary + expanded reasons
  });

  it("applies a preset and custom filters through the API", async () => {
    window.location.hash = "#/scanner";
    const api = mockApi(routes());
    render(<App />);
    const user = userEvent.setup();
    await screen.findByTestId("scan-row-AAA");
    await user.click(screen.getByRole("button", { name: "Penny stocks" }));
    await waitFor(() => expect(api.called("POST /scanner/preset/penny_stocks")).toHaveLength(1));
    await user.type(screen.getByLabelText("Max price ($)"), "7.5");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => expect(api.called("PUT /settings")[0].body).toEqual({ scanner: { filters: { max_price: 7.5 } } }));
  });

  it("shows missing values as '-' rather than zero", async () => {
    window.location.hash = "#/scanner";
    mockApi({ ...routes(), "GET /scanner": scanner([scanRow("NOR", { rvol: null, rank: 1 })]) });
    render(<App />);
    const row = await screen.findByTestId("scan-row-NOR");
    expect(within(row).getAllByText("-").length).toBeGreaterThan(0);
  });
});

describe("positions page", () => {
  it("flattens a position with a market sell and can cancel a pending order", async () => {
    window.location.hash = "#/positions";
    const api = mockApi({
      ...base(), "GET /portfolio/positions": [position("AAA")], "GET /portfolio/orders": [order()],
      "POST /orders": { id: "ORD-2", status: "pending", reject_reason: "" }, "DELETE /orders/ORD-000001": { canceled: true },
    });
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Flatten" }));
    await waitFor(() => expect(api.called("POST /orders")[0].body).toMatchObject({ symbol: "AAA", side: "sell", quantity: 100, order_type: "market" }));
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(api.called("DELETE /orders/ORD-000001")).toHaveLength(1));
  });

  it("shows API errors instead of failing silently", async () => {
    window.location.hash = "#/positions";
    mockApi({ ...base(), "GET /portfolio/positions": [position("AAA")], "GET /portfolio/orders": [], "POST /orders": () => errorResponse(400, "Emergency stop is active.") });
    render(<App />);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Flatten" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Emergency stop is active.");
  });
});

describe("settings page", () => {
  it("saves edited risk settings and surfaces server-side validation errors", async () => {
    window.location.hash = "#/settings";
    let fail = true;
    const api = mockApi({ ...base(), "GET /settings": settingsResp(), "PUT /settings": () => (fail ? errorResponse(400, "Invalid settings: last_entry must not be later than liquidation_start") : {}) });
    render(<App />);
    const user = userEvent.setup();
    const field = await screen.findByLabelText("Max simultaneous holdings");
    await user.clear(field);
    await user.type(field, "8");
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    expect(await screen.findByText(/last_entry must not be later/)).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Saved."));
    const body = api.called("PUT /settings").at(-1)!.body as { risk: { max_positions: number; starting_capital: number }; data_mode: string };
    expect(body.risk.max_positions).toBe(8);
    expect(body.risk.starting_capital).toBe(100000);
    expect(body.data_mode).toBe("demo");
  });

  it("never asks for an API key and states real-money execution is impossible", async () => {
    window.location.hash = "#/settings";
    mockApi({ ...base(), "GET /settings": settingsResp() });
    render(<App />);
    expect(await screen.findByText(/real money: impossible/)).toBeInTheDocument();
    expect(screen.getByText(/never asks for or stores keys/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/api key/i)).not.toBeInTheDocument();
  });

  it("explains each data mode", async () => {
    window.location.hash = "#/settings";
    mockApi({ ...base(), "GET /settings": settingsResp() });
    render(<App />);
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText("Data mode"), "connected");
    expect(screen.getByText(/your own provider key/)).toBeInTheDocument();
  });
});

describe("strategy lab", () => {
  const labRoutes = () => ({
    ...base(), "GET /strategies": [strategyInfo()], "GET /datasets": [{ id: "synthetic_sample", name: "synthetic-sample", synthetic: true, dates: ["2026-09-14", "2026-09-15", "2026-09-16"], description: "FABRICATED" }],
    "GET /backtests": [], "PUT /settings": {},
  });

  it("generates a parameter form from the strategy schema, with bounds and nullable fields", async () => {
    window.location.hash = "#/lab";
    const api = mockApi(labRoutes());
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit parameters" }));
    const stop = screen.getByLabelText("Stop Loss Pct");
    expect(stop).toHaveAttribute("max", "20");
    expect(screen.getByLabelText("Max Hold Minutes")).toHaveAttribute("step", "1");
    const tp = screen.getByLabelText("Take Profit Pct");
    await user.clear(tp);
    expect(tp).toHaveAttribute("placeholder", "off");
    await user.clear(stop);
    await user.type(stop, "3.5");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.called("PUT /settings")).toHaveLength(1));
    expect(api.called("PUT /settings")[0].body).toMatchObject({ strategies: { params: { opening_momentum: { stop_loss_pct: 3.5, take_profit_pct: null } } } });
  });

  it("runs a backtest job end-to-end and shows warnings, summary and exports", async () => {
    window.location.hash = "#/lab";
    const result = {
      id: "abc123", created_at: "2026-10-08T00:00:00Z", name: "Backtest", config: {},
      summary: { initial_capital: 100000, final_equity: 101000, total_return_pct: 1.0, benchmark_return_pct: -0.5, max_drawdown_pct: 0.4, max_drawdown_usd: 400, n_days: 3, n_trades: 4, win_rate: 0.5, profit_factor: 1.8, expectancy: 250, net_pnl: 1000, avg_win: 500, avg_loss: -250, sharpe: null, ratios_note: "Only 3 trading days: Sharpe/Sortino are not statistically meaningful", by_strategy: {} },
      result: { feed: feed(), days: [], equity_curve: [["2026-09-14T13:30:00+00:00", 100000], ["2026-09-14T14:30:00+00:00", 101000]], warnings: ["SYNTHETIC data: results show machinery only", "Only 4 trades (< 30): statistically weak"], trades: [], result_hash: "x" },
    };
    let polls = 0;
    const api = mockApi({
      ...labRoutes(), "POST /backtests": { job_id: "job1" },
      "GET /backtests/jobs/job1": () => (++polls < 2 ? { id: "job1", status: "running", progress: 0.4, message: "2026-09-15", result_ids: [], error: "" } : { id: "job1", status: "done", progress: 1, message: "done", result_ids: ["abc123"], error: "" }),
      "GET /backtests/abc123": result,
    });
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Run backtest" }));
    expect(await screen.findByTestId("bt-summary", {}, { timeout: 4000 })).toHaveTextContent("+1.00%");
    expect(screen.getByTestId("bt-warnings")).toHaveTextContent("SYNTHETIC data");
    expect(screen.getByTestId("bt-warnings")).toHaveTextContent("statistically weak");
    expect(screen.getByText("withheld")).toBeInTheDocument(); // Sharpe not shown for tiny samples
    expect(screen.getByRole("link", { name: "trades.csv" })).toHaveAttribute("href", "/api/backtests/abc123/export/trades.csv");
    const req = api.called("POST /backtests")[0].body as Record<string, unknown>;
    expect(req).toMatchObject({ dataset: "synthetic_sample", compare: true, oos_fraction: 0.3, strategies: ["opening_momentum", "gap_continuation"] });
    expect(req.dates).toEqual(["2026-09-14", "2026-09-15", "2026-09-16"]);
  });

  it("reports backtest failures", async () => {
    window.location.hash = "#/lab";
    mockApi({ ...labRoutes(), "POST /backtests": () => errorResponse(400, "dataset 'x' unavailable") });
    render(<App />);
    await userEvent.setup().click(await screen.findByRole("button", { name: "Run backtest" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("dataset 'x' unavailable");
  });
});

describe("stock detail", () => {
  it("asks the user to start a session first", async () => {
    window.location.hash = "#/stock";
    mockApi({ ...base(), "GET /scanner": scanner([]) });
    render(<App />);
    expect(await screen.findByText(/Start a session from the Overview page/)).toBeInTheDocument();
  });

  it("submits a simulated order and shows the outcome, including rejections", async () => {
    window.location.hash = "#/stock";
    const stock = {
      symbol: "AAA", name: "AAA Corp", as_of: "x", bars: [{ t: 1, o: 1, h: 2, l: 0.5, c: 1.5, v: 10 }], open_time: 1, scan: scanRow("AAA"),
      evaluations: [{ strategy: "opening_momentum", symbol: "AAA", passed: false, strength: 0, conditions: [{ label: "Above VWAP", passed: false, detail: "-1%" }], stop_loss_pct: 2.5, take_profit_pct: 5, trailing_stop_pct: 2, max_hold_minutes: 90 }],
      halted: false, position: null, held: false, feed: feed(),
    };
    let reject = false;
    const api = mockApi({
      ...base(), "GET /status": status({ session_status: "paused_stream", session_date: "2026-10-08", phase: "trading" }), "GET /scanner": scanner(), "GET /stocks/AAA": stock,
      "POST /orders": () => (reject ? { id: "ORD-9", status: "rejected", reject_reason: "insufficient buying power" } : { id: "ORD-8", status: "pending", reject_reason: "" }),
    });
    render(<App />);
    const user = userEvent.setup();
    expect(await screen.findByTestId("eval-opening_momentum")).toHaveTextContent("Above VWAP");
    await user.click(screen.getByRole("button", { name: "Submit simulated buy" }));
    expect(await screen.findByRole("status")).toHaveTextContent("ORD-8 accepted");
    reject = true;
    await user.click(screen.getByRole("button", { name: "Submit simulated buy" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Rejected: insufficient buying power"));
    expect(api.called("POST /orders")[0].body).toMatchObject({ symbol: "AAA", side: "buy", quantity: 100, order_type: "market" });
  });
});
