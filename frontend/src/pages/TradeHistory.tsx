import { useMemo, useState } from "react";
import { useApp } from "../App";
import { api } from "../api";
import { exitCategory } from "../components/explain";
import { Chip, ErrorState, Loading, Segmented, usePref } from "../components/kit";
import { Badge, Card, Empty, PnL, Select, TextInput } from "../components/ui";
import { chicagoTime, money, num, pct, signedMoney, titleCase } from "../format";
import { usePoll } from "../hooks";
import type { Trade } from "../types";

type View = "trades" | "orders" | "days";
type Result = "all" | "win" | "loss";

const EXIT_NAMES: Record<string, string> = {
  target: "Target hit", risk: "Stop / trailing", forecast: "Forecast exit", end_of_day: "End of day", time: "Deadline", rotation: "Swapped",
  emergency: "Emergency", limit: "Loss limit", manual: "Manual", other: "Other",
};

function toCsv(rows: Trade[]): string {
  const cols: (keyof Trade)[] = ["session_date", "symbol", "mode", "quantity", "entry_time", "entry_price", "exit_time", "exit_price", "net_pnl", "return_pct", "hold_minutes", "entry_reason", "exit_reason"];
  const esc = (v: unknown) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  return [cols.join(","), ...rows.map((r) => cols.map((c) => esc(r[c])).join(","))].join("\n");
}

