"""Tests for node playback volume + mute (with the muted-but-audible ready chime)."""

from __future__ import annotations

import numpy as np
import pytest

from kenzy.llm.builtin_skills.volume import classify
from kenzy.node.client import (
    _MUTED_ALERT_FLOOR,
    NodeClient,
    _SoundPlayer,
    _StreamBuffer,
    _volume_to_gain,
)
from kenzy.server.server import NodeSession, TranscribingServer


class _StubWS:
    async def send(self, m):  # noqa: ANN001, ANN201
        pass


def _bare_player() -> _SoundPlayer:
    """A _SoundPlayer without opening a real output stream (no audio hardware)."""
    p = _SoundPlayer.__new__(_SoundPlayer)
    p._volume = 1.0
    p._muted = False
    p._streaming = False
    p._ring = _StreamBuffer()
    p._restart = False
    p._alert = False
    p._pending_alert = False
    sample = np.full((4, 1), 10000, dtype=np.int16)
    p._audio = sample
    p._chime = sample
    p._pending = sample
    p._pos = len(sample)
    return p


# ---------------------------------------------------------------------------
# Gain / mute math (the RT-callback heart)
# ---------------------------------------------------------------------------


def test_volume_to_gain_clamps():
    assert _volume_to_gain(100) == 1.0
    assert _volume_to_gain(0) == 0.0
    assert _volume_to_gain(50) == 0.5
    assert _volume_to_gain(250) == 1.0  # over-range clamped
    assert _volume_to_gain(-10) == 0.0
    assert _volume_to_gain("bad") == 1.0  # non-numeric → full volume


def test_apply_gain_scales_volume():
    p = _bare_player()
    p._volume = 0.5
    buf = np.full((3, 1), 10000, dtype=np.int16)
    p._apply_gain(buf, alert=False)
    assert buf.flatten().tolist() == [5000, 5000, 5000]


def test_apply_gain_full_volume_is_noop():
    p = _bare_player()
    p._volume = 1.0
    buf = np.array([[123], [456]], dtype=np.int16)
    p._apply_gain(buf, alert=False)
    assert buf.flatten().tolist() == [123, 456]


def test_muted_silences_non_alert():
    p = _bare_player()
    p._muted = True
    buf = np.full((4, 1), 10000, dtype=np.int16)
    p._apply_gain(buf, alert=False)
    assert buf.flatten().tolist() == [0, 0, 0, 0]


def test_muted_keeps_alert_chime_audible():
    p = _bare_player()
    p._muted = True
    buf = np.full((2, 1), 10000, dtype=np.int16)
    p._apply_gain(buf, alert=True)
    expected = int(10000 * _MUTED_ALERT_FLOOR)
    assert buf.flatten().tolist() == [expected, expected]
    assert expected > 0  # the chime is still heard while muted


def test_callback_routes_alert_flag():
    # TTS (non-alert) while muted → silence.
    p = _bare_player()
    p._muted = True
    p._pending = np.full((2, 1), 10000, dtype=np.int16)
    p._pending_alert = False
    p._restart = True
    out = np.zeros((2, 1), dtype=np.int16)
    p._callback(out, 2, None, None)
    assert out.flatten().tolist() == [0, 0]

    # Chime (alert) while muted → audible floor.
    p = _bare_player()
    p._muted = True
    p._pending = np.full((2, 1), 10000, dtype=np.int16)
    p._pending_alert = True
    p._restart = True
    out = np.zeros((2, 1), dtype=np.int16)
    p._callback(out, 2, None, None)
    assert out[0, 0] == int(10000 * _MUTED_ALERT_FLOOR)


def test_set_volume_and_muted_clamp():
    p = _bare_player()
    p.set_volume(2.0)
    assert p._volume == 1.0
    p.set_volume(-1.0)
    assert p._volume == 0.0
    p.set_muted(True)
    assert p._muted is True


# ---------------------------------------------------------------------------
# Node: live apply via config-pull
# ---------------------------------------------------------------------------


class _FakePlayer:
    def __init__(self):
        self.volume = None
        self.muted = None

    def set_volume(self, v):  # noqa: ANN001, ANN201
        self.volume = v

    def set_muted(self, m):  # noqa: ANN001, ANN201
        self.muted = m


def test_node_applies_volume_and_mute_live():
    client = NodeClient({"node_id": "n1", "room_id": "kitchen"})
    fake = _FakePlayer()
    client._player = fake  # type: ignore[assignment]
    client._apply_pulled_config({"volume": 40, "muted": True})
    assert client._volume == 0.4
    assert client._muted is True
    assert fake.volume == 0.4
    assert fake.muted is True


def test_node_reads_volume_from_initial_config():
    client = NodeClient({"node_id": "n1", "volume": 25, "muted": True})
    assert client._volume == 0.25
    assert client._muted is True


# ---------------------------------------------------------------------------
# Server: set_node_volume / set_node_muted + action dispatch
# ---------------------------------------------------------------------------


async def test_set_node_volume_persists_and_clamps(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    srv = TranscribingServer({})
    srv._nodes["k"] = NodeSession(ws=_StubWS(), node_id="k", room_id="kitchen")

    assert await srv.set_node_volume("k", level=50) == 50
    assert srv.read_node_override("k")["volume"] == 50
    assert await srv.set_node_volume("k", delta=15) == 65  # relative to stored 50
    assert await srv.set_node_volume("k", delta=-100) == 0  # clamped low
    assert await srv.set_node_volume("k", level=250) == 100  # clamped high
    assert await srv.set_node_volume("k") is None  # nothing to do


async def test_set_node_muted_is_transient_and_connected_only(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    srv = TranscribingServer({})
    assert await srv.set_node_muted("ghost", True) is False  # not connected

    srv._nodes["k"] = NodeSession(ws=_StubWS(), node_id="k", room_id="kitchen")
    assert await srv.set_node_muted("k", True) is True
    assert srv._effective_node_config("k")["muted"] is True
    # Mute is never written to the persisted override (comes back un-muted on restart).
    assert "muted" not in srv.read_node_override("k")
    assert await srv.set_node_muted("k", False) is True
    assert srv._effective_node_config("k")["muted"] is False


async def test_dispatch_set_volume_action(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    srv = TranscribingServer({})
    srv._nodes["k"] = NodeSession(ws=_StubWS(), node_id="k", room_id="kitchen")

    await srv._dispatch_actions([{"type": "set_volume", "level": 30}], "k", "kitchen")
    assert srv.read_node_override("k")["volume"] == 30
    await srv._dispatch_actions([{"type": "set_volume", "muted": True}], "k", "kitchen")
    assert srv._effective_node_config("k")["muted"] is True


# ---------------------------------------------------------------------------
# Voice fast-intent classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utterance,expected",
    [
        ("turn it up", ("up", None)),
        ("louder please", ("up", None)),
        ("volume up", ("up", None)),
        ("turn it down", ("down", None)),
        ("quieter", ("down", None)),
        ("lower the volume", ("down", None)),
        ("mute", ("mute", None)),
        ("be quiet", ("mute", None)),
        ("unmute", ("unmute", None)),
        ("turn the sound back on", ("unmute", None)),
        ("set the volume to 40", ("set", 40)),
        ("volume 75", ("set", 75)),
        ("set volume at 60 percent", ("set", 60)),
        ("what time is it", None),
        ("turn on the lights", None),
        ("", None),
    ],
)
def test_volume_classify(utterance, expected):
    assert classify(utterance) == expected
