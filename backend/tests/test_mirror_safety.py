"""What the bot does when orders are mirrored to an outside venue such as MarketWatch."""

from __future__ import annotations

from typing import ClassVar

from mwbot.bot.engine import TradingBot
from mwbot.domain import OrderRequest, OrderStatus
from mwbot.execution.base import (
    ExecutionAdapter,
    ExecutionError,
    ExternalOrder,
    Holding,
    MirrorSkipped,
    PreparedOrder,
    SessionStatus,
)
from mwbot.trading.risk import BotState

from .test_opportunity_bot import ScriptedForecaster, drift_up, run


class RecordingMirror(ExecutionAdapter):
    name: ClassVar[str] = "recording"

    def __init__(self, *, real_feed_only: bool, mode: str = "ok") -> None:
        self.requires_real_feed = real_feed_only  # type: ignore[misc]
        self.mode = mode
        self.prepared: list[OrderRequest] = []
        self.submitted: list[OrderRequest] = []

    def check_session(self) -> SessionStatus:
        return SessionStatus(self.name, True, None)

    def get_buying_power(self) -> float:
        return 0.0

    def get_holdings(self) -> list[Holding]:
        return []

    def prepare_order(self, request: OrderRequest) -> PreparedOrder:
        self.prepared.append(request)
        if self.mode == "skip":
            raise MirrorSkipped("test stage")
        if self.mode == "fail":
            raise ExecutionError("venue down")
        return PreparedOrder(request, self.name, None, None)

    def submit_order(self, prepared: PreparedOrder) -> ExternalOrder:
        self.submitted.append(prepared.request)
        return ExternalOrder(f"E{len(self.submitted)}", prepared.request.client_order_id, OrderStatus.PENDING)

    def get_order_status(self, external_id: str) -> ExternalOrder:
        return ExternalOrder(external_id, "", OrderStatus.PENDING)

    def cancel_order(self, external_id: str) -> bool:
        return True


def _bars(sess):
    return {"TST": drift_up(sess, 40, 11.2, 60)}


def test_control_a_normal_mirror_receives_the_orders(sess):
    m = RecordingMirror(real_feed_only=False)
    _bot, broker, _ = run(_bars(sess), forecaster=ScriptedForecaster(1.5), mirror=m)
    assert broker.trades and m.submitted  # the bot trades and the venue is told


def test_a_real_feed_only_venue_is_never_sent_orders_decided_on_replayed_or_synthetic_data(sess):
    m = RecordingMirror(real_feed_only=True)
    bot, broker, _ = run(_bars(sess), forecaster=ScriptedForecaster(1.5), mirror=m)
    assert m.prepared == [] and m.submitted == [] and not broker.trades
    assert any("refusing to send orders" in n["message"] for n in bot.decision_log), [
        n["message"] for n in bot.decision_log
    ][-6:]


def test_a_skipped_order_is_a_note_not_an_alert_and_does_not_pause_the_bot(sess):
    m = RecordingMirror(real_feed_only=False, mode="skip")
    bot, broker, _ = run(_bars(sess), forecaster=ScriptedForecaster(1.5), mirror=m)
    assert broker.trades  # the paper account keeps working
    assert m.prepared and m.submitted == []
    assert not [a for a in bot.alerts if a["code"] == "mirror_failed"]
    assert any("NOT sent" in n["message"] for n in bot.decision_log)
    assert bot.state != BotState.PAUSED


def test_a_real_failure_still_alerts_and_pauses(sess):
    m = RecordingMirror(real_feed_only=False, mode="fail")
    bot, _broker, _ = run(_bars(sess), forecaster=ScriptedForecaster(1.5), mirror=m)
    assert [a for a in bot.alerts if a["code"] == "mirror_failed" and a["level"] == "critical"]
    assert isinstance(bot, TradingBot) and bot.state == BotState.PAUSED
