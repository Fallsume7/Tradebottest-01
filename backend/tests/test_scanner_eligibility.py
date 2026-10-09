"""The scanner's recommended list can only ever contain stocks CONFIRMED tradable in the user's game."""

from __future__ import annotations

from datetime import date

import pytest

from mwbot.config import GameRules, ScannerConfig, ScannerFilters
from mwbot.eligibility.models import EligibilityRecord, EligibilityStatus
from mwbot.eligibility.universe import EligibilityUniverse
from mwbot.forecast.features import market_state
from mwbot.forecast.ranking import OpportunityRanker
from mwbot.scanner.scanner import Scanner

from .conftest import at, build_day, make_source, meta, relaxed_settings

T = EligibilityStatus.CONFIRMED_TRADABLE
U = EligibilityStatus.CONFIRMED_UNAVAILABLE
V = EligibilityStatus.UNVERIFIED


@pytest.fixture()
def contexts(sess):
    # three identical, strongly trending names: only their eligibility differs
    bars = lambda: build_day(sess, 10.0, [(330 + 10, 10.0, 80_000), (50, 10.9, 160_000)], base_volume=80_000)  # noqa: E731
    src = make_source(sess, {s: (meta(s), bars()) for s in ("GOOD", "GONE", "WHO")})
    return src.contexts_at(at(sess, 75))


def scanner() -> Scanner:
    return Scanner(relaxed_settings().scanner, GameRules())


def uni() -> EligibilityUniverse:
    return EligibilityUniverse(
        [
            EligibilityRecord("GOOD", T),
            EligibilityRecord("GONE", U, rejection_reason="not in this game"),
            # WHO: no record at all -> unverified
        ]
    )


def test_only_confirmed_tradable_names_are_actionable(contexts):
    res = {r.symbol: r for r in scanner().scan(contexts, uni(), None)}
    assert res["GOOD"].actionable and res["GOOD"].rank == 1
    for sym in ("GONE", "WHO"):
        assert not res[sym].actionable and res[sym].rank == 0, sym
    assert res["WHO"].eligibility_status == V and res["GONE"].eligibility_status == U


def test_scanner_without_an_eligibility_list_recommends_nothing(contexts):
    """Passing no universe means 'not evaluated' - never 'assume everything is tradable'."""
    assert not any(r.actionable for r in scanner().scan(contexts, None, None))


def test_actionable_names_come_first_even_when_others_score_higher(contexts):
    res = scanner().scan(contexts, uni(), None)
    assert res[0].symbol == "GOOD"


def test_opportunity_ranking_lists_unverified_as_research_and_drops_unavailable(contexts):
    sc = scanner().scan(contexts, uni(), None)
    s = relaxed_settings()
    ops = OpportunityRanker().rank(
        contexts, sc, market_state([r.metrics for r in sc]), s.profile_for(), s.effective_costs()
    )
    by = {o.symbol: o for o in ops}
    assert "GONE" not in by  # known-unsupported: neither recommended nor researched
    assert by["WHO"].research_only and not by["WHO"].actionable and by["WHO"].rank == 0
    assert by["GOOD"].eligibility_status == T
    assert all(o.rank == 0 for o in ops if not o.actionable)


def test_confirmed_tradable_but_breaking_a_game_rule_is_not_actionable(sess):
    cheap = build_day(sess, 0.8, [(330 + 10, 0.8, 80_000), (50, 0.9, 160_000)], base_volume=80_000)
    src = make_source(sess, {"PENNY": (meta("PENNY", prev_close=0.8), cheap)})
    ctx = src.contexts_at(at(sess, 75))
    cfg = ScannerConfig(
        filters=ScannerFilters(min_price=0.1, max_price=50, min_bars=10, max_data_age_seconds=300)
    )
    sc = Scanner(cfg, GameRules(min_price=1.0))
    (r,) = sc.scan(ctx, EligibilityUniverse([EligibilityRecord("PENNY", T)]), None)
    assert r.eligibility_status == T and not r.actionable
    assert r.eligibility and any("price" in v.message.lower() for v in r.eligibility.violations)


def test_replay_uses_the_list_as_it_was_on_the_session_date(contexts):
    hist = EligibilityUniverse(
        [
            EligibilityRecord(
                "GOOD", T, effective_from=date(2026, 12, 1)
            ),  # confirmed only AFTER the session
            EligibilityRecord("GONE", T, effective_from=date(2026, 1, 1)),
            EligibilityRecord("GONE", U, effective_from=date(2026, 12, 1)),
        ]
    )
    day = date(2026, 10, 8)
    res = {r.symbol: r for r in scanner().scan(contexts, hist, day)}
    assert not res["GOOD"].actionable  # today's knowledge must not leak backwards
    assert res["GONE"].actionable  # it WAS tradable then
    now = {r.symbol: r for r in scanner().scan(contexts, hist, None)}
    assert now["GOOD"].actionable and not now["GONE"].actionable
