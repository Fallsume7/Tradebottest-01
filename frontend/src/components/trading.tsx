import { useState } from "react";
import { api } from "../api";
import { money, num, pct, titleCase } from "../format";
import type { EligibilityStatus, OppRow, Position, Status } from "../types";
import { useConfirm, useToast } from "./kit";
import { Badge, Button } from "./ui";

// ---------------------------------------------------------------- labels
const ELIG: Record<EligibilityStatus, { color: "up" | "down" | "warn"; text: string; hint: string }> = {
  confirmed_tradable: { color: "up", text: "Available", hint: "Confirmed tradable in your MarketWatch game" },
  confirmed_unavailable: { color: "down", text: "Unavailable", hint: "Confirmed NOT available in your MarketWatch game - never traded" },
  unverified: { color: "warn", text: "Unverified", hint: "Not yet checked against your game. Research only - the bot will not buy it" },
};

export function EligibilityChip({ status, note }: { status: EligibilityStatus; note?: string }) {
  const e = ELIG[status];
  return <Badge color={e.color} title={note ? `${e.hint}. ${note}` : e.hint}>{e.text}</Badge>;
}

/** Says plainly whether a forecast is a validated model or a rule-of-thumb estimate. Never overstates. */
export function EstimateBadge({ calibrated, label }: { calibrated: boolean; label?: string | null }) {
  return calibrated
    ? <Badge color="up" title={label ?? "Probabilities validated out-of-sample"}>Validated model</Badge>
    : <Badge color="warn" title={label ?? "A rule-of-thumb estimate, not a validated probability"}>Estimate - not validated</Badge>;
}

export function DataKindBadge({ kind }: { kind: string }) {
  const map: Record<string, { c: "warn" | "info" | "up"; t: string }> = {
    synthetic: { c: "warn", t: "SYNTHETIC DATA" },
    historical: { c: "info", t: "HISTORICAL DATA" },
    delayed: { c: "warn", t: "DELAYED DATA" },
    realtime: { c: "up", t: "REAL-TIME DATA" },
  };
  const m = map[kind] ?? { c: "warn" as const, t: kind.toUpperCase() };
  return <Badge color={m.c}>{m.t}</Badge>;
}

// ---------------------------------------------------------------- visuals
/** Typical range (downside..upside) with the expected value marked. All numbers are % returns. */
export function ForecastRange({ down, mid, up }: { down: number; mid: number; up: number }) {
  const lo = Math.min(down, -0.5), hi = Math.max(up, 0.5);
  const x = (v: number) => ((v - lo) / (hi - lo)) * 100;
  return (
    <div role="img" aria-label={`Expected ${pct(mid)}; typical range ${pct(down)} to ${pct(up)}`} className="relative h-6 w-full">
      <div className="absolute inset-x-0 top-1/2 h-1.5 -translate-y-1/2 rounded-full bg-line" />
      <div className="absolute top-1/2 h-1.5 -translate-y-1/2 rounded-full bg-accent/40" style={{ left: `${x(down)}%`, width: `${Math.max(x(up) - x(down), 1)}%` }} />
      <div className="absolute top-0 h-6 w-px bg-muted/70" style={{ left: `${x(0)}%` }} title="0%" />
      <div className="absolute top-1/2 h-3.5 w-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-accent ring-2 ring-bg" style={{ left: `${x(mid)}%` }} title={`Expected ${pct(mid)}`} />
    </div>
  );
}

/** Where a position stands between its risk exit and its profit target. */
export function PlanTrack({ stop, entry, target, price }: { stop: number | null; entry: number; target: number | null; price: number }) {
  if (stop === null || target === null || target <= stop) return <div className="text-xs text-muted">No target/stop plan on this position.</div>;
  const x = (v: number) => Math.min(100, Math.max(0, ((v - stop) / (target - stop)) * 100));
  return (
    <div className="space-y-1" data-testid="plan-track">
      <div role="img" aria-label={`Price ${money(price, 2)} between risk exit ${money(stop, 2)} and target ${money(target, 2)}`} className="relative h-3 rounded-full bg-gradient-to-r from-down/40 via-line to-up/40">
        <div className="absolute inset-y-0 w-px bg-ink/50" style={{ left: `${x(entry)}%` }} title={`Entry ${money(entry, 2)}`} />
        <div className="absolute top-1/2 h-4 w-4 -translate-x-1/2 -translate-y-1/2 rounded-full bg-ink ring-2 ring-bg" style={{ left: `${x(price)}%` }} title={`Now ${money(price, 2)}`} />
      </div>
      <div className="num flex justify-between text-[10px] text-muted"><span>risk exit {money(stop, 2)}</span><span>entry {money(entry, 2)}</span><span>target {money(target, 2)}</span></div>
    </div>
  );
}

