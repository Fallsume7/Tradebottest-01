"""Adapter factory with the paper-only safety latch."""

from __future__ import annotations

from mwbot.config import EnvSettings
from mwbot.execution.base import ExecutionAdapter, ExecutionError


def assert_virtual_only(adapter: ExecutionAdapter) -> None:
    """Hard stop: no adapter that claims to move real money may ever be wired into the bot."""
    if adapter.is_real_money:
        raise ExecutionError(f"{adapter.name}: real-money adapters are forbidden in this project")


def create_adapter(env: EnvSettings) -> ExecutionAdapter:
    """Build the external (mirror) adapter selected by MWBOT_EXECUTION_MODE.

    ``paper`` needs no external adapter (the internal PaperBroker is always the source of truth).
    """
    if env.allow_real_money:  # defensive: the settings validator already rejects this
        raise ExecutionError("real money is not supported")
    if env.execution_mode == "mock_browser":
        from mwbot.execution.mock_browser import MockBrowserAdapter

        ad = MockBrowserAdapter(env.mock_site_url, env.mock_site_user, env.mock_site_password)
        assert_virtual_only(ad)
        ad.ensure_logged_in()
        return ad
    if env.execution_mode == "marketwatch":
        from pathlib import Path

        from mwbot.execution.marketwatch import MarketWatchAdapter

        if not env.marketwatch_game_url:
            raise ExecutionError("set MWBOT_MARKETWATCH_GAME_URL to your game's address first")
        data = Path(env.data_dir)
        mw = MarketWatchAdapter(
            env.marketwatch_game_url,
            cdp_url=env.marketwatch_cdp_url,
            stage=env.marketwatch_stage,
            ledger_path=data / "marketwatch_ledger.json",
            shots_dir=data / "marketwatch_shots",
            min_action_gap_s=env.marketwatch_min_action_gap_s,
        )
        assert_virtual_only(mw)
        return mw
    raise ExecutionError("execution mode 'paper' uses the internal broker; no external adapter to create")
