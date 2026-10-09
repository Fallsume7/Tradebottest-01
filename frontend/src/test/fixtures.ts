import { vi } from "vitest";
import type { Feed, OppResp, OppRow, Order, Position, ScanRow, ScannerResp, SchemaField, Settings, SettingsResp, SettingsSchema, Status, StrategyInfo } from "../types";

export const feed = (over: Partial<Feed> = {}): Feed => ({
  kind: "synthetic", provider: "synthetic-demo", label: "SYNTHETIC demo data - fabricated prices, not a real market",
  delay_seconds: 0, warnings: [], is_realtime: false, available: true, ...over,
});

export const status = (over: Partial<Status> = {}): Status => ({
  mode: { id: "max_expected_return", label: "Maximum Expected Return", recommended: true },
  market: { phase: "idle", label: "No session running", open: false, simulated: true },
  bot: { state: "stopped", entries_allowed: false, daily_loss_halt: false, emergency_behavior: "flatten_all" },
  forecaster: { name: "heuristic-v1", kind: "heuristic", calibrated: false, trained_through: null, label: "Rule-of-thumb estimate - not a validated probability", requested: "auto" },
  eligibility: { applied: true, n_tickers: 30, source: "demo list (made-up, for demo mode)" },
  settings_saved_at: null,
  ui: { simple_mode: true, theme: "dark", notifications: true, show_research_candidates: true, log_level: "INFO" },
  bot_state: "stopped", session_status: "idle", session_error: "", phase: "idle", session_date: null, sim_time_utc: null,
  sim_time_chicago: null, progress: 0, schedule: null, data_mode: "demo", execution_mode: "paper", real_money: false, feed: feed(),
  provider_error: "",
  portfolio: { equity: 100000, cash: 100000, buying_power: 100000, starting_capital: 100000, total_return_pct: 0, daily_pnl: 0, daily_pnl_pct: 0, realized_pnl: 0, unrealized_pnl: 0, open_positions: 0, pending_orders: 0 },
  alerts: [], alerts_critical: 0, scheduler_enabled: false, restored_from_db: false, ...over,
});

/** Convenience: a status whose bot is in `state` (keeps the legacy bot_state field in sync). */
export const withBot = (state: Status["bot"]["state"], over: Partial<Status> = {}): Status =>
  status({ bot: { state, entries_allowed: state === "running", daily_loss_halt: false, emergency_behavior: "flatten_all" }, bot_state: state, ...over });

export const scanRow = (symbol: string, o: Partial<ScanRow> & { pct?: number; rvol?: number | null } = {}): ScanRow => ({
  symbol, name: `${symbol} Corp (synthetic)`, rank: o.rank ?? 1, score: o.score ?? 50, tradable: o.tradable ?? true, continuing: o.continuing ?? false,
  metrics: {
    symbol, price: 5, prev_close: 4.5, pct_change: o.pct ?? 11, premarket_pct: 8, gap_pct: 7, volume: 1_200_000, rvol: o.rvol === undefined ? 3.2 : o.rvol,
    dollar_volume: 6e6, market_cap: 2e8, volatility_pct: 0.4, spread_pct: 0.3, momentum_short: 0.5, momentum_long: 1, vs_vwap_pct: 2, vwap: 4.9,
    dist_from_high_pct: 1, high_of_day: 5.1, n_bars: 120, halted: false, abnormal: [],
  },
  components: [
    { name: "rvol", label: "Relative volume (x)", raw: 3.2, score: 0.5, weight: 1.5, points: 12.5, note: "", available: true },
    { name: "spread", label: "Bid-ask spread (%)", raw: null, score: null, weight: 0.5, points: 0, note: "unavailable", available: false },
  ],
  reasons: o.reasons ?? [`${symbol} relative volume 3.2x normal`], warnings: o.warnings ?? [], blockers: o.blockers ?? [], ...("tradable" in o ? {} : {}),
});

