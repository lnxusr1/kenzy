"""Shared logging helpers for Kenzy services and the node.

Two levels are in play, deliberately decoupled (see
``design/log-visibility.md``):

* **display level** — what the process prints to its console (``log_level``,
  default ``info``); set on the ``kenzy`` logger's own StreamHandler.
* **capture level** — how deep the in-memory ``RingBufferHandler`` keeps records
  for the dashboard log viewer (``log_capture_level``, default ``debug``); set on
  the ring handler. The owning logger is held at the *minimum* of the two so the
  deeper records reach the ring handler while the console stays at the display
  level.

A custom ``TRACE`` (5) level sits below ``DEBUG`` for hot-path/per-frame logs so
default ``debug`` capture stays readable; viewing TRACE is an explicit opt-in.
"""

from __future__ import annotations

import collections
import logging
from typing import Any

#: Custom level below DEBUG for hot-path / per-frame logs.
TRACE = 5
logging.addLevelName(TRACE, "TRACE")

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def level_value(name: object, default: int = logging.INFO) -> int:
    """Resolve a level name (``"debug"``, ``"trace"``, …) or int to its number."""
    if isinstance(name, int):
        return name
    if name is None:
        return default
    return logging.getLevelNamesMapping().get(str(name).upper(), default)


def _stream_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [
        h
        for h in logger.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, RingBufferHandler)
    ]


def configure_logging(
    display_level: int, verbose: bool = False, logger_name: str = "kenzy"
) -> logging.Logger:
    """Set up console logging for a Kenzy process and return the app logger.

    Third-party logs go through the root logger (``basicConfig``): everything at
    ``display_level`` when ``verbose``, else only WARNING+. The app logger gets
    its **own** StreamHandler pinned to ``display_level`` and does not propagate,
    so lowering its level later (to capture DEBUG into the ring buffer) never
    spills DEBUG onto the console.
    """
    logging.basicConfig(level=display_level if verbose else logging.WARNING, format=_FMT)
    logger = logging.getLogger(logger_name)
    logger.setLevel(display_level)
    logger.propagate = False
    for h in _stream_handlers(logger):  # avoid duplicates if called again
        logger.removeHandler(h)
    sh = logging.StreamHandler()
    sh.setLevel(display_level)
    sh.setFormatter(logging.Formatter(_FMT))
    logger.addHandler(sh)
    return logger


def set_display_level(level: int, logger_name: str = "kenzy") -> None:
    """Change the console (display) level live, preserving any capture depth."""
    logger = logging.getLogger(logger_name)
    for h in _stream_handlers(logger):
        h.setLevel(level)
    ring_levels = [h.level for h in logger.handlers if isinstance(h, RingBufferHandler)]
    logger.setLevel(min([level, *ring_levels]))


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


def install_ring_handler(
    logger_name: str = "kenzy", capacity: int = 1000, level: int = logging.DEBUG
) -> RingBufferHandler:
    """Attach a RingBufferHandler at ``level`` and return it for reading.

    The handler captures records at or above ``level``; the owning logger is
    lowered to ``min(current, level)`` so those records actually reach it (the
    console StreamHandler keeps its own higher level, so the console is unchanged).
    """
    handler = RingBufferHandler(capacity)
    handler.setLevel(level)
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    logger.setLevel(min(logger.level, level) if logger.level else level)
    return handler


def remove_ring_handler(
    handler: RingBufferHandler, logger_name: str = "kenzy", display_level: int | None = None
) -> None:
    """Detach a ring handler; restore the logger to the display level if given."""
    logger = logging.getLogger(logger_name)
    logger.removeHandler(handler)
    if display_level is not None:
        ring_levels = [h.level for h in logger.handlers if isinstance(h, RingBufferHandler)]
        logger.setLevel(min([display_level, *ring_levels]))
