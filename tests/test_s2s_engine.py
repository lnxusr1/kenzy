"""Engine-client tests against a fake Realtime server replaying MEASURED engine
behaviors (2026-08-22 probe findings — see kenzy-design/app/s2s-design.md):

- OpenAI-shaped: requires response.create; input transcript arrives LATE
  (after response.done — the race that makes transcript-gating mandatory).
- HF-s2s-shaped: STT-driven auto-response; a commit-time response.create is
  auto-CANCELLED; transcript always precedes the response.

The fake asserts on what the client SENDS (the normalization under test) and
scripts what it receives (the event parsing under test).
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from websockets.asyncio.server import ServerConnection, serve

from kenzy.s2s import (
    AudioDelta,
    EngineClient,
    EngineEvent,
    InputTranscript,
    ResponseDone,
    ResponseStarted,
    SessionReady,
    ToolCall,
)
from kenzy.s2s.profiles import HF_LOCAL, OPENAI_REALTIME, EngineProfile


def _b64(pcm: bytes) -> str:
    return base64.b64encode(pcm).decode()


class _FakeEngine:
    """Scripted Realtime server; records every client message."""

    def __init__(self, flavor: str) -> None:
        self.flavor = flavor  # "openai" | "hf"
        self.received: list[dict[str, Any]] = []

    async def handler(self, ws: ServerConnection) -> None:
        try:
            async for raw in ws:
                msg = json.loads(raw)
                self.received.append(msg)
                mtype = msg.get("type")
                if mtype == "session.update":
                    await ws.send(json.dumps({"type": "session.updated"}))
                elif mtype == "input_audio_buffer.commit" and self.flavor == "hf":
                    # STT-driven: transcript FIRST, then auto-response.
                    await ws.send(
                        json.dumps(
                            {
                                "type": "conversation.item.input_audio_transcription.completed",
                                "transcript": "what time is it",
                            }
                        )
                    )
                    await ws.send(json.dumps({"type": "response.created"}))
                    await ws.send(
                        json.dumps({"type": "response.audio.delta", "delta": _b64(b"\x01\x02")})
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "type": "response.done",
                                "response": {
                                    "status": "completed",
                                    "usage": {"input_tokens": 3, "output_tokens": 2},
                                },
                            }
                        )
                    )
                elif mtype == "response.create" and self.flavor == "hf":
                    # Measured: the HF server auto-cancels a commit-time create.
                    await ws.send(
                        json.dumps(
                            {"type": "response.done", "response": {"status": "cancelled"}}
                        )
                    )
                elif mtype == "response.create" and self.flavor == "openai":
                    # Response BEFORE transcript; transcript lands after done
                    # (the measured late-transcript race).
                    await ws.send(json.dumps({"type": "response.created"}))
                    await ws.send(
                        json.dumps(
                            {
                                "type": "response.output_audio.delta",  # GA name family
                                "delta": _b64(b"\x03\x04"),
                            }
                        )
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "type": "response.done",
                                "response": {
                                    "status": "completed",
                                    "usage": {"input_tokens": 43, "output_tokens": 71},
                                },
                            }
                        )
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "type": "conversation.item.input_audio_transcription.completed",
                                "transcript": "what time is it",
                            }
                        )
                    )
                elif mtype == "response.cancel":
                    await ws.send(
                        json.dumps(
                            {"type": "response.done", "response": {"status": "cancelled"}}
                        )
                    )
        except Exception:  # noqa: BLE001 — fake server: end quietly on close
            pass

    def sent_types(self) -> list[str]:
        return [str(m.get("type")) for m in self.received]


async def _collect(client: EngineClient, count: int, timeout: float = 5.0) -> list[EngineEvent]:
    """Collect ``count`` events, skipping SessionReady (configure's ack — noise
    for these assertions)."""
    out: list[EngineEvent] = []

    async def run() -> None:
        async for evt in client.events():
            if isinstance(evt, SessionReady):
                continue
            out.append(evt)
            if len(out) >= count:
                return

    await asyncio.wait_for(run(), timeout)
    return out


def _local(profile: EngineProfile, port: int) -> EngineProfile:
    return EngineProfile(
        name=profile.name,
        url=f"ws://127.0.0.1:{port}",
        requires_response_create=profile.requires_response_create,
        engine_transcription=profile.engine_transcription,
        voice_map=profile.voice_map,
        default_voice=profile.default_voice,
        auth="none",
    )


async def test_openai_profile_sends_response_create_and_flags_late_transcript() -> None:
    fake = _FakeEngine("openai")
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with EngineClient(_local(OPENAI_REALTIME, port)) as client:
            await client.configure(instructions="hi", voice="bm_fable")
            await client.append(b"\x00" * 320)
            await client.commit()
            events = await _collect(client, 4)

    assert "response.create" in fake.sent_types()  # the OpenAI-shaped requirement
    assert isinstance(events[0], ResponseStarted)
    assert isinstance(events[1], AudioDelta) and events[1].pcm == b"\x03\x04"
    done = events[2]
    assert isinstance(done, ResponseDone) and done.input_tokens == 43
    late = events[3]
    assert isinstance(late, InputTranscript)
    assert late.late is True  # the measured race, surfaced explicitly


async def test_hf_profile_omits_response_create_and_transcript_precedes() -> None:
    fake = _FakeEngine("hf")
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with EngineClient(_local(HF_LOCAL, port)) as client:
            await client.configure(instructions="hi", voice="marin")
            await client.append(b"\x00" * 320)
            await client.commit()
            events = await _collect(client, 4)

    # The normalization under test: NO response.create for an auto-responding engine.
    assert "response.create" not in fake.sent_types()
    transcript = events[0]
    assert isinstance(transcript, InputTranscript) and transcript.late is False
    assert isinstance(events[1], ResponseStarted)


async def test_voice_is_mapped_never_passed_through() -> None:
    fake = _FakeEngine("hf")
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with EngineClient(_local(HF_LOCAL, port)) as client:
            await client.configure(instructions="hi", voice="marin")  # canonical
            await asyncio.sleep(0.05)

    update = next(m for m in fake.received if m.get("type") == "session.update")
    voice = update["session"]["audio"]["output"]["voice"]
    assert voice == "bm_fable"  # mapped into the engine's namespace
    # and an unknown canonical voice falls back, never passes through raw
    assert HF_LOCAL.map_voice("totally_unknown") == "bm_fable"


async def test_tool_call_parsed_and_result_round_trip() -> None:
    fake = _FakeEngine("hf")
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with EngineClient(_local(HF_LOCAL, port)) as client:
            await client.configure(instructions="hi", voice="marin")

            async def emit_tool() -> None:
                # Script a function_call from the fake's side via its handler
                # is message-driven; simplest is to inject through commit here.
                await client.append(b"\x00" * 320)

            await emit_tool()
            # Direct parse-path check (no wire round trip needed for the shape):
            parsed = client._parse(  # noqa: SLF001 — deliberate parse-path unit test
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "set_light",
                        "arguments": '{"room": "office", "on": true}',
                    },
                }
            )
            assert isinstance(parsed, ToolCall)
            assert parsed.name == "set_light" and parsed.call_id == "call_1"

            await client.submit_tool_result("call_1", '{"status": "ok"}')
            await asyncio.sleep(0.05)

    types = fake.sent_types()
    item_idx = types.index("conversation.item.create")
    assert types[item_idx + 1] == "response.create"  # result then explicit respond
    item = fake.received[item_idx]["item"]
    assert item["type"] == "function_call_output" and item["call_id"] == "call_1"


async def test_cancel_sends_response_cancel() -> None:
    fake = _FakeEngine("openai")
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with EngineClient(_local(OPENAI_REALTIME, port)) as client:
            await client.configure(instructions="hi", voice="bm_fable")
            await client.cancel()
            events = await _collect(client, 1)

    assert "response.cancel" in fake.sent_types()
    done = events[-1]
    assert isinstance(done, ResponseDone) and done.status == "cancelled"


def test_nested_chat_tools_normalize_to_flat_realtime_shape() -> None:
    """The skill registry emits chat-completions NESTED schemas; Realtime
    sessions take them FLAT. The client normalizes at the seam boundary —
    found live: the mixed shape passed the local engine and errored OpenAI's
    GA API, silently dropping every cloud conversation to classic."""
    nested = {
        "type": "function",
        "function": {
            "name": "set_light",
            "description": "Turn a light on or off.",
            "parameters": {"type": "object", "properties": {"on": {"type": "boolean"}}},
        },
    }
    flat = EngineClient._realtime_tool(nested)
    assert flat == {
        "type": "function",
        "name": "set_light",
        "description": "Turn a light on or off.",
        "parameters": {"type": "object", "properties": {"on": {"type": "boolean"}}},
    }
    # An already-flat tool (END_CONVERSATION_TOOL's shape) passes through intact.
    assert EngineClient._realtime_tool(flat) == flat
