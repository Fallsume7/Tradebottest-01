"""MarketWatch stock eligibility.

The game supports only a subset of U.S. stocks. A ticker is tradable *for this bot* only if somebody
(you, or a future authorised verifier) has explicitly confirmed it. Existing on Nasdaq/NYSE, or having
market data from any provider, proves nothing about eligibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

EPOCH = date(2000, 1, 1)  # "effective since forever" for lists that carry no dates


class EligibilityStatus(StrEnum):
    CONFIRMED_TRADABLE = "confirmed_tradable"
    CONFIRMED_UNAVAILABLE = "confirmed_unavailable"
    UNVERIFIED = "unverified"

    @property
    def label(self) -> str:
        return {
            "confirmed_tradable": "Confirmed tradable",
            "confirmed_unavailable": "Confirmed unavailable",
            "unverified": "Unverified",
        }[self.value]


@dataclass(slots=True)
class EligibilityRecord:
    """One dated statement about a ticker. The list is append-only history; the latest record on or
    before a date is the status *as of* that date."""

    ticker: str
    status: EligibilityStatus
    effective_from: date = EPOCH
    verified_on: date | None = None
    name: str = ""
    exchange: str = ""
    security_type: str = ""
    rejection_reason: str = ""
    restrictions: str = ""  # free text, e.g. "max 5,000 shares", "no short selling"
    source: str = "manual"  # manual | csv | demo-seed | verifier:<name> | default

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "status": str(self.status),
            "status_label": self.status.label,
            "effective_from": self.effective_from.isoformat(),
            "verified_on": self.verified_on.isoformat() if self.verified_on else None,
            "name": self.name,
            "exchange": self.exchange,
            "security_type": self.security_type,
            "rejection_reason": self.rejection_reason,
            "restrictions": self.restrictions,
            "source": self.source,
        }


def unverified(ticker: str) -> EligibilityRecord:
    return EligibilityRecord(ticker=ticker.upper(), status=EligibilityStatus.UNVERIFIED, source="default")


@dataclass(slots=True)
class RuleViolation:
    code: str
    message: str


@dataclass(slots=True)
class EligibilityCheck:
    """What the scanner/bot needs to know about a ticker on a given date."""

    record: EligibilityRecord
    violations: list[RuleViolation] = field(default_factory=list)
    applied: bool = True  # False => the eligibility layer was switched off for this run (labelled everywhere)

    @property
    def status(self) -> EligibilityStatus:
        return self.record.status

    @property
    def confirmed(self) -> bool:
        return self.record.status == EligibilityStatus.CONFIRMED_TRADABLE

    @property
    def actionable(self) -> bool:
        """Safe to open a NEW position: confirmed tradable AND no game-rule violation."""
        return self.confirmed and not self.violations

    def to_dict(self) -> dict[str, Any]:
        d = self.record.to_dict()
        d["violations"] = [v.message for v in self.violations]
        d["applied"] = self.applied
        d["actionable"] = self.actionable
        return d
