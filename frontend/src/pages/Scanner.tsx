import { Fragment, useMemo, useState } from "react";
import { useApp } from "../App";
import { api } from "../api";
import { Badge, Button, Card, Empty, ErrorBanner, Field, TextInput } from "../components/ui";
import { compact, money, num, pct, tone } from "../format";
import { usePoll } from "../hooks";
import type { ScanRow } from "../types";

type SortKey = "rank" | "symbol" | "price" | "pct" | "volume" | "rvol" | "mom" | "vwap" | "score";
const PRESETS = [
  ["penny_stocks", "Penny stocks"],
  ["small_cap_momentum", "Small-cap momentum"],
  ["broad_us_equities", "Broad U.S. equities"],
] as const;

const FILTERS: [string, string, string][] = [
  ["min_price", "Min price ($)", "0.01"],
  ["max_price", "Max price ($)", "0.01"],
  ["min_pct_change", "Min change (%)", "0.5"],
  ["min_volume", "Min volume (sh)", "10000"],
  ["min_rvol", "Min RVOL (x)", "0.1"],
  ["min_dollar_volume", "Min $ volume", "50000"],
  ["max_spread_pct", "Max spread (%)", "0.1"],
];

function sortValue(r: ScanRow, k: SortKey): number | string {
  const m = r.metrics;
  switch (k) {
    case "symbol": return r.symbol;
    case "price": return m.price ?? -Infinity;
    case "pct": return m.pct_change ?? -Infinity;
    case "volume": return m.volume;
    case "rvol": return m.rvol ?? -Infinity;
    case "mom": return m.momentum_short ?? -Infinity;
    case "vwap": return m.vs_vwap_pct ?? -Infinity;
    case "score": return r.score;
    default: return r.rank || 9999;
  }
}

