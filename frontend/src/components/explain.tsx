import { useEffect, useState } from "react";
import { api } from "../api";
import { chicagoTime, money, num, pct, signedMoney, titleCase } from "../format";
import type { OppDetail, Position, StockResp, Trade } from "../types";
import { PriceChart } from "./charts";
import { Drawer, Loading, useConfirm, useToast } from "./kit";
import { DataKindBadge, EligibilityChip, EstimateBadge, ForecastRange, Meter, PlanTrack } from "./trading";
import { Badge, Button } from "./ui";

export type Target =
  | { kind: "symbol"; symbol: string }
  | { kind: "position"; position: Position }
  | { kind: "trade"; trade: Trade };

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return <section className="mb-5"><h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted">{title}</h3>{children}</section>;
}
const List = ({ items, tone }: { items: string[]; tone?: string }) => (
  <ul className={`list-disc space-y-1 pl-5 text-[13px] ${tone ?? ""}`}>{items.map((x) => <li key={x}>{x}</li>)}</ul>
);

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return <div className="flex justify-between gap-3 py-1 text-[13px]"><span className="text-muted">{k}</span><span className="num text-right">{v}</span></div>;
}

// ---------------------------------------------------------------- an opportunity (why this stock?)
function SymbolView({ symbol, onChanged }: { symbol: string; onChanged: () => void }) {
  const [d, setD] = useState<OppDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [chart, setChart] = useState<StockResp | null>(null);
  const toast = useToast();
  const confirm = useConfirm();
  useEffect(() => {
    let alive = true;
    api.opportunity(symbol).then((x) => alive && setD(x)).catch((e) => alive && setErr(e instanceof Error ? e.message : String(e)));
    api.stock(symbol).then((x) => alive && setChart(x)).catch(() => undefined); // chart is a bonus: only with an active session
    return () => { alive = false; };
  }, [symbol]);

  const mark = async (status: "confirmed_tradable" | "confirmed_unavailable") => {
    if (status === "confirmed_unavailable" && !(await confirm({ title: `Mark ${symbol} unavailable?`, body: "The bot will never buy it and will stop recommending it. Existing holdings are still managed and sold normally.", confirmLabel: "Mark unavailable" }))) return;
    try {
      await api.markEligibility(symbol, status, status === "confirmed_unavailable" ? "Marked unavailable from the dashboard" : "");
      toast(`${symbol} marked ${status === "confirmed_tradable" ? "available" : "unavailable"} in your game list`);
      onChanged();
      setD(await api.opportunity(symbol).catch(() => d));
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "error");
    }
  };

  if (err) return <div role="alert" className="rounded-lg border border-line bg-panel p-4 text-[13px] text-muted">{err}. The stock is not in the current ranking (no active session, or it is filtered out for data quality).</div>;
  if (!d) return <Loading rows={5} label="Loading explanation" />;
  const f = d.forecast, p = d.plan, ex = d.explanation;
  const st = d.eligibility?.status ?? "unverified";
  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <EligibilityChip status={st} note={d.eligibility?.rejection_reason} />
        {f && <EstimateBadge calibrated={f.calibrated} label={f.label} />}
        <span className="num text-sm">{money(d.price, 2)}</span>
        {d.day_change_pct !== null && <span className="text-xs text-muted">{pct(d.day_change_pct)} today (not used to rank)</span>}
      </div>

      {st !== "confirmed_tradable" && (
        <div className="mb-4 rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn" role="note">
          {st === "unverified" ? "Not verified in your game yet: this is a research candidate. The bot will not buy it until you confirm it is available." : `Unavailable in your game${d.eligibility?.rejection_reason ? ` (${d.eligibility.rejection_reason})` : ""}.`}
        </div>
      )}

      {ex.summary && <Section title="Why it ranks here"><p className="text-[13px] leading-relaxed">{ex.summary}</p>{ex.drivers && ex.drivers.length > 0 && <div className="mt-2"><List items={ex.drivers} /></div>}</Section>}

      {f && (
        <Section title="What the model expects">
          <ForecastRange down={f.downside_pct} mid={f.expected_return_pct} up={f.upside_pct} />
          <div className="mt-2 grid grid-cols-2 gap-x-6">
            <Row k="Expected return (after costs)" v={pct(f.expected_net_return_pct)} />
            <Row k="Typical uncertainty" v={`±${num(f.uncertainty_pct, 2)}%`} />
            <Row k="Chance of a gain" v={`${(f.p_positive * 100).toFixed(0)}%`} />
            <Row k="Likely range" v={`${pct(f.downside_pct)} to ${pct(f.upside_pct)}`} />
            <Row k="Looking ahead" v={`${num(f.horizon_minutes, 0)} min`} />
            <Row k="Round-trip cost" v={`${num(f.cost_pct, 2)}%`} />
          </div>
          <p className="mt-2 text-[11px] leading-snug text-muted">
            {f.calibrated ? "Probabilities passed out-of-sample calibration tests." : "This is a rule-of-thumb estimate. Probabilities have NOT been validated and can be wrong - treat them as a ranking aid, not a promise."}
            {" "}Expected return is a statistical average; the target below is a plausible upside level, a different thing.
          </p>
        </Section>
      )}

      {p && (
        <Section title="The plan if bought">
          <PlanTrack stop={p.stop_price} entry={p.entry_price} target={p.target_price} price={d.price} />
          <div className="mt-3 grid grid-cols-2 gap-x-6">
            <Row k="Profit target" v={`${money(p.target_price, 2)} (${pct(p.target_return_pct)})`} />
            <Row k="Chance of reaching it" v={`${(p.p_target * 100).toFixed(0)}%`} />
            <Row k="Risk exit (stop)" v={`${money(p.stop_price, 2)} (-${num(p.stop_pct, 2)}%)`} />
            <Row k="Reward vs risk" v={num(p.reward_risk, 2)} />
            <Row k="Maximum hold" v={`${num(p.max_hold_minutes, 0)} min`} />
            <Row k="Minutes to the close" v={num(d.minutes_to_close, 0)} />
          </div>
          {p.notes.length > 0 && <div className="mt-2 text-xs text-muted"><List items={p.notes} /></div>}
        </Section>
      )}

      {d.gates.length > 0 && (
        <Section title="Checks before buying">
          <ul className="space-y-1" data-testid="gates">
            {d.gates.map((g) => (
              <li key={g.label} className="flex gap-2 text-[13px]">
                <span className={g.passed ? "text-up" : "text-down"} aria-label={g.passed ? "passed" : "not passed"}>{g.passed ? "✓" : "✗"}</span>
                <span><span className="font-medium">{g.label}</span> <span className="text-muted">{g.detail}</span></span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {ex.risks && ex.risks.length > 0 && <Section title="Working against it"><List items={ex.risks} /></Section>}
      {ex.invalidation && <Section title="What would change the picture"><List items={ex.invalidation} /></Section>}

      {f && f.contributions.length > 0 && (
        <Section title="Signals">
          <div className="space-y-2">
            {f.contributions.slice(0, 7).map((c) => <Meter key={c.name} label={c.label} value={Math.min(1, Math.abs(c.points) * 4)} tone={c.points >= 0 ? "up" : "down"} text={`${c.points >= 0 ? "+" : ""}${c.points.toFixed(2)} pts`} />)}
          </div>
          {f.warnings.length > 0 && <p className="mt-2 text-[11px] text-muted">{f.warnings.join(" · ")}</p>}
        </Section>
      )}

      {chart && chart.bars.length > 0 && (
        <Section title={`Price today ${chart.feed ? "" : ""}`}>
          <div className="mb-2 flex items-center gap-2"><DataKindBadge kind={chart.feed.kind} /><span className="text-[11px] text-muted">{chart.feed.label}</span></div>
          <div className="[&>div]:!h-[260px]"><PriceChart bars={chart.bars} openTime={chart.open_time} resetKey={chart.symbol} /></div>
        </Section>
      )}

      <Section title="Your game list">
        <Row k="Status" v={<EligibilityChip status={st} />} />
        {d.eligibility?.verified_on && <Row k="Verified on" v={d.eligibility.verified_on} />}
        {d.eligibility?.source && <Row k="Source" v={d.eligibility.source} />}
        {d.eligibility?.violations?.map((v) => <div key={v} className="text-[12px] text-warn">{v}</div>)}
        <div className="mt-3 flex gap-2">
          {st !== "confirmed_tradable" && <Button onClick={() => mark("confirmed_tradable")}>Mark available</Button>}
          {st !== "confirmed_unavailable" && <Button variant="danger" onClick={() => mark("confirmed_unavailable")}>Mark unavailable</Button>}
        </div>
        <p className="mt-2 text-[11px] text-muted">Only mark a stock available after you have checked it in your MarketWatch game. Being listed on NYSE/Nasdaq does not make it available there.</p>
      </Section>
    </div>
  );
}

// ---------------------------------------------------------------- an open position
function PositionView({ p }: { p: Position }) {
  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <EligibilityChip status={p.eligibility} />
        <EstimateBadge calibrated={p.forecast_calibrated} label={p.forecast_label} />
        {p.mode && <Badge color="muted">{titleCase(p.mode)}</Badge>}
      </div>
      <Section title="Where it stands">
        <PlanTrack stop={p.stop_price} entry={p.avg_cost} target={p.take_profit_price} price={p.mark} />
        <div className="mt-3 grid grid-cols-2 gap-x-6">
          <Row k="Current price" v={money(p.mark, 2)} />
          <Row k="Entry" v={money(p.avg_cost, 2)} />
          <Row k="Profit target" v={p.take_profit_price ? `${money(p.take_profit_price, 2)} (${pct(p.target_return_pct)})` : "-"} />
          <Row k="Risk exit" v={p.stop_price ? `${money(p.stop_price, 2)}${p.breakeven_armed ? " (breakeven)" : ""}` : "-"} />
          <Row k="Open P/L" v={`${signedMoney(p.unrealized_pnl)} (${pct(p.unrealized_pct)})`} />
          <Row k="Progress to target" v={p.progress_to_target_pct === null ? "-" : `${num(Math.max(p.progress_to_target_pct, 0), 0)}%`} />
          <Row k="Plan deadline" v={p.deadline ? `${chicagoTime(p.deadline)} CT (${num(p.minutes_left ?? 0, 0)} min)` : "-"} />
          <Row k="Market closes in" v={p.minutes_to_close === null ? "-" : `${num(p.minutes_to_close, 0)} min`} />
        </div>
      </Section>
      <Section title="Why it was bought"><p className="text-[13px] leading-relaxed">{p.entry_reason || "Manual simulated order."}</p></Section>
      {p.expected_net_return_pct !== null && (
        <Section title="Expectation at entry vs now">
          <div className="grid grid-cols-2 gap-x-6">
            <Row k="Expected at entry (after costs)" v={pct(p.expected_net_return_pct)} />
            <Row k="Expected now (remaining)" v={p.current_forecast ? pct(p.current_forecast.expected_net_return_pct) : "re-checking..."} />
            <Row k="Chance of reaching target" v={p.p_target === null ? "-" : `${(p.p_target * 100).toFixed(0)}%`} />
            <Row k="Chance of a gain now" v={p.current_forecast ? `${(p.current_forecast.p_positive * 100).toFixed(0)}%` : "-"} />
          </div>
        </Section>
      )}
      <Section title="What would close it">
        <List items={[
          p.take_profit_price ? `Price reaches the profit target (${money(p.take_profit_price, 2)})` : "A profit target",
          p.stop_price ? `Price falls to the risk exit (${money(p.stop_price, 2)})` : "A risk exit",
          "The updated forecast stays poor for several checks in a row",
          "The plan's deadline, or end-of-day exits before the close",
          "A much better opportunity appearing (only if the gain beats the costs)",
        ]} />
      </Section>
      {p.plan_notes.length > 0 && <Section title="How the plan was set"><List items={p.plan_notes} /></Section>}
      {p.eligibility_flag && <div className="rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn">This stock is no longer confirmed available in your game list. It is still managed and will be sold by its normal exit rules - eligibility never blocks an exit.</div>}
    </div>
  );
}

// ---------------------------------------------------------------- a closed trade (why closed?)
const EXIT_TEXT: Record<string, string> = {
  target: "Profit target reached - sold early rather than holding to the close.",
  risk: "A risk exit was hit (stop-loss, trailing stop or breakeven stop).",
  forecast: "The updated forecast stayed poor, so the position was sold.",
  end_of_day: "End-of-day exit before the close.",
  time: "The planned holding time ran out.",
  rotation: "Sold to switch into a clearly better opportunity.",
  emergency: "Emergency stop.",
  limit: "Daily loss limit reached.",
  manual: "You closed it manually.",
};

export function exitCategory(reason: string): string {
  const r = reason.toLowerCase();
  if (r.includes("profit target")) return "target";
  if (r.includes("manual") || r.includes("closed by you")) return "manual";
  if (r.includes("emergency")) return "emergency";
  if (r.includes("daily loss")) return "limit";
  if (r.includes("end-of-day") || r.includes("unresolved at close")) return "end_of_day";
  if (r.includes("rotation")) return "rotation";
  if (r.includes("forecast")) return "forecast";
  if (r.includes("time stop") || r.includes("deadline")) return "time";
  if (["stop loss", "trailing stop", "breakeven", "failed breakout", "momentum lost", "gap failed", "surge faded"].some((k) => r.includes(k))) return "risk";
  return "other";
}

function TradeView({ t }: { t: Trade }) {
  const cat = exitCategory(t.exit_reason);
  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Badge color={t.net_pnl >= 0 ? "up" : "down"}>{signedMoney(t.net_pnl)} ({pct(t.return_pct)})</Badge>
        <Badge color="muted">{titleCase(cat)}</Badge>
        {t.mode && <Badge color="muted">{titleCase(t.mode)}</Badge>}
        {t.predicted_net_return_pct !== null && t.predicted_net_return_pct !== undefined && <EstimateBadge calibrated={Boolean(t.forecast_calibrated)} />}
      </div>
      <Section title="Why it was bought"><p className="text-[13px] leading-relaxed">{t.entry_reason}</p></Section>
      <Section title="Why it was closed"><p className="text-[13px] leading-relaxed">{t.exit_reason}</p><p className="mt-1 text-xs text-muted">{EXIT_TEXT[cat] ?? ""}</p></Section>
      <Section title="What happened">
        {t.planned_target_price && t.planned_stop_price && <PlanTrack stop={t.planned_stop_price} entry={t.entry_price} target={t.planned_target_price} price={t.exit_price} />}
        <div className="mt-3 grid grid-cols-2 gap-x-6">
          <Row k="Bought" v={`${t.quantity} @ ${money(t.entry_price, 2)} · ${chicagoTime(t.entry_time)} CT`} />
          <Row k="Sold" v={`${money(t.exit_price, 2)} · ${chicagoTime(t.exit_time)} CT`} />
          <Row k="Held" v={`${num(t.hold_minutes, 0)} min`} />
          <Row k="Commissions" v={money(t.commissions)} />
          <Row k="Gross P/L" v={signedMoney(t.gross_pnl)} />
          <Row k="Net P/L" v={signedMoney(t.net_pnl)} />
        </div>
      </Section>
      {t.predicted_net_return_pct !== null && t.predicted_net_return_pct !== undefined && (
        <Section title="Forecast vs result">
          <div className="grid grid-cols-2 gap-x-6">
            <Row k="Model expected (after costs)" v={pct(t.predicted_net_return_pct)} />
            <Row k="Actual (after costs)" v={pct(t.return_pct)} />
            <Row k="Chance of a gain (est.)" v={t.predicted_p_positive == null ? "-" : `${(t.predicted_p_positive * 100).toFixed(0)}%`} />
            <Row k="Chance of target (est.)" v={t.predicted_p_target == null ? "-" : `${(t.predicted_p_target * 100).toFixed(0)}%`} />
          </div>
          <p className="mt-2 text-[11px] text-muted">One trade proves nothing about the model. Accuracy is only meaningful over many trades (see Strategy Lab).</p>
        </Section>
      )}
    </div>
  );
}

export function ExplainDrawer({ target, onClose, onChanged }: { target: Target; onClose: () => void; onChanged: () => void }) {
  const title = target.kind === "symbol" ? target.symbol : target.kind === "position" ? target.position.symbol : target.trade.symbol;
  const sub = target.kind === "symbol" ? "Why the bot likes it, or doesn't" : target.kind === "position" ? "Open position" : `Closed trade · ${target.trade.session_date}`;
  return (
    <Drawer title={title} subtitle={sub} onClose={onClose}>
      {target.kind === "symbol" && <SymbolView symbol={target.symbol} onChanged={onChanged} />}
      {target.kind === "position" && <PositionView p={target.position} />}
      {target.kind === "trade" && <TradeView t={target.trade} />}
    </Drawer>
  );
}
