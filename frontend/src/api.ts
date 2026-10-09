import type {
  BacktestListItem, BacktestResult, DailyReport, Dataset, Decision, EquityPoint, Job, Order, Performance, Position,
  ScannerResp, Settings, SettingsResp, Status, StockResp, StrategyInfo, Trade,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
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
    try {
      const j = await res.json();
      if (typeof j.detail === "string") detail = j.detail;
      else if (Array.isArray(j.detail)) detail = j.detail.map((d: { msg?: string }) => d.msg).join("; ");
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail, res.status);
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
  bot: (action: "start" | "pause" | "resume" | "emergency_stop" | "clear_emergency") => post<{ state: string }>(`/bot/${action}`),
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
