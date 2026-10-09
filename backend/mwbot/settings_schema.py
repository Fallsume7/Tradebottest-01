"""Describes every user-editable setting once, for the dashboard AND the server.

The Settings page is rendered from :func:`build_schema` (controls, ranges, plain-language help, recommended
values, what each setting affects), and every change is checked by :func:`validate_changes` on the server with
the same bounds - so the browser and the backend can never disagree. Pydantic (``AppSettings``) remains the
final authority.

Paths are ``section.key``. The ``mode.`` prefix means "the ACTIVE trading mode's profile" (the dashboard shows
one set of controls and the mode switch decides which preset it edits).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from mwbot.config import (
    PRESET_FILTERS,
    AppSettings,
    TradingMode,
    preset_profile,
)

CATEGORIES: list[dict[str, str]] = [
    {
        "id": "strategy",
        "label": "Trading Strategy",
        "blurb": "How the bot picks stocks and plans each trade. The recommended defaults already work - you do not need to change anything here.",
    },
    {
        "id": "risk",
        "label": "Risk Management",
        "blurb": "Hard limits that protect your virtual account, whatever the strategy wants to do.",
    },
    {
        "id": "schedule",
        "label": "Trading Schedule",
        "blurb": "When the bot looks, trades and gets out. Times are US Central (Chicago).",
    },
    {
        "id": "game",
        "label": "MarketWatch Game",
        "blurb": "The rules of YOUR game. The bot only buys stocks you have confirmed are available there.",
    },
    {
        "id": "data",
        "label": "Market Data",
        "blurb": "Where prices come from and how trading costs are estimated. Synthetic/historical data is always labelled.",
    },
    {"id": "general", "label": "General", "blurb": "Appearance and how much of the interface you see."},
]

# What happens to a change (shown next to the control and in the "what will this do?" preview)
EFFECTS: dict[str, str] = {
    "immediate": "Takes effect immediately.",
    "new_trades": "Applies to new trades only. Open positions keep the plan they were opened with.",
    "open_positions": "Applies to new trades and also to open positions.",
    "next_session": "Takes effect from the next session.",
    "idle_only": "Can only be changed while no session is running and nothing is held.",
}


@dataclass(slots=True)
class FieldSpec:
    path: str
    category: str
    label: str
    help: str
    control: str  # slider | number | switch | select | multiselect | time | text
    effect: str = "new_trades"
    advanced: bool = False
    min: float | None = None
    max: float | None = None
    step: float | None = None
    unit: str = ""
    options: list[tuple[str, str]] = field(default_factory=list)
    nullable: bool = False  # None means "off"
    group: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "category": self.category,
            "label": self.label,
            "help": self.help,
            "control": self.control,
            "effect": self.effect,
            "effect_text": EFFECTS[self.effect],
            "advanced": self.advanced,
            "min": self.min,
            "max": self.max,
            "step": self.step,
            "unit": self.unit,
            "options": [{"value": v, "label": lab} for v, lab in self.options],
            "nullable": self.nullable,
            "group": self.group,
        }


def _f(path: str, category: str, label: str, help_: str, control: str, **kw: Any) -> FieldSpec:
    return FieldSpec(path, category, label, help_, control, **kw)


S, R, T, G, D, U = "strategy", "risk", "schedule", "game", "data", "general"

FIELDS: list[FieldSpec] = [
    # ---------------------------------------------------------------- Trading Strategy
    _f(
        "trading.mode",
        S,
        "Trading mode",
        "Maximum Expected Return (recommended) picks the best risk-adjusted opportunities. Aggressive Upside "
        "accepts more variance and sizes positions larger for more potential upside. Both keep the same safety limits.",
        "select",
        effect="new_trades",
        options=[(m.value, m.label) for m in TradingMode],
        group="Mode",
    ),
    _f(
        "trading.existing_positions_policy",
        S,
        "When settings change, open positions...",
        "Keep their original plan (target, stop, deadline) - recommended - or also adopt a TIGHTER stop/trailing "
        "stop from the new settings. Positions are never closed just because a setting changed.",
        "select",
        effect="immediate",
        options=[("keep_original", "Keep original plan"), ("tighten_only", "Adopt tighter stops only")],
        advanced=True,
        group="Mode",
    ),
    _f(
        "trading.forecaster",
        S,
        "Forecast model",
        "Automatic uses the transparent rule-based model. Statistical uses a trained model if you have run a model "
        "check; it is still labelled 'not validated' unless it passed out-of-sample tests.",
        "select",
        effect="new_trades",
        options=[
            ("auto", "Automatic (recommended)"),
            ("heuristic", "Rule-based only"),
            ("statistical", "Statistical (if available)"),
        ],
        advanced=True,
        group="Mode",
    ),
    _f(
        "mode.gates.min_expected_net_return_pct",
        S,
        "Minimum expected return",
        "Skip trades the model expects to earn less than this after trading costs.",
        "slider",
        min=-1,
        max=5,
        step=0.05,
        unit="%",
        group="Entry rules",
    ),
    _f(
        "mode.gates.min_p_positive",
        S,
        "Minimum chance of a gain",
        "Estimated probability that the trade ends with a profit after costs. An estimate, not a promise.",
        "slider",
        min=0.3,
        max=0.9,
        step=0.01,
        group="Entry rules",
    ),
    _f(
        "mode.gates.min_p_target",
        S,
        "Minimum chance of reaching the target",
        "Skip trades whose profit target is unlikely to be touched.",
        "slider",
        min=0.05,
        max=0.9,
        step=0.01,
        advanced=True,
        group="Entry rules",
    ),
    _f(
        "mode.gates.min_reward_risk",
        S,
        "Minimum reward-to-risk",
        "Target gain divided by the stop distance (both after costs).",
        "slider",
        min=0,
        max=5,
        step=0.05,
        advanced=True,
        group="Entry rules",
    ),
    _f(
        "mode.gates.max_horizon_sigma_pct",
        S,
        "Maximum expected swing",
        "Skip stocks whose typical move over the planning window is bigger than this (too wild for the mode).",
        "slider",
        min=0.5,
        max=30,
        step=0.5,
        unit="%",
        advanced=True,
        group="Entry rules",
    ),
    _f(
        "mode.gates.max_cost_pct",
        S,
        "Maximum trading cost",
        "Skip stocks where buying and selling would cost more than this (wide spreads).",
        "slider",
        min=0.05,
        max=5,
        step=0.05,
        unit="%",
        advanced=True,
        group="Entry rules",
    ),
    _f(
        "mode.gates.min_minutes_to_close",
        S,
        "No new trades in the last",
        "Stop opening positions this many minutes before the close.",
        "slider",
        min=0,
        max=240,
        step=5,
        unit="min",
        group="Entry rules",
    ),
    _f(
        "mode.plan.max_horizon_minutes",
        S,
        "Longest holding plan",
        "The longest the bot plans to hold one trade. The window shrinks automatically toward the close.",
        "slider",
        min=10,
        max=390,
        step=5,
        unit="min",
        advanced=True,
        group="Trade plan",
    ),
    _f(
        "mode.plan.target_prob_goal",
        S,
        "Profit-target ambition",
        "Higher = a nearer, easier target (hit more often, smaller gains). Lower = a farther target.",
        "slider",
        min=0.1,
        max=0.9,
        step=0.01,
        advanced=True,
        group="Trade plan",
    ),
    _f(
        "mode.plan.max_target_pct",
        S,
        "Largest profit target",
        "Cap on how far above the entry a target may be placed.",
        "slider",
        min=0.5,
        max=30,
        step=0.5,
        unit="%",
        advanced=True,
        group="Trade plan",
    ),
    _f(
        "mode.plan.trailing_pct",
        S,
        "Trailing stop once in profit",
        "Follows the price up and sells if it falls back by this much. Off = disabled.",
        "slider",
        min=0.3,
        max=15,
        step=0.1,
        unit="%",
        nullable=True,
        group="Trade plan",
    ),
    _f(
        "mode.plan.breakeven_progress",
        S,
        "Protect gains at",
        "After this share of the way to the target, the stop moves up to breakeven. Off = disabled.",
        "slider",
        min=0.1,
        max=1,
        step=0.05,
        nullable=True,
        advanced=True,
        group="Trade plan",
    ),
    _f(
        "mode.turnover.rotation_enabled",
        S,
        "Swap into better ideas",
        "Sell the weakest holding for a clearly better opportunity (only if the gain beats the costs).",
        "switch",
        group="Turnover",
    ),
    _f(
        "mode.turnover.min_advantage_pct",
        S,
        "How much better to swap",
        "A new idea must beat the weakest holding's outlook by this much after costs.",
        "slider",
        min=0,
        max=5,
        step=0.05,
        unit="%",
        advanced=True,
        group="Turnover",
    ),
    _f(
        "mode.turnover.cooldown_minutes",
        S,
        "Wait before re-buying",
        "Do not buy a stock again this soon after selling it.",
        "slider",
        min=0,
        max=240,
        step=5,
        unit="min",
        advanced=True,
        group="Turnover",
    ),
    _f(
        "mode.turnover.min_hold_minutes",
        S,
        "Minimum hold before discretionary exits",
        "Avoids selling on the first wobble. Stops and targets still apply.",
        "slider",
        min=0,
        max=60,
        step=1,
        unit="min",
        advanced=True,
        group="Turnover",
    ),
    _f(
        "mode.turnover.max_entries_per_hour",
        S,
        "Most new trades per hour",
        "A brake on over-trading.",
        "number",
        min=1,
        max=60,
        step=1,
        advanced=True,
        group="Turnover",
    ),
    _f(
        "mode.forecast_exit.enabled",
        S,
        "Sell when the outlook turns bad",
        "Re-checks every holding; sells if the updated forecast stays poor for several checks in a row.",
        "switch",
        effect="open_positions",
        group="Exits",
    ),
    _f(
        "mode.forecast_exit.confirm_evaluations",
        S,
        "Checks in a row before selling",
        "More checks = fewer knee-jerk exits.",
        "slider",
        min=1,
        max=10,
        step=1,
        effect="open_positions",
        advanced=True,
        group="Exits",
    ),
    _f(
        "strategies.enabled",
        S,
        "Strategies in use",
        "The recommended predictive strategy is on by default. The classic momentum strategies can be added for comparison in the Strategy Lab.",
        "multiselect",
        advanced=True,
        effect="new_trades",
        group="Strategies",
    ),
    # ---------------------------------------------------------------- Risk Management
    _f(
        "risk.starting_capital",
        R,
        "Starting capital",
        "Virtual money the account starts with. Changing it needs a reset of the account.",
        "number",
        min=1000,
        max=100_000_000,
        step=1000,
        unit="$",
        effect="idle_only",
        group="Account",
    ),
    _f(
        "risk.max_position_pct",
        R,
        "Largest single position",
        "Maximum share of your account in one stock.",
        "slider",
        min=1,
        max=100,
        step=1,
        unit="%",
        group="Position size",
    ),
    _f(
        "risk.risk_per_trade_pct",
        R,
        "Risk per trade",
        "How much of the account you are willing to lose if a trade hits its stop. Position size follows from this.",
        "slider",
        min=0.1,
        max=5,
        step=0.1,
        unit="%",
        group="Position size",
    ),
    _f(
        "risk.max_positions",
        R,
        "Most positions at once",
        "Number of different stocks held at the same time.",
        "slider",
        min=1,
        max=20,
        step=1,
        group="Position size",
    ),
    _f(
        "risk.max_daily_loss_pct",
        R,
        "Daily loss limit",
        "If the account falls this much in a day the bot sells everything and stops trading for the day.",
        "slider",
        min=0.5,
        max=20,
        step=0.5,
        unit="%",
        effect="open_positions",
        group="Loss limits",
    ),
    _f(
        "risk.stop_loss_pct",
        R,
        "Widest stop allowed",
        "Upper limit on how far below the entry any stop may be placed (each trade's own stop is usually tighter).",
        "slider",
        min=0.5,
        max=15,
        step=0.5,
        unit="%",
        group="Loss limits",
    ),
    _f(
        "risk.max_trades_per_day",
        R,
        "Most trades per day",
        "Hard daily cap on new positions.",
        "number",
        min=1,
        max=200,
        step=1,
        advanced=True,
        group="Loss limits",
    ),
    _f(
        "risk.block_reentry_after_loss",
        R,
        "No re-entry after a stop-out",
        "Do not buy a stock again today if it already stopped you out.",
        "switch",
        advanced=True,
        group="Loss limits",
    ),
    _f(
        "trading.emergency_behavior",
        R,
        "Emergency stop does...",
        "Sell everything immediately (recommended), or only block new trades and let open positions follow their normal exits.",
        "select",
        effect="immediate",
        options=[
            ("flatten_all", "Sell everything now"),
            ("block_entries_only", "Only block new trades"),
        ],
        group="Emergency",
    ),
    # ---------------------------------------------------------------- Trading Schedule
    _f(
        "schedule.scanner_start",
        T,
        "Start scanning",
        "Chicago time. The bot looks for ideas but does not trade before the observation period ends.",
        "time",
        effect="next_session",
        group="Day",
    ),
    _f(
        "schedule.observe_minutes",
        T,
        "Watch the open for",
        "Minutes after the open to observe before trading.",
        "slider",
        min=0,
        max=60,
        step=1,
        unit="min",
        effect="next_session",
        group="Day",
    ),
    _f(
        "schedule.last_entry",
        T,
        "Last time to open a position",
        "Chicago time. No new positions after this.",
        "time",
        effect="next_session",
        group="Day",
    ),
    _f(
        "schedule.liquidation_start",
        T,
        "Start end-of-day exits",
        "Chicago time. Any position still open is sold from here, with retries until flat.",
        "time",
        effect="next_session",
        group="Day",
    ),
    _f(
        "mode.plan.end_buffer_minutes",
        T,
        "Plan to be out before the close by",
        "Each trade's planned holding time ends this many minutes before the close.",
        "slider",
        min=0,
        max=120,
        step=5,
        unit="min",
        group="Day",
    ),
    _f(
        "schedule.scanner_refresh_minutes",
        T,
        "Re-rank opportunities every",
        "How often the bot re-scores every stock.",
        "slider",
        min=1,
        max=30,
        step=1,
        unit="min",
        effect="immediate",
        advanced=True,
        group="Cadence",
    ),
    _f(
        "schedule.monitor_minutes",
        T,
        "Re-check holdings every",
        "How often each open position's forecast is refreshed (stops and targets are checked every minute).",
        "slider",
        min=1,
        max=15,
        step=1,
        unit="min",
        effect="immediate",
        advanced=True,
        group="Cadence",
    ),
    _f(
        "auto_schedule",
        T,
        "Run automatically on trading days",
        "Follow the real exchange calendar (including holidays and early closes) in the background. Only meaningful with a live feed.",
        "switch",
        effect="immediate",
        advanced=True,
        group="Cadence",
    ),
    # ---------------------------------------------------------------- MarketWatch Game
    _f(
        "game.min_price",
        G,
        "Lowest price your game allows",
        "Stocks below this price are never bought.",
        "number",
        min=0,
        max=1000,
        step=0.5,
        unit="$",
        group="Rules",
    ),
    _f(
        "game.allowed_exchanges",
        G,
        "Allowed exchanges",
        "Comma-separated, e.g. NYSE, NASDAQ.",
        "text",
        advanced=True,
        group="Rules",
    ),
    _f(
        "game.security_types",
        G,
        "Allowed security types",
        "Comma-separated, e.g. common_stock, etf.",
        "text",
        advanced=True,
        group="Rules",
    ),
    _f(
        "game.supported_order_types",
        G,
        "Order types your game supports",
        "If market orders are not available the bot uses marketable limit orders instead.",
        "multiselect",
        options=[("market", "Market"), ("limit", "Limit")],
        group="Rules",
    ),
    _f(
        "game.max_position_pct",
        G,
        "Game limit: % of account in one stock",
        "Leave empty if your game has no such limit. The bot uses the stricter of this and its own limit.",
        "number",
        min=0.1,
        max=100,
        step=0.5,
        unit="%",
        nullable=True,
        group="Limits",
    ),
    _f(
        "game.max_position_value",
        G,
        "Game limit: $ in one stock",
        "Leave empty if there is no dollar limit.",
        "number",
        min=1,
        max=1e9,
        step=1000,
        unit="$",
        nullable=True,
        advanced=True,
        group="Limits",
    ),
    _f(
        "game.max_positions",
        G,
        "Game limit: number of holdings",
        "Leave empty if there is no limit.",
        "number",
        min=1,
        max=1000,
        step=1,
        nullable=True,
        advanced=True,
        group="Limits",
    ),
    _f(
        "game.commission_per_trade",
        G,
        "Commission per trade",
        "Flat fee per order in your game.",
        "number",
        min=0,
        max=1000,
        step=0.5,
        unit="$",
        group="Costs",
    ),
    _f(
        "game.commission_per_share",
        G,
        "Commission per share",
        "Per-share fee in your game.",
        "number",
        min=0,
        max=10,
        step=0.001,
        unit="$",
        advanced=True,
        group="Costs",
    ),
    _f(
        "game.eligibility_mode",
        G,
        "Stock list used in replays",
        "Live/demo sessions use your list as it stands today. Replays and backtests use the list as it was on each date, so today's knowledge never leaks into the past.",
        "select",
        effect="next_session",
        options=[
            ("current_list", "Current list (live / demo)"),
            ("point_in_time", "List as of each date (replays)"),
        ],
        advanced=True,
        group="Stock list",
    ),
    # ---------------------------------------------------------------- Market Data
    _f(
        "data_mode",
        D,
        "Data source",
        "Demo = built-in synthetic market (fabricated prices). Historical = replay a dataset. Connected = live provider (needs an API key).",
        "select",
        effect="idle_only",
        options=[
            ("demo", "Demo (synthetic)"),
            ("historical", "Historical replay"),
            ("connected", "Connected provider"),
        ],
        group="Source",
    ),
    _f(
        "demo_seed",
        D,
        "Demo market seed",
        "Same number = same fabricated market every time.",
        "number",
        min=0,
        max=1_000_000,
        step=1,
        effect="idle_only",
        advanced=True,
        group="Source",
    ),
    _f(
        "historical_dataset",
        D,
        "Historical dataset",
        "Folder name of the dataset to replay.",
        "text",
        effect="idle_only",
        advanced=True,
        group="Source",
    ),
    _f(
        "demo_speed",
        D,
        "Replay speed",
        "Simulated minutes per real second when streaming a demo or historical day.",
        "slider",
        min=0.5,
        max=600,
        step=0.5,
        effect="immediate",
        group="Source",
    ),
    _f(
        "allow_delayed_feed_trading",
        D,
        "Allow trading on a delayed feed",
        "OFF is strongly recommended: delayed prices make automated entries unreliable.",
        "switch",
        effect="immediate",
        advanced=True,
        group="Source",
    ),
    _f(
        "costs.assumed_spread_bps",
        D,
        "Assumed spread when the data has no quotes",
        "Basis points (100 = 1%). Bar-only data cannot show the bid/ask spread, so a cost is assumed. Higher = the bot trades less.",
        "slider",
        min=0,
        max=200,
        step=1,
        unit="bps",
        group="Costs",
    ),
    _f(
        "costs.slippage_bps",
        D,
        "Slippage per trade",
        "Extra price movement against you on every fill (basis points).",
        "slider",
        min=0,
        max=100,
        step=0.5,
        unit="bps",
        advanced=True,
        group="Costs",
    ),
    _f(
        "costs.max_participation",
        D,
        "Largest share of a minute's volume",
        "Orders larger than this fraction of one minute's volume fill partially.",
        "slider",
        min=0.01,
        max=1,
        step=0.01,
        advanced=True,
        group="Costs",
    ),
    _f(
        "scanner.filters.min_price",
        D,
        "Scanner: lowest price",
        "Basic liquidity filter applied before ranking.",
        "number",
        min=0,
        max=10_000,
        step=0.5,
        unit="$",
        advanced=True,
        group="Scanner",
    ),
    _f(
        "scanner.filters.max_price",
        D,
        "Scanner: highest price",
        "Basic filter applied before ranking.",
        "number",
        min=0.5,
        max=100_000,
        step=1,
        unit="$",
        advanced=True,
        group="Scanner",
    ),
    _f(
        "scanner.filters.min_dollar_volume",
        D,
        "Scanner: minimum dollars traded",
        "Skips thinly traded stocks.",
        "number",
        min=0,
        max=1e10,
        step=10_000,
        unit="$",
        advanced=True,
        group="Scanner",
    ),
    _f(
        "scanner.filters.max_spread_pct",
        D,
        "Scanner: widest spread",
        "Skips stocks with wider quoted spreads. Empty = no limit.",
        "number",
        min=0.01,
        max=20,
        step=0.05,
        unit="%",
        nullable=True,
        advanced=True,
        group="Scanner",
    ),
    # ---------------------------------------------------------------- General
    _f(
        "ui.simple_mode",
        U,
        "Simple mode",
        "Hide advanced controls. The recommended defaults need no tuning.",
        "switch",
        effect="immediate",
        group="Interface",
    ),
    _f(
        "ui.theme",
        U,
        "Theme",
        "Dark is the default.",
        "select",
        effect="immediate",
        options=[("dark", "Dark"), ("light", "Light")],
        group="Interface",
    ),
    _f(
        "ui.show_research_candidates",
        U,
        "Show research candidates",
        "Show promising stocks that are not yet confirmed as available in your game (never traded).",
        "switch",
        effect="immediate",
        group="Interface",
    ),
    _f(
        "ui.notifications",
        U,
        "In-app alerts",
        "Show banners for warnings such as emergency stops or stale data.",
        "switch",
        effect="immediate",
        group="Interface",
    ),
]

BY_PATH: dict[str, FieldSpec] = {f.path: f for f in FIELDS}
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# --------------------------------------------------------------------------- path helpers
def real_path(settings: AppSettings, path: str) -> list[str]:
    parts = path.split(".")
    if parts[0] == "mode":
        return ["trading", "profiles", settings.trading.mode.value, *parts[1:]]
    return parts


def _dig(obj: Any, parts: list[str]) -> Any:
    cur = obj
    for p in parts:
        cur = cur.get(p) if isinstance(cur, dict) else getattr(cur, p)
    return cur


def get_value(settings: AppSettings, path: str) -> Any:
    return _dig(settings.model_dump(mode="json"), real_path(settings, path))


def recommended(settings: AppSettings, path: str) -> Any:
    """The recommended value: the active mode's built-in preset (mode.*, risk.*) or the application default."""
    mode = settings.trading.mode
    parts = path.split(".")
    if parts[0] == "mode":
        return _dig(preset_profile(mode).model_dump(mode="json"), parts[1:])
    if parts[0] == "risk":
        return _dig(preset_profile(mode).risk.model_dump(mode="json"), parts[1:])
    if path == "trading.mode":
        return TradingMode.MAX_EXPECTED_RETURN.value
    return _dig(AppSettings().model_dump(mode="json"), parts)


