"""The engine client: one turn's audio in, typed events out, cancel on demand.

Kenzy-side turn policy throughout (seam decision 1): server VAD is disabled and
the CALLER commits the turn boundary — frames stream in as they arrive (the
paced-streaming measurement showed engines work ahead during speech), and
``commit()`` is the endpoint decision.

The event surface is deliberately tiny and engine-independent. Two measured
behaviors shape it (see kenzy-design/app/s2s-design.md, probe findings):

- ``response.done`` is NOT end-of-turn bookkeeping: the cloud engine's input
  transcript can arrive AFTER it (measured 653 ms after). The event stream
  therefore stays open until the consumer closes it, and late transcripts are
  flagged as such.
- Both event-name families are parsed (``response.audio.delta`` and the GA
  ``response.output_audio.delta``): engines mix them.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from kenzy.s2s.profiles import EngineProfile


@dataclass(frozen=True)
class SessionReady:
    """The engine acknowledged the session configuration."""


@dataclass(frozen=True)
class InputTranscript:
    """The engine's transcript of the user's audio (only if the profile asked
    for engine transcription). ``late`` means it arrived after ``response.done``
    — the measured cloud race that makes transcript-gating mandatory."""

    text: str
    late: bool


@dataclass(frozen=True)
class ResponseStarted:
    """The engine began generating a response."""


@dataclass(frozen=True)
class AudioDelta:
    """A chunk of reply audio (raw pcm16 at the session's output rate)."""

    pcm: bytes


@dataclass(frozen=True)
class ReplyTranscriptDelta:
    """Incremental transcript of the engine's SPOKEN reply."""

    text: str


@dataclass(frozen=True)
class ToolCall:
    """A completed function call from the engine. Execution is the caller's —
    and gated (seam decision 4): nothing here runs anything."""

    call_id: str
    name: str
    arguments_json: str


@dataclass(frozen=True)
class ResponseDone:
    """The response finished. NOT the end of the event stream — late input
    transcripts may still follow."""

    status: str
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class EngineError:
    """An error event from the engine."""

    message: str


EngineEvent = (
    SessionReady
    | InputTranscript
    | ResponseStarted
    | AudioDelta
    | ReplyTranscriptDelta
    | ToolCall
    | ResponseDone
    | EngineError
)

_AUDIO_DELTA_TYPES = ("response.audio.delta", "response.output_audio.delta")
_REPLY_TRANSCRIPT_TYPES = (
    "response.audio_transcript.delta",
    "response.output_audio_transcript.delta",
)


class EngineClient:
    """A single engine session: configure, stream, commit, read events, cancel.

    One instance is one engine session; the conversation lifecycle above the
    seam decides when sessions begin and end.
    """

    def __init__(self, profile: EngineProfile, *, model: str = "", api_key: str = "",
                 url: str = "") -> None:
        self._profile = profile
        self._model = model
        self._api_key = api_key
        self._url = url or profile.url
        self._ws: ClientConnection | None = None
        self._saw_response_done = False

    async def __aenter__(self) -> EngineClient:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None,
                        exc: BaseException | None, tb: TracebackType | None) -> None:
        await self.aclose()

    async def connect(self) -> None:
        url = f"{self._url}?model={self._model}" if self._model else self._url
        headers: dict[str, str] = {}
        if self._profile.auth == "bearer" and self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        ssl_ctx = None
        if url.startswith("wss://"):
            # Mesh TLS: the house posture is encrypted-unverified self-signed
            # (KENZY_TLS_VERIFY/KENZY_TLS_CA harden it) — same as every service.
            from kenzy import tlsutil

            ssl_ctx = tlsutil.client_context_from_env()
        self._ws = await websockets.connect(
            url, additional_headers=headers, max_size=1 << 24, ssl=ssl_ctx
        )

    async def aclose(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    @staticmethod
    def _realtime_tool(tool: dict[str, Any]) -> dict[str, Any]:
        """Normalize a tool schema to the flat Realtime shape.

        The skill registry emits chat-completions NESTED schemas
        (``{"type": "function", "function": {...}}``); Realtime sessions take
        them FLAT. Normalized here, at the seam boundary, so the wire shape
        never depends on where a schema came from — found live 2026-08-29:
        the mixed shape sailed through the local engine (whose provider wants
        nested anyway) and errored OpenAI's GA API, silently dropping every
        cloud conversation to the classic fallback.
        """
        fn = tool.get("function")
        if not isinstance(fn, dict):
            return tool
        return {
            "type": "function",
            "name": str(fn.get("name", "")),
            "description": str(fn.get("description", "")),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        }

    async def configure(self, *, instructions: str, voice: str,
                        tools: list[dict[str, Any]] | None = None,
                        rate: int = 24000) -> None:
        """Send the session configuration (GA shape; both measured engines accept it).

        ``voice`` is the CANONICAL Kenzy voice — mapped through the profile,
        never passed through raw (seam decision 5).
        """
        session: dict[str, Any] = {
            "type": "realtime",
            "output_modalities": ["audio"],
            "instructions": instructions,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": rate},
                    "turn_detection": None,  # seam decision 1: Kenzy commits the turn
                    **(
                        {"transcription": {"model": "whisper-1"}}
                        if self._profile.engine_transcription
                        else {}
                    ),
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": rate},
                    "voice": self._profile.map_voice(voice),
                },
            },
        }
        if tools:
            session["tools"] = [self._realtime_tool(t) for t in tools]
            session["tool_choice"] = "auto"
        await self._send({"type": "session.update", "session": session})

    async def append(self, pcm: bytes) -> None:
        """Stream one chunk of captured audio (call as frames arrive)."""
        await self._send(
            {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}
        )

    async def commit(self, *, respond: bool = True) -> None:
        """The endpoint decision: the utterance is over.

        Per-profile normalization (the first measured conformance divergence):
        OpenAI-shaped engines need an explicit ``response.create``; STT-driven
        engines auto-respond and would auto-cancel one sent at commit time.

        ``respond=False`` commits WITHOUT requesting a response — the ask()
        answer turn's door: the engine transcribes the utterance (input
        transcription runs at commit, GA semantics) but the model stays out of
        it, because the answer belongs to the parked skill, not to a fresh
        generation over a still-open function call.
        """
        await self._send({"type": "input_audio_buffer.commit"})
        if respond and self._profile.requires_response_create:
            await self._send({"type": "response.create"})

    async def cancel(self) -> None:
        """Stop the in-flight response (measured: ~65 ms cloud, ~2 ms local)."""
        await self._send({"type": "response.cancel"})

    async def respond(self) -> None:
        """Ask for a response with no new audio — the DELIVERY TURN's door:
        a background task's completion lands as a context item and this makes
        the model speak it."""
        await self._send({"type": "response.create"})

    async def add_context(self, text: str) -> None:
        """Append a system-context item to the conversation — no response asked.

        The identity-injection door (OQ3 slice 1, ruled 2026-08-29): facts
        Kenzy resolves out-of-band (who is speaking, later people/rooms/
        occupancy) land as conversation context the model reads on its next
        response. CONTEXT ONLY — authorization stays with the gate.
        """
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )

    async def submit_tool_result(
        self, call_id: str, output: str, *, respond: bool = True
    ) -> None:
        """Return a gated tool's result — and, normally, ask the engine to
        speak about it. ``respond=False`` records the output WITHOUT a
        follow-on response: the detach hand-off's door (the calling response
        already spoke "I'll look that up" — a follow-on only repeats it,
        lived 2026-08-29 as the double acknowledgment)."""
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {"type": "function_call_output", "call_id": call_id, "output": output},
            }
        )
        if respond:
            await self._send({"type": "response.create"})

    async def events(self) -> AsyncIterator[EngineEvent]:
        """Yield typed events until the connection closes or the caller stops.

        Deliberately does NOT stop at ``response.done`` — late input
        transcripts arrive after it (measured), and the conversation layer
        owns the session's end, not the engine.
        """
        ws = self._require_ws()
        while True:
            try:
                raw = await ws.recv()
            except websockets.exceptions.ConnectionClosed:
                return
            evt = json.loads(raw)
            parsed = self._parse(evt)
            if parsed is not None:
                yield parsed

    def _parse(self, evt: dict[str, Any]) -> EngineEvent | None:
        et = str(evt.get("type", ""))
        if et in _AUDIO_DELTA_TYPES:
            return AudioDelta(base64.b64decode(evt.get("delta", "")))
        if et in _REPLY_TRANSCRIPT_TYPES:
            return ReplyTranscriptDelta(str(evt.get("delta", "")))
        if et == "conversation.item.input_audio_transcription.completed":
            return InputTranscript(str(evt.get("transcript", "")), late=self._saw_response_done)
        if et == "response.output_item.done":
            item = evt.get("item") or {}
            if item.get("type") == "function_call":
                return ToolCall(
                    call_id=str(item.get("call_id", "")),
                    name=str(item.get("name", "")),
                    arguments_json=str(item.get("arguments", "")),
                )
            return None
        if et == "response.created":
            return ResponseStarted()
        if et == "response.done":
            self._saw_response_done = True
            resp = evt.get("response") or {}
            usage = resp.get("usage") or {}
            return ResponseDone(
                status=str(resp.get("status", "")),
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
            )
        if et == "session.updated":
            return SessionReady()
        if et == "error":
            return EngineError(json.dumps(evt.get("error", {})))
        return None

    async def _send(self, payload: dict[str, Any]) -> None:
        await self._require_ws().send(json.dumps(payload))

    def _require_ws(self) -> ClientConnection:
        if self._ws is None:
            raise RuntimeError("engine client is not connected")
        return self._ws
