import { vi } from "vitest";
import type { Feed, Order, Position, ScanRow, ScannerResp, Settings, SettingsResp, Status, StrategyInfo } from "../types";

export const feed = (over: Partial<Feed> = {}): Feed => ({
  kind: "synthetic", provider: "synthetic-demo", label: "SYNTHETIC demo data - fabricated prices, not a real market",
  delay_seconds: 0, warnings: [], is_realtime: false, available: true, ...over,
});

export const status = (over: Partial<Status> = {}): Status => ({
  bot_state: "running", session_status: "idle", session_error: "", phase: "idle", session_date: null, sim_time_utc: null,
  sim_time_chicago: null, progress: 0, schedule: null, data_mode: "demo", execution_mode: "paper", real_money: false, feed: feed(),
  provider_error: "",
  portfolio: { equity: 100000, cash: 100000, buying_power: 100000, starting_capital: 100000, total_return_pct: 0, daily_pnl: 0, daily_pnl_pct: 0, realized_pnl: 0, unrealized_pnl: 0, open_positions: 0, pending_orders: 0 },
  alerts: [], alerts_critical: 0, scheduler_enabled: false, restored_from_db: false, ...over,
});

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

export const position = (symbol = "AAA"): Position => ({
  symbol, quantity: 100, avg_cost: 5, mark: 5.5, market_value: 550, unrealized_pnl: 50, unrealized_pct: 10, strategy: "opening_momentum",
  opened_at: "2026-10-08T14:00:00+00:00", entry_reason: "Open drive 3%", stop_price: 4.8, take_profit_price: 5.5, trailing_stop_pct: 2,
  high_water_mark: 5.6, max_hold_minutes: 90, exit_order_ids: [],
});

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
