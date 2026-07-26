"""4.4.0 pre-release hardening: regression tests for the streaming review
findings. Written test-first — each of these FAILED on the pre-fix code,
reproducing the finding deterministically.

Crit-1: a disconnect mid-streamed-reply must reset the node's streaming state
        (player ring mode, _tts_stream, _state) — else the next buffered sound
        is silent and a half-duplex node ignores wake words forever.
Crit-2: a new TTS session must cancel the previous session's drain task —
        else the old task stops the new session's stream and stomps its state.
Maj-3:  a mid-stream transport failure after speech began keeps what was
        spoken (no raise → no error cue over speech); a failure before any
        speech falls back to the buffered path (return None).
Maj-4:  a raw control char inside the reply's text (prompt-tier local models)
        must not poison the stored reply — finalize parses it (strict=False)
        so the streamed pieces stay a perfect prefix of end.text.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets

from kenzy.node.client import _STATE_IDLE, _STATE_TTS, NodeClient
from kenzy.server.server import NodeSession, TranscribingServer

# ---------------------------------------------------------------------------
# Node-side harness
# ---------------------------------------------------------------------------


class _FakePlayer:
    """Mirrors _SoundPlayer's streaming-mode semantics (no PortAudio)."""

    def __init__(self) -> None:
        self.streaming = False
        self.ring_pending = False  # test-controlled "ring still has samples"
        self.aborted = False
        self._audio_len = 0
        self._pos = 0

    def start_stream(self) -> None:
        self.streaming = True

    def stop_stream(self) -> None:
        self.streaming = False
        self.ring_pending = False

    def feed(self, pcm: Any) -> None:
        pass

    def abort(self) -> None:
        self.aborted = True
        self.looping = False
        self.bed_active = False

    def play_pcm(
        self,
        audio: Any,
        interrupt: bool = False,
        alert: bool = False,
        loop: bool = False,
        bed: bool = False,
        cue: bool = False,
    ) -> None:
        self.looping = loop
        self.bed_active = bed or loop

    def overlay(self, cue: Any, duck: float = 0.25, lead: int = 2400) -> bool:
        return False  # no bed in this harness — cue falls back to play_pcm

    looping = False
    bed_active = False
    cue_remaining_s = 0.0

    @property
    def stream_pending(self) -> bool:
        return self.streaming and self.ring_pending

    @property
    def active(self) -> bool:
        # The half-duplex wake gate keys off this: stuck-True = deaf node.
        return self.streaming or self._pos < self._audio_len


class _DroppingWS:
    """Yields scripted frames, then the connection 'drops' (ConnectionClosed)."""

    def __init__(self, frames: list[str]):
        self._frames = list(frames)
        self.sent: list[str] = []

    async def send(self, m: str) -> None:
        self.sent.append(m)

    async def recv(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        raise websockets.exceptions.ConnectionClosedOK(None, None)

    async def close(self) -> None:
        pass


def _node() -> tuple[NodeClient, _FakePlayer]:
    client = NodeClient({"node_id": "n1", "room_id": "office"})
    client._audio_ready = True  # skip config-wait + audio init in _run_session
    player = _FakePlayer()
    client._player = player  # type: ignore[assignment]
    return client, player


# ---------------------------------------------------------------------------
# Crit-1 — disconnect mid-streamed-reply resets streaming state
# ---------------------------------------------------------------------------


async def test_disconnect_mid_streamed_reply_resets_streaming_state():
    client, player = _node()
    await client._begin_tts("s1", 24000, 1, stream=True)
    assert client._state == _STATE_TTS and player.streaming

    # The connection drops before tts_end ever arrives.
    await client._run_session(_DroppingWS([]))  # type: ignore[arg-type]

    assert player.streaming is False  # ring mode released
    assert client._tts_stream is False
    assert client._tts_stream_started is False
    assert client._state == _STATE_IDLE
    # The half-duplex deadlock leg: the wake gate polls player.active — a
    # stuck-streaming player would suppress wake words forever.
    assert player.active is False


async def test_disconnect_mid_buffered_tts_also_returns_to_idle():
    client, player = _node()
    await client._begin_tts("s1", 24000, 1, stream=False)
    await client._run_session(_DroppingWS([]))  # type: ignore[arg-type]
    assert client._state == _STATE_IDLE
    assert player.streaming is False


# ---------------------------------------------------------------------------
# Crit-2 — a new session cancels the previous session's drain task
# ---------------------------------------------------------------------------


async def test_new_streamed_session_survives_prior_drain_task():
    client, player = _node()

    # Session 1: streamed, played, tts_end arrives while the ring still drains.
    await client._begin_tts("s1", 24000, 1, stream=True)
    client._tts_stream_started = True
    player.ring_pending = True
    await client._end_tts("complete")  # spawns the drain task
    assert client._tts_task is not None and not client._tts_task.done()

    # Session 2 begins inside session 1's drain window (announce right after
    # a streamed reply).
    await client._begin_tts("s2", 24000, 1, stream=True)
    client._tts_stream_started = True

    # Let session 1's drain task run to completion (ring empties).
    player.ring_pending = False
    await asyncio.sleep(0.35)  # > drain poll + 0.1s tail

    # Session 2 must be alive and untouched by session 1's completion.
    assert client._state == _STATE_TTS
    assert client._session_id == "s2"
    assert client._tts_stream is True
    assert player.streaming is True  # session 1 must NOT have stopped the ring

    # And session 2 still closes down normally.
    player.ring_pending = False
    await client._end_tts("complete")
    if client._tts_task:
        await client._tts_task
    assert client._state == _STATE_IDLE


# ---------------------------------------------------------------------------
# Maj-3 — server stream-failure policy
# ---------------------------------------------------------------------------


class _StubWS:
    async def send(self, m: Any) -> None:
        pass


class _FakeHttpxClient:
    """Stands in for httpx.AsyncClient: .stream() yields a scripted response."""

    def __init__(self, resp: Any):
        self._resp = resp

    async def __aenter__(self) -> _FakeHttpxClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass

    def stream(self, method: str, url: str, **kw: Any) -> Any:
        resp = self._resp

        class _CM:
            async def __aenter__(self) -> Any:
                return resp

            async def __aexit__(self, *exc: Any) -> None:
                pass

        return _CM()


class _FakeStreamResp:
    def __init__(self, status: int, lines: list[dict], die_with: Exception | None = None):
        self.status_code = status
        self._lines = [json.dumps(ln) for ln in lines]
        self._die = die_with

    def raise_for_status(self) -> None:
        import httpx

        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}",
                request=httpx.Request("POST", "http://llm/process/stream"),
                response=httpx.Response(self.status_code),
            )

    async def aiter_lines(self) -> Any:
        for ln in self._lines:
            yield ln
        if self._die is not None:
            raise self._die


