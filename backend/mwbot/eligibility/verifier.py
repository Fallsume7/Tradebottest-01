"""Interface for a FUTURE, AUTHORISED MarketWatch eligibility verifier (local integration phase).

Nothing here talks to MarketWatch. A real verifier would, with the user's permission and while the user
is logged in on their own machine, check whether a ticker can actually be found/ordered in *their* game
and report the result. The cloud build ships only:

* ``EligibilityVerifier``     - the interface;
* ``MarketWatchVerifier``     - a skeleton that refuses to run (documented in MARKETWATCH_INTEGRATION.md);
* ``MockSiteVerifier``        - a test double that asks the bundled MOCK trading site.

``apply_verification`` is the only place verification results become eligibility records, so the audit
trail (source, date, reason) is uniform whichever verifier produced them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import ClassVar

from mwbot.eligibility.models import EligibilityRecord, EligibilityStatus


class VerifierNotConfiguredError(Exception):
    pass


@dataclass(slots=True)
class VerificationResult:
    ticker: str
    status: EligibilityStatus  # UNVERIFIED means "could not determine" (never guess)
    reason: str = ""
    name: str = ""
    exchange: str = ""
    restrictions: str = ""


class EligibilityVerifier(ABC):
    name: ClassVar[str]

    @abstractmethod
    def verify(self, ticker: str) -> VerificationResult: ...

    def verify_many(self, tickers: Iterable[str]) -> list[VerificationResult]:
        return [self.verify(t) for t in tickers]

    def close(self) -> None:  # noqa: B027 - optional hook
        pass


class MarketWatchVerifier(EligibilityVerifier):
    """SKELETON. Not implemented on purpose - see docs/MARKETWATCH_INTEGRATION.md section 7."""

    name: ClassVar[str] = "marketwatch"

    def verify(self, ticker: str) -> VerificationResult:
        raise VerifierNotConfiguredError(
            "MarketWatch eligibility verification is not implemented. It needs your permission, your logged-in "
            "session on your own computer, and page research - see docs/MARKETWATCH_INTEGRATION.md. "
            "Until then maintain the list by hand (Settings -> MarketWatch Game) or via CSV import."
        )


class MockSiteVerifier(EligibilityVerifier):
    """Asks the bundled MOCK trading site whether it knows a ticker (test double for the real thing)."""

    name: ClassVar[str] = "mock-site"

    def __init__(self, adapter: object) -> None:
        self._adapter = adapter

    def verify(self, ticker: str) -> VerificationResult:
        from mwbot.domain import OrderRequest, Side
        from mwbot.execution.base import ExecutionError

        try:
            self._adapter.prepare_order(OrderRequest(f"verify-{ticker}", ticker, Side.BUY, 1))  # type: ignore[attr-defined]
        except ExecutionError as exc:
            msg = str(exc)
            if "Unknown ticker" in msg or "not available" in msg:
                return VerificationResult(ticker, EligibilityStatus.CONFIRMED_UNAVAILABLE, reason=msg)
            return VerificationResult(ticker, EligibilityStatus.UNVERIFIED, reason=f"could not verify: {msg}")
        return VerificationResult(ticker, EligibilityStatus.CONFIRMED_TRADABLE)


def apply_verification(
    results: Iterable[VerificationResult], source: str, today: date
) -> list[EligibilityRecord]:
    """Turn verification results into records. UNVERIFIED results are NOT recorded (they carry no information)."""
    out = []
    for r in results:
        if r.status == EligibilityStatus.UNVERIFIED:
            continue
        out.append(
            EligibilityRecord(
                ticker=r.ticker.upper(),
                status=r.status,
                effective_from=today,
                verified_on=today,
                name=r.name,
                exchange=r.exchange,
                rejection_reason=r.reason if r.status == EligibilityStatus.CONFIRMED_UNAVAILABLE else "",
                restrictions=r.restrictions,
                source=f"verifier:{source}",
            )
        )
    return out
