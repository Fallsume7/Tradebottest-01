export type Json = string | number | boolean | null | Json[] | { [k: string]: Json };

export interface Feed {
  kind: "synthetic" | "historical" | "realtime" | "delayed";
  provider: string;
  label: string;
  delay_seconds: number;
  warnings: string[];
  is_realtime: boolean;
  available: boolean;
}

export interface Alert {
  ts: string;
  level: "warning" | "critical";
  code: string;
  message: string;
}

export interface Portfolio {
  equity: number;
  cash: number;
  buying_power: number;
  starting_capital: number;
  total_return_pct: number;
  daily_pnl: number;
  daily_pnl_pct: number;
  realized_pnl: number;
  unrealized_pnl: number;
  open_positions: number;
  pending_orders: number;
}

export type BotState = "running" | "paused" | "emergency_stop" | "stopped";

export interface ModeRef {
  id: "max_expected_return" | "aggressive_upside";
  label: string;
  recommended?: boolean;
}

export interface ForecasterInfo {
  name: string;
  kind: string;
  calibrated: boolean;
  trained_through: string | null;
  label: string;
  requested: string;
}

export interface Status {
  mode: ModeRef;
  market: { phase: string; label: string; open: boolean; simulated: boolean };
  bot: { state: BotState; entries_allowed: boolean; daily_loss_halt: boolean; emergency_behavior: string };
  forecaster: ForecasterInfo;
  eligibility: { applied: boolean; n_tickers: number; source: string };
  settings_saved_at: string | null;
  ui: { simple_mode: boolean; theme: "dark" | "light"; notifications: boolean; show_research_candidates: boolean; log_level: string };
  bot_state: BotState;
  session_status: "idle" | "running" | "paused_stream" | "finished" | "error" | "live";
  session_error: string;
  phase: string;
  session_date: string | null;
  sim_time_utc: string | null;
  sim_time_chicago: string | null;
  progress: number;
  schedule: Record<string, string | boolean> | null;
  data_mode: "demo" | "historical" | "connected";
  execution_mode: string;
  real_money: boolean;
  feed: Feed;
  provider_error: string;
  portfolio: Portfolio;
  alerts: Alert[];
  alerts_critical: number;
  scheduler_enabled: boolean;
  restored_from_db: boolean;
}

export interface Position {
  symbol: string;
  quantity: number;
  avg_cost: number;
  mark: number;
  market_value: number;
  unrealized_pnl: number;
  unrealized_pct: number;
  strategy: string;
  opened_at: string;
  entry_reason: string;
  stop_price: number | null;
  take_profit_price: number | null;
  trailing_stop_pct: number | null;
  high_water_mark: number;
  max_hold_minutes: number | null;
  exit_order_ids: string[];
  mode: string;
  initial_stop_price: number | null;
  breakeven_armed: boolean;
  target_return_pct: number | null;
  deadline: string | null;
  minutes_left: number | null;
  minutes_to_close: number | null;
  progress_to_target_pct: number | null;
  stop_cushion_pct: number | null;
  expected_net_return_pct: number | null;
  p_positive: number | null;
  p_target: number | null;
  forecast_label: string | null;
  forecast_calibrated: boolean;
  plan_notes: string[];
  current_forecast: { expected_net_return_pct: number; p_positive: number; horizon_minutes: number; calibrated: boolean; label: string } | null;
  eligibility: EligibilityStatus;
  eligibility_label: string;
  eligibility_flag: boolean;
  closing: boolean;
}

export interface Order {
  id: string;
  client_order_id: string;
  symbol: string;
  side: "buy" | "sell";
  quantity: number;
  filled_quantity: number;
  order_type: string;
  limit_price: number | null;
  status: "new" | "pending" | "partially_filled" | "filled" | "canceled" | "rejected" | "expired";
  avg_fill_price: number;
  strategy: string;
  reason: string;
  reject_reason: string;
  created_at: string;
  updated_at: string | null;
  external_id: string | null;
  history: [string, string, string][];
}

