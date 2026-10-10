"""Run browser-adapter methods on the adapter's one dedicated thread.

Playwright's sync API is bound to the thread that started it. The bot calls adapters from its own
session thread (and the API from others), so every browser touch is funnelled to ONE worker thread.
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable
from typing import Any


def on_worker(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        if threading.get_ident() == self._worker_ident:
            return method(self, *args, **kwargs)
        return self._pool.submit(method, self, *args, **kwargs).result()

    return wrapper