def nested_patch(settings: AppSettings, changes: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path, value in changes.items():
        cur = out
        parts = real_path(settings, path)
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value
    return out


# --------------------------------------------------------------------------- validation
def _check_value(spec: FieldSpec, value: Any) -> str | None:
    if value is None:
        return None if spec.nullable else "A value is required."
    c = spec.control
    if c in ("slider", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "Must be a number."
        if spec.min is not None and value < spec.min:
            return f"Must be at least {spec.min:g}{spec.unit}."
        if spec.max is not None and value > spec.max:
            return f"Must be at most {spec.max:g}{spec.unit}."
    elif c == "switch":
        if not isinstance(value, bool):
            return "Must be on or off."
    elif c == "select":
        if value not in {v for v, _ in spec.options}:
            return "Pick one of: " + ", ".join(v for v, _ in spec.options)
    elif c == "time":
        if not isinstance(value, str) or not _TIME_RE.match(value):
            return "Use 24-hour HH:MM, e.g. 14:45."
    elif c == "multiselect":
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            return "Must be a list."
        if spec.options:
            bad = [x for x in value if x not in {v for v, _ in spec.options}]
            if bad:
                return f"Not allowed: {', '.join(bad)}."
        if not value:
            return "Pick at least one."
    elif c == "text":
        if isinstance(value, list):
            return None
        if not isinstance(value, str):
            return "Must be text."
    return None


def validate_changes(settings: AppSettings, changes: dict[str, Any]) -> list[dict[str, str]]:
    """Field-level problems (server-side twin of the browser checks). Empty list = fine so far."""
    errors: list[dict[str, str]] = []
    for path, value in changes.items():
        spec = BY_PATH.get(path)
        if spec is None:
            errors.append({"path": path, "message": "Unknown setting."})
            continue
        msg = _check_value(spec, value)
        if msg:
            errors.append({"path": path, "message": f"{spec.label}: {msg}"})
    return errors


def apply_changes(
    settings: AppSettings, changes: dict[str, Any]
) -> tuple[AppSettings | None, list[dict[str, str]]]:
    """Validate then build the new settings object. Returns (settings, errors)."""
    errors = validate_changes(settings, changes)
    if errors:
        return None, errors
    patch = nested_patch(settings, changes)
    if "strategies" in patch and "enabled" in patch["strategies"]:
        from mwbot.strategies.builtin import STRATEGY_CLASSES

        bad = [s for s in patch["strategies"]["enabled"] if s not in STRATEGY_CLASSES]
        if bad:
            return None, [{"path": "strategies.enabled", "message": f"Unknown strategy: {', '.join(bad)}"}]
    merged = _deep_merge(settings.model_dump(mode="json"), patch)
    _split_lists(merged)
    try:
        new = AppSettings.model_validate(merged)
    except ValidationError as exc:
        return None, friendly_errors(exc)
    return new, []


def _split_lists(d: dict[str, Any]) -> None:
    g = d.get("game", {})
    for k in ("allowed_exchanges", "security_types"):
        if isinstance(g.get(k), str):
            g[k] = [x.strip() for x in g[k].split(",") if x.strip()]


def friendly_errors(exc: ValidationError) -> list[dict[str, str]]:
    out = []
    for e in exc.errors():
        loc = [str(x) for x in e["loc"]]
        # map a real path back to the dashboard's virtual path when possible
        path = ".".join(loc)
        if len(loc) > 3 and loc[0] == "trading" and loc[1] == "profiles":
            path = "mode." + ".".join(loc[3:])
        spec = BY_PATH.get(path)
        msg = str(e["msg"]).replace("Value error, ", "")
        out.append({"path": path, "message": f"{spec.label}: {msg}" if spec else msg})
    return out


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------- effects preview
def effects_of(
    settings: AppSettings,
    changes: dict[str, Any],
    *,
    session_running: bool,
    open_positions: int,
) -> list[dict[str, Any]]:
    """Plain-language consequences of a change set - shown BEFORE saving and after."""
    out: list[dict[str, Any]] = []
    for path, value in changes.items():
        spec = BY_PATH.get(path)
        if spec is None:
            continue
        old = get_value(settings, path)
        if old == value:
            continue
        eff = spec.effect
        text = EFFECTS[eff]
        blocked = False
        if eff == "idle_only" and (session_running or open_positions):
            blocked = True
            text = "Not allowed right now: stop the running session and close positions first."
        elif eff == "next_session" and session_running:
            text = "Saved now; takes effect from the next session (the running one keeps its schedule)."
        elif eff in ("new_trades", "open_positions") and open_positions:
            text = EFFECTS[eff]
        out.append(
            {
                "path": path,
                "label": spec.label,
                "from": old,
                "to": value,
                "effect": eff,
                "message": text,
                "blocked": blocked,
            }
        )
    if "trading.mode" in changes and changes["trading.mode"] != settings.trading.mode.value:
        out.append(
            {
                "path": "trading.mode",
                "label": "Trading mode",
                "from": settings.trading.mode.value,
                "to": changes["trading.mode"],
                "effect": "new_trades",
                "message": "New trades use the other mode's sizing and rules. "
                + (
                    f"Your {open_positions} open position(s) keep the plan they were opened with."
                    if open_positions
                    else "Trade history is kept."
                ),
                "blocked": False,
            }
        )
    return out


# --------------------------------------------------------------------------- schema for the UI
def build_schema(settings: AppSettings) -> dict[str, Any]:
    from mwbot.strategies.builtin import STRATEGY_CLASSES

    fields = []
    for spec in FIELDS:
        d = spec.to_dict()
        if spec.path == "strategies.enabled":
            d["options"] = [{"value": sid, "label": cls.name} for sid, cls in STRATEGY_CLASSES.items()]
        d["value"] = get_value(settings, spec.path)
        d["recommended"] = recommended(settings, spec.path)
        d["is_recommended"] = _same(d["value"], d["recommended"])
        fields.append(d)
    mode = settings.trading.mode
    return {
        "categories": CATEGORIES,
        "fields": fields,
        "mode": {
            "active": mode.value,
            "label": mode.label,
            "modes": [
                {
                    "id": m.value,
                    "label": m.label,
                    "recommended": m == TradingMode.MAX_EXPECTED_RETURN,
                    "summary": MODE_SUMMARY[m.value],
                }
                for m in TradingMode
            ],
        },
        "scanner_presets": sorted(PRESET_FILTERS),
    }


MODE_SUMMARY = {
    "max_expected_return": (
        "The recommended default. Looks for the stocks with the best expected return after costs for the time "
        "left in the day, takes moderate-sized positions, and cuts losses early."
    ),
    "aggressive_upside": (
        "Accepts more variance for more potential upside: larger positions, a wider range of stocks and farther "
        "targets. It keeps the same stop-losses, daily loss limit and end-of-day exits - it just risks more per trade."
    ),
}


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-9
        except (TypeError, ValueError):
            return False
    return bool(a == b)


def recommended_changes(settings: AppSettings, category: str | None = None) -> dict[str, Any]:
    """Changes that bring a category (or everything) back to the recommended values."""
    out: dict[str, Any] = {}
    for spec in FIELDS:
        if category and spec.category != category:
            continue
        if spec.path == "risk.starting_capital":
            continue  # resetting preferences must never silently change the account's starting money
        if spec.path in ("trading.mode", "data_mode", "historical_dataset", "demo_seed") and not category:
            continue  # "reset to recommended" keeps your mode and data source choices
        out[spec.path] = recommended(settings, spec.path)
    return out
