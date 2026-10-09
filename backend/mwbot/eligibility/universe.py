"""Point-in-time eligibility lookups + game-rule checks."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable
from datetime import date

from mwbot.config import GameRules
from mwbot.eligibility.models import (
    EligibilityCheck,
    EligibilityRecord,
    EligibilityStatus,
    RuleViolation,
    unverified,
)


class EligibilityUniverse:
    """Dated eligibility history.

    * ``status_on(ticker, d)``: latest record with ``effective_from <= d``; no record => UNVERIFIED.
    * ``as_of=None``: "the list as it stands now" (latest record, whatever its date) - used for live and
      demo sessions, which are always "now" from the bot's point of view. Backtests and historical
      replays pass a date so that today's list never leaks into the past.
    * ``ignore=True`` builds the explicit *not applied* universe used by unit tests and by backtests run
      with ``eligibility_mode="none"``: every ticker passes, and ``applied=False`` is carried into every
      check so results can be labelled.
    """

    def __init__(self, records: Iterable[EligibilityRecord] = (), *, ignore: bool = False) -> None:
        self.ignore = ignore
        self._by_ticker: dict[str, list[EligibilityRecord]] = {}
        for r in records:
            self._by_ticker.setdefault(r.ticker.upper(), []).append(r)
        for lst in self._by_ticker.values():
            lst.sort(key=lambda r: r.effective_from)
        self._keys = {t: [r.effective_from for r in lst] for t, lst in self._by_ticker.items()}

    @classmethod
    def allow_all(cls) -> EligibilityUniverse:
        return cls(ignore=True)

    def __len__(self) -> int:
        return len(self._by_ticker)

    def tickers(self) -> list[str]:
        return sorted(self._by_ticker)

    def record_on(self, ticker: str, as_of: date | None = None) -> EligibilityRecord:
        t = ticker.upper()
        lst = self._by_ticker.get(t)
        if not lst:
            return unverified(t)
        if as_of is None:
            return lst[-1]
        i = bisect_right(self._keys[t], as_of)
        return lst[i - 1] if i else unverified(t)

    def check(
        self,
        ticker: str,
        *,
        price: float | None,
        rules: GameRules | None = None,
        as_of: date | None = None,
        exchange_hint: str = "",
    ) -> EligibilityCheck:
        if self.ignore:
            rec = EligibilityRecord(
                ticker.upper(), EligibilityStatus.CONFIRMED_TRADABLE, source="not-applied"
            )
            return EligibilityCheck(rec, applied=False)
        rec = self.record_on(ticker, as_of)
        return EligibilityCheck(rec, check_rules(rec, price, rules, exchange_hint))

    def history(self, ticker: str) -> list[EligibilityRecord]:
        return list(self._by_ticker.get(ticker.upper(), []))


def check_rules(
    rec: EligibilityRecord, price: float | None, rules: GameRules | None, exchange_hint: str = ""
) -> list[RuleViolation]:
    """Game-rule violations that make a *new* position unacceptable even for a confirmed ticker."""
    if rules is None:
        return []
    out: list[RuleViolation] = []
    if price is not None and price < rules.min_price:
        out.append(
            RuleViolation(
                "min_price", f"price ${price:.2f} is below the game's minimum ${rules.min_price:.2f}"
            )
        )
    exch = (rec.exchange or exchange_hint).strip()
    if rules.allowed_exchanges and exch and exch.upper() not in {e.upper() for e in rules.allowed_exchanges}:
        out.append(RuleViolation("exchange", f"exchange {exch} is not allowed in this game"))
    if (
        rules.security_types
        and rec.security_type
        and rec.security_type.lower() not in {s.lower() for s in rules.security_types}
    ):
        out.append(
            RuleViolation("security_type", f"security type {rec.security_type} is not allowed in this game")
        )
    return out
