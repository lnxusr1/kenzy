"""kenzy-s2s — the Realtime-shaped session server: v6.0's engine behind the seam.

Spec: kenzy-design/app/s2s-design.md, decision 8. This is not a new model
stack — it is an **orchestration layer over the services Kenzy already runs**
(kenzy-stt, kenzy-llm, kenzy-tts), speaking the OpenAI Realtime protocol shape
so everything above the seam (the engine client, the gate, the conversation
layer) cannot tell it from the engines it will someday be replaced by. The
three stages arrive **injected** (``transcribe`` / ``generate`` /
``synthesize``): the service entry point wires HTTP adapters to the real
services; tests wire fakes. Models load once, in the services — never here.

Constitutive properties, not options:

- **Transcript-first by construction** (decision 4, the qualifying bar): the
  input transcript event is emitted before ANY response output exists,
  because the cascade cannot generate without transcribing. Our engine meets
  the bar natively rather than by adaptation.
- **GA turn semantics**: ``commit`` does not auto-respond — the caller sends
  ``response.create`` (the seam's north-star shape; the HF server's STT-driven
  auto-response was measured as a conformance divergence, not copied).
- **Cancel stops the stream immediately** (the local engine measured ~2 ms,
  0 late deltas — that behavior is the contract here).
- **One history, never a fork**: tool results submitted between responses land
  in the same conversation the next response continues from.

No Kenzy policy lives here (the seam's oldest rule): identity, the gate, the
lockbox, and delivery all sit client-side of this server.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from ssl import SSLContext
from typing import Any, Protocol
from urllib.parse import parse_qsl

import websockets
from websockets.asyncio.server import Server, ServerConnection
from websockets.asyncio.server import serve as _ws_serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from kenzy.sentences import split_sentences, strip_spoken_markup

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ stage seams


@dataclass(frozen=True)
class GenText:
    """A chunk of the reply's text from the generation stage."""

    text: str


@dataclass(frozen=True)
class GenToolCall:
    """A completed function call from the generation stage."""

    call_id: str
    name: str
    arguments_json: str


GenEvent = GenText | GenToolCall


@dataclass(frozen=True)
class GenRequest:
    """Everything the generation stage sees for one response."""

    instructions: str
    tools: list[dict[str, Any]]
    history: list[dict[str, Any]]


#: Whole-utterance transcription (kenzy-stt's door; open question 1's chunked
#: variant slots in behind the same signature).
Transcribe = Callable[[bytes], Awaitable[str]]
#: Streamed generation over the session history — text deltas and tool calls.
Generate = Callable[[GenRequest], AsyncIterator[GenEvent]]
#: Streamed synthesis: (text, voice) -> pcm16 chunks (kenzy-tts already streams).
Synthesize = Callable[[str, str], AsyncIterator[bytes]]


