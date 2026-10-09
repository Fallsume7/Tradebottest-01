import { useApp } from "../App";
import { api } from "../api";
import { EquityChart } from "../components/charts";
import { Loading, Segmented, useConfirm, usePref, useToast } from "../components/kit";
import { EligibilityChip, EstimateBadge, PositionSummary, ReturnCell, useModeSwitch } from "../components/trading";
import { Badge, Button, Card, Empty, ErrorBanner, Progress, Select } from "../components/ui";
import { money, pct, signedMoney } from "../format";
import { usePoll } from "../hooks";
import type { ActivityItem, OppRow } from "../types";

const SPEEDS = [
  { label: "Watch it (10 sim-min/sec)", v: "10" },
  { label: "Slow (3 sim-min/sec)", v: "3" },
  { label: "Fast (60 sim-min/sec)", v: "60" },
  { label: "Instant (whole day now)", v: "instant" },
];

const KIND: Record<string, { icon: string; color: string }> = {
  entry: { icon: "↗", color: "text-up" },
  exit: { icon: "↘", color: "text-info" },
  trade: { icon: "●", color: "text-info" },
  fill: { icon: "✓", color: "text-muted" },
  scan: { icon: "◎", color: "text-accent" },
  control: { icon: "⏻", color: "text-warn" },
  alert: { icon: "!", color: "text-down" },
  info: { icon: "·", color: "text-muted" },
  session: { icon: "▣", color: "text-muted" },
};

function ActivityFeed({ items }: { items: ActivityItem[] | undefined }) {
  if (!items) return <Loading rows={4} label="Loading activity" />;
  if (items.length === 0) return <Empty>Nothing yet. Press Start and the bot will narrate what it sees, buys and sells.</Empty>;
  return (
    <ul className="max-h-[380px] divide-y divide-line overflow-auto text-[13px]" data-testid="activity-feed">
      {items.map((d, i) => {
        const k = KIND[d.kind] ?? KIND.info;
        return (
          <li key={d.ts + i} className="flex gap-3 px-4 py-2">
            <span className="num w-11 shrink-0 pt-0.5 text-xs text-muted">{d.ts_chicago}</span>
            <span className={`w-4 shrink-0 text-center font-bold ${k.color}`} aria-hidden>{k.icon}</span>
            <span className="min-w-0 break-words">{d.message}</span>
          </li>
        );
      })}
    </ul>
  );
}

