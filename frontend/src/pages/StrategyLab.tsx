import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { useApp } from "../App";
import { api } from "../api";
import { CompareChart, EquityChart, type NamedLine } from "../components/charts";
import { Loading, Segmented, usePref, useToast } from "../components/kit";
import { DataKindBadge, EstimateBadge } from "../components/trading";
import { Badge, Button, Card, Empty, ErrorBanner, Field, PnL, Progress, Select, TextInput, Toggle } from "../components/ui";
import { chicagoTime, money, num, pct, signedMoney } from "../format";
import { usePoll } from "../hooks";
import type { BacktestResult, ComparePayload, Job, ModelCheckPayload, ParamSchema, StrategyInfo, Trade } from "../types";

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
      {s.eligibility && <p className="mt-2 text-[11px] text-muted">{s.eligibility.applied ? `MarketWatch stock list applied point-in-time (${s.eligibility.n_tickers ?? "?"} tickers).` : s.eligibility.note}</p>}
      {s.exits && s.exits.counts && Object.keys(s.exits.counts).length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2 text-xs" data-testid="bt-exits">
          {Object.entries(s.exits.counts).map(([k, n]) => <Badge key={k} color={k === "target" ? "up" : k === "risk" ? "down" : "muted"}>{s.exits?.labels[k] ?? k}: {n}</Badge>)}
          {s.exits.target_hit_rate !== null && <span className="text-muted">target hit {(s.exits.target_hit_rate * 100).toFixed(0)}% · stops {((s.exits.stop_rate ?? 0) * 100).toFixed(0)}% · end of day {((s.exits.end_of_day_rate ?? 0) * 100).toFixed(0)}%</span>}
        </div>
      )}
      {s.forecast_accuracy && s.forecast_accuracy.n > 0 && <p className="mt-2 text-[11px] text-muted" data-testid="bt-forecast-accuracy">Forecast vs result over {s.forecast_accuracy.n} trades: predicted {pct(s.forecast_accuracy.mean_predicted_net_pct ?? null)} vs realised {pct(s.forecast_accuracy.mean_realised_net_pct ?? null)} per trade{s.forecast_accuracy.correlation != null ? ` · correlation ${num(s.forecast_accuracy.correlation)}` : ""}. {s.forecast_accuracy.note}</p>}
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

// ---------------------------------------------------------------- shared job polling
function useJob() {
  const [job, setJob] = useState<{ id: string; progress: number; status: string; message: string } | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout>>();
  useEffect(() => () => clearTimeout(timer.current), []);
  const watch = (id: string, onDone: (j: Job) => void, onError: (m: string) => void) => {
    const tick = async () => {
      try {
        const j = await api.job(id);
        setJob({ id, progress: j.progress, status: j.status, message: j.message });
        if (j.status === "done") onDone(j);
        else if (j.status === "error") onError(j.error);
        else timer.current = setTimeout(() => void tick(), 800);
      } catch (e) {
        onError(e instanceof Error ? e.message : String(e));
      }
    };
    setJob({ id, progress: 0, status: "queued", message: "" });
    void tick();
  };
  return { job, watch, busy: job !== null && (job.status === "queued" || job.status === "running") };
}

const COLORS: Record<string, string> = { max_expected_return: "#2dd4bf", aggressive_upside: "#f59e0b", baseline_momentum: "#8b98a8" };
const pctOrDash = (v: number | null | undefined, d = 0) => (v === null || v === undefined ? "-" : `${(v * 100).toFixed(d)}%`);

