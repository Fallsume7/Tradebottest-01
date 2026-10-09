import { useMemo } from "react";
import { Bar, BarChart, CartesianGrid, Legend, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useApp } from "../App";
import { api } from "../api";
import { EquityChart } from "../components/charts";
import { exitCategory } from "../components/explain";
import { ErrorState, Loading, Segmented, useConfirm, usePref, useToast } from "../components/kit";
import { PositionSummary } from "../components/trading";
import { Badge, Button, Card, Empty, PnL, Stat } from "../components/ui";
import { chicagoTime, money, num, pct, signedMoney, titleCase } from "../format";
import { usePoll } from "../hooks";

const EXIT_NAMES: Record<string, string> = {
  target: "Profit target", risk: "Stop / trailing", forecast: "Forecast turned bad", end_of_day: "End of day", time: "Plan deadline",
  rotation: "Swapped for better idea", emergency: "Emergency stop", limit: "Daily loss limit", manual: "Closed manually", other: "Other",
};

export default function Portfolio() {
  const { status, explain, reload, version } = useApp();
  const confirm = useConfirm();
  const toast = useToast();
  const [range, setRange] = usePref<string>("eq-range", "1D");
  const live = status?.session_status === "running" || status?.session_status === "live";
  const every = live ? 2500 : 7000;
  const positions = usePoll(api.positions, every, [status?.session_status, version]);
  const orders = usePoll(api.orders, every, [status?.session_status, version]);
  const perf = usePoll(api.performance, every * 2, [status?.session_status, version]);
  const trades = usePoll(api.trades, every * 2, [status?.session_status, version]);
  const series = usePoll(() => api.equitySeries(range), every, [range, status?.session_status]);

  const exitMix = useMemo(() => {
    const m: Record<string, number> = {};
    for (const t of trades.data ?? []) m[exitCategory(t.exit_reason)] = (m[exitCategory(t.exit_reason)] ?? 0) + 1;
    return Object.entries(m).sort((a, b) => b[1] - a[1]);
  }, [trades.data]);

  const closeOne = async (symbol: string) => {
    if (!(await confirm({ title: `Close ${symbol}?`, body: "Sells the whole position at the next available price (a simulated order).", confirmLabel: "Close position", danger: true }))) return;
    try { await api.closePosition(symbol); toast(`Closing ${symbol}`); } catch (e) { toast(e instanceof Error ? e.message : String(e), "error"); }
    positions.reload(); reload();
  };
  const closeAll = async () => {
    if (!(await confirm({ title: "Close all positions?", body: "Sells every open position at the next available price. The bot keeps running afterwards.", confirmLabel: "Close all", danger: true }))) return;
    try { await api.bot("flatten"); toast("Closing all positions"); } catch (e) { toast(e instanceof Error ? e.message : String(e), "error"); }
    positions.reload(); reload();
  };
  const cancel = async (id: string) => {
    try { await api.cancelOrder(id); toast("Order canceled"); } catch (e) { toast(e instanceof Error ? e.message : String(e), "error"); }
    orders.reload();
  };

  if (!status) return <Loading rows={6} label="Loading portfolio" />;
  const p = status.portfolio;
  const pf = perf.data;
  const ts = pf?.trade_stats;
  const pts = (series.data?.points ?? []).map((e) => ({ time: Math.floor(new Date(e.ts).getTime() / 1000), value: e.equity }));
  const daily = (pf?.daily ?? []).map((d) => ({ date: d.date.slice(5), Bot: d.pnl_pct, Benchmark: d.benchmark_pct ?? 0 }));
  const css = (n: string) => getComputedStyle(document.documentElement).getPropertyValue(n).trim() || "#888";
  const open = (positions.data ?? []);
  const pending = (orders.data ?? []).filter((o) => ["new", "pending", "partially_filled"].includes(o.status));
  const nTrades = trades.data?.length ?? 0;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Portfolio value" value={money(p.equity)} sub={`cash ${money(p.cash)}`} />
        <Stat label="Unrealized P/L" value={signedMoney(p.unrealized_pnl)} valueClass={p.unrealized_pnl >= 0 ? "text-up" : "text-down"} sub="open positions" />
        <Stat label="Realized P/L" value={signedMoney(p.realized_pnl)} valueClass={p.realized_pnl >= 0 ? "text-up" : "text-down"} sub="closed trades" />
        <Stat label="Max drawdown" value={pf ? pct(-pf.max_drawdown_pct, 2, false) : "-"} valueClass="text-down" sub={pf ? money(pf.max_drawdown_usd, 0) : undefined} />
      </div>

      <Card title="Active positions" right={open.length > 0 && <Button variant="danger" onClick={closeAll}>Close all</Button>}>
        {positions.error && !positions.data ? <ErrorState message={positions.error} onRetry={positions.reload} /> : positions.loading && !positions.data ? <Loading rows={3} /> : open.length === 0 ? (
          <Empty>No open positions.</Empty>
        ) : (
          <ul className="divide-y divide-line" data-testid="positions">
            {open.map((q) => (
              <li key={q.symbol} className="flex flex-col gap-3 py-3 first:pt-0 last:pb-0 sm:flex-row sm:items-center">
                <div className="min-w-0 flex-1 cursor-pointer" onClick={() => explain({ kind: "position", position: q })}><PositionSummary p={q} /></div>
                <div className="flex shrink-0 gap-2">
                  <Button onClick={() => explain({ kind: "position", position: q })}>Why?</Button>
                  <Button variant="danger" onClick={() => closeOne(q.symbol)} disabled={q.closing} aria-label={`Close ${q.symbol}`}>{q.closing ? "Closing..." : "Close"}</Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {pending.length > 0 && (
        <Card title="Pending orders" pad={false}>
          <table className="w-full text-[13px]">
            <thead className="text-left text-[10px] uppercase tracking-wider text-muted"><tr><th className="px-4 py-2">Order</th><th>Side</th><th>Qty</th><th>Type</th><th>Status</th><th className="pr-4 text-right" /></tr></thead>
            <tbody>{pending.map((o) => (
              <tr key={o.id} className="border-t border-line"><td className="px-4 py-2 font-medium">{o.symbol}</td><td className={o.side === "buy" ? "text-up" : "text-down"}>{o.side}</td>
                <td className="num">{o.filled_quantity}/{o.quantity}</td><td>{o.order_type}{o.limit_price ? ` @ ${money(o.limit_price, 2)}` : ""}</td><td><Badge color="info">{titleCase(o.status)}</Badge></td>
                <td className="pr-4 text-right"><Button variant="ghost" onClick={() => cancel(o.id)}>Cancel</Button></td></tr>
            ))}</tbody>
          </table>
        </Card>
      )}

      <Card title="Performance" right={<Segmented label="Chart range" value={range as "1D"} onChange={setRange} options={["1D", "1W", "1M", "ALL"].map((r) => ({ value: r as "1D", label: r }))} />}>
        {pts.length > 1 ? <EquityChart points={pts} baseline={series.data?.start_equity} height={280} /> : <Empty>Run a session to build the curve.</Empty>}
      </Card>

      {pf && ts && (
        <div className="grid gap-4 lg:grid-cols-2">
          <Card title="How the trades did">
            {nTrades === 0 ? <Empty>No closed trades yet.</Empty> : (
              <>
                <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-[13px]">
                  {[
                    ["Win rate", ts.win_rate === null ? "-" : `${(ts.win_rate * 100).toFixed(0)}%`],
                    ["Average trade return", ts.avg_return_pct === null ? "-" : pct(ts.avg_return_pct)],
                    ["Profit factor", ts.profit_factor === null ? "-" : num(ts.profit_factor)],
                    ["Average hold", ts.avg_hold_minutes === null ? "-" : `${num(ts.avg_hold_minutes, 0)} min`],
                    ["Average win / loss", `${money(ts.avg_win, 0)} / ${money(ts.avg_loss, 0)}`],
                    ["Total return", pct(pf.total_return_pct)],
                    ["Benchmark", pf.benchmark_return_pct === null ? "-" : pct(pf.benchmark_return_pct)],
                    ["Sharpe", pf.daily_stats.sharpe === null ? "withheld (too few days)" : num(pf.daily_stats.sharpe)],
                  ].map(([k, v]) => <div key={k} className="flex justify-between border-b border-line/60 py-1"><dt className="text-muted">{k}</dt><dd className="num">{v}</dd></div>)}
                </dl>
                {ts.n_trades < 30 && <p className="mt-3 text-[11px] text-warn">Only {ts.n_trades} trades: these figures are not statistically reliable yet.</p>}
                {pf.benchmark_note && <p className="mt-1 text-[11px] text-muted">{pf.benchmark_note}</p>}
              </>
            )}
          </Card>
          <Card title="How trades ended">
            {exitMix.length === 0 ? <Empty>No closed trades yet.</Empty> : (
              <ul className="space-y-2.5" data-testid="exit-mix">
                {exitMix.map(([k, n]) => (
                  <li key={k}><div className="mb-1 flex justify-between text-[13px]"><span>{EXIT_NAMES[k] ?? titleCase(k)}</span><span className="num text-muted">{n} · {((n / nTrades) * 100).toFixed(0)}%</span></div>
                    <div className="h-1.5 overflow-hidden rounded-full bg-line"><div className="h-full rounded-full bg-accent" style={{ width: `${(n / nTrades) * 100}%` }} /></div></li>
                ))}
              </ul>
            )}
          </Card>
          <Card title="Daily returns vs benchmark (%)" className="lg:col-span-2">
            {daily.length === 0 ? <Empty>No completed days yet.</Empty> : (
              <div className="h-60" data-testid="daily-returns">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={daily} margin={{ left: -10, right: 8 }}>
                    <CartesianGrid stroke={css("--border")} strokeDasharray="3 3" vertical={false} />
                    <XAxis dataKey="date" stroke={css("--muted")} fontSize={11} /><YAxis stroke={css("--muted")} fontSize={11} unit="%" />
                    <Tooltip contentStyle={{ background: css("--panel"), border: `1px solid ${css("--border")}`, borderRadius: 8, fontSize: 12 }} formatter={(v: number) => `${v.toFixed(2)}%`} />
                    <Legend wrapperStyle={{ fontSize: 12 }} /><ReferenceLine y={0} stroke={css("--muted")} />
                    <Bar dataKey="Bot" fill={css("--accent")} radius={[3, 3, 0, 0]} /><Bar dataKey="Benchmark" fill={css("--muted")} radius={[3, 3, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </Card>
        </div>
      )}
      {(trades.data ?? []).length > 0 && <p className="text-xs text-muted">Last trade closed {chicagoTime((trades.data ?? [])[0].exit_time, true)} CT · <PnL value={(trades.data ?? [])[0].net_pnl} text={signedMoney((trades.data ?? [])[0].net_pnl)} /></p>}
    </div>
  );
}
