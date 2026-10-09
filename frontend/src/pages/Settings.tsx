import { useEffect, useState } from "react";
import { useApp } from "../App";
import { api } from "../api";
import { Badge, Button, Card, ErrorBanner, Field, Select, TextInput, Toggle } from "../components/ui";
import { usePoll } from "../hooks";
import type { Settings } from "../types";

type Draft = Settings;

function NumField({ label, value, onChange, step = "any", hint, nullable = false }: {
  label: string; value: number | null; onChange: (v: number | null) => void; step?: string; hint?: string; nullable?: boolean;
}) {
  return (
    <Field label={label} hint={hint}>
      <TextInput type="number" step={step} aria-label={label} placeholder={nullable ? "off" : undefined} value={value === null ? "" : String(value)}
        onChange={(e) => onChange(e.target.value === "" ? (nullable ? null : 0) : Number(e.target.value))} />
    </Field>
  );
}

export default function SettingsPage() {
  const { status, reloadStatus } = useApp();
  const { data, reload, error } = usePoll(api.settings, null);
  const [d, setD] = useState<Draft | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState(false);
  const [saving, setSaving] = useState(false);
  const locked = status?.session_status === "running";

  useEffect(() => {
    if (data && !d) setD(structuredClone(data.settings));
  }, [data, d]);

  if (!d || !data) return <ErrorBanner message={error ?? "Loading settings..."} />;
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => { setD({ ...d, [k]: v }); setOk(false); };
  const sub = <K extends "risk" | "costs" | "schedule">(k: K, field: keyof Draft[K], v: unknown) => set(k, { ...d[k], [field]: v } as Draft[K]);

  const save = async () => {
    setSaving(true); setErr(null); setOk(false);
    try {
      await api.saveSettings({
        risk: d.risk, costs: d.costs, schedule: d.schedule, data_mode: d.data_mode, demo_seed: d.demo_seed, demo_speed: d.demo_speed,
        historical_dataset: d.historical_dataset, auto_schedule: d.auto_schedule, allow_delayed_feed_trading: d.allow_delayed_feed_trading,
        pause_on_mirror_failure: d.pause_on_mirror_failure,
      });
      setOk(true);
      reload();
      reloadStatus();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const env = data.env;
  return (
    <div className="space-y-4">
      <ErrorBanner message={err} onClose={() => setErr(null)} />
      {locked && <div className="rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn">A session is streaming: stop it to change capital, data mode, schedule or costs.</div>}

      <Card title="Data & execution" right={<Badge color="up">PAPER ONLY</Badge>}>
        <div className="grid gap-4 md:grid-cols-3">
          <Field label="Data mode" hint={{ demo: "Synthetic data, works instantly with no keys.", historical: "Deterministic replay of a CSV dataset (the bundled one is synthetic).", connected: "Live/delayed market data from your own provider key. Labelled honestly in the header." }[d.data_mode]}>
            <Select aria-label="Data mode" value={d.data_mode} onChange={(e) => set("data_mode", e.target.value as Draft["data_mode"])}>
              <option value="demo">Demo (synthetic)</option><option value="historical">Historical replay</option><option value="connected">Connected (your API key)</option>
            </Select>
          </Field>
          {d.data_mode === "historical" && <Field label="Historical dataset folder" hint="Folder under MWBOT_HISTORICAL_DIR with manifest.json + bars.csv[.gz]."><TextInput aria-label="Historical dataset" value={d.historical_dataset} onChange={(e) => set("historical_dataset", e.target.value)} /></Field>}
          {d.data_mode === "demo" && <NumField label="Demo random seed" value={d.demo_seed} step="1" onChange={(v) => set("demo_seed", v ?? 42)} hint="Same seed + date = identical synthetic day." />}
          <NumField label="Replay speed (sim-minutes per second)" value={d.demo_speed} onChange={(v) => set("demo_speed", v ?? 10)} />
        </div>
        <div className="mt-4 grid gap-3 text-[13px] md:grid-cols-2">
          <div className="rounded-lg bg-panel2 p-3">
            <div className="mb-1 font-semibold">Market-data API</div>
            <div className="flex items-center gap-2">{env.massive_key_configured ? <Badge color="up">Massive key configured</Badge> : <Badge color="muted">no key configured</Badge>}<Badge color="info">plan: {env.massive_feed}</Badge></div>
            <p className="mt-2 text-xs text-muted">API keys are read from the <code>MWBOT_MASSIVE_API_KEY</code> environment variable / <code>.env</code> file on the machine running the backend. The dashboard never asks for or stores keys.</p>
          </div>
          <div className="rounded-lg bg-panel2 p-3">
            <div className="mb-1 font-semibold">Execution</div>
            <div className="flex items-center gap-2"><Badge color="accent">{env.execution_mode}</Badge><Badge color="up">real money: impossible</Badge></div>
            <p className="mt-2 text-xs text-muted">Decisions are always executed by the local virtual engine. <code>mock_browser</code> additionally mirrors orders to the bundled test website. A MarketWatch adapter is a documented skeleton (see docs).</p>
          </div>
        </div>
        <div className="mt-4 space-y-3">
          <label className="flex items-center gap-3 text-[13px]"><Toggle checked={d.allow_delayed_feed_trading} onChange={(v) => set("allow_delayed_feed_trading", v)} label="Allow trading on a delayed feed" />Allow automated entries on a <b>delayed</b> feed (off = observe only)</label>
          <label className="flex items-center gap-3 text-[13px]"><Toggle checked={d.auto_schedule} onChange={(v) => set("auto_schedule", v)} label="Run on schedule" />Run the live-clock scheduler automatically on real trading days while the app is open</label>
          <label className="flex items-center gap-3 text-[13px]"><Toggle checked={d.pause_on_mirror_failure} onChange={(v) => set("pause_on_mirror_failure", v)} label="Pause on mirror failure" />Pause the bot if a mirrored external order fails</label>
        </div>
      </Card>

      <Card title="Virtual capital & risk">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <NumField label="Starting capital ($)" value={d.risk.starting_capital} onChange={(v) => sub("risk", "starting_capital", v)} hint="Changes only apply before the first trade (or after Reset)." />
          <NumField label="Max position size (% equity)" value={d.risk.max_position_pct} onChange={(v) => sub("risk", "max_position_pct", v)} />
          <NumField label="Risk per trade (% equity)" value={d.risk.risk_per_trade_pct} onChange={(v) => sub("risk", "risk_per_trade_pct", v)} hint="Loss if the stop is hit." />
          <NumField label="Max simultaneous holdings" value={d.risk.max_positions} step="1" onChange={(v) => sub("risk", "max_positions", v)} />
          <NumField label="Max daily loss (%)" value={d.risk.max_daily_loss_pct} onChange={(v) => sub("risk", "max_daily_loss_pct", v)} hint="Flattens and blocks entries." />
          <NumField label="Default stop loss (%)" value={d.risk.stop_loss_pct} onChange={(v) => sub("risk", "stop_loss_pct", v)} />
          <NumField label="Default profit target (%)" value={d.risk.take_profit_pct} nullable onChange={(v) => sub("risk", "take_profit_pct", v)} />
          <NumField label="Max trades per day" value={d.risk.max_trades_per_day} step="1" onChange={(v) => sub("risk", "max_trades_per_day", v)} />
        </div>
      </Card>

      <Card title="Execution realism (costs)">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <NumField label="Commission per share ($)" value={d.costs.commission_per_share} onChange={(v) => sub("costs", "commission_per_share", v)} />
          <NumField label="Slippage (bps)" value={d.costs.slippage_bps} onChange={(v) => sub("costs", "slippage_bps", v)} />
          <NumField label="Assumed spread when unknown (bps)" value={d.costs.assumed_spread_bps} onChange={(v) => sub("costs", "assumed_spread_bps", v)} />
          <NumField label="Max share of bar volume" value={d.costs.max_participation} onChange={(v) => sub("costs", "max_participation", v)} hint="0.10 = fills at most 10% of a minute's volume (partial fills beyond)." />
          <NumField label="Order latency (seconds)" value={d.costs.latency_seconds} step="1" onChange={(v) => sub("costs", "latency_seconds", v)} />
          <NumField label="Impact (bps per 1% participation)" value={d.costs.impact_bps_per_pct_participation} onChange={(v) => sub("costs", "impact_bps_per_pct_participation", v)} />
        </div>
      </Card>

      <Card title="Daily schedule (America/Chicago)">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          {([["scanner_start", "Scanner starts"], ["last_entry", "No new positions after"], ["liquidation_start", "Begin liquidation"]] as const).map(([k, l]) => (
            <Field key={k} label={l}><TextInput aria-label={l} type="time" value={d.schedule[k]} onChange={(e) => sub("schedule", k, e.target.value)} /></Field>
          ))}
          <NumField label="Observe after open (min)" value={d.schedule.observe_minutes} step="1" onChange={(v) => sub("schedule", "observe_minutes", v)} />
        </div>
        <p className="mt-2 text-[11px] text-muted">Market open/close come from the real NYSE calendar (holidays, DST, early closes). Times after an early close are clamped automatically.</p>
      </Card>

      <div className="sticky bottom-14 z-10 flex items-center gap-3 rounded-xl border border-line bg-panel/95 p-3 backdrop-blur md:bottom-3">
        <Button variant="primary" onClick={save} disabled={saving}>{saving ? "Saving..." : "Save settings"}</Button>
        {ok && <span role="status" className="text-[13px] text-up">Saved.</span>}
        <div className="ml-auto flex gap-2">
          <Button onClick={async () => { await api.bot(status?.bot_state === "paused" ? "resume" : "pause"); reloadStatus(); }} disabled={status?.bot_state === "stopped" || status?.bot_state === "emergency_stop"}>{status?.bot_state === "paused" ? "Resume bot" : "Pause bot"}</Button>
          <Button variant="danger" disabled={locked} onClick={async () => { if (window.confirm("Reset the virtual account? This deletes all trades, orders and reports.")) { try { await api.resetAccount(); setD(null); reload(); reloadStatus(); } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } } }}>Reset account</Button>
        </div>
      </div>
    </div>
  );
}
