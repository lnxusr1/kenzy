"""The conversation layer — a conversation is a bounded object, not an ambient state.

Spec: kenzy-design/app/s2s-design.md, "Ending a conversation" + the follow-up
feature entry (2026-08-26). The wake word opens the floor; activity holds it;
one of five releases closes it:

1. **verbal** — the model's judgment, exercised through the gated
   :data:`END_CONVERSATION_TOOL` (required kit: the model can only exercise
   the judgment if the tool ships). No deterministic phrase set.
2. **silence** — the follow-up window (~8 s, question-aware) expires with no
   onset. Anchored at the NODE's playback-complete, never server TTS-stream
   end (the 5.0.1 lesson: a timer is only as good as the instant it counts
   from) — which is why the anchor is an event the caller reports, not a
   timestamp this layer guesses.
3. **walk_away** — room presence dropped (enhancement-where-present).
4. **hard_cap** — max conversation duration; cost protection on cloud,
   stuck-open protection everywhere.
5. **wake** — already the law: cancels in-flight, opens fresh.

The mic is hot only inside capture + reply playback + follow-up windows — the
union of windows, never an unbounded stream. That posture is the trust story,
so this layer owns the windows and nothing else opens one.

:class:`TurnRunner` drives one turn's engine events through the gate. It
encodes the measured sequencing rule (seam divergence 4): a gated tool's
result is HELD until the calling response's ``response.done`` — submitting
inside the response window wedges the HF server — and the turn completes only
when a response has finished, the transcript is on record, and nothing is
pending. The late-transcript race (measured on the cloud engine: transcript
653 ms after ``response.done``) is therefore handled, bounded by a timeout
that fails the turn closed (the gate held every action; nothing ran).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import uuid4

from kenzy.s2s.engine import (
    AudioDelta,
    EngineError,
    EngineEvent,
    InputTranscript,
    ReplyTranscriptDelta,
    ResponseDone,
    ToolCall,
)
from kenzy.s2s.gate import ToolRule, ToolVerdict, TurnGate
from kenzy.s2s.identity import SessionIdentity

EndReason = Literal["verbal", "silence", "walk_away", "hard_cap", "wake", "error"]

#: The tool that makes verbal closure the model's judgment (decision 9): "end
#: session" / "end conversation" / "that's all" are utterances the model maps
#: here. Gated like any tool — but ending is always safe, so its rule admits
#: any tier (the fail-closed direction is to END, not to keep the mic hot).
END_CONVERSATION = "end_conversation"

log = logging.getLogger(__name__)

END_CONVERSATION_TOOL: dict[str, Any] = {
    "type": "function",
    "name": END_CONVERSATION,
    "description": (
        "End the current conversation. Call this ONLY when the user explicitly "
        "signals they are finished — e.g. 'end session', 'end conversation', 'end "
        "chat', 'that's all', 'never mind', 'goodbye', 'thanks, we're done'. "
        "Completing a command or answering a question is NOT the end: after a "
        "completed action the conversation stays open and the user may follow up. "
        "After the result returns you may speak one short farewell."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "farewell": {
                "type": "string",
                "description": "Optional short goodbye to speak after ending.",
            }
        },
        "required": [],
    },
}


def end_conversation_rule() -> ToolRule:
    """The household policy for the end tool: any tier may end a conversation."""
    return ToolRule(min_tier="unknown")


@dataclass(frozen=True)
class WindowPolicy:
    """The conversation's timing knobs (config-backed at wiring time)."""

    #: Follow-up window after a reply finishes playing (founder: ~8 s).
    followup_s: float = 8.0
    #: A reply that asks a question arms a longer window (the expect_response
    #: distinction, kept).
    question_followup_s: float = 15.0
    #: Max conversation duration — release 4.
    hard_cap_s: float = 900.0
    #: Local engines may hold the session warm after the end for cheap re-wake;
    #: cloud closes immediately. Config, not architecture.
    linger_s: float = 30.0
    #: The working->deferred promotion threshold (the async tool contract):
    #: an inline tool still running after this long is adopted into the task
    #: executor and the turn gets its hand-off string — one rung, never two.
    working_promote_s: float = 20.0


