import type {
  ActivityItem, ApiFieldError, BacktestListItem, BacktestResult, DailyReport, Dataset, Decision, EligibilityRow, EligibilityStatus,
  EligibilitySummary, Effect, EquityPoint, EquitySeries, Job, LabLatest, OppDetail, OppResp, Order, Performance, Position,
  ScannerResp, Settings, SettingsResp, SettingsSchema, Status, StockResp, StrategyInfo, Trade,
} from "./types";

export type Action = "start" | "pause" | "resume" | "stop" | "flatten" | "emergency_stop" | "clear_emergency";

export class ApiError extends Error {
  status: number;
  fieldErrors: ApiFieldError[];
  constructor(message: string, status: number, fieldErrors: ApiFieldError[] = []) {
    super(message);
    this.status = status;
    this.fieldErrors = fieldErrors;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError("Cannot reach the backend. Is `mwbot serve` running?", 0);
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    let fieldErrors: ApiFieldError[] = [];
    try {
      const j = await res.json();
      if (typeof j.detail === "string") detail = j.detail;
      else if (Array.isArray(j.detail)) detail = j.detail.map((d: { msg?: string }) => d.msg).join("; ");
      if (Array.isArray(j.errors)) fieldErrors = j.errors;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail, res.status, fieldErrors);
  }
  return (await res.json()) as T;
}

const get = <T,>(p: string) => request<T>("GET", p);
const post = <T,>(p: string, b?: unknown) => request<T>("POST", p, b ?? {});

export const api = {
  status: () => get<Status>("/status"),
  startSession: (body: { speed?: number; instant?: boolean; date?: string }) => post<{ date: string }>("/session/start", body),
  stopSession: () => post("/session/stop"),
  pauseStream: () => post("/session/pause-stream"),
  resumeStream: () => post("/session/resume-stream"),
  step: (n: number) => post<Status>("/session/step", { n }),
  bot: (action: Action) => post<{ state: string; action: string }>(`/bot/${action}`),
  closePosition: (symbol: string) => post<{ closing: string }>(`/positions/${encodeURIComponent(symbol)}/close`),
  setMode: (mode: string) => post<{ mode: { from: string; to: string; message: string; open_positions: number } }>("/mode", { mode }),
  activity: (limit = 60) => get<ActivityItem[]>(`/activity?limit=${limit}`),
  opportunities: () => get<OppResp>("/opportunities"),
  opportunity: (symbol: string) => get<OppDetail>(`/opportunities/${encodeURIComponent(symbol)}`),
  equitySeries: (range: string) => get<EquitySeries>(`/portfolio/equity?range=${range}`),
  eligibility: (q = "", status = "all") =>
    get<{ summary: EligibilitySummary; rows: EligibilityRow[] }>(`/eligibility?q=${encodeURIComponent(q)}&status=${status}&limit=1000`),
  markEligibility: (ticker: string, status: EligibilityStatus, rejection_reason = "", name = "") =>
    request<EligibilityRow>("PUT", `/eligibility/${encodeURIComponent(ticker)}`, { status, rejection_reason, name }),
  deleteEligibility: (ticker: string) => request<{ deleted: number }>("DELETE", `/eligibility/${encodeURIComponent(ticker)}`),
  clearEligibility: () => request<{ status: string }>("DELETE", "/eligibility"),
  importEligibility: (csv: string, dry_run = false) =>
    post<{ n_rows: number; errors: string[]; applied: boolean; preview: EligibilityRow[] }>("/eligibility/import", { csv, dry_run }),
  seedEligibilityDemo: () => post<{ added: number }>("/eligibility/seed-demo"),
  eligibilityExportUrl: () => "/api/eligibility/export.csv",
  settingsSchema: () => get<SettingsSchema>("/settings/schema"),
  saveChanges: (changes: Record<string, unknown>) =>
    post<{ settings: Settings; effects: Effect[]; saved_at: string }>("/settings/changes", { changes }),
  previewChanges: (changes: Record<string, unknown>) =>
    post<{ valid: boolean; errors: ApiFieldError[]; effects: Effect[] }>("/settings/preview", { changes }),
  resetSettings: (category?: string) => post<{ effects: Effect[]; reset: string }>("/settings/reset", { category }),
  exportSettings: () => get<Record<string, unknown>>("/settings/export"),
  importSettings: (payload: unknown, apply: boolean) =>
    post<{ applied: boolean; n_changes: number; effects: Effect[] }>("/settings/import", { payload, apply }),
  labLatest: () => get<LabLatest>("/strategy-lab/latest"),
  compareModes: (body: unknown) => post<{ job_id: string }>("/compare-modes", body),
  modelCheck: (body: unknown) => post<{ job_id: string }>("/model-check", body),
  resetAccount: () => post("/account/reset"),
  positions: () => get<Position[]>("/portfolio/positions"),
  orders: () => get<Order[]>("/portfolio/orders"),
  trades: () => get<Trade[]>("/portfolio/trades"),
  equity: () => get<EquityPoint[]>("/portfolio/equity"),
  performance: () => get<Performance>("/performance"),
  decisions: (limit = 200) => get<Decision[]>(`/decisions?limit=${limit}`),
  reports: () => get<DailyReport[]>("/reports"),
  scanner: () => get<ScannerResp>("/scanner"),
  presets: () => get<Record<string, Record<string, number>>>("/scanner/presets"),
  applyPreset: (name: string) => post(`/scanner/preset/${name}`),
  stock: (symbol: string) => get<StockResp>(`/stocks/${encodeURIComponent(symbol)}`),
  placeOrder: (o: { symbol: string; side: "buy" | "sell"; quantity: number; order_type: "market" | "limit"; limit_price?: number; client_order_id?: string }) =>
    post<{ id: string; status: string; reject_reason: string }>("/orders", o),
  cancelOrder: (id: string) => request<{ canceled: boolean }>("DELETE", `/orders/${id}`),
  strategies: () => get<StrategyInfo[]>("/strategies"),
  settings: () => get<SettingsResp>("/settings"),
  saveSettings: (patch: unknown) => request<Settings>("PUT", "/settings", patch),
  datasets: () => get<Dataset[]>("/datasets"),
  runBacktest: (body: unknown) => post<{ job_id: string }>("/backtests", body),
  job: (id: string) => get<Job>(`/backtests/jobs/${id}`),
  backtests: () => get<BacktestListItem[]>("/backtests"),
  backtest: (id: string) => get<BacktestResult>(`/backtests/${id}`),
  exportUrl: (id: string, kind: "trades" | "summary") => `/api/backtests/${id}/export/${kind}.csv`,
};
