"""4.4 backchannel: the spoken "On it." acknowledgement when the LLM stage runs
long — timer-armed around the LLM calls, cancelled when the reply is fast, and
allowed to finish (session-clean) when it races the reply."""

from __future__ import annotations

import asyncio

from kenzy.server import server as srv
from kenzy.server.server import TranscribingServer


def _server() -> TranscribingServer:
    return TranscribingServer({})


async def test_fast_reply_never_cues(monkeypatch):
    monkeypatch.setattr(srv, "_BACKCHANNEL_MS", 200)
    s = _server()
    played: list[str] = []

    async def fake_cue(node_id: str) -> None:
        played.append(node_id)

    monkeypatch.setattr(s, "_play_thinking_cue", fake_cue)

    async def quick() -> str:
        return "done"

    assert await s._with_backchannel("n1", quick()) == "done"
    await asyncio.sleep(0.3)  # past the threshold — the timer must be dead
    assert played == []


async def test_slow_reply_cues_once(monkeypatch):
    monkeypatch.setattr(srv, "_BACKCHANNEL_MS", 20)
    s = _server()
    played: list[str] = []

    async def fake_cue(node_id: str) -> None:
        played.append(node_id)

    monkeypatch.setattr(s, "_play_thinking_cue", fake_cue)

    async def slow() -> str:
        await asyncio.sleep(0.15)
        return "done"

    assert await s._with_backchannel("n1", slow()) == "done"
    assert played == ["n1"]


async def test_mid_play_cue_finishes_before_return(monkeypatch):
    # The reply lands while the cue is streaming: the clip must complete (its
    # TTS session closes) before _with_backchannel returns.
    monkeypatch.setattr(srv, "_BACKCHANNEL_MS", 10)
    s = _server()
    finished: list[bool] = []

    async def slow_cue(node_id: str) -> None:
        await asyncio.sleep(0.1)
        finished.append(True)

    monkeypatch.setattr(s, "_play_thinking_cue", slow_cue)

    async def reply() -> str:
        await asyncio.sleep(0.05)  # cue already started, still playing
        return "done"

    assert await s._with_backchannel("n1", reply()) == "done"
    assert finished == [True]


async def test_llm_error_still_cancels_pending_cue(monkeypatch):
    monkeypatch.setattr(srv, "_BACKCHANNEL_MS", 200)
    s = _server()
    played: list[str] = []

    async def fake_cue(node_id: str) -> None:
        played.append(node_id)

    monkeypatch.setattr(s, "_play_thinking_cue", fake_cue)

    async def boom() -> str:
        raise RuntimeError("llm down")

    try:
        await s._with_backchannel("n1", boom())
        raise AssertionError("should have raised")
    except RuntimeError:
        pass
    await asyncio.sleep(0.3)
    assert played == []


async def test_cue_resolves_default_sound(monkeypatch):
    s = _server()
    monkeypatch.setattr(s, "_effective_node_config", lambda node_id: {})
    specs: list[str] = []
    streamed: list[bytes] = []

    from kenzy.server import tones

    def fake_load(spec: str) -> bytes:
        specs.append(spec)
        return b"\x00\x01"

    async def fake_stream(node_id: str, pcm: bytes) -> None:
        streamed.append(pcm)

    monkeypatch.setattr(tones, "load_tone", fake_load)
    monkeypatch.setattr(s, "_stream_pcm", fake_stream)
    await s._play_thinking_cue("n1")
    assert specs == ["thinking.wav"]
    assert streamed == [b"\x00\x01"]


async def test_empty_sound_key_stays_silent(monkeypatch):
    s = _server()
    monkeypatch.setattr(s, "_effective_node_config", lambda node_id: {"sound_thinking": ""})
    streamed: list[bytes] = []

    async def fake_stream(node_id: str, pcm: bytes) -> None:
        streamed.append(pcm)

    monkeypatch.setattr(s, "_stream_pcm", fake_stream)
    await s._play_thinking_cue("n1")
    assert streamed == []