export const scanner = (rows: ScanRow[] = [scanRow("AAA", { rank: 1, score: 70 }), scanRow("BBB", { rank: 2, score: 40, pct: 25 }), scanRow("CCC", { rank: 0, tradable: false, blockers: ["price $50.00 outside $1-$20"], score: 80 })]): ScannerResp => ({
  as_of: "2026-10-08T15:00:00+00:00", feed: feed(), n_tradable: rows.filter((r) => r.tradable).length, results: rows,
  config: { preset: "small_cap_momentum", filters: { min_price: 1, max_price: 20, min_pct_change: 3, min_volume: 100000, min_rvol: 1.5, min_dollar_volume: 250000, max_spread_pct: 2 }, weights: {} },
});

export const position = (symbol = "AAA", over: Partial<Position> = {}): Position => ({
  symbol, quantity: 100, avg_cost: 5, mark: 5.3, market_value: 530, unrealized_pnl: 30, unrealized_pct: 6, strategy: "expected_opportunity",
  opened_at: "2026-10-08T14:00:00+00:00", entry_reason: "Expected +0.9% after costs over ~90 min", stop_price: 4.85, take_profit_price: 5.6, trailing_stop_pct: 2,
  high_water_mark: 5.35, max_hold_minutes: 90, exit_order_ids: [], mode: "max_expected_return", initial_stop_price: 4.85, breakeven_armed: false,
  target_return_pct: 12, deadline: "2026-10-08T19:30:00+00:00", minutes_left: 60, minutes_to_close: 200, progress_to_target_pct: 50, stop_cushion_pct: 8.5,
  expected_net_return_pct: 0.9, p_positive: 0.58, p_target: 0.41, forecast_label: "MODEL FORECAST - heuristic-v1 (heuristic/uncalibrated estimate, not a reliable probability)",
  forecast_calibrated: false, plan_notes: ["target set where the estimated chance of touching it is about 45%"], current_forecast: { expected_net_return_pct: 0.6, p_positive: 0.55, horizon_minutes: 55, calibrated: false, label: "" },
  eligibility: "confirmed_tradable", eligibility_label: "Confirmed tradable", eligibility_flag: false, closing: false, ...over,
});

export const oppRow = (symbol: string, o: Partial<OppRow> = {}): OppRow => ({
  symbol, name: `${symbol} Corp (synthetic)`, price: 5, day_change_pct: 1.2, eligibility: "confirmed_tradable", eligibility_label: "Confirmed tradable", eligibility_note: "",
  actionable: true, research_only: false, held: false, score: 62, utility: 0.4, expected_net_return_pct: 0.8, expected_return_pct: 1.3, uncertainty_pct: 2.1, p_positive: 0.58,
  downside_pct: -2.4, upside_pct: 3.9, target_price: 5.3, target_return_pct: 6, p_target: 0.42, stop_price: 4.85, horizon_minutes: 90, minutes_to_close: 240,
  calibrated: false, forecast_label: "heuristic", blocked_by: null, summary: "Expected +0.8% after costs", ...o,
});

export const oppResp = (over: Partial<OppResp> = {}): OppResp => ({
  as_of: "2026-10-08T15:00:00+00:00", feed: feed(), mode: { id: "max_expected_return", label: "Maximum Expected Return" },
  forecaster: status().forecaster, eligibility_source: "demo list", eligibility_applied: true,
  top: [oppRow("AAA"), oppRow("BBB", { actionable: false, expected_net_return_pct: 0.1, blocked_by: "Expected return after costs: +0.10% (need >= 0.25%)" })],
  ready: [oppRow("AAA")], watching: [oppRow("BBB", { actionable: false, expected_net_return_pct: 0.1, blocked_by: "Expected return after costs: +0.10% (need >= 0.25%)" })],
  research: [oppRow("RSCH", { eligibility: "unverified", eligibility_label: "Unverified", actionable: false, research_only: true })],
  unavailable_skipped: [{ symbol: "GONE", name: "Gone Corp", score: 70, reason: "not offered in this game" }],
  counts: { scanned: 40, eligible: 2, ready: 1, research: 1, unavailable_skipped: 1 }, waiting: null,
  note: "Ranked by expected return after costs ... NOT validated probabilities.", ...over,
});

