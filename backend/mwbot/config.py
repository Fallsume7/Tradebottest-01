"""Pydantic configuration models (user-editable via the dashboard) and environment settings."""

from __future__ import annotations

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

    execution_mode: Literal["paper", "mock_browser"] = "paper"
    allow_real_money: bool = False

    mock_site_url: str = "http://127.0.0.1:8765"
    mock_site_user: str = "demo"
    mock_site_password: str = "demo"

    frontend_dist: str = ""

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

    @field_validator("scanner_start", "last_entry", "liquidation_start")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        h, _, m = v.partition(":")
        if not (h.isdigit() and m.isdigit() and 0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise ValueError(f"expected HH:MM, got {v!r}")
        return f"{int(h):02d}:{int(m):02d}"


class StrategySelection(BaseModel):
    enabled: list[str] = Field(
        default_factory=lambda: ["opening_momentum", "gap_continuation", "momentum_breakout", "volume_surge"]
    )
    params: dict[str, dict[str, float | int | bool | str | None]] = Field(default_factory=dict)


class AppSettings(BaseModel):
    """Everything the dashboard's Settings page edits. Persisted in SQLite."""

    risk: RiskConfig = Field(default_factory=RiskConfig)
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

    @model_validator(mode="after")
    def _check_times(self) -> AppSettings:
        s = self.schedule
        if not (s.last_entry <= s.liquidation_start):
            raise ValueError("last_entry must not be later than liquidation_start")
        return self
