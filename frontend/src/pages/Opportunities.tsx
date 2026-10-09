import { useMemo, useRef, useState } from "react";
import { useApp } from "../App";
import { api } from "../api";
import { Chip, ErrorState, Loading, useConfirm, usePref, useToast } from "../components/kit";
import { DataKindBadge, EligibilityChip, EstimateBadge, ReturnCell, opportunityStatus } from "../components/trading";
import { Badge, Button, Card, Empty, Field, Select, TextInput } from "../components/ui";
import { money, num, pct, readText } from "../format";
import { usePoll } from "../hooks";
import type { EligibilityStatus, OppRow } from "../types";

type Filter = "all" | EligibilityStatus;
type Sort = "default" | "expected" | "chance" | "score" | "symbol";

const SORTS: { v: Sort; label: string }[] = [
  { v: "default", label: "Bot's ranking" },
  { v: "expected", label: "Expected return" },
  { v: "chance", label: "Chance of a gain" },
  { v: "score", label: "Score" },
  { v: "symbol", label: "Symbol (A-Z)" },
];

function unavailableRow(u: { symbol: string; name: string; score: number; reason: string }): OppRow {
  return {
    symbol: u.symbol, name: u.name, price: 0, day_change_pct: null, eligibility: "confirmed_unavailable", eligibility_label: "Confirmed unavailable",
    eligibility_note: u.reason, actionable: false, research_only: false, held: false, score: u.score, utility: -99, expected_net_return_pct: null,
    expected_return_pct: null, uncertainty_pct: null, p_positive: null, downside_pct: null, upside_pct: null, target_price: null, target_return_pct: null,
    p_target: null, stop_price: null, horizon_minutes: 0, minutes_to_close: 0, calibrated: false, forecast_label: null, blocked_by: "Marked unavailable in your game", summary: null,
  };
}

