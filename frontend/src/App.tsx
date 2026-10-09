import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { FeedBadge } from "./components/FeedBadge";
import { Badge, Button, Icon } from "./components/ui";
import { safeStorage, useHashRoute, usePoll } from "./hooks";
import History from "./pages/History";
import Overview from "./pages/Overview";
import Performance from "./pages/Performance";
import Positions from "./pages/Positions";
import Scanner from "./pages/Scanner";
import SettingsPage from "./pages/Settings";
import StockDetail from "./pages/StockDetail";
import StrategyLab from "./pages/StrategyLab";
import type { Status } from "./types";

interface Ctx {
  status: Status | undefined;
  statusError: string | null;
  reloadStatus: () => void;
  go: (route: string) => void;
  selected: string | null;
  select: (symbol: string | null) => void;
}
const AppCtx = createContext<Ctx>(null as unknown as Ctx);
export const useApp = () => useContext(AppCtx);

const NAV: { id: string; label: string; icon: string }[] = [
  { id: "overview", label: "Overview", icon: "overview" },
  { id: "scanner", label: "Scanner", icon: "scanner" },
  { id: "stock", label: "Stock", icon: "stock" },
  { id: "positions", label: "Positions", icon: "positions" },
  { id: "history", label: "History", icon: "history" },
  { id: "lab", label: "Strategy Lab", icon: "lab" },
  { id: "performance", label: "Performance", icon: "performance" },
  { id: "settings", label: "Settings", icon: "settings" },
];

const BOT_COLOR = { running: "up", paused: "warn", emergency_stop: "down", stopped: "muted" } as const;

export default function App() {
  const [route, go] = useHashRoute();
  const [selected, select] = useState<string | null>(null);
  const [theme, setTheme] = useState(() => safeStorage().get("mwbot-theme") ?? "dark");
  const live = (s?: Status) => s?.session_status === "running" || s?.session_status === "live";
  const [fast, setFast] = useState(false);
  const { data: status, error, reload } = usePoll(api.status, fast ? 1000 : 3000, [fast]);
  useEffect(() => setFast(live(status)), [status]);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    safeStorage().set("mwbot-theme", theme);
  }, [theme]);

  const ctx = useMemo<Ctx>(
    () => ({ status, statusError: error, reloadStatus: reload, go, selected, select }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [status, error, selected, go],
  );

  const emergency = useCallback(async () => {
    if (!window.confirm("EMERGENCY STOP: cancel pending buys, flatten all virtual positions and block new entries until cleared. Continue?")) return;
    await api.bot("emergency_stop");
    reload();
  }, [reload]);

  const page = (() => {
    switch (route) {
      case "scanner": return <Scanner />;
      case "stock": return <StockDetail />;
      case "positions": return <Positions />;
      case "history": return <History />;
      case "lab": return <StrategyLab />;
      case "performance": return <Performance />;
      case "settings": return <SettingsPage />;
      default: return <Overview />;
    }
  })();

  const criticals = (status?.alerts ?? []).filter((a) => a.level === "critical").slice(-3);

  return (
    <AppCtx.Provider value={ctx}>
      <div className="flex min-h-full flex-col md:flex-row">
        <aside className="hidden w-52 shrink-0 flex-col border-r border-line bg-panel md:flex" aria-label="Primary">
          <div className="flex items-center gap-2 px-4 py-4">
            <div className="grid h-7 w-7 place-items-center rounded-lg bg-accent/15 text-accent"><Icon name="stock" /></div>
            <div className="leading-tight"><div className="text-sm font-semibold">MarketWatchBot</div><div className="text-[11px] text-muted">paper trading lab</div></div>
          </div>
          <nav className="flex flex-col gap-0.5 px-2">
            {NAV.map((n) => (
              <a key={n.id} href={`#/${n.id}`} aria-current={route === n.id || (n.id === "overview" && route === "") ? "page" : undefined}
                className={`flex items-center gap-2.5 rounded-lg px-3 py-2 text-[13px] transition ${route === n.id || (n.id === "overview" && !NAV.some((x) => x.id === route)) ? "bg-accent/10 font-medium text-accent" : "text-muted hover:bg-panel2 hover:text-ink"}`}>
                <Icon name={n.icon} />{n.label}
              </a>
            ))}
          </nav>
          <div className="mt-auto space-y-2 p-3 text-[11px] text-muted">
            <Badge color="up" title="This project has no real-money execution path.">PAPER ONLY - no real money</Badge>
            <div className="flex items-center justify-between">
              <span>Times: America/Chicago</span>
              <button className="rounded px-1.5 py-0.5 ring-1 ring-line hover:text-ink" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} aria-label="Toggle theme">{theme === "dark" ? "Light" : "Dark"}</button>
            </div>
          </div>
        </aside>

        <div className="flex min-w-0 flex-1 flex-col pb-16 md:pb-0">
          <header className="sticky top-0 z-20 flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line bg-bg/90 px-4 py-2.5 backdrop-blur sm:px-6">
            <FeedBadge feed={status?.feed} />
            <span className="num text-xs text-muted" data-testid="sim-clock">
              {status?.sim_time_chicago ? `${status.sim_time_chicago} CT` : "no active session"}
            </span>
            {status && <Badge color="info">{status.phase.replace(/_/g, " ")}</Badge>}
            {status && <Badge color={BOT_COLOR[status.bot_state]}>bot: {status.bot_state.replace("_", " ")}</Badge>}
            <div className="ml-auto flex items-center gap-2">
              <Button variant="danger" onClick={emergency} disabled={!status || status.bot_state === "emergency_stop"} aria-label="Emergency stop">EMERGENCY STOP</Button>
              <Button variant="ghost" className="md:hidden" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} aria-label="Toggle theme">{theme === "dark" ? "Light" : "Dark"}</Button>
            </div>
          </header>
          {(error || criticals.length > 0 || status?.session_error) && (
            <div className="space-y-1 px-4 pt-3 sm:px-6" aria-live="polite">
              {error && <div role="alert" className="rounded-lg border border-down/40 bg-down/10 px-3 py-2 text-[13px] text-down">{error}</div>}
              {status?.session_error && <div role="alert" className="rounded-lg border border-down/40 bg-down/10 px-3 py-2 text-[13px] text-down">Session error: {status.session_error}</div>}
              {criticals.map((a) => (
                <div key={a.ts + a.code} role="alert" className="rounded-lg border border-down/40 bg-down/10 px-3 py-2 text-[13px] text-down">
                  <strong className="mr-2">ALERT</strong>{a.message}
                </div>
              ))}
            </div>
          )}
          <main className="min-w-0 flex-1 px-4 py-4 sm:px-6">{page}</main>
        </div>

        <nav className="fixed inset-x-0 bottom-0 z-30 flex overflow-x-auto border-t border-line bg-panel/95 py-1 backdrop-blur md:hidden" aria-label="Mobile">
          {NAV.map((n) => (
            <a key={n.id} href={`#/${n.id}`} className={`flex min-w-[64px] shrink-0 flex-col items-center gap-0.5 px-2 py-1 text-[10px] ${route === n.id ? "text-accent" : "text-muted"}`}>
              <Icon name={n.icon} className="h-5 w-5" />{n.label}
            </a>
          ))}
        </nav>
      </div>
    </AppCtx.Provider>
  );
}