function TopFive({ rows, onOpen }: { rows: OppRow[]; onOpen: (s: string) => void }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[560px] text-[13px]" data-testid="top5">
        <thead className="text-left text-[10px] uppercase tracking-wider text-muted">
          <tr><th className="px-4 py-2">#</th><th>Stock</th><th>Game</th><th className="text-right">Price</th><th className="text-right">Expected*</th><th className="hidden text-right lg:table-cell">Target</th><th className="hidden text-right lg:table-cell">To target</th><th className="pr-4 text-right">Status</th></tr>
        </thead>
        <tbody>
          {rows.map((o, i) => (
            <tr key={o.symbol} onClick={() => onOpen(o.symbol)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && onOpen(o.symbol)}
              className="cursor-pointer border-t border-line hover:bg-panel2 focus:bg-panel2" aria-label={`Explain ${o.symbol}`}>
              <td className="num px-4 py-2.5 text-muted">{i + 1}</td>
              <td><div className="font-semibold">{o.symbol}</div><div className="max-w-[140px] truncate text-[11px] text-muted">{o.name}</div></td>
              <td><EligibilityChip status={o.eligibility} note={o.eligibility_note} /></td>
              <td className="num text-right">{money(o.price, 2)}</td>
              <td className="text-right"><ReturnCell v={o.expected_net_return_pct} /></td>
              <td className="num hidden text-right lg:table-cell">{o.target_price ? `${money(o.target_price, 2)} (${pct(o.target_return_pct)})` : "-"}</td>
              <td className="num hidden text-right lg:table-cell">{o.p_target === null ? "-" : `${(o.p_target * 100).toFixed(0)}%`}</td>
              <td className="pr-4 text-right">{o.actionable ? <Badge color="up">Ready to buy</Badge> : <Badge color="muted" title={o.blocked_by ?? ""}>Watching</Badge>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="px-4 pb-3 pt-1 text-[11px] text-muted">*Expected return after trading costs over the time left today. An estimate - not a promise, and not how much the stock already gained.</p>
    </div>
  );
}

export default function Dashboard() {
  const { status, reload, go, explain, version } = useApp();
  const confirm = useConfirm();
  const toast = useToast();
  const switchMode = useModeSwitch(status, reload);
  const [speed, setSpeed] = usePref<string>("speed", "10");
  const [range, setRange] = usePref<string>("eq-range", "1D");
  const live = status?.session_status === "running" || status?.session_status === "live";
  const every = live ? 2000 : 6000;
  const opp = usePoll(api.opportunities, every, [status?.session_status, version]);
  const positions = usePoll(api.positions, every, [status?.session_status, version]);
  const activity = usePoll(() => api.activity(40), every, [status?.session_status, version]);
  const series = usePoll(() => api.equitySeries(range), live ? 3000 : 8000, [range, status?.session_status, status?.portfolio.equity === undefined]);

  if (!status) return <Loading rows={6} label="Connecting to the backend" />;
  const active = ["running", "paused_stream", "live"].includes(status.session_status);
  const pts = (series.data?.points ?? []).map((e) => ({ time: Math.floor(new Date(e.ts).getTime() / 1000), value: e.equity }));
  const firstRun = !active && status.session_status !== "finished" && status.portfolio.open_positions === 0 && status.portfolio.total_return_pct === 0;
  const endSession = async () => {
    if (!(await confirm({ title: "End the session?", body: status.portfolio.open_positions ? `You hold ${status.portfolio.open_positions} position(s); they stay open and are restored if you start again.` : "The simulated day stops here.", confirmLabel: "End session" }))) return;
    try { await api.stopSession(); toast("Session ended"); } catch (e) { toast(e instanceof Error ? e.message : String(e), "error"); }
    reload();
  };
  const top = opp.data?.top ?? [];

  return (
    <div className="space-y-4">
      <ErrorBanner message={status.provider_error} />

      {firstRun && (
        <Card title="Welcome - nothing to configure">
          <ol className="grid gap-3 text-[13px] sm:grid-cols-3">
            <li className="rounded-lg bg-panel2 p-3"><b className="text-accent">1.</b> Press <b>Start</b> (top right). The recommended <b>{status.mode.label}</b> settings work out of the box.</li>
            <li className="rounded-lg bg-panel2 p-3"><b className="text-accent">2.</b> The bot ranks only stocks <b>confirmed available in your game</b> by expected return, buys the best, and exits at its own target, stop or deadline.</li>
            <li className="rounded-lg bg-panel2 p-3"><b className="text-accent">3.</b> Click any stock or position to see <b>why</b>. Everything here is paper trading on {status.feed.kind} data{status.feed.kind === "synthetic" ? " (made-up prices)" : ""}.</li>
          </ol>
        </Card>
      )}

      <div className="flex flex-wrap items-center gap-2 text-xs text-muted" aria-label="Data and model honesty">
        <EstimateBadge calibrated={status.forecaster.calibrated} label={status.forecaster.label} />
        <span>{status.forecaster.label}</span>
        <span className="hidden sm:inline">·</span>
        <span>Stock list: {status.eligibility.applied ? `${status.eligibility.n_tickers} stocks, ${status.eligibility.source}` : "NOT applied"}</span>
      </div>

      <div className="grid items-start gap-4 xl:grid-cols-3">
        <Card title="Top 5 opportunities" className="xl:col-span-2" pad={false}
          right={<Button variant="ghost" onClick={() => go("opportunities")}>See all</Button>}>
          {opp.loading && !opp.data ? <Loading rows={5} label="Loading opportunities" /> : top.length === 0 ? (
            <Empty>
              {active ? (opp.data?.waiting ?? "No stock that is confirmed available in your game has usable data yet.") : "Start the bot to see the best-supported opportunities."}
              {opp.data && opp.data.counts.research > 0 && <div className="mt-2 text-xs">{opp.data.counts.research} research candidate(s) are waiting for you to confirm they exist in your game - see Opportunities.</div>}
            </Empty>
          ) : <TopFive rows={top} onOpen={(s) => explain({ kind: "symbol", symbol: s })} />}
        </Card>

        <Card title="Mode & session">
          <div className="space-y-3">
            <div role="radiogroup" aria-label="Trading mode" className="space-y-2">
              {[
                { id: "max_expected_return", label: "Maximum Expected Return", rec: true, text: "Recommended. Best risk-adjusted opportunities, moderate positions." },
                { id: "aggressive_upside", label: "Aggressive Upside", rec: false, text: "More variance for more upside: larger positions, wider net. Same safety limits." },
              ].map((m) => (
                <button key={m.id} role="radio" aria-checked={status.mode.id === m.id} onClick={() => switchMode(m.id, m.label)}
                  className={`block w-full rounded-lg border px-3 py-2 text-left transition ${status.mode.id === m.id ? "border-accent bg-accent/10" : "border-line hover:border-muted/60"}`}>
                  <div className="flex items-center gap-2 text-[13px] font-semibold">{m.label}{m.rec && <Badge color="accent">recommended</Badge>}{status.mode.id === m.id && <Badge color="up">active</Badge>}</div>
                  <div className="mt-0.5 text-xs text-muted">{m.text}</div>
                </button>
              ))}
            </div>
            <div className="border-t border-line pt-3">
              <div className="mb-1.5 flex items-center justify-between text-xs text-muted"><span>Session</span><span className="num">{status.session_date ?? "none"}</span></div>
              {!active ? (
                <Select aria-label="Replay speed" value={speed} onChange={(e) => setSpeed(e.target.value)}>{SPEEDS.map((s) => <option key={s.v} value={s.v}>{s.label}</option>)}</Select>
              ) : (
                <div className="space-y-2"><Progress value={status.progress} /><Button variant="ghost" className="w-full" onClick={endSession}>End session</Button></div>
              )}
              {status.restored_from_db && !active && status.portfolio.open_positions > 0 && <p className="mt-2 text-xs text-warn">Restored {status.portfolio.open_positions} open position(s) from the last run. Press Start to resume managing them.</p>}
            </div>
          </div>
        </Card>
      </div>

      <div className="grid items-start gap-4 xl:grid-cols-3">
        <Card title="Active positions" className="xl:col-span-2" right={<Button variant="ghost" onClick={() => go("portfolio")}>Portfolio</Button>}>
          {positions.loading && !positions.data ? <Loading rows={3} label="Loading positions" /> : (positions.data ?? []).length === 0 ? <Empty>No open positions. The bot only buys when an opportunity clears all its checks.</Empty> : (
            <ul className="divide-y divide-line">
              {(positions.data ?? []).map((p) => (
                <li key={p.symbol} className="cursor-pointer py-3 first:pt-0 last:pb-0" onClick={() => explain({ kind: "position", position: p })}>
                  <PositionSummary p={p} />
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card title="Bot activity" pad={false}><ActivityFeed items={activity.data} /></Card>
      </div>

      <Card title="Portfolio performance"
        right={<Segmented label="Chart range" value={range as "1D"} onChange={setRange} options={["1D", "1W", "1M", "ALL"].map((r) => ({ value: r as "1D", label: r }))} />}>
        {series.error ? <ErrorBanner message={series.error} /> : pts.length > 1 ? (
          <>
            <div className="mb-2 flex items-baseline gap-3"><span className="num text-lg font-semibold">{money(series.data?.end_equity)}</span>
              <span className={`num text-sm ${(series.data?.change ?? 0) >= 0 ? "text-up" : "text-down"}`}>{signedMoney(series.data?.change)} ({pct(series.data?.change_pct)}) over {range}</span>
              <Badge color={status.feed.kind === "synthetic" ? "warn" : "info"}>{status.feed.kind} data</Badge></div>
            <EquityChart points={pts} baseline={series.data?.start_equity} height={300} />
          </>
        ) : <Empty>The equity curve builds up as the session runs.</Empty>}
      </Card>
    </div>
  );
}
