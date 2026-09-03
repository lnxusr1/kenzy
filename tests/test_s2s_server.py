"""kenzy-s2s session-server tests — the constitutive properties pinned
(transcript-first by construction, GA turn semantics, immediate cancel, one
history), plus the round trip: our real EngineClient against our real engine
over a live WebSocket — the seam proven end-to-end."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from kenzy.s2s.engine import (
    AudioDelta,
    EngineClient,
    EngineError,
    EngineEvent,
    InputTranscript,
    ResponseDone,
    ToolCall,
)
from kenzy.s2s.profiles import KENZY_S2S
from kenzy.s2s.server import GenRequest, GenText, GenToolCall, RealtimeSession, serve

GenEventT = GenText | GenToolCall  # what the fake generation stages yield


class _Pipe:
    """A fake transport: the test feeds client->server events, reads emissions."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []

    async def recv(self) -> str | bytes:
        msg = await self.inbox.get()
        if msg is None:
            raise EOFError
        return msg

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def push(self, evt: dict[str, Any]) -> None:
        self.inbox.put_nowait(json.dumps(evt))

    def close(self) -> None:
        self.inbox.put_nowait(None)

    def types(self) -> list[str]:
        return [str(e["type"]) for e in self.sent]

    async def wait_for(self, etype: str, timeout: float = 2.0) -> None:
        async with asyncio.timeout(timeout):
            while etype not in self.types():
                await asyncio.sleep(0.005)


async def _transcribe(_pcm: bytes) -> str:
    return "turn on the light"


def _rig(pipe: _Pipe, generate: Any, synth_voices: list[str] | None = None) -> asyncio.Task[None]:
    async def synthesize(_text: str, voice: str) -> AsyncIterator[bytes]:
        if synth_voices is not None:
            synth_voices.append(voice)
        yield b"\x01\x02"

    session = RealtimeSession(
        pipe, transcribe=_transcribe, generate=generate, synthesize=synthesize
    )
    return asyncio.get_running_loop().create_task(session.run())


def _b64(pcm: bytes) -> str:
    return base64.b64encode(pcm).decode()


async def test_transcript_emitted_before_any_output_exists() -> None:
    async def generate(_req: GenRequest) -> AsyncIterator[GenEventT]:
        yield GenToolCall("c1", "set_light", "{}")
        yield GenText("On it.")

    pipe = _Pipe()
    task = _rig(pipe, generate)
    pipe.push({"type": "session.update", "session": {"instructions": "be brief"}})
    pipe.push({"type": "input_audio_buffer.append", "audio": _b64(b"\x00" * 320)})
    pipe.push({"type": "input_audio_buffer.commit"})
    pipe.push({"type": "response.create"})
    await pipe.wait_for("response.done")
    pipe.close()
    await task

    types = pipe.types()
    assert "input_audio_buffer.committed" in types  # GA shape: commit acks, never responds
    transcript_at = types.index("conversation.item.input_audio_transcription.completed")
    # the qualifying bar, met by construction: transcript precedes every output
    assert transcript_at < types.index("response.output_item.done")
    assert transcript_at < types.index("response.audio.delta")


async def test_cancel_stops_the_stream_with_zero_late_deltas() -> None:
    async def generate(_req: GenRequest) -> AsyncIterator[GenEventT]:
        yield GenText("thinking")
        await asyncio.sleep(3600)  # a never-finishing response — cancel's case

    pipe = _Pipe()
    task = _rig(pipe, generate)
    pipe.push({"type": "response.create"})
    await pipe.wait_for("response.output_audio_transcript.delta")
    pipe.push({"type": "response.cancel"})
    await pipe.wait_for("response.done")
    pipe.close()
    await task

    done = next(e for e in pipe.sent if e["type"] == "response.done")
    assert done["response"]["status"] == "cancelled"
    assert pipe.types()[-1] == "response.done"  # nothing after the done — no late deltas


