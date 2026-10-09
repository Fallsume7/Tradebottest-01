import { Bar, BarChart, CartesianGrid, Legend, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api";
import { EquityChart } from "../components/charts";
import { Card, Empty, ErrorBanner, PnL, Stat } from "../components/ui";
import { money, num, pct, signedMoney } from "../format";
import { usePoll } from "../hooks";

export default function Performance() {
  const { data: perf, error } = usePoll(api.performance, 6000);
  const { data: equity } = usePoll(api.equity, 6000);
  if (!perf) return <Empty>{error ?? "Loading..."}</Empty>;
  const ts = perf.trade_stats;
  const points = (equity ?? []).map((e) => ({ time: Math.floor(new Date(e.ts).getTime() / 1000), value: e.equity }));
  const daily = perf.daily.map((d) => ({ date: d.date.slice(5), Bot: d.pnl_pct, Benchmark: d.benchmark_pct ?? 0 }));
  const css = (n: string) => getComputedStyle(document.documentElement).getPropertyValue(n).trim() || "#888";

  return (
    <div className="space-y-4">
      <ErrorBanner message={error} />
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Total return" value={pct(perf.total_return_pct)} valueClass={perf.total_return_pct >= 0 ? "text-up" : "text-down"} sub={`${money(perf.equity, 0)} equity`} />
        <Stat label="Benchmark" value={pct(perf.benchmark_return_pct)} sub="equal-weight universe, open→close" />
        <Stat label="Max drawdown" value={pct(-perf.max_drawdown_pct, 2, false)} valueClass="text-down" sub={money(perf.max_drawdown_usd, 0)} />
        <Stat label="Win rate" value={ts.win_rate === null ? "-" : `${(ts.win_rate * 100).toFixed(0)}%`} sub={`${ts.n_trades} trades`} />
        <Stat label="Avg win / loss" value={`${money(ts.avg_win, 0)} / ${money(ts.avg_loss, 0)}`} sub={ts.payoff_ratio ? `payoff ${num(ts.payoff_ratio)}` : undefined} />
        <Stat label="Profit factor" value={ts.profit_factor === null ? "-" : num(ts.profit_factor)} />
        <Stat label="Expectancy / trade" value={signedMoney(ts.expectancy)} valueClass={(ts.expectancy ?? 0) >= 0 ? "text-up" : "text-down"} />
        <Stat label="Sharpe" value={perf.daily_stats.sharpe === null ? "withheld" : num(perf.daily_stats.sharpe)} sub={`${perf.daily_stats.n_days} day(s)`} />
      </div>
      {perf.daily_stats.ratios_note && <p className="text-xs text-muted">{perf.daily_stats.ratios_note}</p>}

      <Card title="Equity curve">{points.length > 1 ? <EquityChart points={points} baseline={perf.starting_capital} height={300} /> : <Empty>Run a session to build the curve.</Empty>}</Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Daily returns vs benchmark (%)">
          {daily.length === 0 ? <Empty>No completed days yet.</Empty> : (
            <div className="h-64" data-testid="daily-returns">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={daily} margin={{ left: -10, right: 8 }}>
                  <CartesianGrid stroke={css("--border")} strokeDasharray="3 3" vertical={false} />
                  <XAxis dataKey="date" stroke={css("--muted")} fontSize={11} />
                  <YAxis stroke={css("--muted")} fontSize={11} unit="%" />
                  <Tooltip contentStyle={{ background: css("--panel"), border: `1px solid ${css("--border")}`, borderRadius: 8, fontSize: 12 }} formatter={(v: number) => `${v.toFixed(2)}%`} />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  <ReferenceLine y={0} stroke={css("--muted")} />
                  <Bar dataKey="Bot" fill={css("--accent")} radius={[3, 3, 0, 0]} />
                  <Bar dataKey="Benchmark" fill={css("--muted")} radius={[3, 3, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
          <p className="mt-2 text-[11px] text-muted">{perf.benchmark_note}</p>
        </Card>

        <Card title="Performance by strategy" pad={false}>
          {Object.keys(perf.by_strategy).length === 0 ? <Empty>No closed trades yet.</Empty> : (
            <table className="w-full text-[13px]">
              <thead className="text-left text-[11px] uppercase text-muted"><tr><th className="px-4 py-2">Strategy</th><th className="text-right">Trades</th><th className="text-right">Win</th><th className="text-right">PF</th><th className="pr-4 text-right">Net P/L</th></tr></thead>
              <tbody>
                {Object.entries(perf.by_strategy).map(([k, s]) => (
                  <tr key={k} className="border-t border-line"><td className="px-4 py-2">{k.replace(/_/g, " ")}</td><td className="num text-right">{s.n_trades}</td>
                    <td className="num text-right">{s.win_rate === null ? "-" : `${(s.win_rate * 100).toFixed(0)}%`}</td><td className="num text-right">{s.profit_factor === null ? "-" : num(s.profit_factor)}</td>
                    <td className="pr-4 text-right"><PnL value={s.net_pnl} text={signedMoney(s.net_pnl)} /></td></tr>
                ))}
              </tbody>
            </table>
          )}
          {ts.n_trades > 0 && ts.n_trades < 30 && <p className="px-4 pb-3 text-[11px] text-warn">Only {ts.n_trades} trades: these statistics are not statistically reliable yet.</p>}
        </Card>
      </div>
    </div>
  );
}