export default function Scanner() {
  const { status, go, select, reloadStatus } = useApp();
  const running = status?.session_status === "running";
  const { data, error, reload } = usePoll(api.scanner, running ? 2000 : 5000, [status?.sim_time_utc]);
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: "rank", dir: 1 });
  const [showFiltered, setShowFiltered] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [err, setErr] = useState<string | null>(null);

  const rows = useMemo(() => {
    let r = (data?.results ?? []).filter((x) => (showFiltered ? true : x.tradable));
    if (q) r = r.filter((x) => x.symbol.includes(q.toUpperCase()) || x.name.toLowerCase().includes(q.toLowerCase()));
    return [...r].sort((a, b) => {
      const va = sortValue(a, sort.key), vb = sortValue(b, sort.key);
      const c = typeof va === "string" ? va.localeCompare(vb as string) : (va as number) - (vb as number);
      return c * sort.dir || a.symbol.localeCompare(b.symbol);
    });
  }, [data, sort, showFiltered, q]);

  const applyPreset = async (name: string) => {
    setErr(null);
    try {
      await api.applyPreset(name);
      setDraft({});
      reload();
      reloadStatus();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };
  const applyFilters = async () => {
    setErr(null);
    const filters: Record<string, number> = {};
    for (const [k, v] of Object.entries(draft)) if (v !== "") filters[k] = Number(v);
    try {
      await api.saveSettings({ scanner: { filters } });
      setDraft({});
      reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const th = (key: SortKey, label: string, right = true) => (
    <th scope="col" className={`px-2 py-2 ${right ? "text-right" : "text-left"}`} aria-sort={sort.key === key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
      <button className="uppercase tracking-wide hover:text-ink" onClick={() => setSort({ key, dir: sort.key === key && sort.dir === 1 ? -1 : 1 })}>
        {label}{sort.key === key ? (sort.dir === 1 ? " ▲" : " ▼") : ""}
      </button>
    </th>
  );

  const f = data?.config.filters ?? {};
  return (
    <div className="space-y-4">
      <ErrorBanner message={err ?? error} onClose={() => setErr(null)} />
      <Card title="Scanner filters" right={data && <Badge color="info">preset: {data.config.preset.replace(/_/g, " ")}</Badge>}>
        <div className="mb-3 flex flex-wrap gap-2">
          {PRESETS.map(([id, label]) => (
            <Button key={id} variant={data?.config.preset === id ? "primary" : "subtle"} onClick={() => applyPreset(id)}>{label}</Button>
          ))}
        </div>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7">
          {FILTERS.map(([k, label, step]) => (
            <Field key={k} label={label}>
              <TextInput type="number" step={step} aria-label={label} placeholder={String(f[k] ?? "")} value={draft[k] ?? ""} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} />
            </Field>
          ))}
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <Button variant="primary" onClick={applyFilters} disabled={Object.values(draft).every((v) => v === "")}>Apply filters</Button>
          <span className="text-xs text-muted">Current: ${String(f.min_price)}-${String(f.max_price)}, ≥{String(f.min_pct_change)}% change, RVOL ≥{String(f.min_rvol)}x. Unavailable data is shown as "-" and never guessed.</span>
        </div>
      </Card>

      <Card
        title={`Opportunities${data ? ` (${data.n_tradable} tradable of ${data.results.length})` : ""}`}
        pad={false}
        right={
          <>
            <TextInput aria-label="Search symbol" placeholder="Search..." className="!w-32" value={q} onChange={(e) => setQ(e.target.value)} />
            <label className="flex items-center gap-1.5 text-xs text-muted"><input type="checkbox" checked={showFiltered} onChange={(e) => setShowFiltered(e.target.checked)} />show filtered-out</label>
          </>
        }
      >
        {!data || data.results.length === 0 ? (
          <Empty>No scan results yet. Start a session from the Overview page — the scanner runs from 08:00 CT.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[920px] text-[13px]">
              <thead className="text-[11px] text-muted"><tr>
                {th("rank", "#", false)}{th("symbol", "Symbol", false)}{th("price", "Price")}{th("pct", "Chg %")}{th("volume", "Volume")}{th("rvol", "RVOL")}{th("mom", "5m mom")}{th("vwap", "vs VWAP")}
                <th className="px-2 text-right">Spread</th>{th("score", "Score")}<th className="px-2 text-left">Why</th>
              </tr></thead>
              <tbody>
                {rows.map((r) => {
                  const m = r.metrics;
                  const isOpen = open === r.symbol;
                  return (
                    <Fragment key={r.symbol}>
                      <tr className={`cursor-pointer border-t border-line hover:bg-panel2 ${r.tradable ? "" : "opacity-60"}`} onClick={() => setOpen(isOpen ? null : r.symbol)} data-testid={`scan-row-${r.symbol}`}>
                        <td className="px-2 py-2 num text-muted">{r.rank || "-"}</td>
                        <td className="px-2 py-2">
                          <button className="font-semibold text-accent hover:underline" onClick={(e) => { e.stopPropagation(); select(r.symbol); go("stock"); }}>{r.symbol}</button>
                          <div className="flex gap-1 pt-0.5">{r.continuing && <Badge color="up" title="Positive momentum, above VWAP, near highs">continuing</Badge>}{m.halted && <Badge color="down">halted</Badge>}{!r.tradable && <Badge color="muted">filtered</Badge>}</div>
                        </td>
                        <td className="num px-2 text-right">{money(m.price, (m.price ?? 2) < 1 ? 4 : 2)}</td>
                        <td className={`num px-2 text-right ${tone(m.pct_change)}`}>{pct(m.pct_change, 1)}</td>
                        <td className="num px-2 text-right">{compact(m.volume)}</td>
                        <td className="num px-2 text-right">{m.rvol === null ? "-" : `${num(m.rvol, 1)}x`}</td>
                        <td className={`num px-2 text-right ${tone(m.momentum_short)}`}>{pct(m.momentum_short, 2)}</td>
                        <td className={`num px-2 text-right ${tone(m.vs_vwap_pct)}`}>{pct(m.vs_vwap_pct, 1)}</td>
                        <td className="num px-2 text-right">{m.spread_pct === null ? "-" : pct(m.spread_pct, 2, false)}</td>
                        <td className="px-2"><div className="ml-auto flex w-24 items-center justify-end gap-2"><div className="h-1.5 w-12 overflow-hidden rounded-full bg-line"><div className="h-full bg-accent" style={{ width: `${r.score}%` }} /></div><span className="num w-8 text-right">{r.score.toFixed(0)}</span></div></td>
                        <td className="max-w-[320px] px-2 text-xs text-muted">{r.tradable ? r.reasons.slice(0, 2).join(" · ") || "-" : r.blockers.slice(0, 2).join(" · ")}</td>
                      </tr>
                      {isOpen && (
                        <tr className="border-t border-line bg-panel2/60"><td colSpan={11} className="px-4 py-3">
                          <div className="grid gap-4 md:grid-cols-2">
                            <div>
                              <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted">Score breakdown (0-100)</div>
                              <ul className="space-y-1">
                                {r.components.map((c) => (
                                  <li key={c.name} className="flex items-center gap-2 text-xs">
                                    <span className="w-44 shrink-0 text-muted">{c.label}</span>
                                    {c.available ? (<><div className="h-1.5 flex-1 overflow-hidden rounded-full bg-line"><div className="h-full bg-accent" style={{ width: `${(c.score ?? 0) * 100}%` }} /></div><span className="num w-20 text-right">{num(c.raw, 2)}</span><span className="num w-14 text-right text-accent">+{c.points.toFixed(1)}</span></>) : <span className="italic text-muted">unavailable - excluded from score</span>}
                                  </li>
                                ))}
                              </ul>
                            </div>
                            <div className="space-y-2 text-xs">
                              {r.reasons.length > 0 && <div><div className="mb-1 font-semibold uppercase tracking-wide text-muted">Reasons</div><ul className="list-disc pl-4">{r.reasons.map((x) => <li key={x}>{x}</li>)}</ul></div>}
                              {r.warnings.length > 0 && <div><div className="mb-1 font-semibold uppercase tracking-wide text-warn">Cautions</div><ul className="list-disc pl-4">{r.warnings.map((x) => <li key={x}>{x}</li>)}</ul></div>}
                              {r.blockers.length > 0 && <div><div className="mb-1 font-semibold uppercase tracking-wide text-down">Filtered out because</div><ul className="list-disc pl-4">{r.blockers.map((x) => <li key={x}>{x}</li>)}</ul></div>}
                            </div>
                          </div>
                        </td></tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
