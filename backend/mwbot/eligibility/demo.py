"""DEMO-ONLY eligibility seed for the synthetic market.

The synthetic tickers are invented, so no real list can describe them. To let the demo show all three
states, this deterministically labels the synthetic pool: most tickers confirmed tradable, some confirmed
unavailable (with plausible rejection reasons), the rest left unverified. Every record has
``source="demo-seed"``; the dashboard says so. Real use: replace it by maintaining your own list.
"""

from __future__ import annotations

import hashlib
from datetime import date

from mwbot.data.synthetic import SYMBOL_POOL
from mwbot.eligibility.models import EPOCH, EligibilityRecord, EligibilityStatus

_EXCHANGES = ["NASDAQ", "NYSE", "NASDAQ", "NYSE American"]
_REASONS = [
    "Not offered in this game (demo example: OTC / foreign listing)",
    "Price below the game's minimum (demo example)",
    "Restricted security in this game (demo example)",
]


def _h(ticker: str) -> int:
    return int(hashlib.sha256(f"demo-eligibility|{ticker}".encode()).hexdigest()[:8], 16)


def demo_seed(today: date | None = None) -> list[EligibilityRecord]:
    out: list[EligibilityRecord] = []
    for ticker, name in SYMBOL_POOL:
        h = _h(ticker)
        bucket = h % 100
        if bucket >= 82:
            continue  # no record => UNVERIFIED (the honest default)
        ok = bucket < 66
        out.append(
            EligibilityRecord(
                ticker=ticker,
                status=EligibilityStatus.CONFIRMED_TRADABLE
                if ok
                else EligibilityStatus.CONFIRMED_UNAVAILABLE,
                effective_from=EPOCH,
                verified_on=today,
                name=f"{name} (synthetic)",
                exchange=_EXCHANGES[(h >> 8) % len(_EXCHANGES)],
                security_type="common_stock",
                rejection_reason="" if ok else _REASONS[(h >> 16) % len(_REASONS)],
                source="demo-seed",
            )
        )
    return out


def dataset_eligibility(symbols: list[str], dates: list[date]) -> list[EligibilityRecord]:
    """DATED synthetic eligibility history for a sample dataset (for testing point-in-time handling).

    Base statuses follow ``demo_seed``. Then two deliberate changes in the middle of the period:
    one confirmed ticker becomes UNAVAILABLE and one previously-unverified ticker becomes TRADABLE.
    """
    base = {r.ticker: r for r in demo_seed(None)}
    out = [base[s] for s in symbols if s in base]
    if len(dates) < 6:
        return out
    tradable = sorted(r.ticker for r in out if r.status == EligibilityStatus.CONFIRMED_TRADABLE)
    unknown = sorted(s for s in symbols if s not in base)
    names = dict(SYMBOL_POOL)
    if tradable:
        t = tradable[len(tradable) // 2]
        out.append(
            EligibilityRecord(
                t,
                EligibilityStatus.CONFIRMED_UNAVAILABLE,
                effective_from=dates[5],
                verified_on=dates[5],
                name=base[t].name,
                exchange=base[t].exchange,
                security_type="common_stock",
                rejection_reason="Removed from the game's list (demo example)",
                source="demo-seed",
            )
        )
    if unknown:
        u = unknown[0]
        out.append(
            EligibilityRecord(
                u,
                EligibilityStatus.CONFIRMED_TRADABLE,
                effective_from=dates[3],
                verified_on=dates[3],
                name=f"{names.get(u, u)} (synthetic)",
                exchange="NASDAQ",
                security_type="common_stock",
                source="demo-seed",
            )
        )
    return out
