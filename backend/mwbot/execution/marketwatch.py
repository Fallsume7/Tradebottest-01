"""SKELETON for a future, AUTHORIZED MarketWatch Virtual Stock Exchange adapter.

Nothing in this class works, on purpose. It exists so the rest of the system has a named seam
and so the integration checklist lives next to the code. See docs/MARKETWATCH_INTEGRATION.md.

Not done (and not to be guessed at from the cloud):
* How the game permits automation at all (terms of use / permission from the game organiser).
* Authentication flow on the user's own machine (the user logs in; we never store passwords or cookies in git).
* Real page structure / selectors, order-confirmation flow, pending-order and holdings pages.
* Rate limits, delays, and what the game does with after-hours / limit orders.

No selectors, endpoints, CAPTCHA handling or private-API calls are implemented or implied here.
"""

from __future__ import annotations

from typing import ClassVar

from mwbot.domain import OrderRequest
from mwbot.execution.base import (
    ExecutionAdapter,
    ExecutionError,
    ExternalOrder,
    Holding,
    PreparedOrder,
    SessionStatus,
)


class NotConfiguredError(ExecutionError):
    pass


class MarketWatchAdapter(ExecutionAdapter):
    name: ClassVar[str] = "marketwatch"
    is_real_money: ClassVar[bool] = False  # MarketWatch Virtual Stock Exchange is a play-money game

    _MSG = "MarketWatch adapter is not implemented. See docs/MARKETWATCH_INTEGRATION.md (local integration phase)."

    def check_session(self) -> SessionStatus:
        return SessionStatus(self.name, False, None, "not configured: " + self._MSG)

    def get_buying_power(self) -> float:
        raise NotConfiguredError(self._MSG)

    def get_holdings(self) -> list[Holding]:
        raise NotConfiguredError(self._MSG)

    def prepare_order(self, request: OrderRequest) -> PreparedOrder:
        raise NotConfiguredError(self._MSG)

    def submit_order(self, prepared: PreparedOrder) -> ExternalOrder:
        raise NotConfiguredError(self._MSG)

    def get_order_status(self, external_id: str) -> ExternalOrder:
        raise NotConfiguredError(self._MSG)

    def cancel_order(self, external_id: str) -> bool:
        raise NotConfiguredError(self._MSG)
