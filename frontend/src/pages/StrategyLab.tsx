import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { EquityChart } from "../components/charts";
import { Badge, Button, Card, Empty, ErrorBanner, Field, PnL, Progress, Select, TextInput, Toggle } from "../components/ui";
import { chicagoTime, money, num, pct, signedMoney } from "../format";
import { usePoll } from "../hooks";
import type { BacktestResult, ParamSchema, StrategyInfo, Trade } from "../types";

// ---------------------------------------------------------------- parameter editor
function bounds(s: ParamSchema): { min?: number; max?: number; nullable: boolean; integer: boolean; bool: boolean } {
  const branch = s.anyOf ? s.anyOf.find((b) => b.type !== "null") : s;
  const b = (branch ?? s) as ParamSchema;
  return {
    min: b.minimum ?? b.exclusiveMinimum,
    max: b.maximum,
    nullable: !!s.anyOf?.some((x) => x.type === "null"),
    integer: b.type === "integer",
    bool: b.type === "boolean",
  };
}

export function ParamForm({ info, values, onChange }: { info: StrategyInfo; values: Record<string, number | boolean | string | null>; onChange: (k: string, v: number | boolean | null) => void }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {Object.entries(info.params_schema).map(([key, schema]) => {
        const b = bounds(schema);
        const label = schema.title ?? key;
        const v = values[key];
        return (
          <Field key={key} label={label} hint={schema.description}>
            {b.bool ? (
              <Toggle checked={Boolean(v)} onChange={(x) => onChange(key, x)} label={label} />
            ) : (
              <TextInput type="number" aria-label={label} min={b.min} max={b.max} step={b.integer ? 1 : "any"} value={v === null || v === undefined ? "" : String(v)}
                placeholder={b.nullable ? "off" : undefined}
                onChange={(e) => onChange(key, e.target.value === "" ? (b.nullable ? null : 0) : Number(e.target.value))} />
            )}
          </Field>
        );
      })}
    </div>
  );
}

function StrategyCard({ info, enabled, params, onToggle, onParam, onSave, saving, dirty }: {
  info: StrategyInfo; enabled: boolean; params: Record<string, number | boolean | string | null>;
  onToggle: (v: boolean) => void; onParam: (k: string, v: number | boolean | null) => void; onSave: () => void; saving: boolean; dirty: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Card title={info.name} right={<><Badge color={enabled ? "up" : "muted"}>{enabled ? "enabled" : "disabled"}</Badge><Toggle checked={enabled} onChange={onToggle} label={`Enable ${info.name}`} /></>}>
      <p className="mb-3 text-[13px] text-muted">{info.summary}</p>
      <div className="grid gap-3 text-xs md:grid-cols-3">
        {([["Entry conditions", info.entry_rules], ["Exit conditions", info.exit_rules], ["Risk controls", info.risk_rules]] as const).map(([t, list]) => (
          <div key={t}><div className="mb-1 font-semibold uppercase tracking-wide text-muted">{t}</div><ul className="list-disc space-y-0.5 pl-4">{list.map((x) => <li key={x}>{x}</li>)}</ul></div>
        ))}
      </div>
      <div className="mt-3 flex items-center gap-2">
        <Button variant="ghost" onClick={() => setOpen(!open)} aria-expanded={open}>{open ? "Hide parameters" : "Edit parameters"}</Button>
        {dirty && <Button variant="primary" onClick={onSave} disabled={saving}>{saving ? "Saving..." : "Save changes"}</Button>}
      </div>
      {open && <div className="mt-3 border-t border-line pt-3"><ParamForm info={info} values={params} onChange={onParam} /></div>}
      <p className="mt-3 text-[11px] italic text-muted">{info.disclaimer}</p>
    </Card>
  );
}

