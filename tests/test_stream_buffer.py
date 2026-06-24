"""Tests for the live streaming playback ring buffer (intercom/media primitive)."""

from __future__ import annotations

import numpy as np

from kenzy.node.client import _StreamBuffer


def test_read_spans_chunk_boundaries():
    buf = _StreamBuffer()
    buf.feed(np.array([1, 2, 3], dtype=np.int16))
    buf.feed(np.array([4, 5], dtype=np.int16))
    out = buf.read(5)
    assert out.tolist() == [1, 2, 3, 4, 5]
    assert out.dtype == np.int16


def test_partial_reads_keep_position():
    buf = _StreamBuffer()
    buf.feed(np.array([10, 20, 30, 40], dtype=np.int16))
    assert buf.read(2).tolist() == [10, 20]
    assert buf.read(2).tolist() == [30, 40]


def test_underflow_zero_pads():
    buf = _StreamBuffer()
    buf.feed(np.array([7, 8], dtype=np.int16))
    out = buf.read(5)  # only 2 available
    assert out.tolist() == [7, 8, 0, 0, 0]
    # Fully drained → silence.
    assert buf.read(3).tolist() == [0, 0, 0]


def test_empty_and_clear():
    buf = _StreamBuffer()
    assert buf.empty
    buf.feed(np.array([1, 2, 3], dtype=np.int16))
    assert not buf.empty
    buf.read(1)
    assert not buf.empty  # 2 samples still buffered
    buf.clear()
    assert buf.empty
    assert buf.read(2).tolist() == [0, 0]


def test_feed_ignores_empty_and_coerces_dtype():
    buf = _StreamBuffer()
    buf.feed(np.array([], dtype=np.int16))
    assert buf.empty
    buf.feed(np.array([1.0, 2.0, 3.0], dtype=np.float64))  # coerced to int16
    out = buf.read(3)
    assert out.tolist() == [1, 2, 3] and out.dtype == np.int16
