"""Spoken failure feedback: when the pipeline fails, the node plays the
pre-recorded error cue instead of leaving the room in silence — pre-recorded
precisely because TTS may be the broken part (everyday-essentials item 3)."""

from __future__ import annotations

import asyncio

import pytest

from kenzy.server import tones
from kenzy.server.server import NodeSession, TranscribingServer


class _WS:
    pass


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = TranscribingServer(
        {
            "stt": {"url": "http://x/transcribe"},
            "llm": {"url": "http://x/process"},
            "tts": {"url": "http://x/speak"},
        }
    )
    s._nodes["n1"] = NodeSession(ws=_WS(), node_id="n1", room_id="office")
    streamed: list[bytes] = []

    async def fake_stream(node_id: str, pcm: bytes) -> None:
        streamed.append(pcm)

    monkeypatch.setattr(s, "_stream_pcm", fake_stream)
    s.streamed = streamed  # type: ignore[attr-defined]
    return s


async def test_stt_failure_plays_cue(srv, monkeypatch):
    async def boom(*a, **k):
        raise OSError("stt down")

    monkeypatch.setattr(srv, "_call_stt", boom)
    await srv._transcribe("n1", "office", "sid", b"\x00\x00" * 1600)
    assert srv.streamed == [tones.load_tone("error.wav")]


async def test_llm_failure_plays_cue_and_ends_dialog(srv, monkeypatch):
    async def stt(*a, **k):
        return "what time is it"

    async def spk(*a, **k):
        return "unknown", 0.0

    async def boom(*a, **k):
        raise OSError("llm down")

    monkeypatch.setattr(srv, "_call_stt", stt)
    monkeypatch.setattr(srv, "_call_speaker", spk)
    monkeypatch.setattr(srv, "_call_llm", boom)
    srv._followup_turns["n1"] = 2  # a held dialog must be released on failure
    await srv._transcribe("n1", "office", "sid", b"\x00\x00" * 1600)
    assert srv.streamed and srv.streamed[0] == tones.load_tone("error.wav")
    assert "n1" not in srv._followup_turns


async def test_tts_failure_plays_cue(srv, monkeypatch):
    async def stt(*a, **k):
        return "hello"

    async def spk(*a, **k):
        return "unknown", 0.0

    async def llm(text, room, sid, speaker=None, node_id=None, identity=None):
        return ("Hi there.", "vp", [], False, False, False)

    async def tts_fail(*a, **k):
        return False  # synthesis/streaming failed

    monkeypatch.setattr(srv, "_call_stt", stt)
    monkeypatch.setattr(srv, "_call_speaker", spk)
    monkeypatch.setattr(srv, "_call_llm", llm)
    monkeypatch.setattr(srv, "_run_tts", tts_fail)
    await srv._transcribe("n1", "office", "sid", b"\x00\x00" * 1600)
    assert srv.streamed == [tones.load_tone("error.wav")]


async def test_success_plays_no_cue(srv, monkeypatch):
    async def stt(*a, **k):
        return "hello"

    async def spk(*a, **k):
        return "unknown", 0.0

    async def llm(text, room, sid, speaker=None, node_id=None, identity=None):
        return ("Hi there.", "vp", [], False, False, False)

    async def tts_ok(*a, **k):
        return True

    monkeypatch.setattr(srv, "_call_stt", stt)
    monkeypatch.setattr(srv, "_call_speaker", spk)
    monkeypatch.setattr(srv, "_call_llm", llm)
    monkeypatch.setattr(srv, "_run_tts", tts_ok)
    await srv._transcribe("n1", "office", "sid", b"\x00\x00" * 1600)
    assert srv.streamed == []


async def test_cue_respects_disable_and_custom_config(srv, monkeypatch):
    srv._node_defaults = {"sound_error": ""}  # opted out → silent failure

    async def boom(*a, **k):
        raise OSError("down")

    monkeypatch.setattr(srv, "_call_stt", boom)
    await srv._transcribe("n1", "office", "sid", b"\x00\x00" * 1600)
    assert srv.streamed == []


async def test_run_tts_semantics(tmp_path, monkeypatch):
    # A deliberately TTS-less config is silence by choice, not a failure.
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = TranscribingServer({})
    assert await s._run_tts("n1", "office", "sid", "text", "vp") is True
    await asyncio.sleep(0)  # let the scheduler task settle before teardown