// ---------------------------------------------------------------- results
function ResultView({ r }: { r: BacktestResult }) {
  const s = r.summary;
  const [open, setOpen] = useState<string | null>(null);
  const curve = useMemo(() => r.result.equity_curve.map(([t, v]) => ({ time: Math.floor(new Date(t).getTime() / 1000), value: v })), [r]);
  const metric = (label: string, value: string, cls = "") => (
    <div className="rounded-lg bg-panel2 px-3 py-2"><div className="text-[10px] uppercase tracking-wider text-muted">{label}</div><div className={`num text-[15px] font-semibold ${cls}`}>{value}</div></div>
  );
  return (
    <Card title={`${r.name} - ${r.id}`} right={<><a className="text-xs text-accent hover:underline" href={api.exportUrl(r.id, "trades")}>trades.csv</a><a className="text-xs text-accent hover:underline" href={api.exportUrl(r.id, "summary")}>summary.csv</a></>}>
      <div className="mb-3 flex flex-wrap items-center gap-2"><Badge color={r.result.feed.kind === "synthetic" ? "warn" : "info"}>{r.result.feed.kind.toUpperCase()} DATA</Badge><span className="text-xs text-muted">{r.result.feed.label}</span></div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6" data-testid="bt-summary">
        {metric("Total return", pct(s.total_return_pct), s.total_return_pct >= 0 ? "text-up" : "text-down")}
        {metric("Benchmark", pct(s.benchmark_return_pct))}
        {metric("Net P/L", signedMoney(s.net_pnl))}
        {metric("Trades", String(s.n_trades))}
        {metric("Win rate", s.win_rate === null ? "-" : `${(s.win_rate * 100).toFixed(0)}%`)}
        {metric("Profit factor", s.profit_factor === null ? "-" : num(s.profit_factor))}
        {metric("Expectancy", signedMoney(s.expectancy))}
        {metric("Avg win / loss", `${money(s.avg_win, 0)} / ${money(s.avg_loss, 0)}`)}
        {metric("Max drawdown", pct(-s.max_drawdown_pct, 2, false), "text-down")}
        {metric("Sharpe", s.sharpe === null ? "withheld" : num(s.sharpe))}
        {metric("Days", String(s.n_days))}
        {metric("Final equity", money(s.final_equity, 0))}
      </div>
      {s.ratios_note && <p className="mt-2 text-[11px] text-muted">{s.ratios_note}</p>}
      {s.in_sample && s.out_of_sample && (
        <div className="mt-3 grid gap-2 text-xs sm:grid-cols-2">
          {([["In-sample (earlier days)", s.in_sample], ["Out-of-sample (held-out later days)", s.out_of_sample]] as const).map(([t, b]) => (
            <div key={t} className="rounded-lg bg-panel2 p-3"><div className="font-semibold">{t}</div><div className="num mt-1 text-muted">{b.n_days} days · {b.n_trades} trades · return {pct(Number(b.total_return_pct))} · win rate {b.win_rate === null ? "-" : `${(Number(b.win_rate) * 100).toFixed(0)}%`}</div></div>
          ))}
        </div>
      )}
      <div className="mt-3 space-y-1" data-testid="bt-warnings">
        {r.result.warnings.map((w) => <div key={w} className="flex gap-2 rounded-md bg-warn/10 px-2.5 py-1.5 text-xs text-warn"><span aria-hidden>!</span>{w}</div>)}
      </div>
      {curve.length > 1 && <div className="mt-3"><EquityChart points={curve} baseline={s.initial_capital} height={200} /></div>}
      <div className="mt-3 overflow-x-auto">
        <table className="w-full min-w-[640px] text-xs">
          <thead className="text-left text-[10px] uppercase text-muted"><tr><th>Date</th><th>Symbol</th><th>Strategy</th><th className="text-right">Entry</th><th className="text-right">Exit</th><th className="text-right">Net P/L</th><th>Exit reason</th></tr></thead>
          <tbody>
            {r.result.trades.map((t: Trade) => (
              <Fragment key={t.id}>
                <tr className="cursor-pointer border-t border-line hover:bg-panel2" onClick={() => setOpen(open === t.id ? null : t.id)}>
                  <td className="py-1.5">{t.session_date}</td><td className="font-medium">{t.symbol}</td><td>{t.strategy.replace(/_/g, " ")}</td>
                  <td className="num text-right">{money(t.entry_price, 3)}</td><td className="num text-right">{money(t.exit_price, 3)}</td>
                  <td className="text-right"><PnL value={t.net_pnl} text={signedMoney(t.net_pnl)} /></td><td className="max-w-[240px] truncate text-muted">{t.exit_reason}</td>
                </tr>
                {open === t.id && <tr className="bg-panel2/60"><td colSpan={7} className="space-y-1 px-3 py-2"><div><b className="text-up">Bought:</b> {t.entry_reason}</div><div><b className="text-info">Sold:</b> {t.exit_reason}</div><div className="text-muted">{chicagoTime(t.entry_time)} → {chicagoTime(t.exit_time)} CT · held {num(t.hold_minutes, 0)} min</div></td></tr>}
              </Fragment>
            ))}
          </tbody>
        </table>
        {r.result.trades.length === 0 && <Empty>No trades were generated.</Empty>}
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------- page
export default function StrategyLab() {
  const { data: strategies, reload: reloadStrategies, error } = usePoll(api.strategies, null);
  const { data: datasets } = usePoll(api.datasets, null);
  const { data: past, reload: reloadPast } = usePoll(api.backtests, null);
  const [enabled, setEnabled] = useState<string[] | null>(null);
  const [edits, setEdits] = useState<Record<string, Record<string, number | boolean | string | null>>>({});
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // backtest form
  const [dataset, setDataset] = useState("synthetic_sample");
  const [sel, setSel] = useState<string[]>(["opening_momentum", "gap_continuation"]);
  const [compare, setCompare] = useState(true);
  const [oos, setOos] = useState(30);
  const [capital, setCapital] = useState("100000");
  const [slip, setSlip] = useState("5");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [job, setJob] = useState<{ id: string; progress: number; status: string; message: string } | null>(null);
  const [results, setResults] = useState<BacktestResult[]>([]);
  const timer = useRef<ReturnType<typeof setTimeout>>();

  useEffect(() => () => clearTimeout(timer.current), []);
  const en = enabled ?? strategies?.filter((s) => s.enabled).map((s) => s.id) ?? [];
  const ds = datasets?.find((d) => d.id === dataset);
  const dates = ds?.dates ?? [];

  const save = async (id?: string) => {
    setSaving(true);
    setErr(null);
    try {
      await api.saveSettings({ strategies: { enabled: en, params: id ? { [id]: edits[id] } : edits } });
      setEdits(id ? Object.fromEntries(Object.entries(edits).filter(([k]) => k !== id)) : {});
      setEnabled(null);
      reloadStrategies();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const poll = async (id: string) => {
    try {
      const j = await api.job(id);
      setJob({ id, progress: j.progress, status: j.status, message: j.message });
      if (j.status === "done") {
        const rs = await Promise.all(j.result_ids.map((rid) => api.backtest(rid)));
        setResults(rs);
        reloadPast();
      } else if (j.status === "error") {
        setErr(`Backtest failed: ${j.error}`);
      } else {
        timer.current = setTimeout(() => void poll(id), 700);
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const run = async () => {
    setErr(null);
    setResults([]);
    const body: Record<string, unknown> = {
      name: compare ? "Strategy comparison" : "Backtest", dataset: dataset === "demo" ? null : dataset, strategies: sel, compare,
      initial_capital: Number(capital), oos_fraction: oos / 100, costs: { slippage_bps: Number(slip) },
    };
    if (dates.length) body.dates = dates.filter((d) => (!from || d >= from) && (!to || d <= to));
    try {
      const { job_id } = await api.runBacktest(body);
      setJob({ id: job_id, progress: 0, status: "queued", message: "" });
      void poll(job_id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const loadPast = async (id: string) => {
    try {
      setResults([await api.backtest(id)]);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const busy = job !== null && (job.status === "queued" || job.status === "running");
  return (
    <div className="space-y-6">
      <ErrorBanner message={err ?? error} onClose={() => setErr(null)} />

      <section aria-label="Backtests" className="space-y-4">
        <Card title="Run a backtest">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Dataset" hint={ds?.description}>
              <Select aria-label="Dataset" value={dataset} onChange={(e) => { setDataset(e.target.value); setFrom(""); setTo(""); }}>
                {(datasets ?? []).map((d) => <option key={d.id} value={d.id}>{d.name}{d.synthetic ? " (SYNTHETIC)" : ""}</option>)}
                <option value="demo">Demo generator - last 5 trading days (SYNTHETIC)</option>
              </Select>
            </Field>
            <Field label="From"><Select aria-label="From date" value={from} onChange={(e) => setFrom(e.target.value)} disabled={!dates.length}><option value="">first day</option>{dates.map((d) => <option key={d}>{d}</option>)}</Select></Field>
            <Field label="To"><Select aria-label="To date" value={to} onChange={(e) => setTo(e.target.value)} disabled={!dates.length}><option value="">last day</option>{dates.map((d) => <option key={d}>{d}</option>)}</Select></Field>
            <Field label="Starting capital ($)"><TextInput type="number" aria-label="Starting capital" value={capital} onChange={(e) => setCapital(e.target.value)} /></Field>
            <Field label="Slippage (bps)" hint="Extra cost against you on every market fill."><TextInput type="number" aria-label="Slippage bps" value={slip} onChange={(e) => setSlip(e.target.value)} /></Field>
            <Field label={`Hold out last ${oos}% of days (out-of-sample)`} hint="Guards against overfitting. Needs ≥4 days."><input type="range" min={0} max={50} step={5} value={oos} aria-label="Out-of-sample percent" onChange={(e) => setOos(Number(e.target.value))} className="w-full accent-[var(--accent)]" /></Field>
            <div className="sm:col-span-2">
              <span className="mb-1 block text-xs font-medium text-muted">Strategies</span>
              <div className="flex flex-wrap gap-x-4 gap-y-1.5">
                {(strategies ?? []).map((s) => (
                  <label key={s.id} className="flex items-center gap-1.5 text-[13px]"><input type="checkbox" checked={sel.includes(s.id)} onChange={(e) => setSel(e.target.checked ? [...sel, s.id] : sel.filter((x) => x !== s.id))} />{s.name}</label>
                ))}
              </div>
              <label className="mt-2 flex items-center gap-1.5 text-xs text-muted"><input type="checkbox" checked={compare} onChange={(e) => setCompare(e.target.checked)} />compare: run each strategy separately, plus all together</label>
            </div>
          </div>
          <div className="mt-4 flex items-center gap-3">
            <Button variant="primary" onClick={run} disabled={busy || sel.length === 0}>{busy ? "Running..." : "Run backtest"}</Button>
            {busy && job && <div className="flex flex-1 items-center gap-3"><Progress value={job.progress} className="max-w-xs" /><span className="text-xs text-muted">{job.message}</span></div>}
          </div>
          <p className="mt-3 text-[11px] text-muted">Backtests replay the same bot used for live simulation, with modelled costs and next-bar fills. Results on synthetic data demonstrate the machinery only; no result guarantees future profit.</p>
        </Card>

        {results.length > 1 && (
          <Card title="Comparison" pad={false}>
            <table className="w-full text-[13px]" data-testid="bt-compare">
              <thead className="text-left text-[11px] uppercase text-muted"><tr><th className="px-4 py-2">Run</th><th className="text-right">Return</th><th className="text-right">vs benchmark</th><th className="text-right">Trades</th><th className="text-right">Win rate</th><th className="text-right">Profit factor</th><th className="pr-4 text-right">Max DD</th></tr></thead>
              <tbody>
                {results.map((r) => (
                  <tr key={r.id} className="border-t border-line">
                    <td className="px-4 py-2 font-medium">{r.name.replace(/^.*: /, "")}</td>
                    <td className="text-right"><PnL value={r.summary.total_return_pct} text={pct(r.summary.total_return_pct)} /></td>
                    <td className="text-right"><PnL value={r.summary.total_return_pct - r.summary.benchmark_return_pct} text={pct(r.summary.total_return_pct - r.summary.benchmark_return_pct)} /></td>
                    <td className="num text-right">{r.summary.n_trades}</td>
                    <td className="num text-right">{r.summary.win_rate === null ? "-" : `${(r.summary.win_rate * 100).toFixed(0)}%`}</td>
                    <td className="num text-right">{r.summary.profit_factor === null ? "-" : num(r.summary.profit_factor)}</td>
                    <td className="num pr-4 text-right text-down">{pct(-r.summary.max_drawdown_pct, 2, false)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        )}
        {results.map((r) => <ResultView key={r.id} r={r} />)}

        {past && past.length > 0 && (
          <Card title="Saved backtests" pad={false}>
            <ul className="divide-y divide-line text-[13px]">
              {past.slice(0, 12).map((p) => (
                <li key={p.id} className="flex items-center gap-3 px-4 py-2"><span className="font-medium">{p.name}</span><span className="text-xs text-muted">{p.config.strategies.join(", ")}</span>
                  <PnL className="ml-auto" value={p.summary.total_return_pct} text={pct(p.summary.total_return_pct)} /><Button variant="ghost" onClick={() => loadPast(p.id)}>Open</Button></li>
              ))}
            </ul>
          </Card>
        )}
      </section>

      <section aria-label="Strategies" className="space-y-4">
        <div className="flex items-center justify-between"><h2 className="text-sm font-semibold uppercase tracking-wide text-muted">Strategies & parameters</h2>
          {enabled && <Button variant="primary" onClick={() => save()} disabled={saving}>Save enabled set</Button>}</div>
        {(strategies ?? []).map((s) => (
          <StrategyCard key={s.id} info={s} enabled={en.includes(s.id)} params={{ ...s.params, ...(edits[s.id] ?? {}) }}
            onToggle={(v) => setEnabled(v ? [...new Set([...en, s.id])] : en.filter((x) => x !== s.id))}
            onParam={(k, v) => setEdits({ ...edits, [s.id]: { ...(edits[s.id] ?? {}), [k]: v } })}
            onSave={() => save(s.id)} saving={saving} dirty={!!edits[s.id]} />
        ))}
      </section>
    </div>
  );
}