async def test_second_response_while_active_is_an_error_not_a_fork() -> None:
    async def generate(_req: GenRequest) -> AsyncIterator[GenEventT]:
        yield GenText("thinking")
        await asyncio.sleep(3600)

    pipe = _Pipe()
    task = _rig(pipe, generate)
    pipe.push({"type": "response.create"})
    await pipe.wait_for("response.output_audio_transcript.delta")
    pipe.push({"type": "response.create"})
    await pipe.wait_for("error")
    pipe.push({"type": "response.cancel"})
    await pipe.wait_for("response.done")
    pipe.close()
    await task
    assert "active response" in next(e for e in pipe.sent if e["type"] == "error")["error"][
        "message"
    ]


async def test_tool_results_continue_one_history_never_a_fork() -> None:
    requests: list[GenRequest] = []

    async def generate(req: GenRequest) -> AsyncIterator[GenEventT]:
        requests.append(req)
        if len(requests) == 1:
            yield GenToolCall("c1", "set_light", "{}")
        else:
            yield GenText("The light is on.")

    pipe = _Pipe()
    task = _rig(pipe, generate)
    pipe.push({"type": "input_audio_buffer.append", "audio": _b64(b"\x00" * 320)})
    pipe.push({"type": "input_audio_buffer.commit"})
    pipe.push({"type": "response.create"})
    await pipe.wait_for("response.done")
    pipe.push(
        {
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": "c1", "output": "on"},
        }
    )
    pipe.push({"type": "response.create"})
    await pipe.wait_for("response.audio.delta")
    pipe.close()
    await task

    second = requests[1].history
    kinds = [item.get("type") or item.get("role") for item in second]
    # the same conversation carries the user turn, the call, and its result
    assert kinds == ["user", "function_call", "function_call_output"]


# ------------------------------------------------------------- the round trip


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_engine_client_round_trip_over_a_real_websocket() -> None:
    requests: list[GenRequest] = []
    voices: list[str] = []

    async def generate(req: GenRequest) -> AsyncIterator[GenEventT]:
        requests.append(req)
        if len(requests) == 1:
            yield GenToolCall("c1", "set_light", "{}")
            yield GenText("On it.")
        else:
            yield GenText("Done.")

    async def synthesize(_text: str, voice: str) -> AsyncIterator[bytes]:
        voices.append(voice)
        yield b"\x01\x02"

    port = _free_port()
    server = await serve(
        "127.0.0.1", port, transcribe=_transcribe, generate=generate, synthesize=synthesize
    )
    profile = replace(KENZY_S2S, url=f"ws://127.0.0.1:{port}/v1/realtime")
    got: list[EngineEvent] = []
    try:
        async with EngineClient(profile) as client:
            await client.configure(instructions="be brief", voice="af_heart")
            await client.append(b"\x00" * 320)
            await client.commit()  # KENZY_S2S is GA-shaped: commit + response.create
            async with asyncio.timeout(5):
                async for evt in client.events():
                    got.append(evt)
                    assert not isinstance(evt, EngineError)
                    if isinstance(evt, ResponseDone):
                        if sum(isinstance(e, ResponseDone) for e in got) == 1:
                            await client.submit_tool_result("c1", "the light is on")
                        else:
                            break
    finally:
        server.close()
        await server.wait_closed()

    transcript = next(e for e in got if isinstance(e, InputTranscript))
    assert transcript.text == "turn on the light" and not transcript.late
    call = next(e for e in got if isinstance(e, ToolCall))
    assert (call.call_id, call.name) == ("c1", "set_light")
    # transcript-first held across the REAL seam, not just in-process
    assert got.index(transcript) < got.index(call)
    assert any(isinstance(e, AudioDelta) and e.pcm == b"\x01\x02" for e in got)
    # decision 8 + passthrough: our engine speaks Kenzy's own voice namespace
    assert voices and all(v == "af_heart" for v in voices)
    # one history: the follow-on generation saw the tool result
    kinds = [i.get("type") or i.get("role") for i in requests[1].history]
    assert "function_call_output" in kinds