class ConversationSession:
    """One conversation's lifecycle: turns, windows, and the five ends."""

    def __init__(
        self,
        *,
        policy: WindowPolicy | None = None,
        session_id: str = "",
        clock: Callable[[], float] | None = None,
        identity: SessionIdentity | None = None,
    ) -> None:
        self._policy = policy or WindowPolicy()
        self._clock: Callable[[], float] = clock or time.monotonic
        self.session_id = session_id or uuid4().hex[:12]
        self.identity = identity or SessionIdentity(clock=self._clock)
        self._started = self._clock()
        self._turns = 0
        self._state: str = "active"
        self._window_deadline: float | None = None
        self._end_reason: EndReason | None = None

    # ----------------------------------------------------------------- state

    @property
    def state(self) -> str:
        """``active`` (turn in progress) | ``followup`` (window open) | ``ended``."""
        return self._state

    @property
    def ended(self) -> bool:
        return self._state == "ended"

    @property
    def end_reason(self) -> EndReason | None:
        return self._end_reason

    @property
    def cap_deadline(self) -> float:
        return self._started + self._policy.hard_cap_s

    @property
    def followup_deadline(self) -> float | None:
        """The open window's expiry, for the runner's scheduling — or None."""
        return self._window_deadline if self._state == "followup" else None

    # ---------------------------------------------------------------- events

    def begin_turn(self) -> str:
        """Speech onset: a turn starts, any open window is consumed."""
        if self.ended:
            raise RuntimeError(f"session {self.session_id} has ended ({self._end_reason})")
        self._turns += 1
        self._state = "active"
        self._window_deadline = None
        return f"{self.session_id}-t{self._turns}"

    def on_playback_complete(self, *, expects_response: bool = False) -> float | None:
        """The NODE finished playing the reply — the window's anchor instant.

        Returns the armed deadline, or None if the session already ended
        (a late playback event after a wake-end must not re-open a window).
        """
        if self.ended:
            return None
        window = (
            self._policy.question_followup_s if expects_response else self._policy.followup_s
        )
        self._window_deadline = self._clock() + window
        self._state = "followup"
        return self._window_deadline

    def poll(self) -> EndReason | None:
        """Timer check: returns the end reason if this poll ended the session."""
        if self.ended:
            return None
        now = self._clock()
        if now >= self.cap_deadline:
            self.end("hard_cap")
            return "hard_cap"
        if self._window_deadline is not None and now >= self._window_deadline:
            self.end("silence")
            return "silence"
        return None

    def end(self, reason: EndReason) -> bool:
        """Close the conversation. Idempotent — the first end wins."""
        if self.ended:
            return False
        self._state = "ended"
        self._end_reason = reason
        self._window_deadline = None
        return True

    def on_wake(self) -> None:
        """The wake word is the law: cancels this session (a fresh one opens)."""
        self.end("wake")

    def on_presence_lost(self) -> None:
        """Room presence dropped — the quiet end (enhancement-where-present)."""
        self.end("walk_away")


# ---------------------------------------------------------------------- turns


class _EngineLike(Protocol):
    """What the runner needs from an engine — satisfied by EngineClient."""

    def events(self) -> AsyncIterator[EngineEvent]: ...

    async def submit_tool_result(
        self, call_id: str, output: str, *, respond: bool = True
    ) -> None: ...

    async def cancel(self) -> None: ...


@dataclass(frozen=True)
class TurnResult:
    """One turn's outcome. ``no_transcript`` means the turn failed CLOSED —
    the gate held every action and nothing executed."""

    status: str  # "ok" | "no_transcript" | "error" | "closed"
    reply_text: str
    results_submitted: int
    session_ended: bool
    transcript: str = ""  # what the user said (the Activity record's spine)
    #: Allowed verdicts actually RESOLVED (executor invoked, or the end tool).
    #: Distinct from results_submitted — execution happens at resolve time,
    #: submission only after response.done, so an errored turn can have acted
    #: without submitting. The bridge's replay-classic guard reads THIS: a
    #: replay is only safe when the turn provably did nothing.
    actions_resolved: int = 0


