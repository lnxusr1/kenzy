"""The follow-up bridge — routes eligible captures through the conversation engine.

Spec: kenzy-design/app/s2s-design.md (the follow-up feature, 2026-08-26).
Enabled and on a `hardware_aec`-capable node, a capture becomes a TURN in a
per-node conversation: PCM streams to the kenzy-s2s engine, the gate authorizes
tool calls, reply audio rides the existing TTS frame path, and the follow-up
window re-arms through the node's own ``expect_utterance`` machinery — whose
window is anchored at the NODE's playback-complete by construction, exactly the
anchor the design demands. Disabled, incapable, or engine-down ⇒ the classic
pipeline takes the capture untouched (decision 7: a complete mode, and the
fallback).

Two implementation rules with reasons:

- **A turn task is never hard-cancelled.** The engine session holds the
  conversation history, so the connection must survive a barge-in: a new
  capture sends ``response.cancel`` (the engine stops in ~2 ms) and the
  in-flight runner drains to its cancelled ``response.done``; the new turn
  queues on the conversation's lock. Killing the task instead would leave the
  cancelled response's events in the socket for the NEXT turn's runner to
  misread.
- **Everything server-shaped is injected** (:class:`BridgeDeps`), so the
  bridge is testable without a server and the server's glue is one-line
  lambdas. No policy lives here: verdicts are the gate's, closure is the
  model's, windows are the session's.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from kenzy.s2s.conversation import (
    END_CONVERSATION_TOOL,
    ConversationSession,
    TurnRunner,
    WindowPolicy,
    end_conversation_rule,
)
from kenzy.s2s.engine import EngineEvent, ToolCall
from kenzy.s2s.gate import AuditRecord, Speaker, ToolRule, TurnGate

log = logging.getLogger(__name__)

#: Peak int16 amplitude below which a capture is a phantom, not speech
#: (~-40 dBFS; a real utterance on any of the lab mics peaks far above it).
_SILENCE_PEAK = 500


def _near_silence(pcm: bytes) -> bool:
    """Cheap phantom check: peak amplitude over the WHOLE capture, early-exit
    on the first loud sample. The whole capture matters: a real command often
    starts with a beat of silence (the pause after the wake phrase), and a
    first-second-only scan threw away "what time is it" as a phantom — found
    live. Only a capture with no loud sample ANYWHERE is a phantom."""
    if len(pcm) < 4:
        return True
    for i in range(0, len(pcm) - 1, 2):
        sample = int.from_bytes(pcm[i : i + 2], "little", signed=True)
        if abs(sample) >= _SILENCE_PEAK:
            return False
    return True


class EngineLike(Protocol):
    """What the bridge needs from an engine session — EngineClient satisfies it."""

    def events(self) -> AsyncIterator[EngineEvent]: ...

    async def configure(
        self,
        *,
        instructions: str,
        voice: str,
        tools: list[dict[str, Any]] | None = None,
        rate: int = 24000,
    ) -> None: ...

    async def append(self, pcm: bytes) -> None: ...

    async def commit(self) -> None: ...

    async def cancel(self) -> None: ...

    async def submit_tool_result(self, call_id: str, output: str) -> None: ...

    async def aclose(self) -> None: ...


@dataclass
class BridgeDeps:
    """The server's contribution, as narrow callables (glue, not surface)."""

    #: The follow-up toggle (``s2s.enabled`` — dashboard-editable, default off).
    enabled: Callable[[], bool]
    #: The engine's ws url — explicit config or the service registry; "" = unknown.
    engine_url: Callable[[], str]
    #: The per-node hardware gate: follow-up needs full duplex (hardware_aec).
    node_capable: Callable[[str], bool]
    #: Connect and return a ready engine session for the url.
    engine_factory: Callable[[str], Awaitable[EngineLike]]
    #: (tier) -> (tool schemas for session.update, {name: min_tier} policy).
    fetch_tools: Callable[[str], Awaitable[tuple[list[dict[str, Any]], dict[str, str]]]]
    #: Speaker-ID one capture: (pcm, room) -> (Speaker, confidence). The glue
    #: also feeds occupancy — voice evidence is the server's concern, not ours.
    identify: Callable[[bytes, str], Awaitable[tuple[Speaker, float]]]
    #: Execute one gate-approved call: (call, node_id, room, speaker) -> result.
    execute_tool: Callable[[ToolCall, str, str, Speaker], Awaitable[str]]
    #: Reply audio to the node (the existing TTS frame path).
    deliver_start: Callable[[str], Awaitable[None]]
    deliver_frame: Callable[[str, bytes], Awaitable[bool]]
    deliver_end: Callable[[str], Awaitable[None]]
    #: Arm the node's follow-up capture window (expect_utterance, no cue).
    hold_floor: Callable[[str], Awaitable[bool]]
    #: Open the node's capture window IMMEDIATELY — she listens while she
    #: thinks, so "wait, I mean…" lands mid-processing. Older nodes ignore the
    #: flag and arm post-TTS only: graceful degradation.
    listen_now: Callable[[str], Awaitable[None]]
    #: A completed turn's Activity record — the household-visible trail
    #: (dashboard Activity tab), same gate as the classic pipeline's.
    activity: Callable[[dict[str, Any]], None]
    #: Clear the node's dialog state (the window is over).
    end_floor: Callable[[str], None]
    #: The classic pipeline — the fallback for every path that can't run here.
    classic: Callable[[str, str, str | None, bytes], Awaitable[None]]
    #: Session instructions for the engine, built PER ROOM (persona, the room
    #: anchor for tool calls, and the closure contract). The room matters: the
    #: home-control skill takes ``room`` as a tool parameter the MODEL must
    #: pass — an unanchored model turns lights on all over the house.
    instructions: Callable[[str], str]
    #: The conduct record's sink.
    audit: Callable[[AuditRecord], None]
    #: Canonical voice — advisory on the kenzy-s2s profile (kenzy-tts's config
    #: is the voice identity, decision 5); "" is fine.
    voice: Callable[[], str] = lambda: ""
    policy: WindowPolicy = field(default_factory=WindowPolicy)


