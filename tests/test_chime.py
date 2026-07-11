"""The house-wide chime (kenzy/chime → play_chime): sound-name validation,
looping/cap, room targeting, and the alert flag that beats mute on nodes."""

from __future__ import annotations

import json

import pytest

from kenzy import protocol
from kenzy.server import tones
from kenzy.server.server import NodeSession, TranscribingServer


class _RecWS:
    def __init__(self):
        self.sent: list = []

    async def send(self, data):
        self.sent.append(data)


@pytest.fixture
def srv(monkeypatch):
    server = TranscribingServer(
        {"integrations": {"mqtt": {"chimes": {"gong": "/srv/sounds/gong.wav"}}}}
    )
    ws_a, ws_b = _RecWS(), _RecWS()
    server._nodes["n1"] = NodeSession(ws=ws_a, node_id="n1", room_id="Kitchen")
    server._nodes["n2"] = NodeSession(ws=ws_b, node_id="n2", room_id="Office")

    loaded: list[str] = []
    cue = b"\x01\x02" * 2400  # 0.1 s of 24 kHz mono int16

    def fake_load(spec):
        loaded.append(spec)
        return cue

    monkeypatch.setattr(tones, "load_tone", fake_load)
    server._tts_chunk_size = 65536  # one frame per node keeps assertions simple
    return server, ws_a, ws_b, loaded, cue


def _tts_starts(ws) -> list[dict]:
    out = []
    for m in ws.sent:
        if isinstance(m, str):
            d = json.loads(m)
            if d.get("type") == protocol.MSG_TTS_START:
                out.append(d)
    return out


def _pcm_bytes(ws) -> int:
    return sum(len(m) for m in ws.sent if isinstance(m, bytes))


async def test_default_chime_all_nodes_alert(srv):
    server, ws_a, ws_b, loaded, cue = srv
    n = await server.play_chime()
    assert n == 2
    assert loaded == ["doorbell.wav"]  # the bundled default
    for ws in (ws_a, ws_b):
        starts = _tts_starts(ws)
        assert len(starts) == 1
        assert starts[0]["alert"] is True  # beats mute at the node's alert floor
        assert _pcm_bytes(ws) == len(cue)  # played once — no loop requested


async def test_loop_seconds_tiles_whole_repeats(srv):
    server, ws_a, _, _, cue = srv
    await server.play_chime("doorbell.wav", seconds=0.35, rooms=["kitchen"])
    # 0.35 s of a 0.1 s cue = 4 whole repeats (never cut mid-"dong").
    assert _pcm_bytes(ws_a) == len(cue) * 4


async def test_loop_cap(srv, monkeypatch):
    from kenzy.server import server as srv_mod

    monkeypatch.setattr(srv_mod, "_CHIME_MAX_S", 0.2)
    server, ws_a, _, _, cue = srv
    await server.play_chime("doorbell.wav", seconds=9999, rooms=["kitchen"])
    assert _pcm_bytes(ws_a) == len(cue) * 2  # capped at 0.2 s = 2 repeats


async def test_rooms_targeting_case_insensitive(srv):
    server, ws_a, ws_b, _, _ = srv
    n = await server.play_chime(rooms=["KITCHEN"])
    assert n == 1
    assert _tts_starts(ws_a) and not _tts_starts(ws_b)


async def test_configured_chime_map(srv):
    server, _, _, loaded, _ = srv
    assert await server.play_chime("gong") == 2
    assert loaded == ["/srv/sounds/gong.wav"]  # the operator-configured path


@pytest.mark.parametrize(
    "name", ["../../etc/passwd", "/etc/shadow", "sounds/../x.wav", ".hidden.wav", "a/b.wav"]
)
async def test_pathlike_names_refused(srv, name):
    server, ws_a, _, loaded, _ = srv
    assert await server.play_chime(name) == 0
    assert loaded == []  # never even attempted to load
    assert not _tts_starts(ws_a)


async def test_unknown_room_plays_nowhere(srv):
    server, ws_a, ws_b, _, _ = srv
    assert await server.play_chime(rooms=["garage"]) == 0
    assert not _tts_starts(ws_a) and not _tts_starts(ws_b)


def test_tts_start_alert_flag_wire_format():
    # The flag is present only when set — old nodes never see an unknown key
    # unless the feature is used, and ignore it when they do.
    assert json.loads(protocol.tts_start("s1", 24000, 1, alert=True))["alert"] is True
    assert "alert" not in json.loads(protocol.tts_start("s1", 24000, 1))


async def test_node_begin_tts_carries_alert():
    from kenzy.node.client import NodeClient

    client = NodeClient({"node_id": "n1"})
    await client._begin_tts("sid", 24000, 1, alert=True)
    assert client._tts_alert is True
    await client._begin_tts("sid2", 24000, 1)
    assert client._tts_alert is False


def test_tile_pcm_whole_repeats():
    cue = b"\x00\x01" * 1200  # 0.05 s
    assert tones.tile_pcm(cue, 0.12) == cue * 3  # rounds UP to complete the ring
    assert tones.tile_pcm(cue, 0) == cue
    assert tones.tile_pcm(b"", 5) == b""
