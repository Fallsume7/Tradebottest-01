"""CSV import/export for the eligibility list (maintained by hand; no MarketWatch access needed)."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date

from mwbot.eligibility.models import EPOCH, EligibilityRecord, EligibilityStatus

COLUMNS = [
    "ticker",
    "status",
    "name",
    "exchange",
    "security_type",
    "verified_on",
    "effective_from",
    "rejection_reason",
    "restrictions",
    "source",
]
_ALIASES = {
    "symbol": "ticker",
    "company": "name",
    "company_name": "name",
    "market": "exchange",
    "type": "security_type",
    "verification_date": "verified_on",
    "verified": "verified_on",
    "reason": "rejection_reason",
    "rejected_reason": "rejection_reason",
    "rules": "restrictions",
    "game_restrictions": "restrictions",
    "since": "effective_from",
}
_STATUS = {
    "confirmed_tradable": EligibilityStatus.CONFIRMED_TRADABLE,
    "tradable": EligibilityStatus.CONFIRMED_TRADABLE,
    "available": EligibilityStatus.CONFIRMED_TRADABLE,
    "yes": EligibilityStatus.CONFIRMED_TRADABLE,
    "y": EligibilityStatus.CONFIRMED_TRADABLE,
    "confirmed_unavailable": EligibilityStatus.CONFIRMED_UNAVAILABLE,
    "unavailable": EligibilityStatus.CONFIRMED_UNAVAILABLE,
    "no": EligibilityStatus.CONFIRMED_UNAVAILABLE,
    "n": EligibilityStatus.CONFIRMED_UNAVAILABLE,
    "rejected": EligibilityStatus.CONFIRMED_UNAVAILABLE,
    "unverified": EligibilityStatus.UNVERIFIED,
    "unknown": EligibilityStatus.UNVERIFIED,
}


@dataclass(slots=True)
class ImportResult:
    records: list[EligibilityRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # "line N: reason"

    @property
    def ok(self) -> bool:
        return not self.errors


def _date(v: str, line: int, col: str, errors: list[str]) -> date | None:
    v = v.strip()
    if not v:
        return None
    try:
        return date.fromisoformat(v)
    except ValueError:
        errors.append(f"line {line}: {col} {v!r} is not a YYYY-MM-DD date")
        return None


def parse_csv(text: str, *, default_source: str = "csv", today: date | None = None) -> ImportResult:
    """Parse a CSV string. Bad rows are reported (with line numbers) and skipped; good rows are kept."""
    res = ImportResult()
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if reader.fieldnames is None:
        res.errors.append("empty file")
        return res
    mapping = {}
    for f in reader.fieldnames:
        key = f.strip().lower().replace(" ", "_")
        mapping[f] = _ALIASES.get(key, key)
    if "ticker" not in mapping.values() or "status" not in mapping.values():
        res.errors.append("header must contain at least 'ticker' and 'status' columns")
        return res
    seen: set[tuple[str, date]] = set()
    for i, raw in enumerate(reader, start=2):
        row = {mapping[k]: (v or "").strip() for k, v in raw.items() if k is not None}
        if not any(row.values()):
            continue
        errs_before = len(res.errors)
        ticker = row.get("ticker", "").upper()
        if not ticker or not all(c.isalnum() or c in ".-" for c in ticker) or len(ticker) > 10:
            res.errors.append(f"line {i}: invalid ticker {row.get('ticker')!r}")
            continue
        st = _STATUS.get(row.get("status", "").lower().replace(" ", "_"))
        if st is None:
            res.errors.append(
                f"line {i}: status {row.get('status')!r} must be confirmed_tradable, confirmed_unavailable or unverified"
            )
            continue
        verified = _date(row.get("verified_on", ""), i, "verified_on", res.errors)
        eff = _date(row.get("effective_from", ""), i, "effective_from", res.errors)
        if len(res.errors) > errs_before:
            continue
        if st == EligibilityStatus.CONFIRMED_UNAVAILABLE and not row.get("rejection_reason"):
            row["rejection_reason"] = "marked unavailable (no reason given)"
        effective = eff or verified or EPOCH
        if (ticker, effective) in seen:
            res.errors.append(f"line {i}: duplicate row for {ticker} effective {effective}")
            continue
        seen.add((ticker, effective))
        if st != EligibilityStatus.UNVERIFIED and verified is None:
            verified = today
        res.records.append(
            EligibilityRecord(
                ticker=ticker,
                status=st,
                effective_from=effective,
                verified_on=verified,
                name=row.get("name", ""),
                exchange=row.get("exchange", "").upper()
                if row.get("exchange", "").islower()
                else row.get("exchange", ""),
                security_type=row.get("security_type", ""),
                rejection_reason=row.get("rejection_reason", ""),
                restrictions=row.get("restrictions", ""),
                source=row.get("source") or default_source,
            )
        )
    return res


def export_csv(records: list[EligibilityRecord]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    w.writeheader()
    for r in sorted(records, key=lambda r: (r.ticker, r.effective_from)):
        d = r.to_dict()
        w.writerow({c: ("" if d.get(c) is None else d.get(c)) for c in COLUMNS} | {"status": str(r.status)})
    return buf.getvalue()