// ---------------------------------------------------------------- the user's own stock list
function GameList({ onChanged }: { onChanged: () => void }) {
  const toast = useToast();
  const confirm = useConfirm();
  const { data, error, reload } = usePoll(() => api.eligibility(), null);
  const [ticker, setTicker] = useState("");
  const [status, setStatus] = useState<EligibilityStatus>("confirmed_tradable");
  const [reason, setReason] = useState("");
  const file = useRef<HTMLInputElement>(null);
  const done = () => { reload(); onChanged(); };
  const fail = (e: unknown) => toast(e instanceof Error ? e.message : String(e), "error");

  const add = async () => {
    try {
      await api.markEligibility(ticker.trim().toUpperCase(), status, reason);
      toast(`${ticker.trim().toUpperCase()} saved to your list`);
      setTicker(""); setReason(""); done();
    } catch (e) { fail(e); }
  };
  const importFile = async (f: File) => {
    try {
      const text = await readText(f);
      const dry = await api.importEligibility(text, true);
      const ok = await confirm({
        title: `Import ${dry.n_rows} row(s)?`,
        body: <div><p>Rows with problems are skipped; the rest are added or update existing entries (older history is kept).</p>{dry.errors.length > 0 && <ul className="mt-2 list-disc pl-5 text-warn">{dry.errors.slice(0, 5).map((x) => <li key={x}>{x}</li>)}</ul>}</div>,
        confirmLabel: "Import",
      });
      if (!ok) return;
      await api.importEligibility(text, false);
      toast(`Imported ${dry.n_rows} row(s)`); done();
    } catch (e) { fail(e); }
    if (file.current) file.current.value = "";
  };
  const clear = async () => {
    if (!(await confirm({ title: "Clear your whole stock list?", body: "Every stock becomes Unverified, so the bot will not buy anything until you add or import a list again. Open positions are still managed.", confirmLabel: "Clear list", danger: true }))) return;
    try { await api.clearEligibility(); toast("List cleared"); done(); } catch (e) { fail(e); }
  };
  const demo = async () => {
    try { const r = await api.seedEligibilityDemo(); toast(`Loaded the demo list (${r.added} made-up entries)`); done(); } catch (e) { fail(e); }
  };

  const s = data?.summary;
  return (
    <Card title="Your MarketWatch stock list" right={s && <Badge color="muted">{s.n_tickers} stocks</Badge>}>
      {error && <ErrorState message={error} onRetry={reload} />}
      {!data ? <Loading rows={3} /> : (
        <div className="space-y-4">
          <p className="text-[13px] leading-relaxed text-muted">{s?.note} Source: <b className="text-ink">{s?.source || "empty"}</b>.</p>
          <div className="flex flex-wrap gap-2 text-xs">
            <Badge color="up">{s?.counts.confirmed_tradable ?? 0} available</Badge>
            <Badge color="down">{s?.counts.confirmed_unavailable ?? 0} unavailable</Badge>
            <Badge color="warn">{s?.counts.unverified ?? 0} unverified</Badge>
          </div>
          <div className="grid items-end gap-3 sm:grid-cols-[1fr_1fr_1.4fr_auto]">
            <Field label="Ticker"><TextInput aria-label="Ticker" placeholder="e.g. ABCD" value={ticker} onChange={(e) => setTicker(e.target.value.toUpperCase())} onKeyDown={(e) => e.key === "Enter" && ticker && add()} maxLength={10} /></Field>
            <Field label="Status"><Select aria-label="List status" value={status} onChange={(e) => setStatus(e.target.value as EligibilityStatus)}>
              <option value="confirmed_tradable">Available in my game</option><option value="confirmed_unavailable">Not available</option><option value="unverified">Unverified</option></Select></Field>
            <Field label="Note (optional)"><TextInput aria-label="Note" placeholder="why, if unavailable" value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
            <Button variant="primary" onClick={add} disabled={!ticker.trim()}>Save</Button>
          </div>
          <div className="flex flex-wrap gap-2 border-t border-line pt-3">
            <input ref={file} type="file" accept=".csv,text/csv" className="sr-only" aria-label="Import CSV file" onChange={(e) => e.target.files?.[0] && importFile(e.target.files[0])} />
            <Button onClick={() => file.current?.click()}>Import CSV</Button>
            <a className="inline-flex items-center rounded-lg bg-panel2 px-3 py-1.5 text-[13px] font-medium ring-1 ring-line hover:ring-muted/50" href={api.eligibilityExportUrl()} download>Export CSV</a>
            <Button variant="ghost" onClick={demo} title="Made-up tickers for the built-in demo market">Load demo list</Button>
            <Button variant="ghost" className="ml-auto text-down" onClick={clear}>Clear list</Button>
          </div>
          <p className="text-[11px] text-muted">CSV columns: ticker, status (confirmed_tradable / confirmed_unavailable / unverified), name, exchange, rejection_reason, effective_from. {s?.verifier.message}</p>
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------- page
export default function Opportunities() {
  const { status, explain, version, refresh } = useApp();
  const toast = useToast();
  const confirm = useConfirm();
  const live = status?.session_status === "running" || status?.session_status === "live";
  const { data, error, loading, reload } = usePoll(api.opportunities, live ? 2500 : 6000, [status?.session_status, version]);
  const [filter, setFilter] = usePref<Filter>("opp-filter", "all");
  const [sort, setSort] = usePref<Sort>("opp-sort", "default");
  const [q, setQ] = useState("");

  const rows = useMemo(() => {
    if (!data) return [];
    const all = [...data.ready, ...data.watching, ...data.research, ...data.unavailable_skipped.map(unavailableRow)];
    const needle = q.trim().toUpperCase();
    let out = all.filter((r) => (filter === "all" || r.eligibility === filter) && (!needle || r.symbol.includes(needle) || r.name.toUpperCase().includes(needle)));
    if (sort !== "default") {
      const key: Record<Sort, (r: OppRow) => number | string> = {
        default: () => 0, expected: (r) => -(r.expected_net_return_pct ?? -99), chance: (r) => -(r.p_positive ?? -1), score: (r) => -r.score, symbol: (r) => r.symbol,
      };
      out = [...out].sort((a, b) => (key[sort](a) < key[sort](b) ? -1 : key[sort](a) > key[sort](b) ? 1 : 0));
    }
    return out;
  }, [data, filter, sort, q]);

  const counts = useMemo(() => {
    const c = { all: 0, confirmed_tradable: 0, unverified: 0, confirmed_unavailable: 0 };
    if (data) {
      c.confirmed_tradable = data.ready.length + data.watching.length;
      c.unverified = data.research.length;
      c.confirmed_unavailable = data.unavailable_skipped.length;
      c.all = c.confirmed_tradable + c.unverified + c.confirmed_unavailable;
    }
    return c;
  }, [data]);

  const mark = async (r: OppRow, to: "confirmed_tradable" | "confirmed_unavailable") => {
    if (to === "confirmed_unavailable" && !(await confirm({ title: `Mark ${r.symbol} unavailable?`, body: "The bot will never buy it and will stop recommending it. Existing holdings are still managed and sold normally.", confirmLabel: "Mark unavailable" }))) return;
    try {
      await api.markEligibility(r.symbol, to, to === "confirmed_unavailable" ? "Marked unavailable from the dashboard" : "", r.name);
      toast(`${r.symbol} marked ${to === "confirmed_tradable" ? "available" : "unavailable"}`);
      refresh(); reload();
    } catch (e) { toast(e instanceof Error ? e.message : String(e), "error"); }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        {data && <><DataKindBadge kind={data.feed.kind} /><EstimateBadge calibrated={data.forecaster.calibrated} label={data.forecaster.label} />
          <span className="text-xs text-muted">{data.mode.label} mode · {data.counts.scanned} scanned · {data.counts.ready} ready</span></>}
      </div>
      {data?.waiting && data.counts.ready === 0 && <div className="rounded-lg border border-line bg-panel px-4 py-2.5 text-[13px] text-muted" role="status"><b className="text-ink">Why nothing is bought right now: </b>{data.waiting}</div>}

      <Card title="Opportunities" pad={false}
        right={<div className="flex items-center gap-2"><label className="sr-only" htmlFor="opp-sort">Sort by</label>
          <Select id="opp-sort" aria-label="Sort by" value={sort} onChange={(e) => setSort(e.target.value as Sort)} className="!w-auto">{SORTS.map((s) => <option key={s.v} value={s.v}>{s.label}</option>)}</Select></div>}>
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
          <Chip active={filter === "all"} onClick={() => setFilter("all")} count={counts.all}>All</Chip>
          <Chip active={filter === "confirmed_tradable"} onClick={() => setFilter("confirmed_tradable")} count={counts.confirmed_tradable}>Available</Chip>
          <Chip active={filter === "unverified"} onClick={() => setFilter("unverified")} count={counts.unverified}>Unverified (research)</Chip>
          <Chip active={filter === "confirmed_unavailable"} onClick={() => setFilter("confirmed_unavailable")} count={counts.confirmed_unavailable}>Unavailable</Chip>
          <div className="ml-auto w-full sm:w-56"><TextInput aria-label="Search stocks" placeholder="Search symbol or name" value={q} onChange={(e) => setQ(e.target.value)} /></div>
        </div>
        {error && !data ? <ErrorState message={error} onRetry={reload} /> : loading && !data ? <Loading rows={6} label="Loading opportunities" /> : rows.length === 0 ? (
          <Empty>{filter !== "all" || q ? "Nothing matches these filters." : status?.session_status === "idle" || status?.session_status === "finished" ? "Start the bot to see what it finds." : "No stock has usable data and a known status in your game yet."}</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[820px] text-[13px]" data-testid="opp-table">
              <thead className="text-left text-[10px] uppercase tracking-wider text-muted">
                <tr><th className="px-4 py-2">Stock</th><th>Eligibility</th><th className="text-right">Price</th><th className="text-right">Expected*</th><th className="text-right">Gain odds</th><th className="text-right">Target</th><th className="text-right">To target</th><th className="text-right">Downside</th><th className="px-3 text-right">Score</th><th>Status</th><th className="pr-4 text-right">Mark</th></tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const st = opportunityStatus(r);
                  return (
                    <tr key={r.symbol} className="border-t border-line hover:bg-panel2">
                      <td className="cursor-pointer px-4 py-2.5" tabIndex={0} onClick={() => explain({ kind: "symbol", symbol: r.symbol })} onKeyDown={(e) => e.key === "Enter" && explain({ kind: "symbol", symbol: r.symbol })} aria-label={`Explain ${r.symbol}`}>
                        <div className="font-semibold">{r.symbol}</div><div className="max-w-[150px] truncate text-[11px] text-muted">{r.name}</div></td>
                      <td><EligibilityChip status={r.eligibility} note={r.eligibility_note} /></td>
                      <td className="num text-right">{r.price ? money(r.price, 2) : "-"}</td>
                      <td className="text-right"><ReturnCell v={r.expected_net_return_pct} /></td>
                      <td className="num text-right">{r.p_positive === null ? "-" : `${(r.p_positive * 100).toFixed(0)}%`}</td>
                      <td className="num text-right">{r.target_price ? `${money(r.target_price, 2)} ${pct(r.target_return_pct, 1)}` : "-"}</td>
                      <td className="num text-right">{r.p_target === null ? "-" : `${(r.p_target * 100).toFixed(0)}%`}</td>
                      <td className="num text-right text-down">{r.downside_pct === null ? "-" : pct(r.downside_pct, 1)}</td>
                      <td className="num px-3 text-right">{num(r.score, 0)}</td>
                      <td><Badge color={st.color} title={r.blocked_by ?? r.summary ?? ""}>{r.eligibility === "confirmed_unavailable" ? "Skipped" : st.text}</Badge></td>
                      <td className="pr-4 text-right whitespace-nowrap">
                        {r.eligibility !== "confirmed_tradable" && <Button variant="ghost" className="!px-2 !py-1 text-xs text-up" onClick={() => mark(r, "confirmed_tradable")} aria-label={`Mark ${r.symbol} available`}>Available</Button>}
                        {r.eligibility !== "confirmed_unavailable" && <Button variant="ghost" className="!px-2 !py-1 text-xs text-down" onClick={() => mark(r, "confirmed_unavailable")} aria-label={`Mark ${r.symbol} unavailable`}>Unavailable</Button>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <p className="border-t border-line px-4 py-2.5 text-[11px] leading-snug text-muted">*Expected return after trading costs over the time left today, ranked by the bot - not by how much a stock already gained. Unverified stocks are research only: the bot never buys a stock until you confirm it exists in your game.</p>
      </Card>

      <GameList onChanged={() => { refresh(); reload(); }} />
    </div>
  );
}
