"""The authority gate — where the model's judgments meet the household's facts.

Spec: kenzy-design/app/s2s-design.md, "The gate, precisely" + decision 9. The
model owns every JUDGMENT (intent, phrasing, tool choice, closure); this gate
owns the FACTS and RIGHTS the model was never given: whose voice is speaking,
what that person may do, what leaves the house, what goes on the record. There
is deliberately NO interceptor and NO transcription here — the fast path is
retired from the v6 path (the classic pipeline keeps its own), and transcripts
arrive from wherever the engine profile sources them.

Five responsibilities, all mechanical:

1. Ordering — no tool verdict is rendered and no held audio is released until
   the turn's transcript is on record. Free-by-construction on the cascade
   engine; ENFORCED here so a future audio-native engine can't race it.
2. Secret screen — an injected matcher (the lockbox's, at assembly time) runs
   on the transcript; a diversion fails the turn's actions closed.
3. Identity — the current speaker is read AT ACTION TIME (decision 6: identity
   refines segment-wise and monotonic; the verdict uses what is known when the
   action asks, never a stale snapshot).
4. Tool verdicts — allow / deny / detach-to-ledger, fail-closed: an unknown
   tool is denied, an unknown speaker meets ``min_tier`` gates, every verdict
   is audited WITH the transcript that authorized it.
5. Delivery release — reply audio is passed through (cascade engines: already
   cleared) or held-and-drained (speculating engines), per the profile.

The gate performs no execution: it renders verdicts and records. The
conversation layer executes allowed tools, detaches ledger tasks, and speaks.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from kenzy.s2s.engine import ToolCall

# Identity tiers, ordered. Deliberately duplicated per package (the same
# convention as kenzy.llm.skills): the gate is a wire-contract consumer and
# must fail closed with no cross-service import.
_TIER_ORDER: dict[str, int] = {"unknown": 0, "recognized": 1, "verified": 2}


def _tier_rank(tier: str) -> int:
    return _TIER_ORDER.get(tier, -1)  # unknown strings rank below everything


@dataclass(frozen=True)
class Speaker:
    """The identity fact at a moment in time — provided by the identity layer,
    never inferred here."""

    name: str
    tier: str


@dataclass(frozen=True)
class ToolRule:
    """The household's policy for one tool."""

    min_tier: str = "unknown"
    #: Layer C: execute as a detached ledger task instead of in-turn. Founder
    #: direction: async may become the default — this is that dial, per tool.
    detach: bool = False


@dataclass(frozen=True)
class ToolVerdict:
    """The gate's answer for one tool call. ``allowed`` is the authority
    decision; ``detach`` says HOW an allowed call runs (in-turn vs ledger)."""

    call: ToolCall
    allowed: bool
    detach: bool
    reason: str


@dataclass(frozen=True)
class SecretDiversion:
    """The screen matched an explicit secret in the transcript. The turn's
    actions fail closed; the conversation layer runs the lockbox flow."""

    detail: str


@dataclass(frozen=True)
class AuditRecord:
    """One line of the turn's conduct record. Every verdict carries the
    transcript that authorized it — inspectability is a household right."""

    turn_id: str
    event: str
    detail: str
    transcript: str
    speaker: str
    tier: str


class TurnGate:
    """One turn's authority checkpoint.

    Wire-in points are injected so the gate has no dependencies of its own:
    ``identity`` returns the CURRENT speaker (segment-wise identity upstream),
    ``secret_screen`` is the lockbox's explicit matcher (or None), ``audit`` is
    the sink for the conduct record, ``tool_rules`` is the household policy.
    """

    def __init__(
        self,
        turn_id: str,
        *,
        identity: Callable[[], Speaker],
        tool_rules: Mapping[str, ToolRule],
        audit: Callable[[AuditRecord], None],
        secret_screen: Callable[[str], str | None] | None = None,
        hold_audio: bool = False,
    ) -> None:
        self._turn_id = turn_id
        self._identity = identity
        self._tool_rules = tool_rules
        self._audit = audit
        self._secret_screen = secret_screen
        self._hold_audio = hold_audio
        self._transcript: str | None = None
        self._diversion: SecretDiversion | None = None
        self._held_calls: list[ToolCall] = []
        self._held_audio: list[bytes] = []

    # ------------------------------------------------------------------ state

    @property
    def cleared(self) -> bool:
        """True once the transcript is on record (responsibility 1)."""
        return self._transcript is not None

    @property
    def diversion(self) -> SecretDiversion | None:
        return self._diversion

    # ----------------------------------------------------------------- events

    def on_transcript(self, text: str) -> list[ToolVerdict]:
        """The turn's transcript is on record: screen it, then render verdicts
        for any tool calls that arrived early (the measured audio-native race).
        """
        self._transcript = text
        self._record("transcript", text)
        if self._secret_screen is not None:
            detail = self._secret_screen(text)
            if detail is not None:
                self._diversion = SecretDiversion(detail)
                self._record("secret_diverted", detail)
        held, self._held_calls = self._held_calls, []
        return [self._verdict(call) for call in held]

    def on_tool_call(self, call: ToolCall) -> ToolVerdict | None:
        """A tool call from the engine. ``None`` = held (no transcript yet —
        the ordering invariant); otherwise the verdict, rendered now."""
        if not self.cleared:
            self._held_calls.append(call)
            self._record("tool_held", call.name)
            return None
        return self._verdict(call)

    def on_audio(self, pcm: bytes) -> bytes:
        """Reply audio from the engine. Pass-through unless this profile holds
        delivery until clearance (responsibility 5); held audio drains via
        :meth:`drain_audio` after the transcript lands."""
        if self._hold_audio and not self.cleared:
            self._held_audio.append(pcm)
            return b""
        return pcm

    def drain_audio(self) -> bytes:
        """Release audio held before clearance (empty for cascade engines)."""
        held, self._held_audio = self._held_audio, []
        return b"".join(held)

    # --------------------------------------------------------------- verdicts

    def _verdict(self, call: ToolCall) -> ToolVerdict:
        speaker = self._identity()  # decision 6: read at ACTION time
        if self._diversion is not None:
            verdict = ToolVerdict(call, False, False, "secret diversion — actions fail closed")
        else:
            rule = self._tool_rules.get(call.name)
            if rule is None:
                verdict = ToolVerdict(call, False, False, f"unknown tool {call.name!r}")
            elif _tier_rank(speaker.tier) < _tier_rank(rule.min_tier):
                verdict = ToolVerdict(
                    call, False, False, f"tier {speaker.tier!r} below {rule.min_tier!r}"
                )
            else:
                verdict = ToolVerdict(call, True, rule.detach, "allowed")
        self._record(
            "tool_allowed" if verdict.allowed else "tool_denied",
            f"{call.name}: {verdict.reason}" + (" (detach)" if verdict.detach else ""),
            speaker,
        )
        return verdict

    # ------------------------------------------------------------------ audit

    def _record(self, event: str, detail: str, speaker: Speaker | None = None) -> None:
        who = speaker or self._identity()
        self._audit(
            AuditRecord(
                turn_id=self._turn_id,
                event=event,
                detail=detail,
                transcript=self._transcript or "",
                speaker=who.name,
                tier=who.tier,
            )
        )


# Re-exported for wiring convenience at assembly time.
__all__ = [
    "AuditRecord",
    "SecretDiversion",
    "Speaker",
    "ToolRule",
    "ToolVerdict",
    "TurnGate",
]