async def test_cancel_never_kills_the_transcript() -> None:
    """Seam decision 4, the cancel edge: a response cancelled mid-transcription
    still puts the input transcript on record BEFORE the cancelled done —
    the cancel kills the response, never the record."""
    release = asyncio.Event()

    async def slow_transcribe(_pcm: bytes) -> str:
        await release.wait()
        return "wait, I mean turn it off"

    async def generate(_req: GenRequest) -> AsyncIterator[GenEventT]:
        yield GenText("never reached")

    async def synthesize(_text: str, _voice: str) -> AsyncIterator[bytes]:
        yield b"\x00"  # never reached: the response is cancelled first

    pipe = _Pipe()
    session = RealtimeSession(
        pipe, transcribe=slow_transcribe, generate=generate, synthesize=synthesize
    )
    task = asyncio.get_running_loop().create_task(session.run())
    pipe.push({"type": "input_audio_buffer.append", "audio": _b64(b"\x00" * 320)})
    pipe.push({"type": "input_audio_buffer.commit"})
    pipe.push({"type": "response.create"})
    await pipe.wait_for("response.created")
    pipe.push({"type": "response.cancel"})  # barge lands mid-transcription
    await asyncio.sleep(0.05)
    release.set()  # the transcription stage now finishes
    await pipe.wait_for("response.done")
    pipe.close()
    await task

    types = pipe.types()
    transcript_at = types.index("conversation.item.input_audio_transcription.completed")
    done_at = types.index("response.done")
    assert transcript_at < done_at  # the record landed first
    done = next(e for e in pipe.sent if e["type"] == "response.done")
    assert done["response"]["status"] == "cancelled"


async def test_synthesis_streams_per_sentence_while_generation_runs() -> None:
    """Sentence-streamed synthesis: the first sentence's AUDIO is emitted
    before the provider has finished the reply — time-to-first-audio is one
    sentence, not the whole text. Engine-internal plumbing (decision 8 guard
    b): same delta events on the wire, just sooner."""
    pipe = _Pipe()
    gate = asyncio.Event()

    async def generate(_req: GenRequest) -> AsyncIterator[GenEventT]:
        yield GenText("One done. ")
        await gate.wait()  # the provider is mid-generation...
        yield GenText("Two done.")

    task = _rig(pipe, generate)
    pipe.push({"type": "input_audio_buffer.append", "audio": _b64(b"\x00\x00")})
    pipe.push({"type": "input_audio_buffer.commit"})
    pipe.push({"type": "response.create"})

    # Sentence one's audio arrives while the provider is still parked.
    await pipe.wait_for("response.audio.delta")
    assert not gate.is_set()
    types_before = pipe.types()
    assert types_before.index("response.audio.delta") < len(types_before)

    gate.set()
    await pipe.wait_for("response.done")
    # The tail ("Two done." — no trailing space, never a "complete" sentence)
    # is spoken after generation ends: one audio delta per segment here.
    assert pipe.types().count("response.audio.delta") == 2
    # And the transcript deltas still carry the full text in order.
    text = "".join(
        e["delta"] for e in pipe.sent if e["type"] == "response.output_audio_transcript.delta"
    )
    assert text == "One done. Two done."
    pipe.close()
    await task