export default function TradeHistory() {
  const { status, explain, version } = useApp();
  const live = status?.session_status === "running" || status?.session_status === "live";
  const trades = usePoll(api.trades, live ? 3000 : 8000, [status?.session_status, version]);
  const orders = usePoll(api.orders, live ? 3000 : 8000, [status?.session_status, version]);
  const reports = usePoll(api.reports, live ? 5000 : 10000, [status?.session_status, version]);
  const [view, setView] = usePref<View>("hist-view", "trades");
  const [result, setResult] = usePref<Result>("hist-result", "all");
  const [exitType, setExitType] = usePref<string>("hist-exit", "all");
  const [q, setQ] = useState("");

  const rows = useMemo(() => (trades.data ?? []).filter((t) =>
    (result === "all" || (result === "win" ? t.net_pnl > 0 : t.net_pnl <= 0)) &&
    (exitType === "all" || exitCategory(t.exit_reason) === exitType) &&
    (!q.trim() || t.symbol.includes(q.trim().toUpperCase()))), [trades.data, result, exitType, q]);
  const total = rows.reduce((s, t) => s + t.net_pnl, 0);
  const exportCsv = () => {
    const url = URL.createObjectURL(new Blob([toCsv(rows)], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url; a.download = "trade_history.csv"; a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Segmented label="History view" value={view} onChange={setView} options={[{ value: "trades", label: "Trades" }, { value: "orders", label: "Orders" }, { value: "days", label: "Days" }]} />
        {view === "trades" && rows.length > 0 && <span className="text-xs text-muted">{rows.length} trade(s) · net <PnL value={total} text={signedMoney(total)} /></span>}
        {view === "trades" && <button onClick={exportCsv} disabled={rows.length === 0} className="ml-auto text-xs text-accent hover:underline disabled:opacity-40">Export CSV</button>}
      </div>

      {view === "trades" && (
        <Card pad={false}>
          <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
            <Chip active={result === "all"} onClick={() => setResult("all")}>All</Chip>
            <Chip active={result === "win"} onClick={() => setResult("win")}>Winners</Chip>
            <Chip active={result === "loss"} onClick={() => setResult("loss")}>Losers</Chip>
            <Select aria-label="Exit type" value={exitType} onChange={(e) => setExitType(e.target.value)} className="!w-auto">
              <option value="all">Any exit</option>{Object.entries(EXIT_NAMES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</Select>
            <div className="ml-auto w-full sm:w-48"><TextInput aria-label="Search symbol" placeholder="Search symbol" value={q} onChange={(e) => setQ(e.target.value)} /></div>
          </div>
          {trades.error && !trades.data ? <ErrorState message={trades.error} onRetry={trades.reload} /> : !trades.data ? <Loading rows={5} label="Loading trades" /> : rows.length === 0 ? (
            <Empty>{(trades.data ?? []).length === 0 ? "No closed trades yet. Each one will appear here with the reasons it was bought and sold." : "No trades match these filters."}</Empty>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-[13px]" data-testid="trade-table">
                <thead className="text-left text-[10px] uppercase tracking-wider text-muted"><tr><th className="px-4 py-2">Closed</th><th>Stock</th><th className="text-right">Qty</th><th className="text-right">Entry</th><th className="text-right">Exit</th><th className="text-right">Net P/L</th><th className="text-right">Return</th><th className="text-right">Held</th><th className="pr-4">How it ended</th></tr></thead>
                <tbody>
                  {rows.map((t) => {
                    const cat = exitCategory(t.exit_reason);
                    return (
                      <tr key={t.id} tabIndex={0} onClick={() => explain({ kind: "trade", trade: t })} onKeyDown={(e) => e.key === "Enter" && explain({ kind: "trade", trade: t })} aria-label={`Explain ${t.symbol} trade`} className="cursor-pointer border-t border-line hover:bg-panel2 focus:bg-panel2">
                        <td className="num px-4 py-2.5 text-xs text-muted">{t.session_date.slice(5)} {chicagoTime(t.exit_time)}</td>
                        <td className="font-semibold">{t.symbol}</td><td className="num text-right">{t.quantity}</td>
                        <td className="num text-right">{money(t.entry_price, 2)}</td><td className="num text-right">{money(t.exit_price, 2)}</td>
                        <td className="text-right"><PnL value={t.net_pnl} text={signedMoney(t.net_pnl)} /></td>
                        <td className="text-right"><PnL value={t.return_pct} text={pct(t.return_pct)} /></td>
                        <td className="num text-right text-muted">{num(t.hold_minutes, 0)}m</td>
                        <td className="pr-4"><Badge color={cat === "target" ? "up" : cat === "risk" || cat === "emergency" || cat === "limit" ? "down" : "muted"}>{EXIT_NAMES[cat] ?? titleCase(cat)}</Badge></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {view === "orders" && (
        <Card pad={false}>
          {!orders.data ? <Loading rows={4} /> : orders.data.length === 0 ? <Empty>No orders yet.</Empty> : (
            <div className="overflow-x-auto"><table className="w-full min-w-[640px] text-[13px]">
              <thead className="text-left text-[10px] uppercase tracking-wider text-muted"><tr><th className="px-4 py-2">Time</th><th>Stock</th><th>Side</th><th className="text-right">Filled</th><th className="text-right">Avg price</th><th>Status</th><th className="pr-4">Why</th></tr></thead>
              <tbody>{orders.data.map((o) => (
                <tr key={o.id} className="border-t border-line align-top"><td className="num px-4 py-2 text-xs text-muted">{chicagoTime(o.created_at)}</td><td className="font-semibold">{o.symbol}</td>
                  <td className={o.side === "buy" ? "text-up" : "text-down"}>{o.side}</td><td className="num text-right">{o.filled_quantity}/{o.quantity}</td><td className="num text-right">{o.avg_fill_price ? money(o.avg_fill_price, 3) : "-"}</td>
                  <td><Badge color={o.status === "filled" ? "up" : o.status === "rejected" || o.status === "expired" ? "down" : "muted"}>{titleCase(o.status)}</Badge></td>
                  <td className="max-w-[320px] truncate pr-4 text-xs text-muted" title={o.reject_reason || o.reason}>{o.reject_reason || o.reason}</td></tr>
              ))}</tbody></table></div>
          )}
        </Card>
      )}

      {view === "days" && (
        <Card pad={false}>
          {!reports.data ? <Loading rows={3} /> : reports.data.length === 0 ? <Empty>No completed days yet.</Empty> : (
            <table className="w-full text-[13px]"><thead className="text-left text-[10px] uppercase tracking-wider text-muted"><tr><th className="px-4 py-2">Day</th><th className="text-right">Start</th><th className="text-right">End</th><th className="text-right">P/L</th><th className="text-right">Trades</th><th className="pr-4 text-right">Flat at close</th></tr></thead>
              <tbody>{[...reports.data].reverse().map((r) => (
                <tr key={r.date} className="border-t border-line"><td className="px-4 py-2 font-medium">{r.date}</td><td className="num text-right">{money(r.start_equity, 0)}</td><td className="num text-right">{money(r.end_equity, 0)}</td>
                  <td className="text-right"><PnL value={r.pnl} text={`${signedMoney(r.pnl)} (${pct(r.pnl_pct)})`} /></td><td className="num text-right">{r.n_trades}</td>
                  <td className="pr-4 text-right">{r.liquidation_complete ? <Badge color="up">yes</Badge> : <Badge color="down" title={r.unresolved_positions.map((u) => u.symbol).join(", ")}>NO</Badge>}</td></tr>
              ))}</tbody></table>
          )}
        </Card>
      )}
    </div>
  );
}