export interface Trade {
  id: string;
  symbol: string;
  strategy: string;
  quantity: number;
  entry_time: string;
  entry_price: number;
  exit_time: string;
  exit_price: number;
  gross_pnl: number;
  commissions: number;
  net_pnl: number;
  return_pct: number;
  hold_minutes: number;
  entry_reason: string;
  exit_reason: string;
  session_date: string;
  mode?: string;
  predicted_net_return_pct?: number | null;
  predicted_p_positive?: number | null;
  predicted_p_target?: number | null;
  planned_target_price?: number | null;
  planned_stop_price?: number | null;
  forecast_calibrated?: boolean;
}

export interface Component {
  name: string;
  label: string;
  raw: number | null;
  score: number | null;
  weight: number;
  points: number;
  note: string;
  available: boolean;
}

export interface Metrics {
  symbol: string;
  price: number | null;
  prev_close: number | null;
  pct_change: number | null;
  premarket_pct: number | null;
  gap_pct: number | null;
  volume: number;
  rvol: number | null;
  dollar_volume: number;
  market_cap: number | null;
  volatility_pct: number | null;
  spread_pct: number | null;
  momentum_short: number | null;
  momentum_long: number | null;
  vs_vwap_pct: number | null;
  vwap: number | null;
  dist_from_high_pct: number | null;
  high_of_day: number | null;
  n_bars: number;
  halted: boolean;
  abnormal: string[];
}

export interface ScanRow {
  symbol: string;
  name: string;
  rank: number;
  score: number;
  tradable: boolean;
  continuing: boolean;
  metrics: Metrics;
  components: Component[];
  reasons: string[];
  warnings: string[];
  blockers: string[];
}

export interface ScannerResp {
  as_of: string | null;
  feed: Feed;
  config: { preset: string; filters: Record<string, number | boolean | null>; weights: Record<string, number> };
  results: ScanRow[];
  n_tradable: number;
}

export interface Condition {
  label: string;
  passed: boolean;
  detail: string;
}

export interface Evaluation {
  strategy: string;
  symbol: string;
  passed: boolean;
  strength: number;
  conditions: Condition[];
  stop_loss_pct: number | null;
  take_profit_pct: number | null;
  trailing_stop_pct: number | null;
  max_hold_minutes: number | null;
}

