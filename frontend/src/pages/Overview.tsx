import { useState } from "react";
import { useApp } from "../App";
import { api } from "../api";
import { EquityChart } from "../components/charts";
import { FeedBadge } from "../components/FeedBadge";
import { Badge, Button, Card, Empty, ErrorBanner, PnL, Progress, Select, Stat } from "../components/ui";
import { chicagoTime, money, pct, signedMoney } from "../format";
import { usePoll } from "../hooks";

const SPEEDS = [
  { label: "Watch it (10 sim-min/sec)", v: "10" },
  { label: "Slow (3 sim-min/sec)", v: "3" },
  { label: "Fast (60 sim-min/sec)", v: "60" },
  { label: "Instant (full day now)", v: "instant" },
];

export default function Overview() {
  const { status, reloadStatus, go, select } = useApp();
  const [speed, setSpeed] = useState("10");
  const [err, setErr] = useState<string | null>(null);
  const running = status?.session_status === "running";
  const active = running || status?.session_status === "paused_stream";
  const { data: equity } = usePoll(api.equity, running ? 2000 : 6000, [status?.session_date, status?.session_status]);
  const { data: decisions } = usePoll(() => api.decisions(14), running ? 2000 : 6000, [status?.session_status]);
  const { data: positions } = usePoll(api.positions, running ? 2000 : 6000, [status?.session_status]);

  const act = async (fn: () => Promise<unknown>) => {
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
    reloadStatus();
  };

  if (!status) return <Empty>Connecting to the backend...</Empty>;
  const p = status.portfolio;
  const points = (equity ?? []).map((e) => ({ time: Math.floor(new Date(e.ts).getTime() / 1000), value: e.equity }));
  const sched = status.schedule;

  return (
    <div className="space-y-4">
      <ErrorBanner message={err} onClose={() => setErr(null)} />
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Portfolio value" testId="stat-equity" value={money(p.equity)} sub={`started with ${money(p.starting_capital, 0)}`} />
        <Stat label="Today's P/L" testId="stat-daily" value={signedMoney(p.daily_pnl)} valueClass={p.daily_pnl >= 0 ? "text-up" : "text-down"} sub={pct(p.daily_pnl_pct)} />
        <Stat label="Total return" testId="stat-return" value={pct(p.total_return_pct)} valueClass={p.total_return_pct >= 0 ? "text-up" : "text-down"} sub={`realized ${signedMoney(p.realized_pnl)}`} />
        <Stat label="Buying power" testId="stat-bp" value={money(p.buying_power)} sub={`cash ${money(p.cash)}`} />
        <Stat label="Active positions" testId="stat-positions" value={p.open_positions} sub={`${p.pending_orders} pending order(s)`} />
        <Stat label="Unrealized P/L" value={signedMoney(p.unrealized_pnl)} valueClass={p.unrealized_pnl >= 0 ? "text-up" : "text-down"} />
        <Stat label="Bot status" value={<span className="text-[18px]">{status.bot_state.replace("_", " ")}</span>} sub={status.restored_from_db ? "account restored from database" : "fresh account"} />
        <Stat label="Market session" value={<span className="text-[18px]">{status.phase.replace(/_/g, " ")}</span>} sub={status.session_date ? `${status.session_date} - ${status.sim_time_chicago ?? ""} CT` : "no session yet"} />
      </div>

      <div className="grid items-start gap-4 xl:grid-cols-3">
        <Card title="Equity" className="xl:col-span-2" right={<span className="text-xs text-muted">{status.session_date ?? "-"}</span>}>
          {points.length > 1 ? <EquityChart points={points} baseline={p.starting_capital} height={340} /> : <Empty>Start a session to see the equity curve build up.</Empty>}
        </Card>

        <div className="space-y-4">
          <Card title="Session control" right={<FeedBadge feed={status.feed} compact />}>
            <div className="space-y-3">
              <div className="text-xs leading-relaxed text-muted">{status.feed.label}</div>
              {status.provider_error && <ErrorBanner message={status.provider_error} />}
              {status.session_status !== "running" && status.session_status !== "paused_stream" && (
                <>
                  <Select aria-label="Replay speed" value={speed} onChange={(e) => setSpeed(e.target.value)}>
                    {SPEEDS.map((s) => <option key={s.v} value={s.v}>{s.label}</option>)}
                  </Select>
                  <Button variant="primary" className="w-full" disabled={!status.feed.available}
                    onClick={() => act(() => api.startSession(speed === "instant" ? { instant: true } : { speed: Number(speed) }))}>
                    {status.session_status === "finished" ? "Run next trading day" : status.data_mode === "connected" ? "Start live session" : "Start session"}
                  </Button>
                </>
              )}
              {running && <Button className="w-full" onClick={() => act(api.pauseStream)}>Pause stream</Button>}
              {status.session_status === "paused_stream" && (
                <div className="grid grid-cols-3 gap-2">
                  <Button onClick={() => act(api.resumeStream)}>Resume</Button>
                  <Button onClick={() => act(() => api.step(1))}>Step 1</Button>
                  <Button onClick={() => act(() => api.step(10))}>Step 10</Button>
                </div>
              )}
              {active && <Button variant="ghost" className="w-full" onClick={() => act(api.stopSession)}>Stop session</Button>}
              {active && <Progress value={status.progress} />}
              <div className="grid grid-cols-2 gap-2 border-t border-line pt-3">
                {status.bot_state === "running" ? (
                  <Button onClick={() => act(() => api.bot("pause"))}>Pause entries</Button>
                ) : status.bot_state === "paused" ? (
                  <Button onClick={() => act(() => api.bot("resume"))}>Resume bot</Button>
                ) : status.bot_state === "emergency_stop" ? (
                  <Button onClick={() => act(() => api.bot("clear_emergency"))}>Clear e-stop</Button>
                ) : (
                  <Button onClick={() => act(() => api.bot("start"))}>Arm bot</Button>
                )}
                <Button variant="ghost" onClick={() => go("settings")}>Settings</Button>
              </div>
            </div>
          </Card>

          {sched && (
            <Card title="Today's schedule (CT)">
              <ol className="space-y-1 text-[13px]">
                {[["scanner_start", "Scanner starts"], ["open", "Market opens"], ["entries_start", "Entries begin"], ["last_entry", "Last new entry"], ["liquidation_start", "Liquidation begins"], ["final_check", "Final check"], ["close", "Market close"]].map(([k, l]) => (
                  <li key={k} className="flex justify-between"><span className="text-muted">{l}</span><span className="num">{String(sched[k])}</span></li>
                ))}
              </ol>
              {sched.early_close && <div className="mt-2"><Badge color="warn">early close</Badge></div>}
            </Card>
          )}
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Open positions" right={<Button variant="ghost" onClick={() => go("positions")}>View all</Button>} pad={false}>
          {(positions ?? []).length === 0 ? <Empty>No open positions.</Empty> : (
            <table className="w-full text-[13px]">
              <thead className="text-left text-[11px] uppercase text-muted"><tr><th className="px-4 py-2">Symbol</th><th>Qty</th><th>Avg</th><th>Last</th><th className="pr-4 text-right">P/L</th></tr></thead>
              <tbody>
                {(positions ?? []).map((q) => (
                  <tr key={q.symbol} className="cursor-pointer border-t border-line hover:bg-panel2" onClick={() => { select(q.symbol); go("stock"); }}>
                    <td className="px-4 py-2 font-medium">{q.symbol}</td><td className="num">{q.quantity}</td><td className="num">{money(q.avg_cost, 4)}</td><td className="num">{money(q.mark, 4)}</td>
                    <td className="pr-4 text-right"><PnL value={q.unrealized_pnl} text={`${signedMoney(q.unrealized_pnl)} (${pct(q.unrealized_pct)})`} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
        <Card title="Why the bot did what it did" pad={false}>
          {(decisions ?? []).length === 0 ? <Empty>No decisions yet.</Empty> : (
            <ul className="max-h-[320px] divide-y divide-line overflow-auto text-[13px]" data-testid="decision-feed">
              {(decisions ?? []).map((d, i) => (
                <li key={d.ts + i} className="flex gap-3 px-4 py-2">
                  <span className="num w-12 shrink-0 text-xs text-muted">{chicagoTime(d.ts)}</span>
                  <Badge color={d.kind === "entry" ? "up" : d.kind === "exit" || d.kind === "trade" ? "info" : d.kind === "alert" ? "down" : "muted"}>{d.kind}</Badge>
                  <span className="min-w-0 break-words">{d.message}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      {status.alerts.length > 0 && (
        <Card title="Alerts" pad={false}>
          <ul className="divide-y divide-line text-[13px]">
            {[...status.alerts].reverse().map((a, i) => (
              <li key={a.ts + i} className="flex gap-3 px-4 py-2"><Badge color={a.level === "critical" ? "down" : "warn"}>{a.level}</Badge><span>{a.message}</span><span className="num ml-auto text-xs text-muted">{chicagoTime(a.ts)}</span></li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