export const schemaResp = (over: Partial<SettingsSchema> = {}): SettingsSchema => {
  const f = (path: string, category: string, label: string, control: SchemaField["control"], value: SchemaField["value"], o: Partial<SchemaField> = {}): SchemaField => ({
    path, category, label, help: `${label} help text`, control, effect: "new_trades", effect_text: "Applies to new trades only. Open positions keep the plan they were opened with.",
    advanced: false, min: null, max: null, step: null, unit: "", options: [], nullable: false, group: "General", value, recommended: value, is_recommended: true, ...o,
  });
  return {
    categories: [
      { id: "strategy", label: "Trading Strategy", blurb: "How the bot picks stocks." }, { id: "risk", label: "Risk Management", blurb: "Hard limits." },
      { id: "schedule", label: "Trading Schedule", blurb: "When it trades." }, { id: "game", label: "MarketWatch Game", blurb: "Your game's rules." },
      { id: "data", label: "Market Data", blurb: "Prices and costs." }, { id: "general", label: "General", blurb: "Appearance." },
    ],
    fields: [
      f("mode.gates.min_p_positive", "strategy", "Minimum chance of a gain", "slider", 0.52, { min: 0.3, max: 0.9, step: 0.01, group: "Entry rules" }),
      f("mode.gates.min_reward_risk", "strategy", "Minimum reward-to-risk", "slider", 1, { min: 0, max: 5, step: 0.05, advanced: true, group: "Entry rules" }),
      f("risk.max_position_pct", "risk", "Largest single position", "slider", 10, { min: 1, max: 100, step: 1, unit: "%", group: "Position size" }),
      f("risk.max_positions", "risk", "Most positions at once", "slider", 5, { min: 1, max: 20, step: 1, group: "Position size" }),
      f("risk.starting_capital", "risk", "Starting capital", "number", 100000, { min: 1000, max: 1e8, step: 1000, unit: "$", effect: "idle_only", effect_text: "Can only be changed while no session is running and nothing is held.", group: "Account" }),
      f("schedule.last_entry", "schedule", "Last time to open a position", "time", "14:15", { effect: "next_session", group: "Day" }),
      f("schedule.liquidation_start", "schedule", "Start end-of-day exits", "time", "14:45", { effect: "next_session", group: "Day" }),
      f("game.min_price", "game", "Lowest price your game allows", "number", 1, { min: 0, max: 1000, step: 0.5, unit: "$", group: "Rules" }),
      f("game.supported_order_types", "game", "Order types your game supports", "multiselect", ["market", "limit"], { options: [{ value: "market", label: "Market" }, { value: "limit", label: "Limit" }], group: "Rules" }),
      f("costs.assumed_spread_bps", "data", "Assumed spread when the data has no quotes", "slider", 40, { min: 0, max: 200, step: 1, unit: "bps", group: "Costs" }),
      f("ui.theme", "general", "Theme", "select", "dark", { options: [{ value: "dark", label: "Dark" }, { value: "light", label: "Light" }], effect: "immediate", group: "Interface" }),
    ],
    mode: { active: "max_expected_return", label: "Maximum Expected Return", modes: [
      { id: "max_expected_return", label: "Maximum Expected Return", recommended: true, summary: "The recommended default." },
      { id: "aggressive_upside", label: "Aggressive Upside", recommended: false, summary: "More variance for more upside." },
    ] },
    scanner_presets: ["small_cap_momentum"], saved_at: null, open_positions: 0, session_running: false, ...over,
  };
};