class Transport(Protocol):
    """What a session needs from its connection — a real WebSocket, or a fake."""

    async def recv(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...


# ---------------------------------------------------------------- the session


class RealtimeSession:
    """One client connection: session config, audio buffer, responses, history."""

    def __init__(
        self,
        transport: Transport,
        *,
        transcribe: Transcribe,
        generate: Generate,
        synthesize: Synthesize,
    ) -> None:
        self._transport = transport
        self._transcribe = transcribe
        self._generate = generate
        self._synthesize = synthesize
        self._send_lock = asyncio.Lock()
        self._instructions = ""
        self._voice = ""
        self._tools: list[dict[str, Any]] = []
        self._buffer = bytearray()
        self._history: list[dict[str, Any]] = []
        self._response_task: asyncio.Task[None] | None = None
        #: The committed utterance's transcription, started AT COMMIT (GA
        #: semantics — input transcription belongs to the item, not to a
        #: response). A response awaits it; a commit with no response (the
        #: ask() answer turn) still transcribes, emits, and records.
        self._transcript_job: asyncio.Task[None] | None = None
        self._last_in_tokens = 0

    async def run(self) -> None:
        """The per-connection loop. Ends when the transport closes; an
        in-flight response is cancelled with it (the client is gone)."""
        try:
            while True:
                try:
                    raw = await self._transport.recv()
                except (websockets.exceptions.ConnectionClosed, EOFError):
                    return
                try:
                    evt = json.loads(raw)
                except ValueError:
                    await self._error("invalid JSON")
                    continue
                await self._dispatch(evt)
        finally:
            if self._response_task is not None and not self._response_task.done():
                self._response_task.cancel()

    # --------------------------------------------------------------- dispatch

    async def _dispatch(self, evt: dict[str, Any]) -> None:
        et = str(evt.get("type", ""))
        if et == "session.update":
            self._apply_session(evt.get("session") or {})
            await self._emit({"type": "session.updated"})
        elif et == "input_audio_buffer.append":
            self._buffer.extend(base64.b64decode(str(evt.get("audio", ""))))
        elif et == "input_audio_buffer.commit":
            pcm = bytes(self._buffer)
            self._buffer.clear()
            await self._emit({"type": "input_audio_buffer.committed"})
            if pcm:
                # Transcription starts NOW, independent of any response — so a
                # commit-without-response (the ask() answer turn) still yields
                # its transcript event, and a response cancel can never kill
                # the record (seam decision 4, now by construction).
                self._transcript_job = asyncio.get_running_loop().create_task(
                    self._record_transcript(pcm)
                )
                self._transcript_job.add_done_callback(_log_transcript_failure)
        elif et == "response.create":
            if self._response_task is not None and not self._response_task.done():
                await self._error("conversation already has an active response")
                return
            self._response_task = asyncio.get_running_loop().create_task(self._respond())
        elif et == "response.cancel":
            if self._response_task is not None and not self._response_task.done():
                self._response_task.cancel()
        elif et == "conversation.item.create":
            item = evt.get("item") or {}
            if item:
                self._history.append(dict(item))
        else:
            log.debug("kenzy-s2s: ignoring client event %r", et)

    def _apply_session(self, session: dict[str, Any]) -> None:
        if "instructions" in session:
            self._instructions = str(session.get("instructions", ""))
        if "tools" in session:
            self._tools = list(session.get("tools") or [])
        audio = session.get("audio") or {}
        output = audio.get("output") or {}
        if "voice" in output:
            self._voice = str(output.get("voice", ""))

    # -------------------------------------------------------------- responses

    async def _record_transcript(self, pcm: bytes) -> None:
        """Transcribe one committed utterance, emit its event, record history.

        Its own task, started at COMMIT — so it exists independent of any
        response: a response cancel can never kill the record (seam decision 4,
        found live: a barge mid-transcription dropped the transcript and failed
        the whole turn closed), and a commit with no response at all (the ask()
        answer turn) still transcribes and emits.
        """
        text = await self._transcribe(pcm)
        self._last_in_tokens = _rough_tokens(text)
        await self._emit(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": text,
            }
        )
        self._history.append({"role": "user", "content": text})

    async def _respond(self) -> None:
        in_tokens = 0
        out_tokens = 0
        job: asyncio.Task[None] | None = None
        # Defined before the try so the cancel/failure handlers can record
        # whatever was already SPOKEN into the room (sentence-streamed audio
        # is emitted mid-generation): on a barge or a mid-stream provider
        # failure, history must not lag the room, or the next turn's model has
        # no record it spoke and re-answers or contradicts itself.
        text_parts: list[str] = []
        try:
            await self._emit({"type": "response.created"})
            job, self._transcript_job = self._transcript_job, None
            if job is not None:
                # SHIELDED: cancelling this response must not cancel the
                # record's own task — it finishes (and emits) regardless; a
                # transcription FAILURE still fails the response, as before.
                await asyncio.shield(job)
            in_tokens = self._last_in_tokens
            unspoken = ""  # generated text not yet synthesized
            request = GenRequest(self._instructions, list(self._tools), list(self._history))
            async for gen in self._generate(request):
                if isinstance(gen, GenText):
                    text_parts.append(gen.text)
                    await self._emit(
                        {"type": "response.output_audio_transcript.delta", "delta": gen.text}
                    )
                    # Sentence-streamed synthesis (engine-internal plumbing —
                    # decision 8 guard (b): the seam sees the same delta
                    # events, just sooner): each completed sentence is
                    # synthesized and its audio emitted while the provider is
                    # still generating the tail, so time-to-first-audio is
                    # one sentence, not the whole reply — the 4.4 overlap,
                    # applied between the engine's own stages.
                    unspoken += gen.text
                    ready, unspoken = split_sentences(unspoken)
                    for segment in ready:
                        await self._speak(segment)
                else:
                    self._history.append(
                        {
                            "type": "function_call",
                            "call_id": gen.call_id,
                            "name": gen.name,
                            "arguments": gen.arguments_json,
                        }
                    )
                    await self._emit(
                        {
                            "type": "response.output_item.done",
                            "item": {
                                "type": "function_call",
                                "call_id": gen.call_id,
                                "name": gen.name,
                                "arguments": gen.arguments_json,
                            },
                        }
                    )
            text = "".join(text_parts)
            out_tokens = _rough_tokens(text)
            if text:
                self._history.append({"role": "assistant", "content": text})
            # The tail: whatever never reached a sentence boundary.
            await self._speak(unspoken)
            await self._done("completed", in_tokens, out_tokens)
        except asyncio.CancelledError:
            # The contract: cancel stops the stream HERE — no late deltas. The
            # input transcript's own task survives the cancel and still emits —
            # and the cancelled done WAITS for it, so the record lands BEFORE
            # response.done (the runner's ordering contract, seam decision 4).
            if job is not None and not job.done():
                with contextlib.suppress(Exception):
                    await asyncio.shield(job)
            self._record_spoken(text_parts)  # what the room already heard
            await self._done("cancelled", in_tokens, out_tokens)
        except Exception as exc:  # noqa: BLE001 — a stage failed; the client is told
            log.exception("kenzy-s2s: response failed")
            self._record_spoken(text_parts)  # what the room already heard
            await self._error(f"response failed: {exc}")
            await self._done("failed", in_tokens, out_tokens)

    def _record_spoken(self, text_parts: list[str]) -> None:
        """Append partial spoken text to history on a cancel/failure — but
        only if the completed path hasn't already recorded it, so a normal
        completion isn't double-appended. Idempotent by the last-item check."""
        text = "".join(text_parts).strip()
        if not text:
            return
        last = self._history[-1] if self._history else None
        already = (
            isinstance(last, dict)
            and last.get("role") == "assistant"
            and last.get("content") == text
        )
        if already:
            return  # the completed path already recorded it
        self._history.append({"role": "assistant", "content": text})

    async def _speak(self, text: str) -> None:
        """Synthesize one text segment and emit its audio deltas.

        The text→audio boundary strips markdown artifacts (a model that
        ignores the plain-prose instruction emits ``**bold**`` and the voice
        says "asterisk asterisk" — lived, 2026-09-02); the transcript deltas
        and history keep the model's own text."""
        text = strip_spoken_markup(text)
        if not text.strip():
            return
        async for chunk in self._synthesize(text, self._voice):
            await self._emit(
                {
                    "type": "response.audio.delta",
                    "delta": base64.b64encode(chunk).decode(),
                }
            )

    async def _done(self, status: str, in_tokens: int, out_tokens: int) -> None:
        await self._emit(
            {
                "type": "response.done",
                "response": {
                    "status": status,
                    "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens},
                },
            }
        )

    # -------------------------------------------------------------- plumbing

    async def _error(self, message: str) -> None:
        await self._emit({"type": "error", "error": {"message": message}})

    async def _emit(self, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            try:
                await self._transport.send(json.dumps(payload))
            except (websockets.exceptions.ConnectionClosed, EOFError):
                raise asyncio.CancelledError from None  # client gone: stop the response


def _rough_tokens(text: str) -> int:
    """Crude usage accounting (chars/4) — honest about being an estimate."""
    return len(text) // 4


def _log_transcript_failure(task: asyncio.Task[None]) -> None:
    """Retrieve a commit-time transcription task's outcome. A response that
    awaited it already reported the failure; a commit with NO response (the
    ask() answer turn) has only this — the client's bounded transcript wait
    fails that turn closed, and this line says why."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("kenzy-s2s: input transcription failed: %s", exc)


# ----------------------------------------------------------------- the server


@dataclass
class _Stages:
    transcribe: Transcribe
    generate: Generate
    synthesize: Synthesize
    sessions: int = field(default=0)


async def serve(
    host: str,
    port: int,
    *,
    transcribe: Transcribe,
    generate: Generate,
    synthesize: Synthesize,
    ssl: SSLContext | None = None,
) -> Server:
    """Start the engine's WebSocket server; returns the listening server.

    The service entry point (config-pull, registration heartbeat) wires this
    at boot; tests bind port 0 and read the ephemeral port back.
    """
    stages = _Stages(transcribe, generate, synthesize)

    async def handler(ws: ServerConnection) -> None:
        stages.sessions += 1
        log.info("kenzy-s2s: session opened (%d this run)", stages.sessions)
        session = RealtimeSession(
            ws,
            transcribe=stages.transcribe,
            generate=stages.generate,
            synthesize=stages.synthesize,
        )
        await session.run()

    def _json(status: int, reason: str, payload: dict[str, Any]) -> Response:
        headers = Headers()
        headers["Content-Type"] = "application/json"
        return Response(status, reason, headers, json.dumps(payload).encode())

    def _schedule_exec(why: str) -> None:
        async def _exec() -> None:
            await asyncio.sleep(0.3)  # let the HTTP response flush first
            log.warning("%s — re-executing service", why)
            os.execv(sys.executable, [sys.executable, *sys.argv])

        asyncio.get_running_loop().create_task(_exec())

    async def process_request(
        _connection: ServerConnection, request: Request
    ) -> Response | None:
        """Plain-HTTP doors on the WS port (the main server's own pattern).

        Every FastAPI backend gets ``/restart``, ``/upgrade`` and ``/unit``
        from ``kenzy.fastapi_auth``; this websockets service must answer the
        SAME doors or the dashboard's buttons and the upgrade sweep report it
        failed while the OLD process keeps serving the upgraded venv (lived on
        prod 2026-09-02 — the fifth s2s service-parity strike). All doors are
        **GET**: websockets' HTTP parser refuses any other method before this
        hook even runs, which is exactly why the FastAPI-shaped POSTs could
        never work here — the dashboard falls back to these signed GETs.
        State-changing doors require the token-proof signature
        (``X-Kenzy-Auth``) whenever a fleet token is set, mirroring the
        FastAPI middleware; ``/health`` stays open like everywhere else."""
        path, _, query = request.path.partition("?")
        if path == "/health":
            from kenzy import version_info

            return _json(
                200,
                "OK",
                {"status": "ok", "service": "s2s", "sessions": stages.sessions, **version_info()},
            )
        if path not in ("/restart", "/upgrade", "/unit"):
            return None
        from kenzy import serviceauth

        token = serviceauth.service_token_from_env()
        if token and (
            serviceauth.verify_service_request(
                request.headers.get(serviceauth.SIG_HEADER), token, "GET", path
            )
            is None
        ):
            return _json(401, "Unauthorized", {"detail": "invalid service token"})
        params = dict(parse_qsl(query))
        if path == "/restart":
            _schedule_exec("Restart requested")
            return _json(200, "OK", {"status": "restarting"})
        if path == "/unit":
            from kenzy.unitctl import disable_unit, unit_state

            unit = "kenzy-s2s.service"
            action = params.get("action")
            if action == "disable":

                async def _later() -> None:
                    await asyncio.sleep(0.5)  # let the response flush first
                    ok, out = await asyncio.to_thread(disable_unit, unit)
                    if not ok:
                        log.error("Self-disable failed: %s", out)

                asyncio.get_running_loop().create_task(_later())
                return _json(200, "OK", {"ok": True})
            if action:
                return _json(200, "OK", {"ok": False, "error": "only 'disable' is supported here"})
            state = await asyncio.to_thread(unit_state, unit)
            return _json(200, "OK", {"unit": unit, **state})
        # /upgrade — awaited like the FastAPI door (pip can take minutes; the
        # dashboard's fan-out call uses a long timeout), re-exec on success.
        from kenzy.upgrade import run_pip_upgrade

        ok, output = await run_pip_upgrade("s2s", params.get("version") or None)
        if ok:
            _schedule_exec("Upgrade applied")
        return _json(200, "OK", {"ok": ok, "output": output})

    return await _ws_serve(
        handler, host, port, max_size=1 << 24, ssl=ssl, process_request=process_request
    )


__all__ = [
    "Generate",
    "GenEvent",
    "GenRequest",
    "GenText",
    "GenToolCall",
    "RealtimeSession",
    "Synthesize",
    "Transcribe",
    "Transport",
    "serve",
]
