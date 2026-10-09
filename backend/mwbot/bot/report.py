"""Daily performance report (dict for JSON/DB + markdown rendering)."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mwbot.bot.engine import TradingBot


def build_daily_report(
    bot: TradingBot,
    session_date: str,
    unresolved: list[dict[str, Any]],
    reconciliation: dict[str, Any] | None,
) -> dict[str, Any]:
    assert bot.stats is not None
    b = bot.broker
    start_eq = bot.stats.start_equity
    end_eq = b.equity()
    todays = [t for t in b.trades if t.session_date == session_date]
    wins = [t for t in todays if t.net_pnl > 0]
    by_strategy: dict[str, dict[str, float]] = {}
    for t in todays:
        s = by_strategy.setdefault(t.strategy, {"trades": 0, "net_pnl": 0.0, "wins": 0})
        s["trades"] += 1
        s["net_pnl"] += t.net_pnl
        s["wins"] += int(t.net_pnl > 0)
    status_counts = Counter(str(o.status) for o in b.orders.values() if o.meta.get("session") == session_date)
    return {
        "date": session_date,
        "start_equity": round(start_eq, 2),
        "end_equity": round(end_eq, 2),
        "pnl": round(end_eq - start_eq, 2),
        "pnl_pct": round((end_eq / start_eq - 1) * 100, 4) if start_eq else 0.0,
        "realized_pnl": round(sum(t.net_pnl for t in todays), 2),
        "unrealized_pnl": round(b.unrealized_pnl(), 2),
        "commissions": round(sum(t.commissions for t in todays), 2),
        "n_trades": len(todays),
        "win_rate": round(len(wins) / len(todays), 4) if todays else None,
        "best_trade": max((t.to_dict() for t in todays), key=lambda d: d["net_pnl"], default=None),
        "worst_trade": min((t.to_dict() for t in todays), key=lambda d: d["net_pnl"], default=None),
        "by_strategy": {k: {**v, "net_pnl": round(v["net_pnl"], 2)} for k, v in by_strategy.items()},
        "trades": [t.to_dict() for t in todays],
        "unresolved_positions": unresolved,
        "liquidation_complete": not unresolved,
        "order_status_counts": dict(status_counts),
        "alerts": [a for a in bot.alerts if a["ts"][:10] >= session_date][-50:],
        "reconciliation": reconciliation,
        "disclaimer": "Simulated trading result. Not investment advice; no guarantee of future results.",
    }


def report_markdown(r: dict[str, Any]) -> str:
    lines = [
        f"# Daily report - {r['date']}",
        "",
        f"- Start equity: ${r['start_equity']:,.2f}",
        f"- End equity: ${r['end_equity']:,.2f}",
        f"- P/L: ${r['pnl']:+,.2f} ({r['pnl_pct']:+.2f}%)",
        f"- Trades: {r['n_trades']}"
        + (f" (win rate {r['win_rate'] * 100:.0f}%)" if r["win_rate"] is not None else ""),
        f"- Liquidation complete: {'yes' if r['liquidation_complete'] else 'NO - see unresolved positions'}",
        "",
    ]
    if r["unresolved_positions"]:
        lines += ["## Unresolved positions", ""]
        lines += [
            f"- {u['symbol']} x{u['quantity']} (unrealized {u['unrealized_pnl']:+.2f})"
            for u in r["unresolved_positions"]
        ]
        lines.append("")
    lines += [
        "## Trades",
        "",
        "| Symbol | Strategy | Qty | Entry | Exit | Net P/L | Exit reason |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in r["trades"]:
        lines.append(
            f"| {t['symbol']} | {t['strategy']} | {t['quantity']} | {t['entry_price']} | {t['exit_price']} | {t['net_pnl']:+.2f} | {t['exit_reason']} |"
        )
    lines += ["", f"_{r['disclaimer']}_", ""]
    return "\n".join(lines)
