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
    ToolAsk,
    TurnResult,
    TurnRunner,
    WindowPolicy,
    end_conversation_rule,
    is_explicit_close,
)
from kenzy.s2s.engine import EngineError, EngineEvent, InputTranscript, ToolCall
from kenzy.s2s.gate import AuditRecord, Speaker, ToolRule, TurnGate

log = logging.getLogger(__name__)

#: Peak int16 amplitude below which a capture is a phantom, not speech
#: (~-40 dBFS; a real utterance on any of the lab mics peaks far above it).
_SILENCE_PEAK = 500


def _idle_event() -> asyncio.Event:
    """An Event born SET — a fresh conversation's node is audibly idle."""
    evt = asyncio.Event()
    evt.set()
    return evt


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

    async def commit(self, *, respond: bool = True) -> None: ...

    async def cancel(self) -> None: ...

    async def respond(self) -> None: ...

    async def add_context(self, text: str) -> None: ...

    async def submit_tool_result(
        self, call_id: str, output: str, *, respond: bool = True
    ) -> None: ...

    async def aclose(self) -> None: ...


@dataclass
class BridgeDeps:
    """The server's contribution, as narrow callables (glue, not surface)."""

    #: Does a FRESH capture auto-open a conversation? True only in ``always``
    #: mode (``s2s.mode``); ``on_demand`` opens a conversation explicitly (a
    #: spoken "start a conversation"), so a fresh capture there stays classic.
    #: An already-open conversation keeps its turns regardless (see should_take).
    enabled: Callable[[], bool]
    #: The engine's ws url — explicit config or the service registry; "" = unknown.
    engine_url: Callable[[], str]
    #: The per-node hardware gate: follow-up needs full duplex (hardware_aec).
    node_capable: Callable[[str], bool]
    #: Connect and return a ready engine session for the url.
    engine_factory: Callable[[str], Awaitable[EngineLike]]
    #: (tier) -> (tool schemas for session.update, {name: min_tier} policy,
    #: {name: pace} — instant/working/deferred, the async tool contract).
    fetch_tools: Callable[
        [str], Awaitable[tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]]
    ]
    #: Speaker-ID one capture: (pcm, room) -> (Speaker, confidence). The glue
    #: also feeds occupancy — voice evidence is the server's concern, not ours.
    identify: Callable[[bytes, str], Awaitable[tuple[Speaker, float]]]
    #: Execute one gate-approved call: (call, node_id, room, speaker) -> result.
    execute_tool: Callable[[ToolCall, str, str, Speaker], Awaitable[str]]
    #: Reply audio to the node (the existing TTS frame path).
    deliver_start: Callable[[str], Awaitable[None]]
    deliver_frame: Callable[[str, bytes], Awaitable[bool]]
    deliver_end: Callable[[str], Awaitable[None]]
    #: Arm the node's post-reply capture window (expect_utterance). The second
    #: arg is a per-window length override in seconds (the on-demand sticky
    #: window) — None = the node's configured default.
    hold_floor: Callable[[str, float | None], Awaitable[bool]]
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
    #: Detach a tool call into the task executor: (call, node, room, speaker,
    #: in-flight work or None) -> the in-progress hand-off string the model
    #: hears. ``work`` given = the working->deferred promotion (the execution
    #: is ALREADY running and gets adopted); None = a deferred-class call the
    #: executor starts itself. Unset = no executor: deferred verdicts and
    #: promotions block inline, honestly.
    detach: (
        Callable[[ToolCall, str, str, Speaker, asyncio.Task[str] | None], Awaitable[str]]
        | None
    ) = None
    #: Undelivered task results for an owner, as speakable lines (marking them
    #: delivered): injected as context when the owner's next conversation
    #: resolves their identity — the pickup path for deliveries the proactive
    #: gate declined. Returns (task_id, speakable line) pairs; the bridge marks
    #: each delivered via ``pickup_delivered`` ONLY after its line successfully
    #: injects, so an engine hiccup mid-pickup leaves the rest deliverable
    #: instead of silently lost. Unset = no pickup.
    pickup: Callable[[str], list[tuple[str, str]]] | None = None
    #: Confirm one picked-up task was handed to the model (mark it delivered).
    pickup_delivered: Callable[[str], None] | None = None
    #: On-demand resume (6.0.x): stash a just-closed conversation's transcript,
    #: keyed by node, for ``resume_window_s``. (node, speaker, history).
    stash: Callable[[str, str, list[tuple[str, str]]], None] | None = None
    #: Return the warm conversation to resume for (node, speaker) as a context
    #: string, or None — identity-gated (only the same person resumes) and
    #: TTL'd. Consumes the warm slot.
    resume: Callable[[str, str], str | None] | None = None
    #: Does this node report playback completion (tts_done, >=5.1.3)? Gates
    #: the delivery turn's audible-idle wait — a node that never reports
    #: would otherwise deadlock the wait after the first cleared event.
    playback_signal: Callable[[str], bool] = lambda _n: False
    policy: WindowPolicy = field(default_factory=WindowPolicy)
    #: The sticky policy for on-demand conversations (a longer follow-up window,
    #: "start a conversation" earns it). Falls back to `policy` if unset.
    policy_on_demand: WindowPolicy | None = None
    #: The ask() continuation bridge (2026-09-03). ``synthesize`` speaks a
    #: parked skill's question VERBATIM (consent/enrollment wording is the
    #: skill's contract, never the model's paraphrase). ``continue_ask``
    #: delivers the user's answer llm-side: (continuation, answer text,
    #: node_id, room, answerer) -> the final result string, or a ToolAsk when
    #: the skill chains another question. ``cancel_ask`` (continuation,
    #: reason) cancels a parked skill — every conversation exit calls it, so
    #: nothing stays parked llm-side. Any of the three unset ⇒ asks are
    #: refused honestly (the model is told, and relays).
    synthesize: Callable[[str], Awaitable[bytes]] | None = None
    continue_ask: (
        Callable[[str, str, str, str, Speaker], Awaitable[str | ToolAsk]] | None
    ) = None
    cancel_ask: Callable[[str, str], Awaitable[None]] | None = None


