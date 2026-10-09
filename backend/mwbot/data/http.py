"""HTTP helper with bounded exponential backoff, Retry-After handling and a simple rate limiter."""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

log = logging.getLogger(__name__)


class ProviderHTTPError(Exception):
    def __init__(self, message: str, status: int | None = None, retriable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retriable = retriable


class AuthError(ProviderHTTPError):
    """401/403: bad key or the subscription does not include this endpoint."""


class RateLimiter:
    """Minimum spacing between calls (thread-safe)."""

    def __init__(self, calls_per_second: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.min_interval = 1.0 / calls_per_second if calls_per_second > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if now < self._next:
                self._sleep(self._next - now)
                now = self._next
            self._next = now + self.min_interval


def backoff_delays(max_retries: int, base: float = 0.5, cap: float = 8.0, jitter: bool = True) -> list[float]:
    """Bounded exponential schedule: base, 2*base, 4*base ... capped (deterministic when jitter=False)."""
    out = []
    for i in range(max_retries):
        d = min(cap, base * (2**i))
        out.append(d * (0.75 + random.random() * 0.5) if jitter else d)
    return out


def request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    max_retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    limiter: RateLimiter | None = None,
    jitter: bool = True,
) -> dict[str, Any]:
    """GET/POST returning JSON. Retries 429/5xx/network errors (bounded); fails fast on 401/403/4xx."""
    delays = backoff_delays(max_retries, jitter=jitter)
    last: Exception | None = None
    for attempt in range(max_retries + 1):
        if limiter:
            limiter.wait()
        try:
            resp = client.request(method, url, params=params)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last = ProviderHTTPError(f"network error: {exc}", retriable=True)
            log.warning("provider network error", extra={"attempt": attempt, "url": _safe(url)})
        else:
            if resp.status_code == 200:
                try:
                    return resp.json()  # type: ignore[no-any-return]
                except ValueError as exc:
                    raise ProviderHTTPError(f"invalid JSON from provider: {exc}") from exc
            if resp.status_code in (401, 403):
                raise AuthError(
                    f"{resp.status_code}: key rejected or endpoint not included in your plan", status=resp.status_code
                )
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                last = ProviderHTTPError(f"HTTP {resp.status_code}", status=resp.status_code, retriable=True)
                if attempt < max_retries:
                    wait = delays[attempt]
                    if retry_after and retry_after.isdigit():
                        wait = min(float(retry_after), 30.0)  # honour but bound
                    log.warning("provider throttled/unavailable", extra={"status": resp.status_code, "wait": wait})
                    sleep(wait)
                continue
            raise ProviderHTTPError(f"HTTP {resp.status_code}", status=resp.status_code)
        if attempt < max_retries:
            sleep(delays[attempt])
    assert last is not None
    raise last


def _safe(url: str) -> str:
    """Strip query strings (API keys!) before logging."""
    return url.split("?", 1)[0]
