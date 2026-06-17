"""Shared logging helpers for the Kenzy FastAPI services."""

from __future__ import annotations

import collections
import logging
from typing import Any


class _HealthCheckAccessFilter(logging.Filter):
    """Log successful /health polls at DEBUG instead of INFO.

    The dashboard (and any load balancer) polls /health constantly; at INFO it
    floods a 24/7 log. Demoting to DEBUG means these lines follow log_level like
    everything else — hidden by default, visible when you ask for debug. Non-2xx
    health responses are left at INFO so real failures stay visible.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "/health" in msg and '" 2' in msg:
            record.levelno = logging.DEBUG
            record.levelname = "DEBUG"
            return record.levelno >= logging.getLogger(record.name).getEffectiveLevel()
        return True


def quiet_health_access_log() -> None:
    """Demote routine /health access lines to DEBUG so they follow log_level.

    Attaches to the ``uvicorn.access`` logger. uvicorn's ``dictConfig`` resets
    that logger's handlers but not its filters, so this survives ``uvicorn.run``.
    Safe to call once per service before starting the server.
    """
    logging.getLogger("uvicorn.access").addFilter(_HealthCheckAccessFilter())


class RingBufferHandler(logging.Handler):
    """A bounded in-memory log handler for the pull-based dashboard log viewer.

    Keeps the last ``capacity`` records as plain dicts; nothing touches disk. Used
    by both the HTTP services (exposed via ``GET /logs``) and the node (returned
    over the ``request_logs``/``logs`` protocol pair).
    """

    def __init__(self, capacity: int = 1000) -> None:
        super().__init__()
        self.buffer: collections.deque[dict[str, Any]] = collections.deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(
                {
                    "ts": record.created,
                    "level": record.levelname,
                    "levelno": record.levelno,
                    "name": record.name,
                    "msg": record.getMessage(),
                }
            )
        except Exception:  # logging must never raise
            pass

    def tail(self, level: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        """Return up to ``limit`` most-recent records at or above ``level``."""
        items = [r for r in self.buffer if r["levelno"] >= level]
        return items[-max(1, min(limit, self.buffer.maxlen or limit)) :]


def install_ring_handler(logger_name: str = "kenzy", capacity: int = 1000) -> RingBufferHandler:
    """Attach a RingBufferHandler to ``logger_name`` and return it for reading."""
    handler = RingBufferHandler(capacity)
    logging.getLogger(logger_name).addHandler(handler)
    return handler