@dataclass(frozen=True)
class _PendingAsk:
    """A skill's question is out in the room: the llm-side continuation that
    holds the parked coroutine, and the engine-side call_id held OPEN until
    the resumed skill's real result is submitted."""

    continuation: str
    call_id: str
    prompt: str


@dataclass
class _Conversation:
    session: ConversationSession
    engine: EngineLike
    rules: dict[str, ToolRule]
    room: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    barged: bool = False
    #: Last speaker name fed to the model as context — inject on CHANGE only,
    #: or every turn appends a duplicate item to the one history.
    injected_identity: str = ""
    #: Set when a barge lands — a working tool awaiting inline reads it as a
    #: downgrade request (promote to deferred, free the floor). Fresh per turn.
    barge_evt: asyncio.Event = field(default_factory=asyncio.Event)
    #: Set while the node is AUDIBLY idle; cleared when a reply's frames
    #: dispatch, set again by the node's tts_done (or a barge, which stops
    #: playback locally). The delivery turn anchors on this — audio sent
    #: while the previous reply still plays is DISCARDED by the node's
    #: player (trap #1), which read live as "she never came back with the
    #: results" (2026-08-29).
    playback_done: asyncio.Event = field(default_factory=_idle_event)
    #: Opened on demand ("start a conversation")? Gates the sticky window and
    #: the warm-session stash on close.
    on_demand: bool = False
    #: The node this conversation runs on (resume/stash key).
    node_id: str = ""
    #: Per-turn (user, assistant) transcript, for the 3-min resume stash.
    history: list[tuple[str, str]] = field(default_factory=list)
    #: One-shot: has a warm resume already been injected this conversation?
    resumed: bool = False
    #: A skill parked on ask(): its question was spoken and the floor armed —
    #: the NEXT capture is the answer (routed to /tool/continue), not a turn.
    pending_ask: _PendingAsk | None = None


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
        keeps its turns (this is how on-demand conversations route — they were
        OPENED by open_on_demand, no deferred-arm state exists); a fresh one
        needs auto-open (``always``) + capable node + a known engine. Everything
        else is the classic pipeline's."""
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

    async def open_on_demand(
        self,
        node_id: str,
        room: str,
        greeting_pcm: bytes = b"",
        greeting_text: str = "Okay, let's talk.",
    ) -> bool:
        """"Start a conversation": open the conversation NOW and speak the
        entry cue through the bridge's own delivery path — so entry rides the
        same hardened machinery as every later turn (no deferred-arm state;
        the option-2 redesign, 2026-09-01, after the arm+held-floor entry
        collided live with the classic cue ladder). Returns False when no
        conversation can open (engine down / half-duplex) — the caller owns
        saying why."""
        if self.active(node_id):
            return True  # already conversing — nothing to open
        if not (self._deps.node_capable(node_id) and self._deps.engine_url()):
            return False
        conv = await self._open(node_id, room, on_demand=True)
        if conv is None:
            return False
        async with conv.lock:
            armed = False
            started = False
            try:
                if greeting_pcm:
                    conv.playback_done.clear()
                    await self._deps.deliver_start(node_id)
                    started = True
                    for i in range(0, len(greeting_pcm), 9600):
                        if not await self._deps.deliver_frame(
                            node_id, greeting_pcm[i : i + 9600]
                        ):
                            break
                # Buffered order: expect_utterance BEFORE tts_end, so the node
                # treats the greeting as floor-holding and opens the sticky
                # window at playback-complete.
                armed = await self._deps.hold_floor(node_id, conv.session.followup_window_s)
            finally:
                if started:
                    await self._deps.deliver_end(node_id)
        if not armed:
            conv.session.end("error")
            await self._close(node_id, "entry window not armed")
            return False
        # The engine must know it greeted, or the model greets AGAIN on turn 1.
        with contextlib.suppress(Exception):
            await conv.engine.add_context(
                f"You just opened this conversation by saying: {greeting_text} "
                "Do not greet again — continue naturally from the user's next words."
            )
        conv.history.append(("", greeting_text))
        log.info("[%s] on-demand conversation opened by voice", node_id)
        return True

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
            async with conv.lock:
                # Reset the barge state INSIDE the lock: a previous turn that
                # was barged may still be draining its cancelled response
                # while holding the lock (its post-cancel audio tail, or its
                # up-to-10 s late-transcript wait). Clearing conv.barged
                # before acquiring the lock would wipe that turn's barge flag
                # mid-drain — un-suppressing its stale audio tail into the
                # room and letting it re-arm the follow-up window over THIS
                # capture (lived 2026-08-29; deliver_completion already does
                # its reset in-lock for exactly this reason).
                conv.barged = False
                conv.barge_evt = asyncio.Event()
                if conv.session.ended:
                    # Ended while we queued (a dying turn closed it) — loop
                    # once and open a FRESH conversation: with the toggle on,
                    # the wake NEVER falls back classic for a live engine.
                    continue
                if conv.pending_ask is not None:
                    # A skill's question is out — this capture is its ANSWER.
                    # Never replayed classic on failure: "yes please" as a
                    # fresh command would misfire; the question dies honestly.
                    try:
                        await self._run_answer_turn(node_id, conv, pcm)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 — fire-and-forget task
                        log.warning(
                            "[%s] s2s: answer turn failed (%s) — closing", node_id, exc
                        )
                        await self._close(node_id, "answer turn failed")
                    return
                try:
                    await self._run_turn(node_id, conv, session_id, pcm)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # A cached engine whose socket died between turns raises at
                    # the append/commit handoff — BEFORE a TurnRunner exists, so
                    # _run_turn's own error→classic path can't catch it, and
                    # this task is fire-and-forget (its exception would vanish
                    # unretrieved, leaving dead air and a stuck conversation).
                    # Close the dead conversation and give the utterance to the
                    # classic pipeline so it still gets answered.
                    log.warning(
                        "[%s] s2s: turn failed at engine handoff (%s) — closing + classic",
                        node_id, exc,
                    )
                    await self._close(node_id, "engine handoff failed")
                    await self._deps.classic(node_id, room, session_id, pcm)
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
            # Barge guard: after an interrupt cancels the response, the
            # engine's already-buffered audio tail still arrives (measured
            # live against the cloud engine: 17 deltas — SECONDS of speech —
            # land instantly post-cancel; the local engine's ≤1 tiny delta
            # just made the same bug invisible). The runner deliberately
            # consumes its response to the end so the socket stays clean for
            # the next turn — but nothing cancelled may reach the room.
            if conv.barged:
                return
            nonlocal started
            if not started:
                if self._deps.playback_signal(node_id):
                    conv.playback_done.clear()  # audibly busy until tts_done
                await self._deps.deliver_start(node_id)
                started = True
            await self._deps.deliver_frame(node_id, out)

        execute, detach = self._tool_closures(conv, node_id)

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
                detach=detach if self._deps.detach is not None else None,
                transcript_timeout_s=10.0,
            )
            result = await runner.run()
            # Arm the follow-up window BEFORE the stream closes: the node needs
            # expect_utterance to precede tts_end (the classic path's buffered
            # order) to treat this reply as floor-holding — that ordering is
            # what makes barge-in live DURING playback and the capture window
            # open at playback-complete. Armed after tts_end, it arms nothing.
            if result.status == "ok" and not conv.session.ended and not conv.barged:
                armed = await self._deps.hold_floor(
                    node_id, conv.session.followup_window_s if conv.on_demand else None
                )
            elif result.status == "ask" and result.ask is not None:
                # A tool parked on ask(): speak the skill's question and arm
                # the answer window (or refuse honestly). Same buffered-order
                # constraint — the prompt's frames and the arm ride before
                # this turn's tts_end.
                await self._carry_ask(
                    conv, node_id, result.ask, result.ask_call_id, result.stale_asks, deliver
                )
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
        # An ask turn's audible reply includes the skill's spoken question.
        response_text = result.reply_text
        if result.status == "ask" and conv.pending_ask is not None:
            response_text = f"{result.reply_text} {conv.pending_ask.prompt}".strip()
        # The household-visible trail (Activity tab) — same shape as the
        # classic pipeline's record; the sink applies the dashboard.logs gate.
        self._deps.activity(
            {
                "ts": time.time(),
                "node_id": node_id,
                "room": conv.room,
                "speaker": conv.session.identity.current.name or "unknown",
                "transcript": result.transcript,
                "response": response_text,
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
        if result.transcript and conv.on_demand:
            # Keep the running transcript for the 3-min resume stash (on-demand
            # only — always-mode conversations never rest between wakes).
            conv.history.append((result.transcript, response_text or ""))
        if conv.session.ended:
            await self._close(node_id, conv.session.end_reason or "verbal")
            return
        if result.status == "ask":
            # Carried (a question is pending), superseded by a barge, or
            # refused-and-spoken — _carry_ask settled every case; the
            # conversation stays open either way.
            return
        if result.status != "ok":
            # An engine fault mid-conversation: end it honestly; the NEXT wake
            # starts fresh (and falls back classic if the engine stays down).
            await self._close(node_id, f"engine {result.status}")
            # And the utterance itself must still get an answer — dead air was
            # the lived symptom of a cloud session rejection (2026-08-29).
            # Replay through the classic pipeline ONLY when the errored turn
            # provably did nothing: no action RESOLVED (execution happens at
            # resolve time, before any submit), no reply audio delivered — so
            # a replay can never double-actuate or double-speak.
            if result.status == "error" and result.actions_resolved == 0 and not started:
                log.warning("[%s] s2s: errored turn replayed on the classic pipeline", node_id)
                await self._deps.classic(node_id, conv.room, session_id, pcm)
            return
        if conv.barged:
            return  # a new capture already owns the floor — never re-arm over it
        if not armed:
            # The dialog machinery declined (turn cap) — the conversation ends.
            conv.session.end("hard_cap")
            await self._close(node_id, "turn cap")

    def _tool_closures(self, conv: _Conversation, node_id: str) -> tuple[Any, Any]:
        """The runner's execute/detach pair — shared by capture turns and
        delivery turns so the promotion ladder behaves identically in both."""

        async def execute(call: ToolCall) -> str:
            # The working-class discipline: run inline — the result IS the
            # reply — but never hold the floor hostage. Past the promotion
            # threshold, or the moment the user barges over the wait (a
            # downgrade request by ruling), the in-flight work is promoted
            # ONE rung: adopted into the task executor, and the model gets
            # the hand-off string instead.
            work: asyncio.Task[str] = asyncio.ensure_future(
                self._deps.execute_tool(
                    call, node_id, conv.room, conv.session.identity.current
                )
            )
            if self._deps.detach is None:
                return await work  # no executor wired: block honestly
            barge = asyncio.get_running_loop().create_task(conv.barge_evt.wait())
            try:
                done, _ = await asyncio.wait(
                    {work, barge},
                    timeout=self._deps.policy.working_promote_s,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                barge.cancel()
            if work in done:
                return work.result()  # a raised error stays the runner's to report
            return await self._deps.detach(
                call, node_id, conv.room, conv.session.identity.current, work
            )

        async def detach(call: ToolCall) -> str:
            # A deferred-class verdict: never inline — the executor starts it.
            assert self._deps.detach is not None  # runner only calls when wired
            return await self._deps.detach(
                call, node_id, conv.room, conv.session.identity.current, None
            )

        return execute, (detach if self._deps.detach is not None else None)

    # ------------------------------------------------- the ask() continuation

    async def _cancel_ask(self, continuation: str, reason: str) -> None:
        """Cancel a parked skill llm-side — best-effort (cancel is idempotent
        there, and a dead llm's backstop sweep collects strays anyway)."""
        if self._deps.cancel_ask is None:
            return
        with contextlib.suppress(Exception):
            await self._deps.cancel_ask(continuation, reason)

    async def _carry_ask(
        self,
        conv: _Conversation,
        node_id: str,
        ask: ToolAsk,
        call_id: str,
        stale: tuple[str, ...],
        deliver: Callable[[bytes], Awaitable[None]],
    ) -> bool:
        """Speak a parked skill's question VERBATIM and arm the answer window.

        True = the question is live (``conv.pending_ask`` set; the next
        capture is its answer). Every refusal path cancels the llm-side
        continuation AND answers the model's open call, so nothing dangles: a
        barge/close is answered quietly (the user moved on); a capability
        refusal is answered with respond=True and the follow-on spoken, so
        the model tells the user instead of dead air (the 5.0.4 say-why rule).
        """
        for extra in stale:
            await self._cancel_ask(extra, "superseded")
        quiet = ""
        if conv.barged:
            quiet = "canceled: the user interrupted before the question was asked"
        elif conv.session.ended:
            quiet = "canceled: the conversation ended before the question was asked"
        if quiet:
            await self._cancel_ask(ask.continuation, quiet)
            with contextlib.suppress(Exception):
                await conv.engine.submit_tool_result(call_id, quiet, respond=False)
            return False
        refusal = ""
        synthesize = self._deps.synthesize
        if ask.capture != "text":
            # ask_audio (enrollment) — stage 2; refused honestly until bridged.
            refusal = (
                "error: this skill needs a voice-recording exchange that "
                "conversations don't support yet — suggest trying it outside "
                "a conversation, or from the dashboard"
            )
        elif synthesize is None or self._deps.continue_ask is None:
            refusal = "error: this deployment cannot relay a skill's question mid-conversation"
        pcm = b""
        if not refusal and synthesize is not None:
            try:
                pcm = await synthesize(ask.prompt)
            except Exception as exc:  # noqa: BLE001 — the refusal below says why
                log.warning("[%s] s2s: ask prompt synthesis failed (%s)", node_id, exc)
            if not pcm:
                refusal = "error: the question could not be spoken (synthesis failed)"
        if not refusal:
            for i in range(0, len(pcm), 9600):
                await deliver(pcm[i : i + 9600])
            window = ask.timeout_s or conv.session.question_window_s
            if await self._deps.hold_floor(node_id, window):
                conv.pending_ask = _PendingAsk(ask.continuation, call_id, ask.prompt)
                log.info(
                    "[%s] s2s: skill question pending (%s, window %.0fs)",
                    node_id, ask.continuation, window,
                )
                return True
            refusal = "canceled: the user's answer window could not be opened"
        await self._cancel_ask(ask.continuation, refusal)
        with contextlib.suppress(Exception):
            await conv.engine.submit_tool_result(call_id, refusal, respond=True)
        try:
            followon = await self._run_followon(
                conv, node_id, deliver, f"[ask refused] {refusal}"
            )
            if followon.status == "ask" and followon.ask is not None:
                # One level only: a follow-on asking AGAIN right after a
                # refusal is refused quietly — never an unbounded ladder.
                await self._cancel_ask(followon.ask.continuation, "ask after refusal")
                with contextlib.suppress(Exception):
                    await conv.engine.submit_tool_result(
                        followon.ask_call_id,
                        "canceled: questions to the user are unavailable right now",
                        respond=False,
                    )
            elif followon.status == "ok" and not conv.session.ended and not conv.barged:
                await self._deps.hold_floor(
                    node_id, conv.session.followup_window_s if conv.on_demand else None
                )
        except Exception as exc:  # noqa: BLE001 — the refusal was already recorded
            log.warning("[%s] s2s: ask refusal follow-on failed (%s)", node_id, exc)
        return False

    async def _run_followon(
        self,
        conv: _Conversation,
        node_id: str,
        deliver: Callable[[bytes], Awaitable[None]],
        provenance: str,
    ) -> TurnResult:
        """Consume one response the ask path just requested (a resumed skill's
        result, or a refusal) — a full TurnRunner, so gated tools in the
        follow-on behave exactly like any turn's. The gate is pre-cleared:
        the user's answer (or the refusal) is the provenance, the delivery-
        turn pattern."""
        gate = TurnGate(
            f"{conv.session.session_id}-ask",
            identity=lambda: conv.session.identity.current,
            tool_rules=conv.rules,
            audit=self._deps.audit,
        )
        gate.on_transcript(provenance)
        execute, detach = self._tool_closures(conv, node_id)
        runner = TurnRunner(
            conv.engine,
            gate,
            conv.session,
            execute=execute,
            deliver=deliver,
            detach=detach,
            transcript_timeout_s=10.0,
        )
        return await runner.run()

    #: The answer wait's bound. STT starts at the commit; a short answer on a
    #: slow host measured ~10-13 s (vm1) — 20 gives headroom without wedging.
    _ANSWER_TRANSCRIPT_S = 20.0

    async def _answer_transcript(self, conv: _Conversation) -> str | None:
        """The engine's transcript of the just-committed answer (no response
        was requested, so the stream carries only the transcript plus any
        cancelled stragglers). None = engine error or timeout — the caller
        fails the question closed."""
        events = aiter(conv.engine.events())
        deadline = time.monotonic() + self._ANSWER_TRANSCRIPT_S
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                evt = await asyncio.wait_for(anext(events), remaining)
            except (StopAsyncIteration, TimeoutError):
                return None
            if isinstance(evt, InputTranscript):
                return evt.text
            if isinstance(evt, EngineError):
                log.warning("s2s: engine error awaiting an answer — %s", evt.message)
                return None
            # Anything else (a cancelled response's stragglers): drain.

    async def _run_answer_turn(self, node_id: str, conv: _Conversation, pcm: bytes) -> None:
        """The capture that answers a pending skill question.

        The engine transcribes it (one transcript method — the 6.0 gate rule)
        but NO response is requested: the answer belongs to the parked skill.
        It resumes via /tool/continue with the ANSWERER's identity, its real
        result closes the model's still-open call, and the follow-on response
        speaks with both the answer (now in session history) and the result
        in view. The deterministic close outranks any pending question.
        """
        pa = conv.pending_ask
        assert pa is not None
        conv.pending_ask = None
        turn_id = conv.session.begin_turn()
        started = False

        async def deliver(out: bytes) -> None:
            nonlocal started
            if conv.barged:
                return
            if not started:
                if self._deps.playback_signal(node_id):
                    conv.playback_done.clear()
                await self._deps.deliver_start(node_id)
                started = True
            await self._deps.deliver_frame(node_id, out)

        identify_task = asyncio.get_running_loop().create_task(
            self._identify(conv, pcm), name=f"s2s-id-{node_id}"
        )
        armed = False
        t0 = time.monotonic()
        result: TurnResult | None = None
        transcript: str | None = None
        try:
            await conv.engine.append(pcm)
            await conv.engine.commit(respond=False)
            await self._deps.listen_now(node_id)
            transcript = await self._answer_transcript(conv)
            # The answerer's identity rides the resume — wait briefly for it
            # (speaker-id usually beats STT, so this rarely actually waits).
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(identify_task), 5.0)
            if transcript is not None and is_explicit_close(transcript):
                conv.session.end("verbal")
                await self._cancel_ask(pa.continuation, "conversation closed")
            elif conv.barged:
                # A fresh capture arrived while we waited — it owns the floor.
                await self._cancel_ask(pa.continuation, "the user interrupted")
                with contextlib.suppress(Exception):
                    await conv.engine.submit_tool_result(
                        pa.call_id, "canceled: the user moved on", respond=False
                    )
            else:
                outcome: str | ToolAsk
                if transcript is None:
                    await self._cancel_ask(pa.continuation, "answer not transcribed")
                    outcome = (
                        "error: the user's answer could not be transcribed — "
                        "the question was abandoned; apologize briefly"
                    )
                elif self._deps.continue_ask is None:  # defensive: carry gates on it
                    await self._cancel_ask(pa.continuation, "no continue door")
                    outcome = "error: the answer could not reach the skill"
                else:
                    outcome = await self._deps.continue_ask(
                        pa.continuation,
                        transcript,
                        node_id,
                        conv.room,
                        conv.session.identity.current,
                    )
                if isinstance(outcome, ToolAsk):
                    # The skill chained another question — same call stays open.
                    await self._carry_ask(conv, node_id, outcome, pa.call_id, (), deliver)
                else:
                    await conv.engine.submit_tool_result(pa.call_id, outcome, respond=True)
                    result = await self._run_followon(
                        conv, node_id, deliver, transcript or "[unheard answer]"
                    )
                    if result.status == "ok" and not conv.session.ended and not conv.barged:
                        armed = await self._deps.hold_floor(
                            node_id,
                            conv.session.followup_window_s if conv.on_demand else None,
                        )
                    elif result.status == "ask" and result.ask is not None:
                        await self._carry_ask(
                            conv,
                            node_id,
                            result.ask,
                            result.ask_call_id,
                            result.stale_asks,
                            deliver,
                        )
        except Exception:
            # take_turn's wrapper closes the conversation on this raise — make
            # sure the parked skill dies with it, not at the backstop sweep.
            await self._cancel_ask(pa.continuation, "answer turn failed")
            raise
        finally:
            if not identify_task.done():
                identify_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await identify_task
            if started:
                await self._deps.deliver_end(node_id)

        reply_text = result.reply_text if result is not None else ""
        if conv.pending_ask is not None:
            reply_text = f"{reply_text} {conv.pending_ask.prompt}".strip()
        log.info(
            "[%s] s2s answer turn %s: %s",
            node_id,
            turn_id,
            "closed"
            if conv.session.ended
            else (
                "question pending"
                if conv.pending_ask is not None
                else (result.status if result is not None else "canceled")
            ),
        )
        self._deps.activity(
            {
                "ts": time.time(),
                "node_id": node_id,
                "room": conv.room,
                "speaker": conv.session.identity.current.name or "unknown",
                "transcript": transcript or "",
                "response": reply_text,
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
        if transcript and conv.on_demand:
            conv.history.append((transcript, reply_text or ""))
        if conv.session.ended:
            await self._close(node_id, conv.session.end_reason or "verbal")
            return
        if conv.pending_ask is not None or conv.barged or result is None:
            return  # chained question live / a new capture owns the floor / canceled
        if result.status == "ask":
            return  # settled by _carry_ask (carried, or refused-and-spoken)
        if result.status != "ok":
            await self._close(node_id, f"engine {result.status}")
            return
        if not armed:
            conv.session.end("hard_cap")
            await self._close(node_id, "turn cap")

    def on_tts_done(self, node_id: str) -> None:
        """The node reports the reply finished PLAYING — the delivery turn's
        audible-idle anchor."""
        conv = self._convs.get(node_id)
        if conv is not None:
            conv.playback_done.set()

    async def stage_completion(self, node_id: str, text: str) -> bool:
        """A LATE completion in a live conversation: no unprompted turn — the
        result is staged as context so the model mentions it alongside its
        NEXT reply ("I've also got those results…"). The gentle in-
        conversation channel, per the announce-window ruling."""
        conv = self._convs.get(node_id)
        if conv is None or conv.session.ended:
            return False
        try:
            await conv.engine.add_context(
                "A background task finished. Do NOT interrupt or start a reply "
                f"for this now — mention it briefly alongside your next reply: {text}"
            )
            return True
        except Exception as exc:  # noqa: BLE001 — staging failed: leave deliverable
            log.debug("s2s: completion staging failed (%s)", exc)
            return False

    async def deliver_completion(self, node_id: str, text: str) -> bool:
        """A finished background task speaks into the LIVE conversation — the
        DELIVERY TURN: no capture, no user utterance. The completion is the
        turn's provenance (audited as its transcript), the model phrases it
        in context, and the reply delivers and re-arms the follow-up window
        like any other. Returns False when there is no live conversation to
        speak into — the caller falls back to the proactive/pickup paths.
        """
        conv = self._convs.get(node_id)
        if conv is None or conv.session.ended:
            return False
        async with conv.lock:
            if conv.session.ended or conv.session.poll() is not None:
                return False
            # The audible-idle anchor: never start speaking a completion while
            # the previous reply is still PLAYING at the node — its player
            # discards audio queued mid-playback (trap #1; lived 2026-08-29
            # as a delivery that logged ok and was never heard). Bounded so a
            # lost tts_done can only delay, never wedge, the delivery.
            if not conv.playback_done.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(conv.playback_done.wait(), 45.0)
            if conv.session.ended:
                return False
            if conv.barged:
                return False  # the user is speaking — the result waits its turn
            conv.barged = False
            conv.barge_evt = asyncio.Event()
            turn_id = conv.session.begin_turn()
            gate = TurnGate(
                turn_id,
                identity=lambda: conv.session.identity.current,
                tool_rules=conv.rules,
                audit=self._deps.audit,
            )
            # Pre-clear the gate: a delivery turn HAS no user utterance — the
            # completion itself is the provenance, on the audit record as such.
            gate.on_transcript(f"[task completion] {text}")
            started = False

            async def deliver(out: bytes) -> None:
                nonlocal started
                if conv.barged:
                    return
                if not started:
                    if self._deps.playback_signal(node_id):
                        conv.playback_done.clear()
                    await self._deps.deliver_start(node_id)
                    started = True
                await self._deps.deliver_frame(node_id, out)

            execute, detach = self._tool_closures(conv, node_id)
            armed = False
            try:
                # The wording must OVERRIDE the hand-off still sitting in
                # history ("not done yet") — live 2026-08-30, the model
                # repeated the hand-off instead of the result until asked.
                await conv.engine.add_context(
                    "A background task update — this SUPERSEDES the earlier "
                    f"'started' status. Relay it to the user now: {text}"
                )
                await conv.engine.respond()
                runner = TurnRunner(
                    conv.engine,
                    gate,
                    conv.session,
                    execute=execute,
                    deliver=deliver,
                    detach=detach,
                    transcript_timeout_s=10.0,
                )
                result = await runner.run()
                if result.status == "ok" and not conv.session.ended and not conv.barged:
                    armed = await self._deps.hold_floor(
                        node_id,
                        conv.session.followup_window_s if conv.on_demand else None,
                    )
            finally:
                if started:
                    await self._deps.deliver_end(node_id)
            log.info(
                "[%s] s2s delivery turn %s: %s", node_id, turn_id, result.status
            )
            self._deps.activity(
                {
                    "ts": time.time(),
                    "node_id": node_id,
                    "room": conv.room,
                    "speaker": "kenzy (task)",
                    "transcript": f"[task completion] {text}",
                    "response": result.reply_text,
                    "mode": "follow-up",
                }
            )
            if conv.session.ended:
                await self._close(node_id, conv.session.end_reason or "verbal")
                return result.status == "ok" and started and not conv.barged
            if result.status != "ok":
                await self._close(node_id, f"engine {result.status}")
                return False
            if not armed and not conv.barged:
                conv.session.end("hard_cap")
                await self._close(node_id, "turn cap")
            # Delivered means HEARD, not merely attempted: a barge (real — or
            # a phantom onset, lived 2026-08-29: rms=3 cancelled the reply
            # 0.8 s in) suppressed the audio, and a turn that never started
            # speaking said nothing. Report False so the result STAYS
            # deliverable (pickup / next attempt) instead of being marked
            # delivered and lost.
            return started and not conv.barged

    async def _identify(self, conv: _Conversation, pcm: bytes) -> None:
        try:
            speaker, confidence = await self._deps.identify(pcm, conv.room)
        except Exception as exc:  # noqa: BLE001 — identity stays fail-closed unknown
            log.warning("s2s: speaker-id failed (%s) — identity stays unknown", exc)
            return
        if speaker.name and speaker.tier != "unknown":
            conv.session.identity.hear(speaker.name, speaker.tier, confidence)
            # OQ3 slice 1 (ruled 2026-08-29): feed the ANSWER to the model as
            # session context — so she can address people and shape actions per
            # speaker. Context only, never authorization (the gate re-reads
            # identity at action time regardless). Injected on change, not per
            # turn; the first turn's generation may already be in flight when
            # this lands, in which case the model reads it from the next
            # response on — "usually before an action", per the ruling.
            if speaker.name != conv.injected_identity:
                conv.injected_identity = speaker.name
                try:
                    await conv.engine.add_context(
                        f"The current speaker is {speaker.name} "
                        f"(voice-recognized, tier: {speaker.tier})."
                    )
                except Exception as exc:  # noqa: BLE001 — context is best-effort
                    log.debug("s2s: identity context not injected (%s)", exc)
                # The pickup path: results the proactive gate declined to
                # announce wait for exactly this moment — the owner is here and
                # talking. Mark each delivered ONLY after its line injects, so
                # an engine hiccup mid-pickup leaves the remaining results
                # deliverable rather than silently marked-and-lost. Separate
                # try from identity so one failure doesn't skip the other.
                if self._deps.pickup is not None:
                    for task_id, line in self._deps.pickup(speaker.name):
                        try:
                            await conv.engine.add_context(
                                f"While away, a background task finished — tell "
                                f"the user when natural: {line}"
                            )
                        except Exception as exc:  # noqa: BLE001
                            log.debug("s2s: pickup line not injected (%s) — stays pending", exc)
                            break
                        if self._deps.pickup_delivered is not None:
                            self._deps.pickup_delivered(task_id)
                # On-demand resume: if this same person stepped away and came
                # back within the window, reattach the prior transcript as
                # context. Identity-gated in the server (a different/unknown
                # speaker gets nothing); one-shot per conversation.
                if conv.on_demand and not conv.resumed and self._deps.resume is not None:
                    conv.resumed = True
                    resume_line = self._deps.resume(conv.node_id, speaker.name)
                    if resume_line:
                        log.info(
                            "[%s] resuming %s's recent conversation (%d chars of context)",
                            conv.node_id, speaker.name, len(resume_line),
                        )
                        try:
                            await conv.engine.add_context(resume_line)
                        except Exception as exc:  # noqa: BLE001 — resume is best-effort
                            log.debug("s2s: resume context not injected (%s)", exc)
        else:
            conv.session.identity.hear_stranger()

    # ------------------------------------------------------------- lifecycle

    async def _open(
        self, node_id: str, room: str, *, on_demand: bool = False
    ) -> _Conversation | None:
        url = self._deps.engine_url()
        if not url:
            return None
        try:
            engine = await self._deps.engine_factory(url)
        except Exception as exc:  # noqa: BLE001 — engine down ⇒ classic fallback
            log.warning("s2s: engine unreachable at %s (%s) — classic fallback", url, exc)
            return None
        try:
            tools, policy, pace = await self._deps.fetch_tools("recognized")
            rules = {
                name: ToolRule(min_tier=tier, detach=pace.get(name) == "deferred")
                for name, tier in policy.items()
            }
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
        # On-demand opens get the sticky window; auto-open (`always`) keeps
        # the default.
        window_policy = (
            self._deps.policy_on_demand
            if on_demand and self._deps.policy_on_demand is not None
            else self._deps.policy
        )
        conv = _Conversation(
            session=ConversationSession(policy=window_policy),
            engine=engine,
            rules=rules,
            room=room,
            on_demand=on_demand,
            node_id=node_id,
        )
        self._convs[node_id] = conv
        # Name the ENGINE, not just the session: which endpoint answered is
        # exactly the question when a profile and a co-running local engine
        # disagree (lived 2026-08-29 — the registry hijack read as "cloud not
        # working" instead of "wrong engine answered").
        log.info("[%s] s2s conversation opened (%s) — engine %s",
                 node_id, conv.session.session_id, url)
        return conv

    async def on_capture_start(self, node_id: str) -> None:
        """A new capture during a conversation: barge-in. Cancel the in-flight
        response (the engine stops in ~2 ms); the runner drains and the new
        turn queues on the conversation's lock."""
        conv = self._convs.get(node_id)
        if conv is not None and conv.lock.locked():
            conv.barged = True
            conv.barge_evt.set()
            conv.playback_done.set()  # the node stopped its player locally
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

    async def close(self, node_id: str, reason: str) -> None:
        """End and tear down one node's conversation (the group-claim path:
        a sibling's wake is the law for the whole group)."""
        conv = self._convs.get(node_id)
        if conv is not None:
            conv.session.on_wake()
        await self._close(node_id, reason)

    async def close_all(self, reason: str) -> None:
        for node_id in list(self._convs):
            await self._close(node_id, reason)

    async def _close(self, node_id: str, reason: str) -> None:
        conv = self._convs.pop(node_id, None)
        if conv is None:
            return
        if conv.pending_ask is not None:
            # Every conversation exit kills the parked skill with it — the
            # classic law (the wake word always cancels), and no leaked
            # coroutine idling llm-side until the backstop sweep.
            pa, conv.pending_ask = conv.pending_ask, None
            await self._cancel_ask(pa.continuation, f"conversation closed ({reason})")
        # Stash the transcript for a short identity-gated resume ("continue" /
        # "start a conversation" again within the window). On-demand only, and
        # only when we know WHO to gate the resume to.
        speaker = conv.session.identity.current.name
        if conv.on_demand and conv.history and speaker and self._deps.stash is not None:
            self._deps.stash(node_id, speaker, list(conv.history))
        if not conv.session.ended:  # every path that names a real reason ended it already
            conv.session.end("error")
        self._deps.end_floor(node_id)
        with contextlib.suppress(Exception):
            await conv.engine.aclose()
        log.info(
            "[%s] s2s conversation closed (%s, %s)", node_id, conv.session.session_id, reason
        )


__all__ = ["BridgeDeps", "EngineLike", "S2SBridge"]
