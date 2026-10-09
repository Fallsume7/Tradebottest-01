"""Look-ahead protection: nothing at/after `as_of` may influence anything computed at `as_of`."""

from datetime import timedelta

import pandas as pd
import pytest

from mwbot.config import scanner_config_for_preset
from mwbot.data.replay import ReplaySource, SymbolDay
from mwbot.data.synthetic import SyntheticProvider
from mwbot.scanner.scanner import Scanner
from mwbot.strategies.base import SignalInput
from mwbot.strategies.builtin import STRATEGY_CLASSES, build_strategy

from .conftest import ONE, TEST_DAY, at, make_source, meta
from .scenarios import fader, scramble_future, strong_runner


def test_bar_becomes_visible_only_when_complete(session_info):
    src = make_source(session_info, {"TST": (meta(), strong_runner(session_info))})
    t = at(session_info, 10)
    ctx = src.context_for("TST", t)
    assert ctx.bars.index[-1] == t - ONE  # bar labelled t-1m is complete at t
    assert t not in ctx.bars.index  # bar labelled t is still forming
    ctx2 = src.context_for("TST", t + timedelta(seconds=30))
    assert ctx2.bars.index[-1] == t - ONE  # still not complete 30s later


def test_context_independent_of_future_bars(session_info):
    good = strong_runner(session_info)
    cutoff = at(session_info, 30)
    bad = scramble_future(good, cutoff)
    a = make_source(session_info, {"TST": (meta(), good)})
    b = make_source(session_info, {"TST": (meta(), bad)})
    for m in (-30, 0, 10, 29):
        ca, cb = a.context_for("TST", at(session_info, m)), b.context_for("TST", at(session_info, m))
        pd.testing.assert_frame_equal(ca.bars, cb.bars)


@pytest.mark.parametrize("minutes", [12, 25, 29])
def test_scanner_and_all_strategies_ignore_the_future(session_info, minutes):
    cutoff = at(session_info, 30)
    sources = []
    for bars in (strong_runner(session_info), scramble_future(strong_runner(session_info), cutoff)):
        sources.append(make_source(session_info, {"TST": (meta(), bars)}))
    outs = []
    for src in sources:
        ctx = src.context_for("TST", at(session_info, minutes))
        scan = Scanner().scan_one(ctx)
        evs = [
            build_strategy(sid).evaluate_entry(SignalInput(ctx, scan.metrics, scan, minutes)).to_dict()
            for sid in sorted(STRATEGY_CLASSES)
        ]
        outs.append((scan.to_dict(), evs))
    assert outs[0] == outs[1]


def test_synthetic_full_day_signals_unchanged_when_future_is_scrambled():
    prov = SyntheticProvider(42)
    src = prov.open_session(TEST_DAY)
    t = at(src.session, 45)
    scanner = Scanner(scanner_config_for_preset("small_cap_momentum"))
    base = [r.to_dict() for r in scanner.scan(src.contexts_at(t))]
    days = {}
    for sym in src.symbols():
        sd = src.full_day(sym)
        days[sym] = SymbolDay(meta=sd.meta, bars=scramble_future(sd.bars, t), halts=[])
    scrambled = ReplaySource(src.session, days, src.feed_info(), synthetic_quotes=True)
    after = [r.to_dict() for r in scanner.scan(scrambled.contexts_at(t))]
    assert base == after


def test_replay_requires_timezone_aware_as_of(session_info):
    src = make_source(session_info, {"TST": (meta(), fader(session_info))})
    with pytest.raises(ValueError, match="tz-aware"):
        src.contexts_at(at(session_info, 5).replace(tzinfo=None))
