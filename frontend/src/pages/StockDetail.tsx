import { useEffect, useState } from "react";
import { useApp } from "../App";
import { api } from "../api";
import { PriceChart } from "../components/charts";
import { Badge, Button, Card, Empty, ErrorBanner, Field, PnL, Select, TextInput } from "../components/ui";
import { compact, money, num, pct, signedMoney, titleCase, tone } from "../format";
import { usePoll } from "../hooks";

export default function StockDetail() {
  const { status, selected, select } = useApp();
  const running = status?.session_status === "running";
  const hasSession = !!status?.session_date && status.session_status !== "idle";
  const { data: scan } = usePoll(api.scanner, running ? 3000 : 8000, [status?.sim_time_utc]);
  const symbols = (scan?.results ?? []).map((r) => r.symbol);
  const symbol = selected ?? symbols[0] ?? null;
  const { data, error } = usePoll(() => (symbol && hasSession ? api.stock(symbol) : Promise.resolve(undefined)), running ? 2000 : 6000, [symbol, status?.sim_time_utc, hasSession]);
  const { reloadStatus } = useApp();

  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [qty, setQty] = useState("100");
  const [type, setType] = useState<"market" | "limit">("market");
  const [limit, setLimit] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  useEffect(() => setMsg(null), [symbol]);

  if (!hasSession) return <Empty>Start a session from the Overview page, then pick a symbol from the Scanner to inspect it here.</Empty>;
  if (!symbol) return <Empty>No symbols in the universe yet.</Empty>;

  const m = data?.scan.metrics;
  const submit = async () => {
    setMsg(null);
    try {
      const r = await api.placeOrder({
        symbol, side, quantity: Number(qty), order_type: type, ...(type === "limit" ? { limit_price: Number(limit) } : {}),
        client_order_id: `ui-${symbol}-${side}-${Date.now()}`,
      });
      setMsg(r.status === "rejected" ? { ok: false, text: `Rejected: ${r.reject_reason}` } : { ok: true, text: `Simulated order ${r.id} accepted (${r.status}). It fills on a later bar.` });
      reloadStatus();
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
    }
  };

  return (
    <div className="space-y-4">
      <ErrorBanner message={error} />
      <div className="flex flex-wrap items-center gap-3">
        <div className="w-40"><Select aria-label="Symbol" value={symbol} onChange={(e) => select(e.target.value)}>{[...new Set([symbol, ...symbols])].map((s) => <option key={s}>{s}</option>)}</Select></div>
        <h1 className="text-lg font-semibold">{data?.symbol} <span className="text-sm font-normal text-muted">{data?.name}</span></h1>
        {data?.halted && <Badge color="down">HALTED</Badge>}
        {data?.scan.continuing && <Badge color="up">momentum continuing</Badge>}
        {data && !data.scan.tradable && <Badge color="muted">fails scanner filters</Badge>}
        {data?.held && <Badge color="info">position open</Badge>}
      </div>

      <div className="grid items-start gap-4 xl:grid-cols-3">
        <Card title="Price (1-min, CT)" className="xl:col-span-2" right={<span className="num text-sm">{money(m?.price, 4)} <span className={tone(m?.pct_change)}>{pct(m?.pct_change)}</span></span>}>
          {data && data.bars.length > 0 ? <PriceChart bars={data.bars} openTime={data.open_time} resetKey={data.symbol} /> : <Empty>No bars yet for this symbol.</Empty>}
        </Card>

        <div className="space-y-4">
          <Card title="Simulated trade ticket">
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-2">
                <Button variant={side === "buy" ? "primary" : "subtle"} onClick={() => setSide("buy")}>Buy</Button>
                <Button variant={side === "sell" ? "danger" : "subtle"} onClick={() => setSide("sell")}>Sell</Button>
              </div>
              <div className="grid grid-cols-2 gap-2">
                <Field label="Shares"><TextInput type="number" min={1} aria-label="Shares" value={qty} onChange={(e) => setQty(e.target.value)} /></Field>
                <Field label="Order type"><Select aria-label="Order type" value={type} onChange={(e) => setType(e.target.value as "market" | "limit")}><option value="market">Market</option><option value="limit">Limit</option></Select></Field>
              </div>
              {type === "limit" && <Field label="Limit price"><TextInput type="number" step="0.01" aria-label="Limit price" value={limit} onChange={(e) => setLimit(e.target.value)} /></Field>}
              <Button variant="primary" className="w-full" onClick={submit} disabled={!Number(qty) || (type === "limit" && !Number(limit))}>Submit simulated {side}</Button>
              {msg && <div role="status" className={`rounded-lg px-3 py-2 text-xs ${msg.ok ? "bg-up/10 text-up" : "bg-down/10 text-down"}`}>{msg.text}</div>}
              <p className="text-[11px] leading-snug text-muted">Virtual money only. Orders fill on a later bar with modelled spread, slippage and liquidity limits - they are not guaranteed to fill.</p>
            </div>
            {data?.position && (
              <div className="mt-3 rounded-lg bg-panel2 p-3 text-xs">
                <div className="mb-1 font-semibold">Open position</div>
                <div className="flex justify-between"><span>{data.position.quantity} @ {money(data.position.avg_cost, 4)}</span><PnL value={data.position.unrealized_pnl} text={signedMoney(data.position.unrealized_pnl)} /></div>
                <div className="mt-1 text-muted">stop {data.position.stop_price ?? "-"} · target {data.position.take_profit_price ?? "-"} · trail {data.position.trailing_stop_pct ?? "-"}%</div>
              </div>
            )}
          </Card>

          <Card title="Indicators">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
              {([
                ["VWAP", m?.vwap === null || m?.vwap === undefined ? "-" : money(m.vwap, 4)],
                ["vs VWAP", pct(m?.vs_vwap_pct)],
                ["RVOL (est.)", m?.rvol === null || m?.rvol === undefined ? "-" : `${num(m.rvol, 1)}x`],
                ["Volume", compact(m?.volume)],
                ["5m / 15m momentum", `${pct(m?.momentum_short)} / ${pct(m?.momentum_long)}`],
                ["Off day high", pct(m?.dist_from_high_pct, 1, false)],
                ["Premarket", pct(m?.premarket_pct, 1)],
                ["Gap", pct(m?.gap_pct, 1)],
                ["1m volatility", pct(m?.volatility_pct, 2, false)],
                ["Spread", m?.spread_pct === null || m?.spread_pct === undefined ? "n/a" : pct(m.spread_pct, 2, false)],
                ["Market cap", m?.market_cap ? `$${compact(m.market_cap)}` : "n/a"],
                ["Scanner score", data ? data.scan.score.toFixed(0) : "-"],
              ] as [string, string][]).map(([k, v]) => (<div key={k} className="contents"><dt className="text-muted">{k}</dt><dd className="num text-right">{v}</dd></div>))}
            </dl>
          </Card>
        </div>
      </div>

      <Card title="Strategy signals - entry conditions evaluated right now" pad={false}>
        <div className="grid gap-px bg-line md:grid-cols-2 xl:grid-cols-5">
          {(data?.evaluations ?? []).map((ev) => (
            <div key={ev.strategy} className="bg-panel p-4" data-testid={`eval-${ev.strategy}`}>
              <div className="mb-2 flex items-center justify-between gap-2">
                <span className="text-[13px] font-semibold">{titleCase(ev.strategy)}</span>
                <Badge color={ev.passed ? "up" : "muted"}>{ev.passed ? "ENTRY SIGNAL" : "no signal"}</Badge>
              </div>
              <ul className="space-y-1 text-xs">
                {ev.conditions.map((c) => (
                  <li key={c.label} className="flex gap-1.5" title={c.detail}>
                    <span className={c.passed ? "text-up" : "text-down"} aria-label={c.passed ? "pass" : "fail"}>{c.passed ? "✓" : "✗"}</span>
                    <span className={c.passed ? "" : "text-muted"}>{c.label}<span className="block text-[11px] text-muted/80">{c.detail}</span></span>
                  </li>
                ))}
              </ul>
              <div className="mt-2 border-t border-line pt-2 text-[11px] text-muted">
                Exits: stop {ev.stop_loss_pct ?? "-"}% · target {ev.take_profit_pct ?? "-"}% · trail {ev.trailing_stop_pct ?? "-"}% · max hold {ev.max_hold_minutes ?? "-"}m
              </div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