export interface Bar {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

export interface StockResp {
  symbol: string;
  name: string;
  as_of: string;
  bars: Bar[];
  open_time: number;
  scan: ScanRow;
  evaluations: Evaluation[];
  halted: boolean;
  position: Position | null;
  held: boolean;
  feed: Feed;
}

export interface ParamSchema {
  title?: string;
  description?: string;
  type?: string;
  default?: number | boolean | string | null;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  anyOf?: { type: string; minimum?: number; maximum?: number; exclusiveMinimum?: number }[];
}

export interface StrategyInfo {
  id: string;
  name: string;
  summary: string;
  entry_rules: string[];
  exit_rules: string[];
  risk_rules: string[];
  params_schema: Record<string, ParamSchema>;
  params: Record<string, number | boolean | string | null>;
  enabled: boolean;
  disclaimer: string;
}

export interface Settings {
  risk: {
    starting_capital: number;
    max_position_pct: number;
    risk_per_trade_pct: number;
    max_positions: number;
    max_daily_loss_pct: number;
    stop_loss_pct: number;
    take_profit_pct: number | null;
    trailing_stop_pct: number | null;
    max_hold_minutes: number | null;
    max_trades_per_day: number;
    block_reentry_after_loss: boolean;
    max_data_age_seconds: number;
  };
  costs: {
    commission_per_share: number;
    commission_min_per_order: number;
    commission_per_order: number;
    slippage_bps: number;
    impact_bps_per_pct_participation: number;
    assumed_spread_bps: number;
    max_participation: number;
    latency_seconds: number;
    min_fill_volume: number;
  };
  schedule: {
    scanner_start: string;
    observe_minutes: number;
    last_entry: string;
    liquidation_start: string;
    final_check_minutes_before_close: number;
    report_delay_minutes: number;
    tick_seconds: number;
  };
  scanner: ScannerResp["config"] & { on_missing_data: "ignore" | "exclude"; top_n: number };
  strategies: { enabled: string[]; params: Record<string, Record<string, number | boolean | string | null>> };
  data_mode: "demo" | "historical" | "connected";
  demo_seed: number;
  demo_speed: number;
  historical_dataset: string;
  auto_schedule: boolean;
  allow_delayed_feed_trading: boolean;
  pause_on_mirror_failure: boolean;
}

export interface SettingsResp {
  settings: Settings;
  env: {
    data_mode_default: string;
    execution_mode: string;
    massive_key_configured: boolean;
    massive_feed: string;
    timezone: string;
    real_money: boolean;
  };
}

export interface Decision {
  ts: string;
  kind: string;
  symbol: string;
  strategy: string;
  message: string;
  details: Record<string, Json>;
}

export interface EquityPoint {
  ts: string;
  equity: number;
  date: string;
}

export interface TradeStats {
  n_trades: number;
  win_rate: number | null;
  net_pnl: number;
  avg_win: number | null;
  avg_loss: number | null;
  payoff_ratio: number | null;
  profit_factor: number | null;
  expectancy: number | null;
  avg_return_pct: number | null;
  avg_hold_minutes: number | null;
  best_trade: number | null;
  worst_trade: number | null;
}

export interface Performance {
  starting_capital: number;
  equity: number;
  total_return_pct: number;
  benchmark_return_pct: number | null;
  benchmark_note: string;
  max_drawdown_pct: number;
  max_drawdown_usd: number;
  daily: { date: string; pnl: number; pnl_pct: number; n_trades: number; end_equity: number; benchmark_pct: number | null }[];
  trade_stats: TradeStats;
  daily_stats: { n_days: number; mean_daily_pct: number | null; std_daily_pct: number | null; sharpe: number | null; sortino: number | null; ratios_note: string | null };
  by_strategy: Record<string, TradeStats>;
}

export interface DailyReport {
  date: string;
  start_equity: number;
  end_equity: number;
  pnl: number;
  pnl_pct: number;
  n_trades: number;
  win_rate: number | null;
  liquidation_complete: boolean;
  unresolved_positions: { symbol: string; quantity: number; unrealized_pnl: number }[];
  benchmark_return_pct?: number;
}

export interface Dataset {
  id: string;
  name: string;
  synthetic: boolean;
  dates: string[] | null;
  description: string;
}

export interface BacktestSummary {
  initial_capital: number;
  final_equity: number;
  total_return_pct: number;
  benchmark_return_pct: number;
  max_drawdown_pct: number;
  max_drawdown_usd: number;
  n_days: number;
  n_trades: number;
  win_rate: number | null;
  profit_factor: number | null;
  expectancy: number | null;
  net_pnl: number;
  avg_win: number | null;
  avg_loss: number | null;
  sharpe: number | null;
  ratios_note: string | null;
  by_strategy: Record<string, TradeStats>;
  in_sample?: Record<string, number | null>;
  out_of_sample?: Record<string, number | null>;
  mode?: string;
  eligibility?: { applied: boolean; note?: string; mode?: string; n_tickers?: number };
  forecaster?: { requested: string; per_day: string[]; calibrated: boolean; note: string };
  exits?: { counts: Record<string, number>; labels: Record<string, string>; target_hit_rate: number | null; stop_rate: number | null; end_of_day_rate: number | null; forecast_exit_rate: number | null };
  forecast_accuracy?: { n: number; mean_predicted_net_pct?: number; mean_realised_net_pct?: number; mean_abs_error_pct?: number; correlation?: number | null; predicted_win_rate?: number | null; realised_win_rate?: number; calibrated?: boolean; note: string };
  avg_return_pct?: number | null;
  avg_hold_minutes?: number | null;
}

export interface BacktestListItem {
  id: string;
  created_at: string;
  name: string;
  config: { strategies: string[] };
  summary: BacktestSummary;
}

export interface BacktestResult {
  id: string;
  created_at: string;
  name: string;
  config: Record<string, Json>;
  summary: BacktestSummary;
  result: {
    feed: Feed;
    days: { date: string; start_equity: number; end_equity: number; pnl: number; return_pct: number; n_trades: number; benchmark_return_pct: number; unresolved: string[] }[];
    equity_curve: [string, number][];
    warnings: string[];
    trades: Trade[];
    result_hash: string;
  };
}

export interface Job {
  id: string;
  status: "queued" | "running" | "done" | "error" | "cancelled";
  progress: number;
  message: string;
  result_ids: string[];
  error: string;
  kind?: string;
  payload?: unknown;
}


// ---------------------------------------------------------------- eligibility, opportunities, settings
export type EligibilityStatus = "confirmed_tradable" | "confirmed_unavailable" | "unverified";

export interface EligibilityRow {
  ticker: string;
  status: EligibilityStatus;
  status_label: string;
  effective_from: string;
  verified_on: string | null;
  name: string;
  exchange: string;
  security_type: string;
  rejection_reason: string;
  restrictions: string;
  source: string;
  n_history?: number;
}

export interface EligibilitySummary {
  applied: boolean;
  n_tickers: number;
  counts: Record<EligibilityStatus, number>;
  source: string;
  note: string;
  verifier: { name: string; available: boolean; message: string };
}

export interface OppRow {
  symbol: string;
  name: string;
  price: number;
  day_change_pct: number | null;
  eligibility: EligibilityStatus;
  eligibility_label: string;
  eligibility_note: string;
  actionable: boolean;
  research_only: boolean;
  held: boolean;
  score: number;
  utility: number;
  expected_net_return_pct: number | null;
  expected_return_pct: number | null;
  uncertainty_pct: number | null;
  p_positive: number | null;
  downside_pct: number | null;
  upside_pct: number | null;
  target_price: number | null;
  target_return_pct: number | null;
  p_target: number | null;
  stop_price: number | null;
  horizon_minutes: number;
  minutes_to_close: number;
  calibrated: boolean;
  forecast_label: string | null;
  blocked_by: string | null;
  summary: string | null;
}

export interface OppResp {
  as_of: string | null;
  feed: Feed;
  mode: { id: string; label: string };
  forecaster: ForecasterInfo;
  eligibility_source: string;
  eligibility_applied: boolean;
  top: OppRow[];
  ready: OppRow[];
  watching: OppRow[];
  research: OppRow[];
  unavailable_skipped: { symbol: string; name: string; score: number; reason: string }[];
  counts: { scanned: number; eligible: number; ready: number; research: number; unavailable_skipped: number };
  waiting: string | null;
  note: string;
}

export interface Gate {
  label: string;
  passed: boolean;
  detail: string;
}

export interface OppDetail {
  symbol: string;
  name: string;
  price: number;
  score: number;
  actionable: boolean;
  research_only: boolean;
  eligibility: { status: EligibilityStatus; status_label: string; rejection_reason: string; restrictions: string; source: string; verified_on: string | null; violations: string[]; applied: boolean } | null;
  forecast: {
    horizon_minutes: number; expected_return_pct: number; expected_net_return_pct: number; uncertainty_pct: number;
    p_positive: number; upside_pct: number; downside_pct: number; cost_pct: number; method: string; calibrated: boolean; label: string;
    contributions: { name: string; label: string; value: number | null; points: number }[]; warnings: string[];
  } | null;
  plan: {
    entry_price: number; target_price: number; target_return_pct: number; target_net_return_pct: number; p_target: number;
    stop_price: number; stop_pct: number; max_hold_minutes: number; reward_risk: number; notes: string[]; blockers: string[];
  } | null;
  gates: Gate[];
  explanation: { summary?: string; drivers?: string[]; risks?: string[]; invalidation?: string[]; plan_notes?: string[]; data_note?: string; calibrated?: boolean };
  minutes_to_close: number;
  day_change_pct: number | null;
}

export interface ActivityItem {
  ts: string;
  ts_chicago: string;
  kind: string;
  symbol: string;
  message: string;
  exit_category: string | null;
}

export interface EquitySeries {
  range: string;
  points: EquityPoint[];
  start_equity: number;
  end_equity: number;
  change: number;
  change_pct: number;
}

export type Control = "number" | "slider" | "switch" | "select" | "multiselect" | "time" | "text";

export interface SchemaField {
  path: string;
  category: string;
  label: string;
  help: string;
  control: Control;
  effect: "immediate" | "new_trades" | "open_positions" | "next_session" | "idle_only";
  effect_text: string;
  advanced: boolean;
  min: number | null;
  max: number | null;
  step: number | null;
  unit: string;
  options: { value: string; label: string }[];
  nullable: boolean;
  group: string;
  value: Json;
  recommended: Json;
  is_recommended: boolean;
}

export interface SettingsSchema {
  categories: { id: string; label: string; blurb: string }[];
  fields: SchemaField[];
  mode: { active: string; label: string; modes: { id: string; label: string; recommended: boolean; summary: string }[] };
  scanner_presets: string[];
  saved_at: string | null;
  open_positions: number;
  session_running: boolean;
}

export interface Effect {
  path: string;
  label: string;
  from: Json;
  to: Json;
  effect: string;
  message: string;
  blocked: boolean;
}

export interface ApiFieldError {
  path: string;
  message: string;
}

export interface CompareRow {
  label: string;
  mode: string;
  id?: string;
  total_return_pct: number | null;
  final_equity?: number | null;
  max_drawdown_pct?: number | null;
  win_rate?: number | null;
  avg_return_pct?: number | null;
  profit_factor?: number | null;
  n_trades: number | null;
  avg_hold_minutes?: number | null;
  target_hit_rate?: number | null;
  stop_rate?: number | null;
  end_of_day_rate?: number | null;
  forecast_exit_rate?: number | null;
  out_of_sample?: { n_days: number; total_return_pct: number; n_trades: number; win_rate: number | null; avg_return_pct: number | null } | null;
}

export interface ComparePayload {
  id: string;
  created_at: string;
  dataset: string;
  rows: CompareRow[];
  verdict: string;
  same_data: { dataset: string; feed: Feed; n_days: number; eligibility: { applied: boolean; note?: string; n_tickers?: number } | null; forecaster: string };
  equity_curves: Record<string, [string, number][]>;
  warnings: string[];
}

export interface ModelRow {
  n: number;
  mae_pct: number;
  rmse_pct: number;
  bias_pct: number;
  mean_realized_net_pct: number;
  share_profitable: number;
  rank_ic: number | null;
  top_pick_net_pct: number | null;
  all_stocks_net_pct: number | null;
  top_pick_edge_pct: number | null;
  calibrated: boolean;
  note: string;
  verdict: string;
  n_test_days: number;
}

export interface ModelCheckPayload {
  id: string;
  ok: boolean;
  reason?: string;
  created_at: string;
  dataset: string;
  n_days: number;
  n_test_samples: number;
  test_days: string[];
  models: Record<string, ModelRow>;
  baselines: Record<string, { mean_net_pct: number | null; n_slices: number }>;
  warnings: string[];
  data: { feed: Feed; note: string; eligibility_applied: boolean; min_test_days_for_calibrated: number };
  saved_model?: { name: string; calibrated: boolean; trained_through: string };
}

export interface LabLatest {
  comparison: ComparePayload | null;
  model_check: ModelCheckPayload | null;
  forecaster: ForecasterInfo;
}
