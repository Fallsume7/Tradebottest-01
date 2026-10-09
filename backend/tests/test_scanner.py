import numpy as np
import pytest

from mwbot.config import ScannerConfig, ScannerFilters, scanner_config_for_preset
from mwbot.data.synthetic import SyntheticProvider, synthetic_regimes
from mwbot.domain import StockMeta
from mwbot.scanner.metrics import compute_metrics
from mwbot.scanner.scanner import Scanner

from .conftest import TEST_DAY, ONE, at, build_day, make_source, meta, relaxed_settings
from .scenarios import fader, strong_runner


def scanner(**filters) -> Scanner:
    f = relaxed_settings().scanner.filters.model_copy(update=filters)
    return Scanner(ScannerConfig(filters=f))


def ctx_at(session, bars, minutes, m=None):
    src = make_source(session, {"TST": (m or meta(), bars)})
    return src.context_for("TST", at(session, minutes))


def test_metrics_match_hand_calculation(session_info):
    bars = strong_runner(session_info)
    ctx = ctx_at(session_info, bars, 15)
    m = compute_metrics(ctx)
    vis = bars[bars.index <= at(session_info, 15) - ONE]
    reg = vis[vis.index >= session_info.open_utc]
    last = vis["close"].iloc[-1]
    assert m.price == pytest.approx(last)
    assert m.pct_change == pytest.approx((last / 10.0 - 1) * 100)
    assert m.premarket_pct == pytest.approx((vis[vis.index < session_info.open_utc]["close"].iloc[-1] / 10 - 1) * 100)
    assert m.gap_pct == pytest.approx((reg["open"].iloc[0] / 10 - 1) * 100)
    tp = (reg["high"] + reg["low"] + reg["close"]) / 3
    assert m.vwap == pytest.approx(float((tp * reg["volume"]).sum() / reg["volume"].sum()))
    assert m.vs_vwap_pct == pytest.approx((last / m.vwap - 1) * 100)
    assert m.volume == pytest.approx(vis["volume"].sum())
    assert m.high_of_day == pytest.approx(vis["high"].max())
    assert m.dist_from_high_pct == pytest.approx((vis["high"].max() - last) / vis["high"].max() * 100)
    ref = vis["close"][vis.index <= vis.index[-1] - 5 * ONE].iloc[-1]
    assert m.momentum_short == pytest.approx((last / ref - 1) * 100)
    assert m.n_bars == len(vis) and m.rvol and m.rvol > 2


def test_rvol_unavailable_without_adv_and_policy(session_info):
    bars = strong_runner(session_info)
    ctx = ctx_at(session_info, bars, 15, StockMeta("TST", "No ADV", prev_close=10.0, avg_daily_volume=None))
    s_ignore = scanner(min_rvol=2.0)
    r = s_ignore.scan_one(ctx)
    assert r.metrics.rvol is None and r.tradable
    assert any("Relative volume" in w and "unavailable" in w for w in r.warnings)
    assert next(c for c in r.components if c.name == "rvol").score is None
    excl = Scanner(ScannerConfig(filters=s_ignore.config.filters, on_missing_data="exclude"))
    r2 = excl.scan_one(ctx)
    assert not r2.tradable and any("relative volume unavailable" in b for b in r2.blockers)


def test_missing_prev_close_means_no_pct_change_not_zero(session_info):
    ctx = ctx_at(session_info, strong_runner(session_info), 15, StockMeta("TST", "x", prev_close=None, avg_daily_volume=1e6))
    m = compute_metrics(ctx)
    assert m.pct_change is None and m.premarket_pct is None and m.gap_pct is None


def test_score_is_sum_of_component_points_and_bounded(session_info):
    r = scanner().scan_one(ctx_at(session_info, strong_runner(session_info), 15))
    assert 0 <= r.score <= 100
    assert r.score == pytest.approx(sum(c.points for c in r.components))
    assert r.reasons and any("Relative volume" in x for x in r.reasons)


def test_missing_components_renormalise_weights(session_info):
    s = scanner(max_spread_pct=None)
    ctx = ctx_at(session_info, strong_runner(session_info), 15)
    ctx.quote = None  # spread unavailable
    r = s.scan_one(ctx)
    assert next(c for c in r.components if c.name == "spread").score is None
    assert sum(c.points for c in r.components) == pytest.approx(r.score)
    assert r.score > 30  # not punished for the missing signal