export const order = (o: Partial<Order> = {}): Order => ({
  id: "ORD-000001", client_order_id: "c1", symbol: "AAA", side: "buy", quantity: 100, filled_quantity: 0, order_type: "limit", limit_price: 4.9,
  status: "pending", avg_fill_price: 0, strategy: "manual", reason: "manual", reject_reason: "", created_at: "2026-10-08T14:00:00+00:00",
  updated_at: null, external_id: null, history: [], ...o,
});

export const settings = (): Settings => ({
  risk: { starting_capital: 100000, max_position_pct: 10, risk_per_trade_pct: 0.5, max_positions: 5, max_daily_loss_pct: 3, stop_loss_pct: 3, take_profit_pct: 6, trailing_stop_pct: null, max_hold_minutes: null, max_trades_per_day: 20, block_reentry_after_loss: true, max_data_age_seconds: 180 },
  costs: { commission_per_share: 0, commission_min_per_order: 0, commission_per_order: 0, slippage_bps: 5, impact_bps_per_pct_participation: 2, assumed_spread_bps: 40, max_participation: 0.1, latency_seconds: 5, min_fill_volume: 100 },
  schedule: { scanner_start: "08:00", observe_minutes: 10, last_entry: "14:15", liquidation_start: "14:45", final_check_minutes_before_close: 3, report_delay_minutes: 5, tick_seconds: 60 },
  scanner: { preset: "small_cap_momentum", filters: {}, weights: {}, on_missing_data: "ignore", top_n: 25 },
  strategies: { enabled: ["opening_momentum"], params: {} }, data_mode: "demo", demo_seed: 42, demo_speed: 10, historical_dataset: "synthetic_sample",
  auto_schedule: false, allow_delayed_feed_trading: false, pause_on_mirror_failure: true,
});

export const settingsResp = (): SettingsResp => ({ settings: settings(), env: { data_mode_default: "demo", execution_mode: "paper", massive_key_configured: false, massive_feed: "delayed", timezone: "America/Chicago", real_money: false } });

export const strategyInfo = (): StrategyInfo => ({
  id: "opening_momentum", name: "A - Opening Momentum", summary: "Buy strong early-session drives.", entry_rules: ["Within the entry window"], exit_rules: ["Below VWAP"], risk_rules: ["Stop from params"],
  params_schema: {
    start_minute: { title: "Start Minute", description: "Earliest minute after open", type: "number", minimum: 0, maximum: 120 },
    stop_loss_pct: { title: "Stop Loss Pct", description: "Initial stop", type: "number", exclusiveMinimum: 0, maximum: 20 },
    take_profit_pct: { title: "Take Profit Pct", description: "Target", anyOf: [{ type: "number", exclusiveMinimum: 0, maximum: 100 }, { type: "null" }] },
    max_hold_minutes: { title: "Max Hold Minutes", anyOf: [{ type: "integer", minimum: 1, maximum: 390 }, { type: "null" }] },
  },
  params: { start_minute: 10, stop_loss_pct: 2.5, take_profit_pct: 5, max_hold_minutes: 90 }, enabled: true, disclaimer: "Not a guarantee of profit.",
});

type Handler = (body: unknown, url: URL, init?: RequestInit) => unknown | Promise<unknown>;
export interface Call { method: string; path: string; body: unknown }

/** Route-table fetch mock. Keys are "METHOD /path" (query string ignored). Unknown routes fail the test loudly. */
export function mockApi(routes: Record<string, Handler | unknown>) {
  const calls: Call[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const method = init?.method ?? "GET";
    const path = url.pathname.replace(/^\/api/, "");
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, path, body });
    const key = `${method} ${path}`;
    if (!(key in routes)) throw new Error(`unmocked API call: ${key}`);
    const h = routes[key];
    const out = typeof h === "function" ? await (h as Handler)(body, url, init) : h;
    if (out instanceof Response) return out;
    return new Response(JSON.stringify(out), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return { calls, fn, called: (key: string) => calls.filter((c) => `${c.method} ${c.path}` === key) };
}

export const errorResponse = (status: number, detail: string) => new Response(JSON.stringify({ detail }), { status, headers: { "Content-Type": "application/json" } });
