"""Tests for config-pull: server effective-config merge, node apply, and the
end-to-end push over the WebSocket protocol (including the join-token gate)."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets
import yaml

from kenzy import protocol
from kenzy.node.client import NodeClient
from kenzy.server.server import AudioServer

# ---------------------------------------------------------------------------
# Server: effective config = defaults + per-room override
# ---------------------------------------------------------------------------


def test_effective_config_defaults_only(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)  # exists but no nodes/<room>.yaml
    srv = AudioServer({"node_defaults": {"wakeword_threshold": 0.5, "silence_ms": 400}})
    eff = srv._effective_node_config("living_room")
    assert eff == {"wakeword_threshold": 0.5, "silence_ms": 400}


def test_effective_config_override_merges(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    nodes = tmp_path / "configs" / "nodes"
    nodes.mkdir(parents=True)
    (nodes / "den.yaml").write_text("wakeword_threshold: 0.7\nhard_cap_ms: 20000\n")
    srv = AudioServer({"node_defaults": {"wakeword_threshold": 0.5, "silence_ms": 400}})
    eff = srv._effective_node_config("den")
    assert eff["wakeword_threshold"] == 0.7  # override wins
    assert eff["silence_ms"] == 400  # default retained
    assert eff["hard_cap_ms"] == 20000  # override adds


# ---------------------------------------------------------------------------
# Node: apply pulled config to live attributes
# ---------------------------------------------------------------------------


def test_apply_pulled_config_updates_live_params():
    node = NodeClient({})
    node._apply_pulled_config(
        {
            "wakeword_threshold": 0.8,
            "silence_rms_threshold": 120,
            "vad_enabled": False,
            "silence_ms": 800,
            "hard_cap_ms": 10000,
            # hardware key — must NOT be applied live:
            "capture_sample_rate": 48000,
        }
    )
    assert node._wakeword_threshold == 0.8
    assert node._silence_rms == 120
    assert node._vad_enabled is False
    assert node._silence_frames == max(800 // protocol.FRAME_MS, 1)
    assert node._hard_cap_frames == max(10000 // protocol.FRAME_MS, 1)
    # hardware change is ignored live (capture rate untouched on the instance)
    assert node._capture_rate == protocol.SAMPLE_RATE


def test_apply_pulled_config_initial_applies_hardware_keys():
    # On the FIRST pull (before audio is built), hardware keys are applied so
    # _init_audio constructs the stream from server-pushed values.
    node = NodeClient({})
    node._apply_pulled_config(
        {
            "audio_device": "USB Mic",
            "capture_sample_rate": 48000,
            "playback_sample_rate": 44100,
            "wakeword_models": ["/m/custom.onnx"],
            "wakeword_vad_threshold": 0.5,
            "sound_ready": "ding.wav",
            "sound_waiting": "",  # falsy → disabled
            "wakeword_threshold": 0.7,  # live keys still apply on the initial pull
        },
        initial=True,
    )
    assert node._audio_device == "USB Mic"
    assert node._capture_rate == 48000
    assert node._playback_rate == 44100
    assert node._wakeword_models == ["/m/custom.onnx"]
    assert node._wakeword_vad_threshold == 0.5
    assert node._sound_ready == "ding.wav"
    assert node._sound_waiting is None
    assert node._wakeword_threshold == 0.7


def test_apply_pulled_config_log_levels():
    import logging

    from kenzy.logutil import TRACE

    node = NodeClient({})
    assert node._log_level == logging.INFO and node._log_capture_level == logging.DEBUG
    node._apply_pulled_config({"log_level": "warning", "log_capture_level": "trace"})
    assert node._log_level == logging.WARNING
    assert node._log_capture_level == TRACE


def test_log_keys_are_overridable():
    keys = AudioServer.allowed_override_keys()
    assert "log_level" in keys and "log_capture_level" in keys


def test_apply_pulled_config_adopts_room(tmp_path):
    # Room name is server-owned: the node adopts + persists it from the config frame.
    cfg_path = tmp_path / "node.yaml"
    cfg_path.write_text('node_id: "n-1"\n')
    node = NodeClient({"node_id": "n-1", "room_id": "kitchen"}, config_path=cfg_path)
    node._apply_pulled_config({"room_id": "office"})
    assert node._room_id == "office"
    assert yaml.safe_load(cfg_path.read_text())["room_id"] == "office"


# ---------------------------------------------------------------------------
# End-to-end over the WebSocket protocol
# ---------------------------------------------------------------------------


async def _serve(server: AudioServer):
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.25)  # let the listener bind
    return task


async def test_config_pushed_after_hello(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    nodes = tmp_path / "configs" / "nodes"
    nodes.mkdir(parents=True)
    (nodes / "den.yaml").write_text("wakeword_threshold: 0.7\n")
    server = AudioServer(
        {
            "host": "127.0.0.1",
            "port": 8799,
            "node_defaults": {"wakeword_threshold": 0.5, "silence_ms": 400},
        }
    )
    task = await _serve(server)
    try:
        async with websockets.connect("ws://127.0.0.1:8799") as ws:
            await ws.send(protocol.hello("den"))
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            msg = json.loads(raw)
            assert msg["type"] == protocol.MSG_CONFIG
            assert msg["config"]["wakeword_threshold"] == 0.7  # override
            assert msg["config"]["silence_ms"] == 400  # default
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_config_pushed_even_when_empty(tmp_path, monkeypatch):
    # A zero-config node (no defaults, no override) must still receive a config
    # frame — it blocks on this frame before initializing audio.
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    server = AudioServer({"host": "127.0.0.1", "port": 8796})
    task = await _serve(server)
    try:
        async with websockets.connect("ws://127.0.0.1:8796") as ws:
            await ws.send(protocol.hello("kitchen", node_id="n-zero"))
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert msg["type"] == protocol.MSG_CONFIG
            assert msg["config"] == {}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_node_id_is_registry_key(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    nodes = tmp_path / "configs" / "nodes"
    nodes.mkdir(parents=True)
    # Override keyed by node_id (not the room name) is the one that's served.
    (nodes / "n-42.yaml").write_text("wakeword_threshold: 0.9\n")
    server = AudioServer(
        {
            "host": "127.0.0.1",
            "port": 8797,
            "node_defaults": {"wakeword_threshold": 0.5},
        }
    )
    task = await _serve(server)
    try:
        async with websockets.connect("ws://127.0.0.1:8797") as ws:
            await ws.send(protocol.hello("kitchen", node_id="n-42"))
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert msg["config"]["wakeword_threshold"] == 0.9  # keyed by node_id
            await asyncio.sleep(0.05)
            assert "n-42" in server._nodes  # registry keyed by node_id
            assert server._nodes["n-42"].room_id == "kitchen"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_join_token_rejects_bad_hello(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    server = AudioServer(
        {
            "host": "127.0.0.1",
            "port": 8798,
            "discovery": {"token": "s3cret"},
            "node_defaults": {"wakeword_threshold": 0.5},
        }
    )
    task = await _serve(server)
    try:
        # Wrong token → server closes the connection.
        async with websockets.connect("ws://127.0.0.1:8798") as ws:
            await ws.send(protocol.hello("den", token="wrong"))
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await asyncio.wait_for(ws.recv(), timeout=2.0)
        # Correct token → config frame is delivered.
        async with websockets.connect("ws://127.0.0.1:8798") as ws:
            await ws.send(protocol.hello("den", token="s3cret"))
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert msg["type"] == protocol.MSG_CONFIG
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
