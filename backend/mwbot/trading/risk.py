"""Position sizing, hard exit rules and the pre-trade risk gate."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from mwbot.calendar import Phase
from mwbot.config import RiskConfig
from mwbot.domain import Position


class BotState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"  # no new entries; exits still managed
    EMERGENCY_STOP = "emergency_stop"  # flatten, then no orders at all until manually reset
    STOPPED = "stopped"  # idle


def size_position(
    equity: float,
    price: float,
    stop_loss_pct: float,
    cfg: RiskConfig,
    buying_power: float,
    cash_buffer_pct: float = 1.0,
) -> tuple[int, dict[str, int]]:
    """Shares to buy = min(risk-based, allocation-based, cash-based). Returns (qty, breakdown)."""
    if price <= 0 or equity <= 0 or stop_loss_pct <= 0:
        return 0, {"risk": 0, "allocation": 0, "cash": 0}
    risk_amt = equity * cfg.risk_per_trade_pct / 100
    by_risk = math.floor(risk_amt / (price * stop_loss_pct / 100))
    by_alloc = math.floor(equity * cfg.max_position_pct / 100 / price)
    by_cash = math.floor(max(buying_power, 0) / (price * (1 + cash_buffer_pct / 100)))
    return max(min(by_risk, by_alloc, by_cash), 0), {"risk": by_risk, "allocation": by_alloc, "cash": by_cash}


def hard_exit_reason(
    position: Position, bar_high: float, bar_low: float, last: float, now: datetime
) -> str | None:
    """Stop / target / trailing / time-stop check against the latest *complete* bar's extremes.

    The triggered exit is later filled by a market order on the NEXT bar, so gaps through a stop
    produce realistic slippage rather than a fill at the stop price.
    """
    if position.stop_price is not None and bar_low <= position.stop_price:
        return f"Stop loss hit (low {bar_low:.4f} <= stop {position.stop_price:.4f})"
    if position.trailing_stop_pct:
        trail = position.high_water_mark * (1 - position.trailing_stop_pct / 100)
        if position.high_water_mark > position.avg_cost and bar_low <= trail:
            return f"Trailing stop hit (low {bar_low:.4f} <= {trail:.4f}, HWM {position.high_water_mark:.4f})"
    if position.take_profit_price is not None and bar_high >= position.take_profit_price:
        return f"Profit target reached (high {bar_high:.4f} >= {position.take_profit_price:.4f})"
    if position.max_hold_minutes is not None:
        held = (now - position.opened_at).total_seconds() / 60
        if held >= position.max_hold_minutes:
            return f"Time stop: held {held:.0f} min (max {position.max_hold_minutes})"
    return None


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    quantity: int = 0
    reasons: list[str] = field(default_factory=list)
    sizing: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class DayStats:
    start_equity: float
    trades_opened: int = 0
    stopped_symbols: set[str] = field(default_factory=set)
    entered_keys: set[tuple[str, str]] = field(default_factory=set)  # (strategy, symbol)


class RiskManager:
    def __init__(self, cfg: RiskConfig) -> None:
        self.cfg = cfg

    def daily_loss_breached(self, equity: float, start_equity: float) -> bool:
        return equity <= start_equity * (1 - self.cfg.max_daily_loss_pct / 100)

    def check_entry(
        self,
        *,
        state: BotState,
        phase: Phase,
        symbol: str,
        strategy: str,
        price: float,
        stop_loss_pct: float,
        equity: float,
        buying_power: float,
        positions: dict[str, Position],
        pending_buy_symbols: set[str],
        stats: DayStats,
        feed_age_seconds: float,
        feed_allows_trading: bool,
        feed_block_reason: str = "",
    ) -> RiskDecision:
        reasons: list[str] = []
        if state != BotState.RUNNING:
            reasons.append(f"bot is {state.value}")
        if phase != Phase.TRADING:
            reasons.append(f"phase is {phase.value} (entries only allowed during trading phase)")
        if not feed_allows_trading:
            reasons.append(feed_block_reason or "data feed does not allow trading")
        if feed_age_seconds > self.cfg.max_data_age_seconds:
            reasons.append(
                f"market data stale ({feed_age_seconds:.0f}s > {self.cfg.max_data_age_seconds:.0f}s)"
            )
        if self.daily_loss_breached(equity, stats.start_equity):
            reasons.append(f"daily loss limit {self.cfg.max_daily_loss_pct:g}% reached")
        if symbol in positions or symbol in pending_buy_symbols:
            reasons.append("already holding or buying this symbol")
        if len(positions) + len(pending_buy_symbols - set(positions)) >= self.cfg.max_positions:
            reasons.append(f"max simultaneous positions ({self.cfg.max_positions}) reached")
        if stats.trades_opened >= self.cfg.max_trades_per_day:
            reasons.append(f"max trades per day ({self.cfg.max_trades_per_day}) reached")
        if self.cfg.block_reentry_after_loss and symbol in stats.stopped_symbols:
            reasons.append("symbol already lost money today (re-entry blocked)")
        if (strategy, symbol) in stats.entered_keys:
            reasons.append("already traded this symbol with this strategy today")
        if price <= 0:
            reasons.append("invalid price")
        if reasons:
            return RiskDecision(False, 0, reasons)
        qty, breakdown = size_position(equity, price, stop_loss_pct, self.cfg, buying_power)
        if qty < 1:
            return RiskDecision(False, 0, [f"position size rounds to zero {breakdown}"], breakdown)
        return RiskDecision(True, qty, [], breakdown)
