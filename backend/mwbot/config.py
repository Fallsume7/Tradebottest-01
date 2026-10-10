"""Pydantic configuration models (user-editable via the dashboard) and environment settings."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DISPLAY_TZ = "America/Chicago"
EXCHANGE_TZ = "America/New_York"
EXCHANGE_CALENDAR = "XNYS"


class EnvSettings(BaseSettings):
    """Process-level settings from environment variables / .env (prefix ``MWBOT_``)."""

    model_config = SettingsConfigDict(env_prefix="MWBOT_", env_file=".env", extra="ignore")

    data_mode: Literal["demo", "historical", "connected"] = "demo"
    database_url: str = "sqlite:///./data/mwbot.db"
    data_dir: str = "./data"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    log_json: bool = False

    massive_api_key: str = ""
    massive_base_url: str = "https://api.massive.com"
    massive_feed: Literal["realtime", "delayed"] = "delayed"
    massive_delay_seconds: int = 900

    historical_dir: str = "./backend/sample_data"

    execution_mode: Literal["paper", "mock_browser", "marketwatch"] = "paper"
    allow_real_money: bool = False

    # --- MarketWatch virtual game (play money). The bot never sees your password: you log in yourself in a
    # dedicated Chrome profile that was started with scripts\marketwatch_chrome.ps1; the bot attaches to it.
    marketwatch_game_url: str = ""  # the game's own page, e.g. https://www.marketwatch.com/games/<your-game>
    marketwatch_cdp_url: str = "http://127.0.0.1:9222"  # local Chrome debugging endpoint (localhost only)
    # read_only: only read cash/holdings | prepare_only: fill the order form, never confirm | live: submit orders
    marketwatch_stage: Literal["read_only", "prepare_only", "live"] = "read_only"
    marketwatch_min_action_gap_s: float = (
        2.0  # pause between page actions (keep within what your game allows)
    )
    autopilot: bool = False  # on start: enable the live-clock schedule so a trading day runs with no clicks

    mock_site_url: str = "http://127.0.0.1:8765"
    mock_site_user: str = "demo"
    mock_site_password: str = "demo"

    frontend_dist: str = ""

    @field_validator("marketwatch_game_url")
    @classmethod
    def _game_url_is_marketwatch(cls, v: str) -> str:
        if v:
            from urllib.parse import urlparse

            u = urlparse(v)
            host = (u.hostname or "").lower()
            if u.scheme != "https" or not (host == "marketwatch.com" or host.endswith(".marketwatch.com")):
                raise ValueError("MWBOT_MARKETWATCH_GAME_URL must be an https://...marketwatch.com address")
        return v

    @field_validator("marketwatch_cdp_url")
    @classmethod
    def _cdp_is_local(cls, v: str) -> str:
        from urllib.parse import urlparse

        if (urlparse(v).hostname or "") not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("MWBOT_MARKETWATCH_CDP_URL must point at this computer (127.0.0.1 / localhost)")
        return v

    @field_validator("allow_real_money")
    @classmethod
    def _never_real_money(cls, v: bool) -> bool:
        if v:
            raise ValueError(
                "MWBOT_ALLOW_REAL_MONEY=true is rejected: this project has no real-money execution path."
            )
        return v


# --------------------------------------------------------------------------- scanner


class ScannerFilters(BaseModel):
    min_price: float = 1.0
    max_price: float = 20.0
    min_pct_change: float = 3.0
    max_pct_change: float | None = None  # reject absurd data (e.g. >1000%)
    min_volume: float = 100_000
    min_rvol: float = 1.5
    min_dollar_volume: float = 250_000
    min_market_cap: float | None = None
    max_market_cap: float | None = None
    max_spread_pct: float | None = 2.0
    min_bars: int = 15  # need some history for momentum / VWAP
    max_data_age_seconds: float = 180.0
    exclude_halted: bool = True


class ScannerWeights(BaseModel):
    """Relative weights of score components. Normalised over *available* components."""

    pct_change: float = 1.0
    premarket: float = 0.8
    rvol: float = 1.5
    momentum_5m: float = 1.5
    momentum_15m: float = 1.0
    vs_vwap: float = 1.2
    near_high: float = 1.2
    liquidity: float = 1.0
    volatility: float = 0.5
    spread: float = 0.5


class ScannerConfig(BaseModel):
    preset: str = "small_cap_momentum"
    filters: ScannerFilters = Field(default_factory=ScannerFilters)
    weights: ScannerWeights = Field(default_factory=ScannerWeights)
    # How to treat a metric that the feed cannot provide:
    #   "ignore"  - the component is marked unavailable and weights renormalise (filters on it are skipped)
    #   "exclude" - the symbol is excluded and the reason shown
    on_missing_data: Literal["ignore", "exclude"] = "ignore"
    momentum_short_minutes: int = 5
    momentum_long_minutes: int = 15
    top_n: int = 25


PRESET_FILTERS: dict[str, dict[str, float | None]] = {
    "penny_stocks": {
        "min_price": 0.20,
        "max_price": 5.0,
        "min_pct_change": 5.0,
        "min_volume": 500_000,
        "min_rvol": 2.0,
        "min_dollar_volume": 150_000,
        "max_spread_pct": 5.0,
    },
    "small_cap_momentum": {
        "min_price": 1.0,
        "max_price": 20.0,
        "min_pct_change": 3.0,
        "min_volume": 100_000,
        "min_rvol": 1.5,
        "min_dollar_volume": 250_000,
        "max_spread_pct": 2.0,
    },
    "broad_us_equities": {
        "min_price": 5.0,
        "max_price": 500.0,
        "min_pct_change": 1.5,
        "min_volume": 300_000,
        "min_rvol": 1.2,
        "min_dollar_volume": 2_000_000,
        "max_spread_pct": 0.75,
    },
}


def scanner_config_for_preset(name: str) -> ScannerConfig:
    if name not in PRESET_FILTERS:
        raise ValueError(f"Unknown scanner preset {name!r}; choose from {sorted(PRESET_FILTERS)}")
    base = ScannerFilters().model_copy(update=PRESET_FILTERS[name])
    return ScannerConfig(preset=name, filters=base)


# --------------------------------------------------------------------------- risk / costs / schedule


class RiskConfig(BaseModel):
    starting_capital: float = Field(100_000.0, gt=0)
    max_position_pct: float = Field(10.0, gt=0, le=100)  # of equity, per position
    risk_per_trade_pct: float = Field(0.5, gt=0, le=10)  # of equity risked to the stop
    max_position_value: float | None = Field(None, gt=0)  # $ cap per position (e.g. from game rules)
    # Most virtual money the bot may have invested at once (positions + pending buys). None = whole account.
    capital_budget: float | None = Field(None, gt=0)
    max_positions: int = Field(5, ge=1)
    max_daily_loss_pct: float = Field(3.0, gt=0)  # halts new entries; flatten if breached
    stop_loss_pct: float = Field(3.0, gt=0)  # default; strategies may override
    take_profit_pct: float | None = 6.0
    trailing_stop_pct: float | None = None
    max_hold_minutes: int | None = None
    max_trades_per_day: int = 20
    # Don't re-enter a symbol that already stopped out today
    block_reentry_after_loss: bool = True
    # Reject entries when the market-data timestamp is older than this
    max_data_age_seconds: float = 180.0


class CostConfig(BaseModel):
    """Documented, deterministic execution assumptions (see ARCHITECTURE.md)."""

    commission_per_share: float = 0.0
    commission_min_per_order: float = 0.0
    commission_per_order: float = 0.0
    slippage_bps: float = 5.0  # base slippage against the trader
    impact_bps_per_pct_participation: float = 2.0  # extra slippage per 1% of bar volume taken
    assumed_spread_bps: float = 40.0  # used when the feed has no quote data
    max_participation: float = Field(0.10, gt=0, le=1)  # max fraction of a bar's volume we may take
    latency_seconds: int = 5  # order -> eligible; also models data latency
    min_fill_volume: int = 100  # bar volume below this => no fill (missing liquidity)


class ScheduleConfig(BaseModel):
    """Local (America/Chicago) wall-clock times, all configurable."""

    scanner_start: str = "08:00"
    observe_minutes: int = Field(10, ge=0)  # after open, observe before trading
    last_entry: str = "14:15"  # no new positions after this
    liquidation_start: str = "14:45"
    final_check_minutes_before_close: int = Field(3, ge=1)
    report_delay_minutes: int = 5
    tick_seconds: int = 60  # live loop cadence
    scanner_refresh_minutes: int = Field(2, ge=1, le=30, description="How often opportunities are re-ranked")
    monitor_minutes: int = Field(1, ge=1, le=15, description="How often held positions get a forecast check")

    @field_validator("scanner_start", "last_entry", "liquidation_start")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        h, _, m = v.partition(":")
        if not (h.isdigit() and m.isdigit() and 0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise ValueError(f"expected HH:MM, got {v!r}")
        return f"{int(h):02d}:{int(m):02d}"


OrderTypeName = Literal["market", "limit", "stop"]


def _default_order_types() -> list[OrderTypeName]:
    return ["market", "limit"]


class GameRules(BaseModel):
    """Rules of YOUR MarketWatch game. Defaults are placeholders: set them to what your game actually allows."""

    min_price: float = Field(1.0, ge=0, le=10_000, description="Lowest share price the game lets you buy")
    allowed_exchanges: list[str] = Field(default_factory=lambda: ["NYSE", "NASDAQ", "NYSE American"])
    security_types: list[str] = Field(default_factory=lambda: ["common_stock", "etf"])
    max_position_pct: float | None = Field(
        None, gt=0, le=100, description="Game limit on % of portfolio in one stock"
    )
    max_position_value: float | None = Field(None, gt=0, description="Game limit on $ in one stock")
    max_positions: int | None = Field(None, ge=1, description="Game limit on distinct holdings")
    commission_per_trade: float = Field(0.0, ge=0, le=1000, description="Flat commission per order ($)")
    commission_per_share: float = Field(0.0, ge=0, le=10, description="Commission per share ($)")
    supported_order_types: list[OrderTypeName] = Field(default_factory=_default_order_types)
    allow_shorting: bool = False
    allow_extended_hours: bool = False
    # How eligibility is looked up: the current list (live/demo) or the list as it was on each date (replays)
    eligibility_mode: Literal["current_list", "point_in_time"] = "current_list"

    @field_validator("allowed_exchanges", "security_types")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        return [x.strip() for x in v if x.strip()]

    @field_validator("supported_order_types")
    @classmethod
    def _need_one(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("at least one order type must be supported")
        return v


class TradingMode(StrEnum):
    MAX_EXPECTED_RETURN = "max_expected_return"
    AGGRESSIVE_UPSIDE = "aggressive_upside"

    @property
    def label(self) -> str:
        return {"max_expected_return": "Maximum Expected Return", "aggressive_upside": "Aggressive Upside"}[
            self.value
        ]


class ObjectiveWeights(BaseModel):
    """How the Expected Opportunity score trades return against risk. Units are percentage points of the
    horizon return (see docs/STRATEGIES.md)."""

    w_return: float = Field(1.0, ge=0, le=5, description="Weight on expected net return")
    w_probability: float = Field(0.6, ge=0, le=5, description="Weight on the chance of a positive net return")
    w_liquidity: float = Field(0.4, ge=0, le=5, description="Weight on liquidity / tight spreads")
    w_downside: float = Field(0.35, ge=0, le=5, description="Penalty on the 10th-percentile loss")
    w_uncertainty: float = Field(0.12, ge=0, le=5, description="Penalty on forecast uncertainty")
    w_upside: float = Field(0.0, ge=0, le=5, description="Reward for the 90th-percentile upside (aggressive)")


class EntryGates(BaseModel):
    min_expected_net_return_pct: float = Field(
        0.25, ge=-1, le=10, description="Skip trades expected to net less than this after costs"
    )
    min_p_positive: float = Field(
        0.52, ge=0, le=1, description="Minimum estimated chance of a positive net return"
    )
    min_p_target: float = Field(
        0.30, ge=0, le=1, description="Minimum estimated chance of touching the profit target"
    )
    min_reward_risk: float = Field(
        1.0, ge=0, le=10, description="Target gain (net) divided by stop distance (+costs)"
    )
    min_minutes_to_close: float = Field(
        45.0, ge=0, le=390, description="No new trades closer than this to the close"
    )
    max_horizon_sigma_pct: float = Field(
        4.0, ge=0.2, le=50, description="Skip stocks whose expected move size over the horizon is larger"
    )
    max_cost_pct: float = Field(
        0.9, ge=0, le=10, description="Skip stocks whose round-trip trading cost exceeds this"
    )
    min_rvol: float = Field(0.0, ge=0, le=50, description="Optional minimum relative volume (0 = off)")


class PlanConfig(BaseModel):
    max_horizon_minutes: float = Field(120.0, ge=10, le=390)
    horizon_fraction: float = Field(
        0.5, gt=0, le=1, description="Use at most this share of the usable remaining session"
    )
    min_horizon_minutes: float = Field(10.0, ge=1, le=120)
    target_prob_goal: float = Field(
        0.45, gt=0.05, lt=0.95, description="Place the target where the chance of touching it is about this"
    )
    min_target_pct: float = Field(0.4, gt=0, le=20)
    max_target_pct: float = Field(6.0, gt=0, le=60)
    stop_sigma_mult: float = Field(
        1.3, gt=0, le=10, description="Stop distance in units of typical 20-minute adverse movement"
    )
    min_stop_pct: float = Field(0.7, gt=0, le=20)
    max_stop_pct: float = Field(4.0, gt=0, le=30)
    trailing_pct: float | None = Field(
        1.6, gt=0, le=20, description="Trailing stop once in profit (off = None)"
    )
    breakeven_progress: float | None = Field(
        0.5, gt=0, le=1, description="Move the stop to breakeven after this share of the way to target"
    )
    end_buffer_minutes: float = Field(
        20.0, ge=0, le=120, description="Planned exit this long before the close"
    )


class TurnoverConfig(BaseModel):
    rotation_enabled: bool = True
    min_advantage_pct: float = Field(
        0.6, ge=0, le=10, description="A new idea must beat the weakest holding by this much (after costs)"
    )
    cooldown_minutes: float = Field(
        20.0, ge=0, le=390, description="Do not re-buy a stock this soon after selling it"
    )
    min_hold_minutes: float = Field(
        8.0, ge=0, le=120, description="Hold at least this long before discretionary exits"
    )
    max_entries_per_hour: int = Field(6, ge=1, le=60)


class ForecastExitConfig(BaseModel):
    enabled: bool = True
    min_net_return_to_hold_pct: float = Field(
        -0.15, ge=-10, le=5, description="Exit if the updated expected net return falls below this"
    )
    confirm_evaluations: int = Field(
        2, ge=1, le=10, description="...on this many consecutive checks (avoids reacting to noise)"
    )
    p_positive_floor: float = Field(0.30, ge=0, le=1)


class ModeProfile(BaseModel):
    """Everything that differs between the two trading modes. Same engines, different presets."""

    mode: TradingMode
    risk: RiskConfig
    objective: ObjectiveWeights = Field(default_factory=ObjectiveWeights)
    gates: EntryGates = Field(default_factory=EntryGates)
    plan: PlanConfig = Field(default_factory=PlanConfig)
    turnover: TurnoverConfig = Field(default_factory=TurnoverConfig)
    forecast_exit: ForecastExitConfig = Field(default_factory=ForecastExitConfig)


def preset_profile(mode: TradingMode) -> ModeProfile:
    """The built-in presets. MER is the recommended default; Aggressive accepts more variance for more upside."""
    if mode == TradingMode.MAX_EXPECTED_RETURN:
        return ModeProfile(mode=mode, risk=RiskConfig())
    return ModeProfile(
        mode=mode,
        risk=RiskConfig(
            max_position_pct=30.0,
            risk_per_trade_pct=1.5,
            max_positions=4,
            max_daily_loss_pct=6.0,
            stop_loss_pct=5.0,
            take_profit_pct=12.0,
            max_trades_per_day=30,
        ),
        objective=ObjectiveWeights(
            w_return=1.0,
            w_probability=0.3,
            w_liquidity=0.2,
            w_downside=0.12,
            w_uncertainty=0.02,
            w_upside=0.25,
        ),
        gates=EntryGates(
            min_expected_net_return_pct=0.2,
            min_p_positive=0.48,
            min_p_target=0.22,
            min_reward_risk=1.2,
            min_minutes_to_close=30.0,
            max_horizon_sigma_pct=9.0,
            max_cost_pct=1.4,
        ),
        plan=PlanConfig(
            max_horizon_minutes=150.0,
            horizon_fraction=0.6,
            target_prob_goal=0.30,
            min_target_pct=0.8,
            max_target_pct=12.0,
            stop_sigma_mult=1.9,
            min_stop_pct=1.0,
            max_stop_pct=6.0,
            trailing_pct=3.0,
            breakeven_progress=0.6,
            end_buffer_minutes=15.0,
        ),
        turnover=TurnoverConfig(
            rotation_enabled=True,
            min_advantage_pct=0.4,
            cooldown_minutes=12.0,
            min_hold_minutes=5.0,
            max_entries_per_hour=10,
        ),
        forecast_exit=ForecastExitConfig(
            min_net_return_to_hold_pct=-0.5, confirm_evaluations=3, p_positive_floor=0.2
        ),
    )


class TradingConfig(BaseModel):
    """Trading style. ``AppSettings.risk`` always holds the ACTIVE mode's risk/sizing; the other mode's is
    kept in ``profiles[...].risk`` and swapped in when the mode is switched."""

    mode: TradingMode = TradingMode.MAX_EXPECTED_RETURN
    profiles: dict[str, ModeProfile] = Field(
        default_factory=lambda: {m.value: preset_profile(m) for m in TradingMode}
    )
    # What a mode switch / settings change does to positions that are ALREADY open:
    #   keep_original  - they keep the plan they were opened with (target, stop, trailing, deadline)
    #   tighten_only   - they additionally adopt the new mode's trailing/stop if that is TIGHTER
    existing_positions_policy: Literal["keep_original", "tighten_only"] = "keep_original"
    forecaster: Literal["auto", "heuristic", "statistical"] = "auto"
    emergency_behavior: Literal["flatten_all", "block_entries_only"] = "flatten_all"

    @field_validator("profiles")
    @classmethod
    def _both(cls, v: dict[str, ModeProfile]) -> dict[str, ModeProfile]:
        for m in TradingMode:
            v.setdefault(m.value, preset_profile(m))
        return v


class UiConfig(BaseModel):
    simple_mode: bool = True  # hide advanced controls by default
    theme: Literal["dark", "light"] = "dark"
    notifications: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    show_research_candidates: bool = True


class StrategySelection(BaseModel):
    enabled: list[str] = Field(default_factory=lambda: ["expected_opportunity"])
    params: dict[str, dict[str, float | int | bool | str | None]] = Field(default_factory=dict)


class AppSettings(BaseModel):
    """Everything the dashboard's Settings page edits. Persisted in SQLite."""

    risk: RiskConfig = Field(default_factory=RiskConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
    game: GameRules = Field(default_factory=GameRules)
    costs: CostConfig = Field(default_factory=CostConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    strategies: StrategySelection = Field(default_factory=StrategySelection)
    data_mode: Literal["demo", "historical", "connected"] = "demo"
    demo_seed: int = 42
    demo_speed: float = Field(10.0, gt=0)  # simulated minutes advanced per real second
    historical_dataset: str = "synthetic_sample"
    auto_schedule: bool = False  # run the live-clock loop on trading days via APScheduler
    # A delayed feed (e.g. 15-min) is unsafe for automated entries; opt in explicitly.
    allow_delayed_feed_trading: bool = False
    pause_on_mirror_failure: bool = True

    def profile_for(self, mode: TradingMode | None = None) -> ModeProfile:
        """The profile for ``mode`` (default: active), with the active mode's live ``risk`` filled in."""
        m = mode or self.trading.mode
        prof = self.trading.profiles[m.value]
        if m == self.trading.mode:
            return prof.model_copy(update={"risk": self.effective_risk()})
        return prof.model_copy(
            update={"risk": prof.risk.model_copy(update={"starting_capital": self.risk.starting_capital})}
        )

    def with_mode(self, mode: TradingMode) -> AppSettings:
        """Copy of these settings running ``mode`` (its risk block swapped in; everything else unchanged)."""
        if mode == self.trading.mode:
            return self.model_copy(deep=True)
        cp = self.model_copy(deep=True)
        profiles = dict(cp.trading.profiles)
        profiles[self.trading.mode.value] = profiles[self.trading.mode.value].model_copy(
            update={"risk": self.risk}
        )
        incoming = profiles[mode.value].risk.model_copy(
            update={"starting_capital": self.risk.starting_capital}
        )
        cp.trading = cp.trading.model_copy(update={"mode": mode, "profiles": profiles})
        cp.risk = incoming
        return cp

    def effective_costs(self) -> CostConfig:
        """Costs actually applied by the simulator: your cost model + the game's commissions."""
        g = self.game
        return self.costs.model_copy(
            update={
                "commission_per_share": self.costs.commission_per_share + g.commission_per_share,
                "commission_per_order": self.costs.commission_per_order + g.commission_per_trade,
            }
        )

    def effective_risk(self) -> RiskConfig:
        """Risk limits actually enforced: your limits, tightened by any game-imposed limits."""
        g = self.game
        upd: dict[str, object] = {}
        if g.max_position_pct is not None:
            upd["max_position_pct"] = min(self.risk.max_position_pct, g.max_position_pct)
        if g.max_positions is not None:
            upd["max_positions"] = min(self.risk.max_positions, g.max_positions)
        if g.max_position_value is not None:
            cur = self.risk.max_position_value
            upd["max_position_value"] = (
                g.max_position_value if cur is None else min(cur, g.max_position_value)
            )
        return self.risk.model_copy(update=upd)

    @model_validator(mode="after")
    def _check_times(self) -> AppSettings:
        s = self.schedule
        if not (s.last_entry <= s.liquidation_start):
            raise ValueError("last_entry must not be later than liquidation_start")
        return self
