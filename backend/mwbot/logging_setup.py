"""Structured logging: key=value text by default, JSON lines when MWBOT_LOG_JSON=true."""

from __future__ import annotations

import json
import logging
import sys
from collections import deque
from datetime import UTC, datetime
from typing import Any

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in _RESERVED})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class KeyValueFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extras = " ".join(f"{k}={v!r}" for k, v in record.__dict__.items() if k not in _RESERVED)
        base = (
            f"{datetime.fromtimestamp(record.created, UTC):%Y-%m-%dT%H:%M:%SZ} "
            f"{record.levelname:<7} {record.name}: {record.getMessage()}"
        )
        out = f"{base} {extras}".rstrip()
        if record.exc_info:
            out += "\n" + self.formatException(record.exc_info)
        return out


class RingBufferHandler(logging.Handler):
    """Keeps the last N warnings/errors so the dashboard can surface them."""

    def __init__(self, capacity: int = 200) -> None:
        super().__init__(level=logging.WARNING)
        self.records: deque[dict[str, Any]] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(
            {
                "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
        )


ring_buffer = RingBufferHandler()


def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        if getattr(h, "_mwbot", False):
            root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if json_logs else KeyValueFormatter())
    handler._mwbot = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    if ring_buffer not in root.handlers:
        root.addHandler(ring_buffer)
    for noisy in ("httpx", "httpcore", "apscheduler", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