export function Meter({ label, value, text, tone = "accent" }: { label: string; value: number; text: string; tone?: "accent" | "up" | "down" | "warn" }) {
  const tones = { accent: "bg-accent", up: "bg-up", down: "bg-down", warn: "bg-warn" };
  return (
    <div>
      <div className="mb-1 flex justify-between text-[11px]"><span className="text-muted">{label}</span><span className="num">{text}</span></div>
      <div className="h-1.5 overflow-hidden rounded-full bg-line" role="progressbar" aria-label={label} aria-valuenow={Math.round(Math.max(0, Math.min(1, value)) * 100)} aria-valuemin={0} aria-valuemax={100}>
        <div className={`h-full rounded-full ${tones[tone]} transition-all duration-500`} style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- bot controls
export function useBotActions(reload: () => void, status: Status | undefined) {
  const confirm = useConfirm();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<unknown>, ok?: string) => {
    setBusy(true);
    try {
      await fn();
      if (ok) toast(ok);
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "error");
    } finally {
      setBusy(false);
      reload();
    }
  };
  const positions = status?.portfolio.open_positions ?? 0;
  return {
    busy,
    start: (speed: string) => {
      const live = status?.session_status === "running" || status?.session_status === "paused_stream" || status?.session_status === "live";
      return run(
        () => (live ? api.bot("start") : api.startSession(speed === "instant" ? { instant: true } : { speed: Number(speed) })),
        "Bot started - it will look for opportunities and manage positions",
      );
    },
    pause: () => run(() => api.bot("pause"), "Entries paused - open positions are still managed"),
    resume: () => run(() => api.bot("resume"), "Entries resumed"),
    stop: async () => {
      const ok = await confirm({
        title: "Stop the bot?",
        body: positions > 0
          ? `You hold ${positions} position(s). Stopping ends new trades. Open positions keep being managed (targets, stops, end-of-day exit) until they are closed - monitoring never silently stops while you hold something.`
          : "The bot will stop looking for new trades.",
        confirmLabel: "Stop the bot",
      });
      if (ok) await run(() => api.bot("stop"), "Bot stopped");
    },
    stopAndFlatten: async () => {
      const ok = await confirm({
        title: "Close everything and stop?",
        body: "Sell all open virtual positions now, then stop the bot.",
        confirmLabel: "Close all and stop",
        danger: true,
      });
      if (ok) await run(async () => { await api.bot("flatten"); await api.bot("stop"); }, "Closing all positions; bot stopped");
    },
    emergency: async () => {
      const flatten = status?.bot.emergency_behavior !== "block_entries_only";
      const ok = await confirm({
        title: "EMERGENCY STOP",
        body: flatten
          ? "Cancels pending buys, sells ALL open virtual positions immediately and blocks every new trade until you clear it. Continue?"
          : "Cancels pending buys and blocks every new trade until you clear it. Open positions stay managed by their normal exits (your Settings choose this). Continue?",
        confirmLabel: "Emergency stop",
        danger: true,
      });
      if (ok) await run(() => api.bot("emergency_stop"), "Emergency stop active");
    },
    clearEmergency: () => run(() => api.bot("clear_emergency"), "Emergency stop cleared - press Start to resume"),
  };
}