def _stream_server(monkeypatch, resp: _FakeStreamResp) -> TranscribingServer:
    import httpx

    srv = TranscribingServer(
        {"llm": {"url": "http://127.0.0.1:9/process"}, "streaming": {"enabled": True}}
    )
    srv._nodes["n1"] = NodeSession(ws=_StubWS(), node_id="n1", room_id="office")

    async def fake_synth(text: str, vp: str, *, sensitive: bool = False) -> bytes:
        return b"\x00\x01" * 8

    monkeypatch.setattr(srv, "_synthesize", fake_synth)

    async def no_cue(node_id: str, key: str, default: str) -> None:
        pass

    monkeypatch.setattr(srv, "_play_cue", no_cue)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeHttpxClient(resp))
    return srv


HEAD = {"event": "head", "voice_prompt": "Calm.", "expect_response": False}


async def test_midstream_failure_after_speech_keeps_spoken_no_raise(monkeypatch):
    import httpx

    resp = _FakeStreamResp(
        200,
        [HEAD, {"event": "delta", "text": "First part. And then it br"}],
        die_with=httpx.ReadTimeout("mid-stream hang"),
    )
    srv = _stream_server(monkeypatch, resp)

    # Must NOT raise (a raise would play the error cue over spoken audio).
    result = await srv._call_llm_stream("q", "office", "sid1", "Adam", "n1", None)
    assert result is not None
    reply, speech = result
    assert speech.spoken == "First part. "  # sentence 1 was spoken
    # The record keeps spoken + the buffered-but-unspoken tail, and stays a
    # superset of the spoken prefix so close() speaks only the remainder.
    assert reply.text == "First part. And then it br"
    assert reply.text.startswith(speech.spoken)
    spoke_ok = await speech.close(reply)
    assert spoke_ok is True  # partial speech ⇒ no error cue


async def test_5xx_before_speech_falls_back_to_buffered(monkeypatch):
    resp = _FakeStreamResp(503, [])
    srv = _stream_server(monkeypatch, resp)
    # Must return None (⇒ caller uses the buffered path), not raise.
    assert await srv._call_llm_stream("q", "office", "sid2", "Adam", "n1", None) is None


async def test_readtimeout_before_speech_falls_back_to_buffered(monkeypatch):
    import httpx

    resp = _FakeStreamResp(200, [], die_with=httpx.ReadTimeout("dead before output"))
    srv = _stream_server(monkeypatch, resp)
    assert await srv._call_llm_stream("q", "office", "sid3", "Adam", "n1", None) is None


# ---------------------------------------------------------------------------
# Maj-4 — raw control chars in the reply text
# ---------------------------------------------------------------------------


def test_raw_newline_in_text_parses_and_streams_consistently():
    from kenzy.llm.streamparse import StreamExtract

    raw = '{"voice_prompt": "v", "expect_response": false, "text": "line one\nline two"}'
    ex = StreamExtract()
    out = ""
    for i in range(0, len(raw), 7):
        out += ex.feed(raw[i : i + 7])
    # The streamed pieces and the authoritative parse must AGREE — the pre-fix
    # code streamed the text but finalize() returned None, so the lenient
    # fallback poisoned history with the raw JSON blob.
    assert ex.finalize() == ("line one\nline two", "v", False)
    assert out == "line one\nline two"


def test_parse_response_handles_raw_control_chars():
    from kenzy.llm.llm import _parse_response

    raw = '{"voice_prompt": "v", "expect_response": true, "text": "a\nb"}'
    text, vp, expect = _parse_response(raw)
    assert text == "a\nb"  # not the raw blob
    assert vp == "v"
    assert expect is True
