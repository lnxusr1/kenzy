"""Tests for the audio-device probe (shared by `kenzy-devices` and the node's hello
capabilities) and the dashboard surfacing it for the device picker."""

from __future__ import annotations

import asyncio
import sys
import types

import httpx

from kenzy.server.dashboard import Dashboard, DashboardConfig
from kenzy.server.server import AudioServer, NodeSession


class _StubWS:
    async def send(self, m):  # noqa: ANN001, ANN201
        pass


def _install_fake_sd(monkeypatch):
    """Inject a fake sounddevice with deterministic device/rate support."""
    cap = {0: {48000}, 1: {16000, 48000}}  # device → supported capture rates
    play = {1: {24000, 48000}}  # device → supported playback rates

    mod = types.ModuleType("sounddevice")

    class PortAudioError(Exception):
        pass

    def query_devices():
        return [
            {
                "name": "Built-in Mic: USB",
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 48000,
            },
            {
                "name": "Anker PowerConf S330: USB",
                "max_input_channels": 2,
                "max_output_channels": 2,
                "default_samplerate": 48000,
            },
            {
                "name": "Loopback",
                "max_input_channels": 0,
                "max_output_channels": 0,
                "default_samplerate": 44100,
            },  # no I/O → skipped
        ]

    def check_input_settings(device, samplerate, channels, dtype):  # noqa: ANN001, ANN201
        if samplerate not in cap.get(device, set()):
            raise PortAudioError()

    def check_output_settings(device, samplerate, channels, dtype):  # noqa: ANN001, ANN201
        if samplerate not in play.get(device, set()):
            raise PortAudioError()

    mod.PortAudioError = PortAudioError  # type: ignore[attr-defined]
    mod.query_devices = query_devices  # type: ignore[attr-defined]
    mod.check_input_settings = check_input_settings  # type: ignore[attr-defined]
    mod.check_output_settings = check_output_settings  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sounddevice", mod)


def test_probe_devices_structures_rates_and_suggestion(monkeypatch):
    _install_fake_sd(monkeypatch)
    from kenzy.node.devices import probe_devices

    devs = probe_devices()
    assert [d["index"] for d in devs] == [0, 1]  # the no-I/O device is dropped

    mic, anker = devs
    assert mic["capture_rates"] == [48000]
    assert mic["playback_rates"] == []
    assert "suggested" not in mic  # input-only → not a full mic+speaker device

    assert anker["capture_rates"] == [16000, 48000]
    assert anker["playback_rates"] == [24000, 48000]
    # Native Kenzy rates supported → suggested directly; short name before the colon.
    assert anker["suggested"] == {
        "audio_device": "Anker PowerConf S330",
        "capture_sample_rate": 16000,
        "playback_sample_rate": 24000,
    }


def test_probe_devices_suggests_resample_rate(monkeypatch):
    """When the native rate isn't supported, suggest the first supported (resampled)."""
    mod = types.ModuleType("sounddevice")

    class PortAudioError(Exception):
        pass

    mod.PortAudioError = PortAudioError  # type: ignore[attr-defined]
    mod.query_devices = lambda: [  # type: ignore[attr-defined]
        {
            "name": "Odd Device",
            "max_input_channels": 1,
            "max_output_channels": 1,
            "default_samplerate": 44100,
        }
    ]

    def check(device, samplerate, channels, dtype):  # 44100 only  # noqa: ANN001, ANN201
        if samplerate != 44100:
            raise PortAudioError()

    mod.check_input_settings = check  # type: ignore[attr-defined]
    mod.check_output_settings = check  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sounddevice", mod)

    from kenzy.node.devices import probe_devices

    s = probe_devices()[0]["suggested"]
    assert s == {
        "audio_device": "Odd Device",
        "capture_sample_rate": 44100,
        "playback_sample_rate": 44100,
    }


def test_probe_devices_no_sounddevice(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)  # import → ImportError-ish
    from kenzy.node.devices import probe_devices

    assert probe_devices() == []


async def test_node_config_api_surfaces_devices(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    server = AudioServer({})
    server._nodes["k"] = NodeSession(
        ws=_StubWS(),
        node_id="k",
        room_id="kitchen",
        capabilities={
            "devices": [{"index": 1, "name": "Anker", "suggested": {"audio_device": "Anker"}}]
        },
    )
    dash = Dashboard(server, {}, DashboardConfig(enabled=True, bind="127.0.0.1", port=8779))
    task = asyncio.create_task(dash.serve())
    await asyncio.sleep(0.25)
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8779") as c:
            r = await c.get("/api/nodes/k/config")
            devices = r.json()["devices"]
            assert devices and devices[0]["name"] == "Anker"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