export function useModeSwitch(status: Status | undefined, reload: () => void) {
  const confirm = useConfirm();
  const toast = useToast();
  return async (mode: string, label: string) => {
    if (!status || mode === status.mode.id) return;
    const pos = status.portfolio.open_positions;
    const ok = await confirm({
      title: `Switch to ${label}?`,
      body: pos > 0
        ? `New trades will use ${label} rules and sizing. Your ${pos} open position(s) keep the plan they were opened with, and trade history is unchanged. Safety limits (stop-losses, daily loss limit, end-of-day exits) stay in force in both modes.`
        : `New trades will use ${label} rules and sizing. Trade history is unchanged. Safety limits stay in force in both modes.`,
      confirmLabel: "Switch mode",
    });
    if (!ok) return;
    try {
      const r = await api.setMode(mode);
      toast(r.mode.message);
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "error");
    }
    reload();
  };
}

export function BotControls({ status, reload, speed, compact = false }: { status: Status | undefined; reload: () => void; speed: string; compact?: boolean }) {
  const a = useBotActions(reload, status);
  if (!status) return null;
  const st = status.bot.state;
  const sessionActive = ["running", "paused_stream", "live"].includes(status.session_status);
  const startLabel = status.session_status === "finished" ? "Run next day" : "Start";
  return (
    <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Bot controls">
      {st === "stopped" && <Button variant="primary" disabled={a.busy || !status.feed.available} onClick={() => a.start(speed)}>{sessionActive ? "Start" : startLabel}</Button>}
      {st === "paused" && <Button variant="primary" disabled={a.busy} onClick={a.resume}>Resume</Button>}
      {st === "running" && <Button disabled={a.busy} onClick={a.pause} title="No new trades; open positions are still managed">Pause</Button>}
      {(st === "running" || st === "paused") && <Button disabled={a.busy} onClick={a.stop} title="Stop new trades; positions are managed until flat">Stop</Button>}
      {st === "emergency_stop" && <Button variant="primary" disabled={a.busy} onClick={a.clearEmergency}>Clear emergency stop</Button>}
      <Button variant="danger" disabled={a.busy || st === "emergency_stop"} onClick={a.emergency} aria-label="Emergency stop">{compact ? "E-STOP" : "Emergency stop"}</Button>
    </div>
  );
}

// ---------------------------------------------------------------- opportunity row bits
export function ReturnCell({ v }: { v: number | null }) {
  if (v === null) return <span className="text-muted">-</span>;
  return <span className={`num ${v > 0 ? "text-up" : v < 0 ? "text-down" : "text-muted"}`}>{pct(v)}</span>;
}

export function opportunityStatus(o: OppRow): { text: string; color: "up" | "warn" | "muted" | "info" } {
  if (o.held) return { text: "Holding", color: "info" };
  if (o.actionable) return { text: "Ready", color: "up" };
  if (o.research_only) return { text: "Research", color: "warn" };
  return { text: "Watching", color: "muted" };
}

export function PositionSummary({ p }: { p: Position }) {
  const prog = p.progress_to_target_pct;
  return (
    <div className="space-y-2">
      <div className="flex items-baseline justify-between gap-2">
        <div className="flex items-center gap-2"><span className="text-[15px] font-semibold">{p.symbol}</span><span className="num text-xs text-muted">{p.quantity} sh @ {money(p.avg_cost, 2)}</span></div>
        <span className={`num text-sm font-semibold ${p.unrealized_pnl >= 0 ? "text-up" : "text-down"}`}>{p.unrealized_pnl >= 0 ? "+" : "-"}{money(Math.abs(p.unrealized_pnl))} ({pct(p.unrealized_pct)})</span>
      </div>
      <PlanTrack stop={p.stop_price} entry={p.avg_cost} target={p.take_profit_price} price={p.mark} />
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted">
        {prog !== null && <span>{num(Math.max(prog, 0), 0)}% of the way to target</span>}
        {p.minutes_left !== null && <span>{num(p.minutes_left, 0)} min left in plan</span>}
        {p.p_target !== null && <span>{(p.p_target * 100).toFixed(0)}% chance of reaching target (est.)</span>}
        {p.breakeven_armed && <Badge color="up">stop at breakeven</Badge>}
        {p.eligibility_flag && <Badge color="warn" title="No longer confirmed tradable in your game list. It is still managed and will be sold by its normal exit rules.">{p.eligibility_label}</Badge>}
        {p.closing && <Badge color="info">closing...</Badge>}
        {p.mode && <span>{titleCase(p.mode)}</span>}
      </div>
    </div>
  );
}
