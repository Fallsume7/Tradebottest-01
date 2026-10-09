import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { ExplainDrawer, type Target } from "./components/explain";
import { FeedBadge } from "./components/FeedBadge";
import { KitProvider, usePref } from "./components/kit";
import { BotControls, useModeSwitch } from "./components/trading";
import { Badge, Icon } from "./components/ui";
import { money, pct, signedMoney } from "./format";
import { safeStorage, useHashRoute, usePoll } from "./hooks";
import Dashboard from "./pages/Dashboard";
import Opportunities from "./pages/Opportunities";
import Portfolio from "./pages/Portfolio";
import SettingsPage from "./pages/Settings";
import StrategyLab from "./pages/StrategyLab";
import TradeHistory from "./pages/TradeHistory";
import type { Status } from "./types";

interface Ctx {
  status: Status | undefined;
  reload: () => void;
  go: (route: string) => void;
  explain: (t: Target) => void;
  /** bumps whenever something the pages show may have changed (mode switch, eligibility edit...) */
  version: number;
  refresh: () => void;
}
const AppCtx = createContext<Ctx>(null as unknown as Ctx);
export const useApp = () => useContext(AppCtx);

export const NAV: { id: string; label: string }[] = [
  { id: "dashboard", label: "Dashboard" },
  { id: "opportunities", label: "Opportunities" },
  { id: "portfolio", label: "Portfolio" },
  { id: "lab", label: "Strategy Lab" },
  { id: "history", label: "Trade History" },
  { id: "settings", label: "Settings" },
];

const BOT_LABEL = { running: "Running", paused: "Paused", stopped: "Stopped", emergency_stop: "EMERGENCY STOP" } as const;
const BOT_COLOR = { running: "up", paused: "warn", stopped: "muted", emergency_stop: "down" } as const;

function Metric({ label, value, sub, tone, testId }: { label: string; value: React.ReactNode; sub?: React.ReactNode; tone?: string; testId?: string }) {
  return (
    <div className="min-w-0" data-testid={testId}>
      <div className="truncate text-[10px] font-medium uppercase tracking-wider text-muted">{label}</div>
      <div className={`num truncate text-[17px] font-semibold leading-tight ${tone ?? ""}`}>{value}</div>
      {sub !== undefined && <div className="truncate text-[11px] text-muted">{sub}</div>}
    </div>
  );
}

function TopBar({ status, reload }: { status: Status | undefined; reload: () => void }) {
  const [speed] = usePref<string>("speed", "10");
  const switchMode = useModeSwitch(status, reload);
  const p = status?.portfolio;
  const up = (v: number | undefined) => ((v ?? 0) >= 0 ? "text-up" : "text-down");
  return (
    <header className="sticky top-0 z-20 border-b border-line bg-bg/90 backdrop-blur" aria-label="Account summary">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 pt-2.5 sm:px-6">
        {status && <Badge color={status.market.open ? "up" : "muted"}><span className={`h-1.5 w-1.5 rounded-full bg-current ${status.market.open ? "pulse-dot" : ""}`} />{status.market.label}</Badge>}
        <FeedBadge feed={status?.feed} compact />
        <span className="num text-xs text-muted" data-testid="sim-clock">{status?.sim_time_chicago ? `${status.sim_time_chicago} CT` : "no session running"}</span>
        {status && <Badge color={BOT_COLOR[status.bot.state]}>Bot: {BOT_LABEL[status.bot.state]}</Badge>}
        <div className="ml-auto"><BotControls status={status} reload={reload} speed={speed} compact /></div>
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-2 px-4 py-2.5 sm:grid-cols-3 sm:px-6 lg:grid-cols-6" data-testid="topbar-metrics">
        <Metric testId="stat-equity" label="Portfolio value" value={p ? money(p.equity) : "-"} sub={p ? `started ${money(p.starting_capital, 0)}` : undefined} />
        <Metric testId="stat-daily" label="Today's P/L" value={p ? signedMoney(p.daily_pnl) : "-"} tone={up(p?.daily_pnl)} sub={p ? pct(p.daily_pnl_pct) : undefined} />
        <Metric testId="stat-return" label="Total return" value={p ? pct(p.total_return_pct) : "-"} tone={up(p?.total_return_pct)} />
        <Metric testId="stat-bp" label="Buying power" value={p ? money(p.buying_power) : "-"} />
        <Metric testId="stat-positions" label="Open positions" value={p ? p.open_positions : "-"} sub={p && p.pending_orders ? `${p.pending_orders} pending order(s)` : undefined} />
        <div className="min-w-0">
          <label htmlFor="mode-select" className="block truncate text-[10px] font-medium uppercase tracking-wider text-muted">Strategy</label>
          <select id="mode-select" aria-label="Trading mode" value={status?.mode.id ?? ""} disabled={!status}
            onChange={(e) => switchMode(e.target.value, e.target.value === "max_expected_return" ? "Maximum Expected Return" : "Aggressive Upside")}
            title="★ = recommended default" className="mt-0.5 w-full max-w-[220px] rounded-md border border-line bg-panel px-1.5 py-0.5 text-[13px] font-medium focus:border-accent">
            <option value="max_expected_return">Max Expected Return ★</option>
            <option value="aggressive_upside">Aggressive Upside</option>
          </select>
        </div>
      </div>
    </header>
  );
}

