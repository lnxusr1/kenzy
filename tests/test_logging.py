"""Tests for the decoupled display/capture logging (design/log-visibility.md)."""

from __future__ import annotations

import logging

from kenzy.logutil import (
    TRACE,
    configure_logging,
    install_ring_handler,
    level_value,
    remove_ring_handler,
    set_display_level,
)


def test_level_value():
    assert level_value("debug") == logging.DEBUG
    assert level_value("TRACE") == TRACE
    assert level_value("info") == logging.INFO
    assert level_value(20) == 20
    assert level_value("bogus", logging.WARNING) == logging.WARNING
    assert level_value(None, logging.ERROR) == logging.ERROR


def test_ring_captures_below_display_level():
    # Console at INFO, ring capture at DEBUG: the buffer must keep DEBUG even
    # though the console wouldn't print it. TRACE (below capture) is dropped.
    name = "kenzy.captest"
    logger = configure_logging(logging.INFO, logger_name=name)
    buf = install_ring_handler(name, capacity=50, level=logging.DEBUG)
    try:
        logger.debug("deep")
        logger.info("shallow")
        logger.log(TRACE, "trace-hidden")

        msgs = {r["msg"] for r in buf.tail()}
        assert "deep" in msgs  # captured below display level
        assert "shallow" in msgs
        assert "trace-hidden" not in msgs  # below capture level → not kept

        # The viewer's level filter still narrows upward within the buffer.
        info_only = {r["msg"] for r in buf.tail(logging.INFO)}
        assert "shallow" in info_only and "deep" not in info_only
    finally:
        remove_ring_handler(buf, name, display_level=logging.INFO)
        assert logger.level == logging.INFO  # restored, no lingering deep capture


def test_set_display_level_preserves_capture():
    name = "kenzy.disptest"
    logger = configure_logging(logging.INFO, logger_name=name)
    buf = install_ring_handler(name, capacity=50, level=logging.DEBUG)
    try:
        set_display_level(logging.WARNING, name)
        # Display raised, but the logger stays low enough to keep feeding capture.
        assert logger.level == logging.DEBUG
        logger.debug("still-captured")
        assert any(r["msg"] == "still-captured" for r in buf.tail())
    finally:
        remove_ring_handler(buf, name, display_level=logging.WARNING)


def test_trace_capture_keeps_trace():
    name = "kenzy.tracetest"
    logger = configure_logging(logging.INFO, logger_name=name)
    buf = install_ring_handler(name, capacity=50, level=TRACE)
    try:
        logger.log(TRACE, "frame")
        assert any(r["msg"] == "frame" and r["level"] == "TRACE" for r in buf.tail())
    finally:
        remove_ring_handler(buf, name, display_level=logging.INFO)