class TurnRunner:
    """Drive one turn: engine events -> gate -> execution -> sequenced results.

    Dependencies are injected, gate-style: ``execute`` runs an allowed in-turn
    tool; ``detach`` (optional) hands an allowed detach-verdict tool to the
    ledger and returns the audible hand-off ("I'll work on that…"); ``deliver``
    receives cleared reply audio. The runner performs no policy of its own —
    verdicts are the gate's, closure is the model's via the end tool.
    """

    def __init__(
        self,
        engine: _EngineLike,
        gate: TurnGate,
        session: ConversationSession,
        *,
        execute: Callable[[ToolCall], Awaitable[str]],
        deliver: Callable[[bytes], Awaitable[None]],
        detach: Callable[[ToolCall], Awaitable[str]] | None = None,
        transcript_timeout_s: float = 5.0,
    ) -> None:
        self._engine = engine
        self._gate = gate
        self._session = session
        self._execute = execute
        self._deliver = deliver
        self._detach = detach
        self._transcript_timeout_s = transcript_timeout_s
        self._transcript = ""
        self._resolved = 0

    async def run(self) -> TurnResult:
        events = aiter(self._engine.events())
        pending: list[tuple[str, str, bool]] = []  # (call_id, output, is_detach_handoff)
        reply: list[str] = []
        submitted = 0
        self._resolved = 0
        awaiting_late_transcript = False
        self._transcript = ""

        while True:
            try:
                if awaiting_late_transcript:
                    evt = await asyncio.wait_for(anext(events), self._transcript_timeout_s)
                else:
                    evt = await anext(events)
            except StopAsyncIteration:
                return self._result("closed", reply, submitted)
            except TimeoutError:
                # The measured cloud race, unresolved: response finished but the
                # transcript never arrived. Fail closed — held actions never ran.
                return self._result("no_transcript", reply, submitted)

            if isinstance(evt, InputTranscript):
                self._transcript = evt.text
                verdicts = self._gate.on_transcript(evt.text)
                held = self._gate.drain_audio()
                if held:
                    await self._deliver(held)
                for verdict in verdicts:
                    pending.append(await self._resolve(verdict))
                if awaiting_late_transcript:
                    submitted += await self._flush(pending)
                    awaiting_late_transcript = False
            elif isinstance(evt, ToolCall):
                rendered = self._gate.on_tool_call(evt)  # None = held pre-transcript
                if rendered is not None:
                    pending.append(await self._resolve(rendered))
            elif isinstance(evt, AudioDelta):
                cleared = self._gate.on_audio(evt.pcm)
                if cleared:
                    await self._deliver(cleared)
            elif isinstance(evt, ReplyTranscriptDelta):
                reply.append(evt.text)
            elif isinstance(evt, ResponseDone):
                if pending:
                    # Divergence 4: results submit only after response.done —
                    # the submit opens a follow-on response and the turn
                    # continues. Every result (a detach hand-off included) gets
                    # the follow-on so it's spoken about — a detached tool must
                    # never start silently (founder ruling 2026-08-29). The
                    # occasional "already acknowledged" double-say is prevented
                    # by the hand-off wording ("if you already said it's
                    # started, add nothing"), not by suppressing the response.
                    submitted += await self._flush(pending)
                elif self._gate.cleared:
                    return self._result("ok", reply, submitted)
                else:
                    awaiting_late_transcript = True
            elif isinstance(evt, EngineError):
                # SAY WHY (the 5.0.4 rule): this status sends the capture to
                # the classic fallback — without the engine's own message, a
                # misconfigured cloud session reads as "streaming just doesn't
                # work" (lived 2026-08-29: a tool-schema rejection was
                # invisible for exactly this reason).
                log.warning("s2s: engine error ends the turn — %s", evt.message)
                return self._result("error", reply, submitted)
            # SessionReady / ResponseStarted: nothing for the runner to do.

    # -------------------------------------------------------------- internals

    async def _resolve(self, verdict: ToolVerdict) -> tuple[str, str, bool]:
        """Turn a gate verdict into the result string the model will hear.

        Denials are reported honestly (the model must not claim success);
        executor exceptions become results too — honest failure is a delivery.
        The third element marks a DETACH hand-off (status, not information).
        """
        call = verdict.call
        if not verdict.allowed:
            return (call.call_id, f"denied: {verdict.reason}", False)
        self._resolved += 1
        if call.name == END_CONVERSATION:
            self._session.end("verbal")
            return (call.call_id, "conversation ended — you may speak one short farewell", False)
        detached = verdict.detach and self._detach is not None
        runner = self._detach if detached else self._execute
        assert runner is not None
        try:
            return (call.call_id, await runner(call), detached)
        except Exception as exc:  # noqa: BLE001 — the model is told, never lied to
            return (call.call_id, f"error: {exc}", False)

    async def _flush(self, pending: list[tuple[str, str, bool]]) -> int:
        """Submit held results and request one follow-on response to speak
        about them. The follow-on is always requested (a detached tool must
        not start silently); the response.create rides only the LAST result
        so N results open exactly one response, not N."""
        count = len(pending)
        if not count:
            return 0
        for i, (call_id, output, _is_detach) in enumerate(pending):
            await self._engine.submit_tool_result(call_id, output, respond=i == count - 1)
        pending.clear()
        return count

    def _result(self, status: str, reply: list[str], submitted: int) -> TurnResult:
        return TurnResult(
            status=status,
            reply_text="".join(reply),
            results_submitted=submitted,
            session_ended=self._session.ended,
            transcript=self._transcript,
            actions_resolved=self._resolved,
        )


__all__ = [
    "END_CONVERSATION",
    "END_CONVERSATION_TOOL",
    "ConversationSession",
    "EndReason",
    "TurnResult",
    "TurnRunner",
    "WindowPolicy",
    "end_conversation_rule",
]