export default function App() {
  const [route, go] = useHashRoute();
  const [theme, setTheme] = useState(() => safeStorage().get("mwbot-theme") ?? "dark");
  const [fast, setFast] = useState(false);
  const { data: status, error, reload } = usePoll(api.status, fast ? 1000 : 3000, [fast]);
  const [target, setTarget] = useState<Target | null>(null);
  const [version, setVersion] = useState(0);
  useEffect(() => setFast(status?.session_status === "running" || status?.session_status === "live"), [status?.session_status]);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    safeStorage().set("mwbot-theme", theme);
  }, [theme]);
  // the server remembers the theme too (Settings > General); adopt it once it is known
  const serverTheme = status?.ui?.theme;
  useEffect(() => { if (serverTheme) setTheme(serverTheme); }, [serverTheme]);
  const toggleTheme = () => {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    void api.saveChanges({ "ui.theme": next }).catch(() => undefined); // remembered across restarts when the backend is reachable
  };

  const ctx = useMemo<Ctx>(
    () => ({ status, reload, go, explain: setTarget, version, refresh: () => { setVersion((v) => v + 1); reload(); } }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [status, version, go],
  );

  const current = NAV.some((n) => n.id === route) ? route : "dashboard";
  const page = (() => {
    switch (current) {
      case "opportunities": return <Opportunities />;
      case "portfolio": return <Portfolio />;
      case "lab": return <StrategyLab />;
      case "history": return <TradeHistory />;
      case "settings": return <SettingsPage />;
      default: return <Dashboard />;
    }
  })();

  const criticals = (status?.alerts ?? []).filter((a) => a.level === "critical").slice(-3);
  const notify = status?.alerts && criticals.length > 0;

  return (
    <KitProvider>
      <AppCtx.Provider value={ctx}>
        <div className="flex min-h-full flex-col md:flex-row">
          <aside className="hidden shrink-0 flex-col border-r border-line bg-panel md:flex md:w-16 lg:w-56" aria-label="Primary">
            <div className="flex items-center gap-2 px-3 py-4 lg:px-4">
              <div className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-accent/15 text-accent"><Icon name="stock" /></div>
              <div className="hidden leading-tight lg:block"><div className="text-sm font-semibold">MarketWatchBot</div><div className="text-[11px] text-muted">paper trading</div></div>
            </div>
            <nav className="flex flex-col gap-0.5 px-2">
              {NAV.map((n) => (
                <a key={n.id} href={`#/${n.id}`} title={n.label} aria-label={n.label} aria-current={current === n.id ? "page" : undefined}
                  className={`flex items-center gap-2.5 rounded-lg px-3 py-2 text-[13px] transition md:justify-center lg:justify-start ${current === n.id ? "bg-accent/10 font-medium text-accent" : "text-muted hover:bg-panel2 hover:text-ink"}`}>
                  <Icon name={n.id === "lab" ? "lab" : n.id} className="h-[18px] w-[18px] shrink-0" /><span className="hidden lg:inline" aria-hidden>{n.label}</span>
                </a>
              ))}
            </nav>
            <div className="mt-auto space-y-2 p-3 text-[11px] text-muted">
              <div className="hidden lg:block"><Badge color="up" title="This project has no real-money execution path.">PAPER ONLY - no real money</Badge></div>
              <div className="flex items-center justify-between md:justify-center lg:justify-between">
                <span className="hidden lg:inline">Times: Chicago</span>
                <button className="rounded px-1.5 py-0.5 ring-1 ring-line hover:text-ink" onClick={toggleTheme} aria-label="Toggle theme">{theme === "dark" ? "Light" : "Dark"}</button>
              </div>
            </div>
          </aside>

          <div className="flex min-w-0 flex-1 flex-col pb-16 md:pb-0">
            <TopBar status={status} reload={reload} />
            {(error || notify || status?.session_error || status?.bot.state === "emergency_stop" || status?.bot.daily_loss_halt) && (
              <div className="space-y-1 px-4 pt-3 sm:px-6" aria-live="polite">
                {error && <div role="alert" className="rounded-lg border border-down/40 bg-down/10 px-3 py-2 text-[13px] text-down">{error}</div>}
                {status?.session_error && <div role="alert" className="rounded-lg border border-down/40 bg-down/10 px-3 py-2 text-[13px] text-down">Session error: {status.session_error}</div>}
                {status?.bot.state === "emergency_stop" && <div role="alert" className="rounded-lg border border-down/40 bg-down/10 px-3 py-2 text-[13px] text-down"><strong className="mr-2">EMERGENCY STOP ACTIVE</strong>No new trades will be made until you clear it.</div>}
                {status?.bot.daily_loss_halt && <div role="alert" className="rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn"><strong className="mr-2">DAILY LOSS LIMIT HIT</strong>Positions were closed and new trades are blocked for the rest of the day.</div>}
                {criticals.map((a) => (
                  <div key={a.ts + a.code} role="alert" className="rounded-lg border border-down/40 bg-down/10 px-3 py-2 text-[13px] text-down"><strong className="mr-2">ALERT</strong>{a.message}</div>
                ))}
              </div>
            )}
            <main className="min-w-0 flex-1 px-4 py-4 sm:px-6">{page}</main>
          </div>

          <nav className="fixed inset-x-0 bottom-0 z-30 flex border-t border-line bg-panel/95 py-1 backdrop-blur md:hidden" aria-label="Mobile">
            {NAV.map((n) => (
              <a key={n.id} href={`#/${n.id}`} aria-current={current === n.id ? "page" : undefined} className={`flex flex-1 flex-col items-center gap-0.5 px-1 py-1 text-[10px] ${current === n.id ? "text-accent" : "text-muted"}`}>
                <Icon name={n.id} className="h-5 w-5" />{n.label.replace("Trade History", "History").replace("Strategy Lab", "Lab")}
              </a>
            ))}
          </nav>
        </div>
        {target && <ExplainDrawer target={target} onClose={() => setTarget(null)} onChanged={ctx.refresh} />}
      </AppCtx.Provider>
    </KitProvider>
  );
}
