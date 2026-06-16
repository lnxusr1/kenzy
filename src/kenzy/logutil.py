"""Shared logging helpers for the Kenzy FastAPI services."""

from __future__ import annotations

import logging


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