@pytest.mark.parametrize(
    "filters,needle",
    [
        ({"max_price": 10.0}, "price"),
        ({"min_price": 20.0, "max_price": 30.0}, "price"),
        ({"min_pct_change": 40.0}, "change"),
        ({"min_volume": 1e9}, "volume"),
        ({"min_dollar_volume": 1e12}, "dollar volume"),
        ({"min_rvol": 500.0}, "RVOL"),
        ({"min_bars": 10_000}, "bars"),
        ({"min_market_cap": 5e9}, "market cap"),
        ({"max_market_cap": 1e6}, "market cap"),
        ({"max_spread_pct": 0.01}, "spread"),
    ],
)
def test_each_filter_blocks_with_a_reason(session_info, filters, needle):
    r = scanner(**filters).scan_one(ctx_at(session_info, strong_runner(session_info), 15))
    assert not r.tradable and any(needle in b for b in r.blockers), r.blockers


def test_halted_symbol_is_blocked(session_info):
    bars = strong_runner(session_info)
    src = make_source(session_info, {"TST": (meta(), bars)}, halts={"TST": [(at(session_info, 10), at(session_info, 20))]})
    r = scanner().scan_one(src.context_for("TST", at(session_info, 15)))
    assert not r.tradable and "trading halted" in r.blockers
    assert scanner(exclude_halted=False).scan_one(src.context_for("TST", at(session_info, 15))).tradable


def test_stale_symbol_is_blocked(session_info):
    bars = strong_runner(session_info)
    bars = bars.drop(bars.index[(bars.index >= at(session_info, 8)) & (bars.index < at(session_info, 15))])
    r = scanner(max_data_age_seconds=120).scan_one(ctx_at(session_info, bars, 15))
    assert any("stale" in b for b in r.blockers)


def test_abnormal_prints_are_flagged(session_info):
    bars = strong_runner(session_info).copy()
    t = at(session_info, 14) - ONE
    bars.loc[t, ["close", "high"]] = [bars.loc[t, "close"] * 3.0] * 2
    r = scanner().scan_one(ctx_at(session_info, bars, 15))
    assert "extreme_one_minute_move" in r.metrics.abnormal and not r.tradable


def test_biggest_gainer_is_not_automatically_top_ranked(session_info):
    """+60% premarket gapper that is fading must rank below a smaller, steadier, still-rising mover."""
    src = make_source(session_info, {
        "FADE": (meta("FADE"), fader(session_info)),
        "RUN": (meta("RUN"), strong_runner(session_info)),
    })
    s = scanner(max_price=50.0, min_pct_change=3.0)
    res = {r.symbol: r for r in s.scan(src.contexts_at(at(session_info, 40)))}
    fade, run = res["FADE"], res["RUN"]
    assert fade.metrics.pct_change > run.metrics.pct_change
    assert run.score > fade.score
    assert run.continuing and not fade.continuing
    assert any("Below VWAP" in w or "off the high" in w or "Negative" in w for w in fade.warnings)


def test_presets_define_expected_price_bands():
    pen = scanner_config_for_preset("penny_stocks").filters
    small = scanner_config_for_preset("small_cap_momentum").filters
    broad = scanner_config_for_preset("broad_us_equities").filters
    assert pen.max_price <= 5 < small.max_price <= 20 < broad.max_price
    with pytest.raises(ValueError):
        scanner_config_for_preset("nope")


def test_scan_orders_tradable_first_with_ranks(session_info):
    src = make_source(session_info, {"FADE": (meta("FADE"), fader(session_info)), "RUN": (meta("RUN"), strong_runner(session_info))})
    res = scanner().scan(src.contexts_at(at(session_info, 40)))
    ranks = [r.rank for r in res if r.tradable]
    assert ranks == sorted(ranks) and ranks[0] == 1
    assert all(r.rank == 0 for r in res if not r.tradable)


def test_synthetic_illiquid_spikers_are_filtered_out():
    src = SyntheticProvider(42).open_session(TEST_DAY)
    regimes = synthetic_regimes(TEST_DAY, 42)
    s = Scanner(scanner_config_for_preset("small_cap_momentum"))
    illiquid = {k for k, v in regimes.items() if v == "illiquid_spike"}
    for minutes in (-20, 30, 120):
        res = s.scan(src.contexts_at(at(src.session, minutes)))
        assert not [r for r in res if r.symbol in illiquid and r.tradable]
    assert any(r.tradable for r in s.scan(src.contexts_at(at(src.session, 60))))


def test_metrics_never_nan_into_json(session_info):
    ctx = ctx_at(session_info, build_day(session_info, 10.0, [], base_volume=0), 30)
    d = compute_metrics(ctx).to_dict()
    assert all(not (isinstance(v, float) and not np.isfinite(v)) for v in d.values())
