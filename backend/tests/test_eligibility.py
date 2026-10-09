"""MarketWatch eligibility: statuses, point-in-time lookups, CSV, persistence, verifier interface, demo seed."""

from datetime import date

import pytest

from mwbot.config import GameRules
from mwbot.data.csv_provider import CsvHistoricalProvider
from mwbot.db.store import SqlStore, make_engine
from mwbot.eligibility.csvio import export_csv, parse_csv
from mwbot.eligibility.demo import dataset_eligibility, demo_seed
from mwbot.eligibility.models import EPOCH, EligibilityRecord, EligibilityStatus
from mwbot.eligibility.universe import EligibilityUniverse
from mwbot.eligibility.verifier import (
    MarketWatchVerifier,
    VerificationResult,
    VerifierNotConfiguredError,
    apply_verification,
)

from .conftest import SAMPLE_DIR

T, U, V = (
    EligibilityStatus.CONFIRMED_TRADABLE,
    EligibilityStatus.CONFIRMED_UNAVAILABLE,
    EligibilityStatus.UNVERIFIED,
)


def rec(t, st, eff=EPOCH, **kw):
    return EligibilityRecord(t, st, effective_from=eff, **kw)


def test_unknown_ticker_is_unverified_never_assumed_tradable():
    u = EligibilityUniverse([rec("AAA", T)])
    assert u.record_on("ZZZ").status == V
    c = u.check("ZZZ", price=5.0, rules=GameRules())
    assert not c.confirmed and not c.actionable and c.record.source == "default"


def test_three_states_and_lookup_is_case_insensitive():
    u = EligibilityUniverse([rec("aaa", T), rec("BBB", U, rejection_reason="OTC"), rec("CCC", V)])
    assert (
        u.record_on("AAA").status == T and u.record_on("bbb").status == U and u.record_on("CCC").status == V
    )
    assert u.check("AAA", price=5, rules=GameRules()).actionable
    assert not u.check("BBB", price=5, rules=GameRules()).actionable
    assert not u.check("CCC", price=5, rules=GameRules()).actionable


def test_point_in_time_uses_the_record_in_force_on_each_date():
    u = EligibilityUniverse(
        [
            rec("AAA", T, date(2026, 1, 1)),
            rec("AAA", U, date(2026, 6, 1), rejection_reason="delisted from game"),
        ]
    )
    assert u.record_on("AAA", date(2025, 12, 31)).status == V  # before any statement: unknown, not tradable
    assert u.record_on("AAA", date(2026, 3, 1)).status == T
    assert u.record_on("AAA", date(2026, 6, 1)).status == U  # effective on its own date
    assert u.record_on("AAA", date(2026, 9, 1)).status == U
    assert u.record_on("AAA", None).status == U  # "current list" = latest statement
    assert len(u.history("AAA")) == 2


def test_not_applied_universe_is_explicit_and_flagged():
    u = EligibilityUniverse.allow_all()
    c = u.check("ANY", price=5.0)
    assert c.actionable and c.applied is False and c.to_dict()["applied"] is False


@pytest.mark.parametrize(
    "price,exchange,sectype,code",
    [
        (0.5, "NASDAQ", "common_stock", "min_price"),
        (5, "OTC", "common_stock", "exchange"),
        (5, "NYSE", "warrant", "security_type"),
    ],
)
def test_game_rules_block_even_confirmed_tickers(price, exchange, sectype, code):
    u = EligibilityUniverse([rec("AAA", T, exchange=exchange, security_type=sectype)])
    c = u.check("AAA", price=price, rules=GameRules(min_price=1.0))
    assert c.confirmed and not c.actionable and [v.code for v in c.violations] == [code]


def test_game_rules_are_configurable():
    u = EligibilityUniverse([rec("AAA", T, exchange="OTC", security_type="adr")])
    rules = GameRules(min_price=0.1, allowed_exchanges=["OTC"], security_types=["adr"])
    assert u.check("AAA", price=0.2, rules=rules).actionable
    with pytest.raises(ValueError):
        GameRules(supported_order_types=[])


# ------------------------------------------------------------------ CSV
CSV = """Ticker,Status,Company,Market,Verified,Reason,Rules
aaa,tradable,Alpha Corp,NASDAQ,2026-09-01,,max 5000 shares
BBB,unavailable,Beta Inc,OTC,2026-09-02,penny stock restriction,
CCC,unverified,,,,,
"""


def test_csv_import_accepts_aliases_and_normalises():
    r = parse_csv(CSV)
    assert r.ok and [x.ticker for x in r.records] == ["AAA", "BBB", "CCC"]
    a, b, c = r.records
    assert (
        a.status == T
        and a.name == "Alpha Corp"
        and a.restrictions == "max 5000 shares"
        and a.verified_on == date(2026, 9, 1)
    )
    assert b.status == U and b.rejection_reason == "penny stock restriction"
    assert c.status == V and c.verified_on is None