@dataclass
class _Conversation:
    session: ConversationSession
    engine: EngineLike
    rules: dict[str, ToolRule]
    room: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    barged: bool = False


class S2SBridge:
    """Per-node conversations behind the follow-up toggle."""

    def __init__(self, deps: BridgeDeps) -> None:
        self._deps = deps
        self._convs: dict[str, _Conversation] = {}

    # ----------------------------------------------------------------- state

    def active(self, node_id: str) -> bool:
        return node_id in self._convs

    def should_take(self, node_id: str) -> bool:
        """Route this capture through the engine? An open conversation always
        keeps its turns; a fresh one needs toggle + capable node + a known
        engine. Everything else is the classic pipeline's."""
        if self.active(node_id):
            return True
        return (
            self._deps.enabled()
            and self._deps.node_capable(node_id)
            and bool(self._deps.engine_url())
        )

    def node_mode(self, node_id: str) -> str:
        """The node's effective mode, for the fleet view: follow-up or classic."""
        return "follow-up" if self.should_take(node_id) else "classic"

    # ----------------------------------------------------------------- turns

    async def take_turn(
        self, node_id: str, room: str, session_id: str | None, pcm: bytes
    ) -> None:
        """One capture = one turn. Falls back to the classic pipeline (same
        pcm, nothing lost) whenever the engine can't take it."""
        if _near_silence(pcm):
            # A phantom capture (onset misfire on a suppressed mic's floor):
            # never spend an engine turn on silence. The conversation, if any,
            # stays open — its windows re-arm on the real reply.
            log.info("[%s] s2s: near-silent capture (%d bytes) — skipped", node_id, len(pcm))
            return
        for _ in range(2):  # at most: once on a stale conversation, once fresh
            conv = self._convs.get(node_id)
            if conv is not None and (conv.session.poll() is not None or conv.session.ended):
                # Capped or otherwise over — close it; this capture starts fresh.
                await self._close(node_id, conv.session.end_reason or "ended")
                conv = None
            if conv is None:
                conv = await self._open(node_id, room)
                if conv is None:
                    await self._deps.classic(node_id, room, session_id, pcm)
                    return
            conv.barged = False
            async with conv.lock:
                if conv.session.ended:
                    # Ended while we queued (a dying turn closed it) — loop
                    # once and open a FRESH conversation: with the toggle on,
                    # the wake NEVER falls back classic for a live engine.
                    continue
                await self._run_turn(node_id, conv, session_id, pcm)
                return
        await self._deps.classic(node_id, room, session_id, pcm)  # two dead convs: give up

    async def _run_turn(
        self, node_id: str, conv: _Conversation, session_id: str | None, pcm: bytes
    ) -> None:
        turn_id = conv.session.begin_turn()
        gate = TurnGate(
            turn_id,
            identity=lambda: conv.session.identity.current,
            tool_rules=conv.rules,
            audit=self._deps.audit,
        )
        started = False

        async def deliver(out: bytes) -> None:
            nonlocal started
            if not started:
                await self._deps.deliver_start(node_id)
                started = True
            await self._deps.deliver_frame(node_id, out)

        async def execute(call: ToolCall) -> str:
            return await self._deps.execute_tool(
                call, node_id, conv.room, conv.session.identity.current
            )

        # Speaker-ID runs beside the turn (decision 6: the gate reads identity
        # at action time — a verdict can't precede the engine's transcript, and
        # speaker-ID is faster than transcript+generation, so it wins the race
        # in practice; when it doesn't, the gate fails closed, by design).
        identify_task = asyncio.get_running_loop().create_task(
            self._identify(conv, pcm), name=f"s2s-id-{node_id}"
        )
        armed = False
        t0 = time.monotonic()
        try:
            await conv.engine.append(pcm)
            await conv.engine.commit()
            # She listens while she thinks: open the node's window NOW, so an
            # interjection during processing becomes the next turn (barge-in).
            await self._deps.listen_now(node_id)
            runner = TurnRunner(
                conv.engine,
                gate,
                conv.session,
                execute=execute,
                deliver=deliver,
                transcript_timeout_s=10.0,
            )
            result = await runner.run()
            # Arm the follow-up window BEFORE the stream closes: the node needs
            # expect_utterance to precede tts_end (the classic path's buffered
            # order) to treat this reply as floor-holding — that ordering is
            # what makes barge-in live DURING playback and the capture window
            # open at playback-complete. Armed after tts_end, it arms nothing.
            if result.status == "ok" and not conv.session.ended and not conv.barged:
                armed = await self._deps.hold_floor(node_id)
        finally:
            if not identify_task.done():
                identify_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await identify_task
            if started:
                await self._deps.deliver_end(node_id)

        log.info(
            "[%s/%s] s2s turn %s: %s (%d tool results)%s",
            node_id,
            (session_id or "?")[:8],
            turn_id,
            result.status,
            result.results_submitted,
            " — conversation ended" if conv.session.ended else "",
        )
        # The household-visible trail (Activity tab) — same shape as the
        # classic pipeline's record; the sink applies the dashboard.logs gate.
        self._deps.activity(
            {
                "ts": time.time(),
                "node_id": node_id,
                "room": conv.room,
                "speaker": conv.session.identity.current.name or "unknown",
                "transcript": result.transcript,
                "response": result.reply_text,
                "fast": False,
                "s2s": True,
                "spans": [],
                "stt_ms": 0,
                "speaker_ms": 0,
                "llm_ms": 0,
                "tts_ms": 0,
                "total_ms": round((time.monotonic() - t0) * 1000.0),
            }
        )
        if conv.session.ended:
            await self._close(node_id, conv.session.end_reason or "verbal")
            return
        if result.status != "ok":
            # An engine fault mid-conversation: end it honestly; the NEXT wake
            # starts fresh (and falls back classic if the engine stays down).
            await self._close(node_id, f"engine {result.status}")
            return
        if conv.barged:
            return  # a new capture already owns the floor — never re-arm over it
        if not armed:
            # The dialog machinery declined (turn cap) — the conversation ends.
            conv.session.end("hard_cap")
            await self._close(node_id, "turn cap")

    async def _identify(self, conv: _Conversation, pcm: bytes) -> None:
        try:
            speaker, confidence = await self._deps.identify(pcm, conv.room)
        except Exception as exc:  # noqa: BLE001 — identity stays fail-closed unknown
            log.warning("s2s: speaker-id failed (%s) — identity stays unknown", exc)
            return
        if speaker.name and speaker.tier != "unknown":
            conv.session.identity.hear(speaker.name, speaker.tier, confidence)
        else:
            conv.session.identity.hear_stranger()

    # ------------------------------------------------------------- lifecycle

    async def _open(self, node_id: str, room: str) -> _Conversation | None:
        url = self._deps.engine_url()
        if not url:
            return None
        try:
            engine = await self._deps.engine_factory(url)
        except Exception as exc:  # noqa: BLE001 — engine down ⇒ classic fallback
            log.warning("s2s: engine unreachable at %s (%s) — classic fallback", url, exc)
            return None
        try:
            tools, policy = await self._deps.fetch_tools("recognized")
            rules = {name: ToolRule(min_tier=tier) for name, tier in policy.items()}
            rules[str(END_CONVERSATION_TOOL["name"])] = end_conversation_rule()
            await engine.configure(
                instructions=self._deps.instructions(room),
                voice=self._deps.voice(),
                tools=[*tools, END_CONVERSATION_TOOL],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("s2s: session setup failed (%s) — classic fallback", exc)
            with contextlib.suppress(Exception):
                await engine.aclose()
            return None
        conv = _Conversation(
            session=ConversationSession(policy=self._deps.policy),
            engine=engine,
            rules=rules,
            room=room,
        )
        self._convs[node_id] = conv
        log.info("[%s] s2s conversation opened (%s)", node_id, conv.session.session_id)
        return conv

    async def on_capture_start(self, node_id: str) -> None:
        """A new capture during a conversation: barge-in. Cancel the in-flight
        response (the engine stops in ~2 ms); the runner drains and the new
        turn queues on the conversation's lock."""
        conv = self._convs.get(node_id)
        if conv is not None and conv.lock.locked():
            conv.barged = True
            with contextlib.suppress(Exception):
                await conv.engine.cancel()

    def on_followup_timeout(self, node_id: str) -> bool:
        """The node's window expired with no speech — the silence end (release
        2), anchored node-side. True = this was ours; the classic follow-up
        machinery should not also act."""
        conv = self._convs.get(node_id)
        if conv is None:
            return False
        if conv.lock.locked():
            # A stale window expiry while a turn is still in flight (the reply
            # is coming) — swallow it; the real post-reply window arms fresh.
            return True
        conv.session.end("silence")
        asyncio.get_running_loop().create_task(
            self._close(node_id, "silence"), name=f"s2s-close-{node_id}"
        )
        return True

    def on_wake_elsewhere(self, node_id: str) -> None:
        """The wake word is the law — an external end (group claim, alarm)."""
        conv = self._convs.get(node_id)
        if conv is not None:
            conv.session.on_wake()

    async def close_all(self, reason: str) -> None:
        for node_id in list(self._convs):
            await self._close(node_id, reason)

    async def _close(self, node_id: str, reason: str) -> None:
        conv = self._convs.pop(node_id, None)
        if conv is None:
            return
        if not conv.session.ended:  # every path that names a real reason ended it already
            conv.session.end("error")
        self._deps.end_floor(node_id)
        with contextlib.suppress(Exception):
            await conv.engine.aclose()
        log.info(
            "[%s] s2s conversation closed (%s, %s)", node_id, conv.session.session_id, reason
        )


__all__ = ["BridgeDeps", "EngineLike", "S2SBridge"]
