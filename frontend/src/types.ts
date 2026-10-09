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

export interface Status {
  bot_state: "running" | "paused" | "emergency_stop" | "stopped";
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
}
