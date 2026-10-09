"""Common strategy interface.

A strategy never sees future data: it receives a ``SignalInput`` built from a ``StockContext``
(complete bars only) and returns an ``Evaluation`` that lists every entry condition with its
pass/fail state - so the dashboard can explain *why* a symbol was or was not bought.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pandas as pd
from pydantic import BaseModel

from mwbot.domain import Position, StockContext
from mwbot.scanner.metrics import StockMetrics
from mwbot.scanner.scanner import ScanResult


@dataclass(slots=True)
class Condition:
    label: str
    passed: bool
    detail: str = ""

    def __post_init__(self) -> None:
        self.passed = bool(self.passed)  # numpy bools are not JSON-serialisable

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "passed": self.passed, "detail": self.detail}


@dataclass(slots=True)
class SignalInput:
    ctx: StockContext
    metrics: StockMetrics
    scan: ScanResult | None
    minutes_since_open: float
    opportunity: Any = None  # forecast.ranking.Opportunity for the predictive strategy

    def _split(self) -> int:
        return int(self.ctx.bars.index.searchsorted(pd.Timestamp(self.ctx.session.open_utc), side="left"))

    @property
    def regular_bars(self) -> pd.DataFrame:
        return self.ctx.bars.iloc[self._split() :]

    @property
    def premarket_bars(self) -> pd.DataFrame:
        return self.ctx.bars.iloc[: self._split()]


@dataclass(slots=True)
class Evaluation:
    strategy: str
    symbol: str
    passed: bool
    strength: float  # 0..1, used to rank competing entries
    conditions: list[Condition]
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    trailing_stop_pct: float | None = None
    max_hold_minutes: int | None = None
    reference: dict[str, float] = field(default_factory=dict)
    plan: dict[str, Any] = field(
        default_factory=dict
    )  # trade plan (predictive strategy): target, stop, deadline...
    forecast: dict[str, Any] = field(default_factory=dict)

    def explanation(self) -> str:
        ok = [c for c in self.conditions if c.passed]
        return "; ".join(f"{c.label}: {c.detail}" if c.detail else c.label for c in ok)

    def failed(self) -> list[Condition]:
        return [c for c in self.conditions if not c.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "passed": self.passed,
            "strength": round(self.strength, 3),
            "conditions": [c.to_dict() for c in self.conditions],
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "trailing_stop_pct": self.trailing_stop_pct,
            "max_hold_minutes": self.max_hold_minutes,
            "reference": self.reference,
        }


@dataclass(slots=True)
class ExitDecision:
    reason: str


class Strategy(ABC):
    id: ClassVar[str]
    name: ClassVar[str]
    summary: ClassVar[str]
    entry_rules: ClassVar[list[str]]
    exit_rules: ClassVar[list[str]]
    risk_rules: ClassVar[list[str]]
    ParamsModel: ClassVar[type[BaseModel]]

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = self.ParamsModel(**(params or {}))

    @abstractmethod
    def evaluate_entry(self, inp: SignalInput) -> Evaluation: ...

    @abstractmethod
    def evaluate_exit(self, inp: SignalInput, position: Position) -> ExitDecision | None:
        """Strategy-specific discretionary exit. Hard risk exits (stop, target, EOD) live in the bot."""

    # ---- shared helpers
    def _in_window(self, inp: SignalInput, start: float, end: float) -> Condition:
        m = inp.minutes_since_open
        return Condition(
            f"Entry window ({start:g}-{end:g} min after open)", start <= m <= end, f"{m:.0f} min since open"
        )

    def _eval(self, inp: SignalInput, conds: list[Condition], strength: float) -> Evaluation:
        p: Any = self.params
        return Evaluation(
            strategy=self.id,
            symbol=inp.ctx.symbol,
            passed=all(c.passed for c in conds),
            strength=max(0.0, min(1.0, strength)),
            conditions=conds,
            stop_loss_pct=getattr(p, "stop_loss_pct", None),
            take_profit_pct=getattr(p, "take_profit_pct", None),
            trailing_stop_pct=getattr(p, "trailing_stop_pct", None),
            max_hold_minutes=getattr(p, "max_hold_minutes", None),
        )

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "summary": self.summary,
            "entry_rules": self.entry_rules,
            "exit_rules": self.exit_rules,
            "risk_rules": self.risk_rules,
            "params_schema": self.ParamsModel.model_json_schema()["properties"],
            "params": self.params.model_dump(),
            "disclaimer": "Illustrative rules. Not a guarantee of profit; past/simulated results do not predict future results.",
        }


def held_minutes(position: Position, inp: SignalInput) -> float:
    return (inp.ctx.as_of - position.opened_at).total_seconds() / 60.0