def test_csv_import_reports_bad_rows_with_line_numbers_and_keeps_good_ones():
    text = "ticker,status,verified_on\nAAA,tradable,2026-01-01\nB!B,tradable,\nCCC,maybe,\nDDD,tradable,01/02/2026\nAAA,tradable,2026-01-01\nEEE,unavailable,\n"
    r = parse_csv(text)
    assert [x.ticker for x in r.records] == ["AAA", "EEE"]
    joined = "\n".join(r.errors)
    assert "line 3" in joined and "line 4" in joined and "line 5" in joined and "line 6: duplicate" in joined
    assert r.records[1].rejection_reason  # unavailable always carries a reason


def test_csv_requires_ticker_and_status_columns():
    assert not parse_csv("symbol,name\nAAA,x\n").ok
    assert not parse_csv("").ok


def test_csv_round_trip_preserves_everything():
    recs = [
        rec(
            "AAA",
            T,
            date(2026, 1, 1),
            verified_on=date(2026, 1, 1),
            name="Alpha, Inc.",
            exchange="NYSE",
            restrictions="x",
        ),
        rec("BBB", U, date(2026, 2, 1), rejection_reason='quote " test'),
    ]
    back = parse_csv(export_csv(recs)).records
    assert [r.to_dict() for r in back] == [r.to_dict() for r in sorted(recs, key=lambda r: r.ticker)]


# ------------------------------------------------------------------ persistence
def test_store_keeps_dated_history_and_survives_restart(tmp_path):
    db = f"sqlite:///{tmp_path / 'e.db'}"
    s = SqlStore(make_engine(db))
    s.eligibility_upsert(
        [
            rec("AAA", T, date(2026, 1, 1), name="Alpha", verified_on=date(2026, 1, 2)),
            rec("AAA", U, date(2026, 6, 1), rejection_reason="gone"),
            rec("BBB", V),
        ]
    )
    s.eligibility_upsert(
        [rec("AAA", T, date(2026, 1, 1), name="Alpha Renamed")]
    )  # same key => update, not duplicate
    again = SqlStore(make_engine(db))
    uni = again.eligibility_universe()
    assert [r.name for r in uni.history("AAA")][0] == "Alpha Renamed" and len(uni.history("AAA")) == 2
    assert uni.record_on("AAA", date(2026, 7, 1)).rejection_reason == "gone"
    assert again.eligibility_delete("AAA") == 2 and uni.record_on("BBB").status == V
    again.eligibility_clear()
    assert again.eligibility_all() == []


# ------------------------------------------------------------------ verifier interface
def test_marketwatch_verifier_is_a_documented_skeleton():
    with pytest.raises(VerifierNotConfiguredError, match="MARKETWATCH_INTEGRATION"):
        MarketWatchVerifier().verify("AAPL")


def test_verification_results_become_dated_audited_records_and_unknowns_are_not_recorded():
    results = [
        VerificationResult("AAA", T, name="Alpha"),
        VerificationResult("BBB", U, reason="not in game"),
        VerificationResult("CCC", V, reason="timeout"),
    ]
    recs = apply_verification(results, "mock-site", date(2026, 10, 1))
    assert [r.ticker for r in recs] == ["AAA", "BBB"]
    assert all(
        r.source == "verifier:mock-site"
        and r.verified_on == date(2026, 10, 1)
        and r.effective_from == date(2026, 10, 1)
        for r in recs
    )
    assert recs[1].rejection_reason == "not in game"


# ------------------------------------------------------------------ demo seed + dataset history
def test_demo_seed_shows_all_three_states_and_is_labelled():
    recs = demo_seed()
    sts = {r.status for r in recs}
    assert {T, U} <= sts and all(r.source == "demo-seed" and "(synthetic)" in r.name for r in recs)
    uni = EligibilityUniverse(recs)
    from mwbot.data.synthetic import SYMBOL_POOL

    assert any(uni.record_on(t).status == V for t, _ in SYMBOL_POOL)  # some tickers are left unverified
    assert all(r.rejection_reason for r in recs if r.status == U)


def test_sample_dataset_ships_dated_eligibility_with_mid_period_changes():
    prov = CsvHistoricalProvider(SAMPLE_DIR)
    uni = EligibilityUniverse(prov.eligibility_records())
    dates = prov.available_dates()
    flips = {
        t: (uni.record_on(t, dates[0]).status, uni.record_on(t, dates[-1]).status) for t in uni.tickers()
    }
    flips = {t: v for t, v in flips.items() if v[0] != v[1]}
    assert sorted(flips.values()) == sorted(
        [(T, U), (V, T)]
    )  # one removed from the game, one newly confirmed
    assert dataset_eligibility(["AAA"], dates[:2]) == []  # too short to inject changes


def test_historical_eligibility_is_not_todays_list():
    """The same ticker answers differently on different dates - backtests must ask with the session date."""
    prov = CsvHistoricalProvider(SAMPLE_DIR)
    uni = EligibilityUniverse(prov.eligibility_records())
    dates = prov.available_dates()
    went_unavailable = next(
        t
        for t in uni.tickers()
        if uni.record_on(t, dates[0]).status == T and uni.record_on(t, dates[-1]).status == U
    )
    assert uni.check(went_unavailable, price=5, rules=GameRules(), as_of=dates[0]).confirmed
    assert not uni.check(went_unavailable, price=5, rules=GameRules(), as_of=dates[-1]).confirmed
