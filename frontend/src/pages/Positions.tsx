import { useState } from "react";
import { useApp } from "../App";
import { api } from "../api";
import { Badge, Button, Card, Empty, ErrorBanner, PnL } from "../components/ui";
import { chicagoTime, money, pct, signedMoney } from "../format";
import { usePoll } from "../hooks";
import type { Order } from "../types";

const STATUS_COLOR: Record<Order["status"], "up" | "down" | "warn" | "info" | "muted"> = {
  filled: "up", partially_filled: "info", pending: "warn", new: "muted", canceled: "muted", rejected: "down", expired: "down",
};

export default function Positions() {
  const { status, reloadStatus, go, select } = useApp();
  const running = status?.session_status === "running";
  const poll = running ? 2000 : 5000;
  const { data: positions, reload: rp, error } = usePoll(api.positions, poll, [status?.sim_time_utc]);
  const { data: orders, reload: ro } = usePoll(api.orders, poll, [status?.sim_time_utc]);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const doit = async (fn: () => Promise<unknown>) => {
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
    rp(); ro(); reloadStatus();
  };
  const pending = (orders ?? []).filter((o) => o.status === "pending" || o.status === "partially_filled");

  return (
    <div className="space-y-4">
      <ErrorBanner message={err ?? error} onClose={() => setErr(null)} />
      <Card title={`Open positions (${positions?.length ?? 0})`} pad={false}>
        {!positions || positions.length === 0 ? <Empty>No open virtual positions.</Empty> : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[900px] text-[13px]">
              <thead className="text-left text-[11px] uppercase text-muted"><tr>
                <th className="px-4 py-2">Symbol</th><th>Strategy</th><th className="text-right">Qty</th><th className="text-right">Entry</th><th className="text-right">Last</th>
                <th className="text-right">Value</th><th className="text-right">Unrealized P/L</th><th className="px-3">Exit plan</th><th>Exit order</th><th className="pr-4" />
              </tr></thead>
              <tbody>
                {positions.map((p) => (
                  <tr key={p.symbol} className="border-t border-line align-top hover:bg-panel2" data-testid={`pos-${p.symbol}`}>
                    <td className="px-4 py-2"><button className="font-semibold text-accent hover:underline" onClick={() => { select(p.symbol); go("stock"); }}>{p.symbol}</button><div className="text-[11px] text-muted">since {chicagoTime(p.opened_at)}</div></td>
                    <td className="text-xs">{p.strategy.replace(/_/g, " ")}<div className="max-w-[240px] truncate text-[11px] text-muted" title={p.entry_reason}>{p.entry_reason}</div></td>
                    <td className="num text-right">{p.quantity}</td>
                    <td className="num text-right">{money(p.avg_cost, 4)}</td>
                    <td className="num text-right">{money(p.mark, 4)}</td>
                    <td className="num text-right">{money(p.market_value)}</td>
                    <td className="text-right"><PnL value={p.unrealized_pnl} text={`${signedMoney(p.unrealized_pnl)} (${pct(p.unrealized_pct)})`} /></td>
                    <td className="px-3 text-xs text-muted">stop {p.stop_price ?? "-"}<br />target {p.take_profit_price ?? "-"}<br />trail {p.trailing_stop_pct ?? "-"}% · hold ≤ {p.max_hold_minutes ?? "-"}m</td>
                    <td>{p.exit_order_ids.length ? <Badge color="warn">exit working: {p.exit_order_ids.join(", ")}</Badge> : <Badge color="muted">none</Badge>}</td>
                    <td className="pr-4 text-right"><Button variant="danger" onClick={() => doit(() => api.placeOrder({ symbol: p.symbol, side: "sell", quantity: p.quantity, order_type: "market", client_order_id: `flatten-${p.symbol}-${Date.now()}` }))}>Flatten</Button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {pending.length > 0 && (
        <Card title="Pending orders" pad={false}>
          <ul className="divide-y divide-line text-[13px]">
            {pending.map((o) => (
              <li key={o.id} className="flex items-center gap-3 px-4 py-2">
                <Badge color={STATUS_COLOR[o.status]}>{o.status.replace("_", " ")}</Badge>
                <span className="font-medium">{o.side.toUpperCase()} {o.quantity} {o.symbol}</span>
                <span className="text-xs text-muted">{o.filled_quantity}/{o.quantity} filled · {o.order_type}{o.limit_price ? ` @ ${o.limit_price}` : ""}</span>
                <Button className="ml-auto" variant="ghost" onClick={() => doit(() => api.cancelOrder(o.id))}>Cancel</Button>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card title="Order history" pad={false}>
        {!orders || orders.length === 0 ? <Empty>No orders yet.</Empty> : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-[13px]">
              <thead className="text-left text-[11px] uppercase text-muted"><tr><th className="px-4 py-2">Time</th><th>Order</th><th>Symbol</th><th className="text-right">Qty</th><th className="text-right">Avg fill</th><th>Status</th><th>Strategy / reason</th></tr></thead>
              <tbody>
                {orders.slice(0, 60).map((o) => (
                  <tr key={o.id} className="cursor-pointer border-t border-line align-top hover:bg-panel2" onClick={() => setOpen(open === o.id ? null : o.id)}>
                    <td className="num px-4 py-2 text-xs text-muted">{chicagoTime(o.created_at)}</td>
                    <td className="num text-xs">{o.id}<div className="text-muted">{o.side} {o.order_type}</div></td>
                    <td className="font-medium">{o.symbol}</td>
                    <td className="num text-right">{o.filled_quantity}/{o.quantity}</td>
                    <td className="num text-right">{o.avg_fill_price ? money(o.avg_fill_price, 4) : "-"}</td>
                    <td><Badge color={STATUS_COLOR[o.status]}>{o.status.replace("_", " ")}</Badge>{o.external_id && <div className="text-[10px] text-muted">venue: {o.external_id}</div>}</td>
                    <td className="max-w-[360px] text-xs text-muted">
                      {o.strategy.replace(/_/g, " ")} - {o.reject_reason || o.reason}
                      {open === o.id && <ol className="mt-1 list-decimal pl-4">{o.history.map(([t, s, n], i) => <li key={i}>{chicagoTime(t)} {s}{n ? ` - ${n}` : ""}</li>)}</ol>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