// ---------------------------------------------------------------- compare modes
function CompareView({ c }: { c: ComparePayload }) {
  const lines: NamedLine[] = useMemo(() => Object.entries(c.equity_curves).map(([k, pts]) => ({
    name: c.rows.find((r) => r.mode === k)?.label ?? k, color: COLORS[k] ?? "#60a5fa",
    points: pts.map(([t, v]) => ({ time: Math.floor(new Date(t).getTime() / 1000), value: v })),
  })), [c]);
  return (
    <Card title="Result" right={<div className="flex gap-2"><DataKindBadge kind={c.same_data.feed.kind} /><Badge color="muted">{c.same_data.n_days} days</Badge></div>}>
      <div className="mb-3 rounded-lg bg-panel2 px-3 py-2 text-[13px]" data-testid="verdict">{c.verdict}</div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[720px] text-[13px]" data-testid="compare-table">
          <thead className="text-left text-[10px] uppercase tracking-wider text-muted"><tr><th className="py-2">Strategy</th><th className="text-right">Return</th><th className="text-right">Worst drawdown</th><th className="text-right">Win rate</th><th className="text-right">Avg trade</th><th className="text-right">Trades</th><th className="text-right">Target hit</th><th className="text-right">Stopped</th><th className="text-right">End of day</th></tr></thead>
          <tbody>
            {c.rows.map((r) => (
              <tr key={r.mode} className="border-t border-line">
                <td className="py-2 font-medium"><span className="mr-2 inline-block h-2 w-2 rounded-full" style={{ background: COLORS[r.mode] ?? "#60a5fa" }} />{r.label}</td>
                <td className="text-right"><PnL value={r.total_return_pct} text={pct(r.total_return_pct)} /></td>
                <td className="num text-right text-down">{r.max_drawdown_pct == null ? "-" : pct(-r.max_drawdown_pct, 2, false)}</td>
                <td className="num text-right">{pctOrDash(r.win_rate)}</td>
                <td className="num text-right">{r.avg_return_pct == null ? "-" : pct(r.avg_return_pct)}</td>
                <td className="num text-right">{r.n_trades ?? "-"}</td>
                <td className="num text-right">{pctOrDash(r.target_hit_rate)}</td>
                <td className="num text-right">{pctOrDash(r.stop_rate)}</td>
                <td className="num text-right">{pctOrDash(r.end_of_day_rate)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {c.rows.some((r) => r.out_of_sample) && (
        <p className="mt-2 text-xs text-muted">Out-of-sample (held-out later days): {c.rows.filter((r) => r.out_of_sample).map((r) => `${r.label} ${pct(r.out_of_sample?.total_return_pct ?? null)} (${r.out_of_sample?.n_trades} trades)`).join(" · ")}</p>
      )}
      {lines.some((l) => l.points.length > 1) && <div className="mt-3"><CompareChart lines={lines} /></div>}
      <div className="mt-3 space-y-1" data-testid="compare-warnings">
        {c.warnings.map((w) => <div key={w} className="flex gap-2 rounded-md bg-warn/10 px-2.5 py-1.5 text-xs text-warn"><span aria-hidden>!</span>{w}</div>)}
      </div>
      <p className="mt-3 text-[11px] text-muted">Same data, costs, stock list and forecaster for every row. Weak or negative results are shown as they are - nothing here is tuned to look good and none of it predicts future results.</p>
    </Card>
  );
}

function CompareTab({ latest, datasets }: { latest: ComparePayload | null | undefined; datasets: { id: string; name: string; synthetic: boolean; dates: string[] | null }[] }) {
  const [dataset, setDataset] = usePref<string>("lab-dataset", "synthetic_sample");
  const [forecaster, setForecaster] = useState("heuristic");
  const [elig, setElig] = useState(true);
  const [oos, setOos] = useState(30);
  const [res, setRes] = useState<ComparePayload | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const { job, watch, busy } = useJob();
  const shown = res ?? latest ?? null;
  const run = async () => {
    setErr(null);
    try {
      const { job_id } = await api.compareModes({ dataset: dataset === "demo" ? null : dataset, forecaster, eligibility: elig ? "point_in_time" : "none", oos_fraction: oos / 100 });
      watch(job_id, (j) => setRes(j.payload as ComparePayload), setErr);
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  return (
    <div className="space-y-4">
      <Card title="Maximum Expected Return vs Aggressive Upside">
        <p className="mb-3 text-[13px] text-muted">Runs both modes (and a simple top-gainer baseline) over the same days with the same costs, so the difference you see comes from the strategy, not the data.</p>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="Dataset"><Select aria-label="Comparison dataset" value={dataset} onChange={(e) => setDataset(e.target.value)}>
            {datasets.map((d) => <option key={d.id} value={d.id}>{d.name}{d.synthetic ? " (SYNTHETIC)" : ""}</option>)}<option value="demo">Demo generator - last 5 days (SYNTHETIC)</option></Select></Field>
          <Field label="Forecast model" hint="Statistical re-fits every day on EARLIER days only."><Select aria-label="Forecast model" value={forecaster} onChange={(e) => setForecaster(e.target.value)}>
            <option value="heuristic">Rule-based (recommended)</option><option value="statistical">Statistical (walk-forward)</option></Select></Field>
          <Field label={`Hold out last ${oos}% of days`} hint="Out-of-sample check. Needs 4+ days."><input type="range" min={0} max={50} step={5} aria-label="Out-of-sample percent" value={oos} onChange={(e) => setOos(Number(e.target.value))} className="w-full accent-[var(--accent)]" /></Field>
          <label className="flex items-center gap-2 pt-5 text-[13px]"><Toggle checked={elig} onChange={setElig} label="Apply my MarketWatch stock list" />Apply my stock list (as of each date)</label>
        </div>
        {!elig && <p className="mt-2 text-xs text-warn">Without the stock list the test may trade stocks your game does not offer. The result will be labelled "eligibility not applied".</p>}
        <div className="mt-4 flex items-center gap-3">
          <Button variant="primary" onClick={run} disabled={busy}>{busy ? "Running..." : "Compare the two modes"}</Button>
          {busy && job && <div className="flex flex-1 items-center gap-3"><Progress value={job.progress} className="max-w-xs" /><span className="text-xs text-muted">{job.message}</span></div>}
        </div>
        <ErrorBanner message={err} onClose={() => setErr(null)} />
      </Card>
      {shown ? <CompareView c={shown} /> : <Card><Empty>No comparison yet. Run one above - it takes about a minute on the sample dataset.</Empty></Card>}
    </div>
  );
}

// ---------------------------------------------------------------- model check
function ModelCheckTab({ latest, datasets }: { latest: ModelCheckPayload | null | undefined; datasets: { id: string; name: string; synthetic: boolean }[] }) {
  const [dataset, setDataset] = usePref<string>("lab-dataset", "synthetic_sample");
  const [res, setRes] = useState<ModelCheckPayload | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const { job, watch, busy } = useJob();
  const shown = res ?? latest ?? null;
  const run = async () => {
    setErr(null);
    try {
      const { job_id } = await api.modelCheck({ dataset: dataset === "demo" ? "" : dataset });
      watch(job_id, (j) => setRes(j.payload as ModelCheckPayload), setErr);
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  return (
    <div className="space-y-4">
      <Card title="Can the forecasts be trusted?">
        <p className="mb-3 text-[13px] text-muted">Each model is trained only on earlier days and scored on the next day it has never seen (walk-forward). A model is only called <b>validated</b> if its probabilities are accurate, it separates winners from losers, and it was tested over at least 10 separate days. Otherwise forecasts stay labelled as estimates.</p>
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Dataset"><Select aria-label="Model check dataset" value={dataset === "demo" ? datasets[0]?.id : dataset} onChange={(e) => setDataset(e.target.value)}>{datasets.map((d) => <option key={d.id} value={d.id}>{d.name}{d.synthetic ? " (SYNTHETIC)" : ""}</option>)}</Select></Field>
          <Button variant="primary" onClick={run} disabled={busy || datasets.length === 0}>{busy ? "Checking..." : "Run model check"}</Button>
          {busy && job && <Progress value={job.progress} className="max-w-xs" />}
        </div>
        <ErrorBanner message={err} onClose={() => setErr(null)} />
      </Card>
      {shown && (shown.ok === false ? <Card><Empty>{shown.reason}</Empty></Card> : (
        <Card title="Results" right={<DataKindBadge kind={shown.data.feed.kind} />}>
          <p className="mb-3 text-xs text-muted">{shown.data.note} {shown.n_test_samples} out-of-sample samples over {shown.test_days.length} day(s).</p>
          <div className="overflow-x-auto"><table className="w-full min-w-[640px] text-[13px]" data-testid="model-table">
            <thead className="text-left text-[10px] uppercase tracking-wider text-muted"><tr><th className="py-2">Model</th><th className="text-right">Ranking skill*</th><th className="text-right">Top picks vs average</th><th className="text-right">Typical error</th><th>Probabilities</th></tr></thead>
            <tbody>{Object.entries(shown.models).map(([k, m]) => (
              <tr key={k} className="border-t border-line align-top"><td className="py-2 font-medium">{k}</td><td className="num text-right">{m.rank_ic === null ? "-" : num(m.rank_ic, 2)}</td>
                <td className="text-right"><PnL value={m.top_pick_edge_pct} text={m.top_pick_edge_pct === null ? "-" : pct(m.top_pick_edge_pct)} /></td><td className="num text-right">{pct(m.mae_pct, 2, false)}</td>
                <td className="max-w-[300px]"><EstimateBadge calibrated={m.calibrated} /><div className="mt-1 text-[11px] text-muted">{m.note || m.verdict}</div></td></tr>
            ))}</tbody></table></div>
          <p className="mt-2 text-[11px] text-muted">*Rank correlation between predicted and realised returns across stocks at the same moment (0 = no skill). Baselines for comparison: {Object.entries(shown.baselines).map(([k, b]) => `${k.replace(/_/g, " ")} ${b.mean_net_pct === null ? "-" : pct(b.mean_net_pct)}`).join(" · ")}.</p>
          <div className="mt-3 space-y-1">{shown.warnings.map((w) => <div key={w} className="rounded-md bg-warn/10 px-2.5 py-1.5 text-xs text-warn">{w}</div>)}</div>
          {shown.saved_model && <p className="mt-2 text-xs text-muted">Trained model saved ({shown.saved_model.calibrated ? "validated" : "not validated"}). Choose "Statistical" under Settings → Trading Strategy → Forecast model to use it.</p>}
        </Card>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- backtests (any strategy set)
function BacktestTab({ datasets, strategies, reloadStrategies }: { datasets: { id: string; name: string; synthetic: boolean; description: string; dates: string[] | null }[]; strategies: StrategyInfo[]; reloadStrategies: () => void }) {
  const { data: past, reload: reloadPast } = usePoll(api.backtests, null);
  const [dataset, setDataset] = usePref<string>("lab-dataset", "synthetic_sample");
  const [sel, setSel] = useState<string[]>(["expected_opportunity"]);
  const [mode, setMode] = useState("max_expected_return");
  const [elig, setElig] = useState(true);
  const [compare, setCompare] = useState(false);
  const [oos, setOos] = useState(30);
  const [results, setResults] = useState<BacktestResult[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const { job, watch, busy } = useJob();
  const ds = datasets.find((d) => d.id === dataset);
  const dates = ds?.dates ?? [];
  const run = async () => {
    setErr(null); setResults([]);
    const body: Record<string, unknown> = { name: compare ? "Strategy comparison" : "Backtest", dataset: dataset === "demo" ? null : dataset, strategies: sel, compare, mode, eligibility: elig ? "point_in_time" : "none", oos_fraction: oos / 100 };
    if (dates.length) body.dates = dates;
    try {
      const { job_id } = await api.runBacktest(body);
      watch(job_id, async (j) => { setResults(await Promise.all(j.result_ids.map((id) => api.backtest(id)))); reloadPast(); }, (m) => setErr(`Backtest failed: ${m}`));
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  return (
    <div className="space-y-4">
      <Card title="Run a backtest">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="Dataset" hint={ds?.description}><Select aria-label="Dataset" value={dataset} onChange={(e) => setDataset(e.target.value)}>
            {datasets.map((d) => <option key={d.id} value={d.id}>{d.name}{d.synthetic ? " (SYNTHETIC)" : ""}</option>)}<option value="demo">Demo generator - last 5 days (SYNTHETIC)</option></Select></Field>
          <Field label="Trading mode"><Select aria-label="Backtest mode" value={mode} onChange={(e) => setMode(e.target.value)}><option value="max_expected_return">Maximum Expected Return</option><option value="aggressive_upside">Aggressive Upside</option></Select></Field>
          <Field label={`Hold out last ${oos}% of days`}><input type="range" min={0} max={50} step={5} value={oos} aria-label="Hold-out percent" onChange={(e) => setOos(Number(e.target.value))} className="w-full accent-[var(--accent)]" /></Field>
          <label className="flex items-center gap-2 pt-5 text-[13px]"><Toggle checked={elig} onChange={setElig} label="Apply my MarketWatch stock list" />Apply my stock list</label>
          <div className="sm:col-span-2 lg:col-span-4">
            <span className="mb-1 block text-xs font-medium text-muted">Strategies</span>
            <div className="flex flex-wrap gap-x-4 gap-y-1.5">{strategies.map((s) => <label key={s.id} className="flex items-center gap-1.5 text-[13px]"><input type="checkbox" checked={sel.includes(s.id)} onChange={(e) => setSel(e.target.checked ? [...sel, s.id] : sel.filter((x) => x !== s.id))} />{s.name}</label>)}</div>
            <label className="mt-2 flex items-center gap-1.5 text-xs text-muted"><input type="checkbox" checked={compare} onChange={(e) => setCompare(e.target.checked)} />compare: run each separately, plus all together</label>
          </div>
        </div>
        <div className="mt-4 flex items-center gap-3">
          <Button variant="primary" onClick={run} disabled={busy || sel.length === 0}>{busy ? "Running..." : "Run backtest"}</Button>
          {busy && job && <div className="flex flex-1 items-center gap-3"><Progress value={job.progress} className="max-w-xs" /><span className="text-xs text-muted">{job.message}</span></div>}
        </div>
        <ErrorBanner message={err} onClose={() => setErr(null)} />
        <p className="mt-3 text-[11px] text-muted">Backtests replay the same bot used for live simulation, with modelled costs and next-bar fills. Results on synthetic data demonstrate the machinery only; no result guarantees future profit.</p>
      </Card>
      {results.length > 1 && (
        <Card title="Comparison" pad={false}>
          <table className="w-full text-[13px]" data-testid="bt-compare"><thead className="text-left text-[11px] uppercase text-muted"><tr><th className="px-4 py-2">Run</th><th className="text-right">Return</th><th className="text-right">Trades</th><th className="text-right">Win rate</th><th className="pr-4 text-right">Max DD</th></tr></thead>
            <tbody>{results.map((r) => <tr key={r.id} className="border-t border-line"><td className="px-4 py-2 font-medium">{r.name.replace(/^.*: /, "")}</td><td className="text-right"><PnL value={r.summary.total_return_pct} text={pct(r.summary.total_return_pct)} /></td><td className="num text-right">{r.summary.n_trades}</td><td className="num text-right">{pctOrDash(r.summary.win_rate)}</td><td className="num pr-4 text-right text-down">{pct(-r.summary.max_drawdown_pct, 2, false)}</td></tr>)}</tbody></table>
        </Card>
      )}
      {results.map((r) => <ResultView key={r.id} r={r} />)}
      {past && past.length > 0 && (
        <Card title="Saved backtests" pad={false}><ul className="divide-y divide-line text-[13px]">
          {past.slice(0, 10).map((p) => <li key={p.id} className="flex items-center gap-3 px-4 py-2"><span className="font-medium">{p.name}</span><span className="text-xs text-muted">{p.config.strategies.join(", ")}</span>
            <PnL className="ml-auto" value={p.summary.total_return_pct} text={pct(p.summary.total_return_pct)} /><Button variant="ghost" onClick={async () => setResults([await api.backtest(p.id)])}>Open</Button></li>)}
        </ul></Card>
      )}
      <ClassicStrategies strategies={strategies} reload={reloadStrategies} />
    </div>
  );
}

function ClassicStrategies({ strategies, reload }: { strategies: StrategyInfo[]; reload: () => void }) {
  const toast = useToast();
  const [edits, setEdits] = useState<Record<string, Record<string, number | boolean | string | null>>>({});
  const [enabled, setEnabled] = useState<string[] | null>(null);
  const [saving, setSaving] = useState(false);
  const en = enabled ?? strategies.filter((s) => s.enabled).map((s) => s.id);
  const save = async (id?: string) => {
    setSaving(true);
    try {
      await api.saveSettings({ strategies: { enabled: en, params: id ? { [id]: edits[id] } : edits } });
      setEdits(id ? Object.fromEntries(Object.entries(edits).filter(([k]) => k !== id)) : {}); setEnabled(null); reload(); toast("Strategy settings saved");
    } catch (e) { toast(e instanceof Error ? e.message : String(e), "error"); } finally { setSaving(false); }
  };
  return (
    <details className="rounded-xl border border-line bg-panel">
      <summary className="cursor-pointer px-4 py-3 text-[13px] font-semibold">Advanced: strategy descriptions and parameters</summary>
      <div className="space-y-4 p-4">
        <p className="text-xs text-muted">The recommended <b>Expected Opportunity</b> strategy needs no tuning. The classic momentum strategies are kept for comparison; enabling them lets the live bot use them too (still only on stocks confirmed available in your game).</p>
        {enabled && <Button variant="primary" onClick={() => save()} disabled={saving}>Save enabled set</Button>}
        {strategies.map((s) => (
          <StrategyCard key={s.id} info={s} enabled={en.includes(s.id)} params={{ ...s.params, ...(edits[s.id] ?? {}) }}
            onToggle={(v) => setEnabled(v ? [...new Set([...en, s.id])] : en.filter((x) => x !== s.id))}
            onParam={(k, v) => setEdits({ ...edits, [s.id]: { ...(edits[s.id] ?? {}), [k]: v } })} onSave={() => save(s.id)} saving={saving} dirty={!!edits[s.id]} />
        ))}
      </div>
    </details>
  );
}

// ---------------------------------------------------------------- page
type Tab = "compare" | "model" | "backtest";
export default function StrategyLab() {
  const { version } = useApp();
  const { data: strategies, reload: reloadStrategies, error } = usePoll(api.strategies, null, [version]);
  const { data: datasets } = usePoll(api.datasets, null);
  const { data: latest, loading } = usePoll(api.labLatest, null);
  const [tab, setTab] = usePref<Tab>("lab-tab", "compare");
  if (error) return <ErrorBanner message={error} />;
  if (!strategies || !datasets || loading) return <Loading rows={6} label="Loading Strategy Lab" />;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Segmented label="Lab section" value={tab} onChange={setTab} options={[{ value: "compare", label: "Compare modes" }, { value: "model", label: "Model check" }, { value: "backtest", label: "Backtests" }]} />
        <span className="text-xs text-muted">Every result is labelled with the kind of data it used. Poor results are shown as they are.</span>
      </div>
      {tab === "compare" && <CompareTab latest={latest?.comparison} datasets={datasets} />}
      {tab === "model" && <ModelCheckTab latest={latest?.model_check} datasets={datasets} />}
      {tab === "backtest" && <BacktestTab datasets={datasets} strategies={strategies} reloadStrategies={reloadStrategies} />}
    </div>
  );
}
