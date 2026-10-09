import { Fragment, useMemo, useState } from "react";
import { api } from "../api";
import { Badge, Card, Empty, ErrorBanner, PnL, Select } from "../components/ui";
import { chicagoTime, money, num, pct, signedMoney } from "../format";
import { usePoll } from "../hooks";

export default function History() {
  const { data: trades, error } = usePoll(api.trades, 5000);
  const { data: reports } = usePoll(api.reports, 8000);
  const { data: decisions } = usePoll(() => api.decisions(300), 6000);
  const [strategy, setStrategy] = useState("all");
  const [result, setResult] = useState("all");
  const [open, setOpen] = useState<string | null>(null);
  const [tab, setTab] = useState<"trades" | "log" | "reports">("trades");

  const strategies = useMemo(() => [...new Set((trades ?? []).map((t) => t.strategy))], [trades]);
  const rows = (trades ?? []).filter((t) => (strategy === "all" || t.strategy === strategy) && (result === "all" || (result === "win" ? t.net_pnl > 0 : t.net_pnl <= 0)));
  const total = rows.reduce((a, t) => a + t.net_pnl, 0);

  return (
    <div className="space-y-4">
      <ErrorBanner message={error} />
      <div className="flex gap-1 rounded-lg bg-panel p-1 ring-1 ring-line w-fit" role="tablist">
        {([["trades", "Completed trades"], ["log", "Decision log"], ["reports", "Daily reports"]] as const).map(([id, label]) => (
          <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)} className={`rounded-md px-3 py-1.5 text-[13px] ${tab === id ? "bg-accent/15 font-medium text-accent" : "text-muted hover:text-ink"}`}>{label}</button>
        ))}
      </div>

      {tab === "trades" && (
        <Card title={`Trades (${rows.length}) - net ${signedMoney(total)}`} pad={false}
          right={<><div className="w-36"><Select aria-label="Strategy filter" value={strategy} onChange={(e) => setStrategy(e.target.value)}><option value="all">All strategies</option>{strategies.map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}</Select></div>
            <div className="w-28"><Select aria-label="Result filter" value={result} onChange={(e) => setResult(e.target.value)}><option value="all">All</option><option value="win">Winners</option><option value="loss">Losers</option></Select></div></>}>
          {rows.length === 0 ? <Empty>No completed trades yet.</Empty> : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[900px] text-[13px]">
                <thead className="text-left text-[11px] uppercase text-muted"><tr><th className="px-4 py-2">Entry (CT)</th><th>Symbol</th><th>Strategy</th><th className="text-right">Qty</th><th className="text-right">Entry</th><th className="text-right">Exit</th><th className="text-right">Net P/L</th><th className="text-right">Return</th><th className="text-right">Held</th><th>Exit reason</th></tr></thead>
                <tbody>
                  {rows.map((t) => (
                    <Fragment key={t.id}>
                      <tr className="cursor-pointer border-t border-line hover:bg-panel2" onClick={() => setOpen(open === t.id ? null : t.id)} data-testid={`trade-${t.id}`}>
                        <td className="num px-4 py-2 text-xs text-muted">{t.session_date}<br />{chicagoTime(t.entry_time)}→{chicagoTime(t.exit_time)}</td>
                        <td className="font-semibold">{t.symbol}</td>
                        <td className="text-xs">{t.strategy.replace(/_/g, " ")}</td>
                        <td className="num text-right">{t.quantity}</td>
                        <td className="num text-right">{money(t.entry_price, 4)}</td>
                        <td className="num text-right">{money(t.exit_price, 4)}</td>
                        <td className="text-right"><PnL value={t.net_pnl} text={signedMoney(t.net_pnl)} /></td>
                        <td className="text-right"><PnL value={t.return_pct} text={pct(t.return_pct)} /></td>
                        <td className="num text-right text-xs">{num(t.hold_minutes, 0)}m</td>
                        <td className="max-w-[260px] truncate text-xs text-muted" title={t.exit_reason}>{t.exit_reason}</td>
                      </tr>
                      {open === t.id && (
                        <tr className="bg-panel2/60"><td colSpan={10} className="space-y-1 px-4 py-3 text-xs">
                          <div><span className="font-semibold text-up">Why bought: </span>{t.entry_reason}</div>
                          <div><span className="font-semibold text-info">Why sold: </span>{t.exit_reason}</div>
                          <div className="text-muted">gross {signedMoney(t.gross_pnl)} · commissions {money(t.commissions)}</div>
                        </td></tr>
                      )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {tab === "log" && (
        <Card title="Decision log (most recent first)" pad={false}>
          {!decisions || decisions.length === 0 ? <Empty>Nothing logged yet.</Empty> : (
            <ul className="max-h-[640px] divide-y divide-line overflow-auto text-[13px]">
              {decisions.map((d, i) => (
                <li key={i} className="flex gap-3 px-4 py-2"><span className="num w-24 shrink-0 text-xs text-muted">{chicagoTime(d.ts, true)}</span>
                  <Badge color={d.kind === "entry" ? "up" : d.kind === "alert" ? "down" : d.kind === "skip" ? "warn" : "muted"}>{d.kind}</Badge><span className="break-words">{d.message}</span></li>
              ))}
            </ul>
          )}
        </Card>
      )}

      {tab === "reports" && (
        <Card title="Daily reports" pad={false}>
          {!reports || reports.length === 0 ? <Empty>Reports are written after each session closes.</Empty> : (
            <table className="w-full text-[13px]">
              <thead className="text-left text-[11px] uppercase text-muted"><tr><th className="px-4 py-2">Date</th><th className="text-right">Start</th><th className="text-right">End</th><th className="text-right">P/L</th><th className="text-right">Trades</th><th className="text-right">Win rate</th><th className="pr-4 text-right">Liquidation</th></tr></thead>
              <tbody>
                {[...reports].reverse().map((r) => (
                  <tr key={r.date} className="border-t border-line">
                    <td className="px-4 py-2 font-medium">{r.date}</td><td className="num text-right">{money(r.start_equity)}</td><td className="num text-right">{money(r.end_equity)}</td>
                    <td className="text-right"><PnL value={r.pnl} text={`${signedMoney(r.pnl)} (${pct(r.pnl_pct)})`} /></td>
                    <td className="num text-right">{r.n_trades}</td><td className="num text-right">{r.win_rate === null ? "-" : `${(r.win_rate * 100).toFixed(0)}%`}</td>
                    <td className="pr-4 text-right">{r.liquidation_complete ? <Badge color="up">complete</Badge> : <Badge color="down" title={r.unresolved_positions.map((u) => `${u.symbol} x${u.quantity}`).join(", ")}>UNRESOLVED</Badge>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      )}
    </div>
  );
}