async def test_spoken_text_is_recorded_in_history_on_cancel() -> None:
    """A barge cancels a sentence-streamed reply mid-stream. Whatever was
    already SPOKEN (emitted to the node) must be recorded in history, or the
    next turn's model has no record it spoke and re-answers/contradicts."""
    gate = asyncio.Event()

    async def generate(_req: GenRequest) -> AsyncIterator[GenEventT]:
        yield GenText("The garage is open. ")  # a complete sentence — spoken
        await gate.wait()  # parked; the cancel lands here
        yield GenText("I'll close it.")

    async def synthesize(_text: str, voice: str) -> AsyncIterator[bytes]:
        yield b"\x01"

    session = RealtimeSession(
        _p := _Pipe(), transcribe=_transcribe, generate=generate, synthesize=synthesize
    )
    task = asyncio.get_running_loop().create_task(session.run())
    _p.push({"type": "response.create"})
    await _p.wait_for("response.audio.delta")  # sentence one was synthesized + spoken
    _p.push({"type": "response.cancel"})
    await _p.wait_for("response.done")
    _p.close()
    await task

    assistant = [h for h in session._history if h.get("role") == "assistant"]
    assert assistant and "garage is open" in assistant[-1]["content"]


# ---------------------------------------------------------------- HTTP doors


async def _serve_doors(monkeypatch=None):
    port = _free_port()

    async def synthesize(_t: str, _v: str) -> AsyncIterator[bytes]:
        yield b"\x00"

    async def generate(_r: GenRequest) -> AsyncIterator[GenText]:
        yield GenText("hi")

    server = await serve(
        "127.0.0.1", port, transcribe=_transcribe, generate=generate, synthesize=synthesize
    )
    return server, f"http://127.0.0.1:{port}"


async def test_http_doors_restart_upgrade_unit(monkeypatch):
    """The service-parity doors (prod 2026-09-02: the dashboard's restart/
    upgrade/disable buttons all POST, which websockets refuses before any
    handler runs — so the doors are signed GETs and the dashboard falls back).
    """
    import os as _os

    import httpx

    import kenzy.upgrade as upgrade_mod

    monkeypatch.delenv("KENZY_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("KENZY_SERVICE_TOKEN", raising=False)

    execs: list[list[str]] = []
    monkeypatch.setattr(_os, "execv", lambda *a: execs.append(list(a)))

    async def fake_pip(extra: str, version):
        return True, f"upgraded {extra} to {version or 'latest'}"

    monkeypatch.setattr(upgrade_mod, "run_pip_upgrade", fake_pip)

    server, base = await _serve_doors()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{base}/health")
            assert r.status_code == 200 and r.json()["service"] == "s2s"

            # restart: 200 + a scheduled re-exec
            r = await client.get(f"{base}/restart")
            assert r.status_code == 200 and r.json()["status"] == "restarting"
            await asyncio.sleep(0.5)
            assert execs, "restart never re-exec'd"

            # upgrade: pip runs, then re-exec
            execs.clear()
            r = await client.get(f"{base}/upgrade", params={"version": "9.9.9"})
            assert r.status_code == 200 and r.json()["ok"] is True
            assert "9.9.9" in r.json()["output"]
            await asyncio.sleep(0.5)
            assert execs, "upgrade never re-exec'd"

            # unit: state read answers; unsupported actions are named
            r = await client.get(f"{base}/unit")
            assert r.status_code == 200 and r.json()["unit"] == "kenzy-s2s.service"
            r = await client.get(f"{base}/unit", params={"action": "enable"})
            assert r.json()["ok"] is False
    finally:
        server.close()
        await server.wait_closed()


async def test_http_doors_require_signature_when_token_set(monkeypatch):
    import httpx

    from kenzy import serviceauth

    monkeypatch.setenv("KENZY_SERVER_TOKEN", "sekret")
    server, base = await _serve_doors()
    try:
        async with httpx.AsyncClient() as client:
            # unsigned → refused; /health stays open (the dashboard polls it)
            r = await client.get(f"{base}/restart")
            assert r.status_code == 401
            r = await client.get(f"{base}/health")
            assert r.status_code == 200

            # a valid signature opens the door
            import os as _os

            monkeypatch.setattr(_os, "execv", lambda *a: None)
            sig = serviceauth.sign_service_request("sekret", "GET", "/restart")
            r = await client.get(f"{base}/restart", headers={serviceauth.SIG_HEADER: sig})
            assert r.status_code == 200
    finally:
        server.close()
        await server.wait_closed()
