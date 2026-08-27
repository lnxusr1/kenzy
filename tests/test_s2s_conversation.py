"""Conversation-layer tests — the five ends, the window anchor, and the turn
runner's measured sequencing rules (spec: kenzy-design/app/s2s-design.md,
"Ending a conversation" + the follow-up feature entry + seam divergence 4)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from kenzy.s2s.conversation import (
    END_CONVERSATION,
    END_CONVERSATION_TOOL,
    ConversationSession,
    TurnRunner,
    WindowPolicy,
    end_conversation_rule,
)
from kenzy.s2s.engine import (
    AudioDelta,
    EngineEvent,
    InputTranscript,
    ResponseDone,
    ToolCall,
)
from kenzy.s2s.gate import Speaker, ToolRule, TurnGate


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _session(clock: _Clock, **policy: float) -> ConversationSession:
    return ConversationSession(policy=WindowPolicy(**policy), clock=clock)


# ------------------------------------------------------------------ lifecycle


def test_followup_window_anchored_at_playback_complete() -> None:
    clock = _Clock()
    s = _session(clock)
    s.begin_turn()
    clock.t += 3.0  # reply took a while to PLAY — the anchor is the node's completion
    deadline = s.on_playback_complete()
    assert deadline == clock.t + 8.0  # founder: ~8 s from playback-complete
    clock.t = deadline - 0.1
    assert s.poll() is None
    clock.t = deadline
    assert s.poll() == "silence" and s.ended and s.end_reason == "silence"


def test_question_arms_a_longer_window() -> None:
    clock = _Clock()
    s = _session(clock)
    s.begin_turn()
    deadline = s.on_playback_complete(expects_response=True)
    assert deadline == clock.t + 15.0  # the expect_response distinction, kept


def test_hard_cap_fires_even_mid_turn() -> None:
    clock = _Clock()
    s = _session(clock, hard_cap_s=100.0)
    s.begin_turn()  # active — no window open
    clock.t += 100.0
    assert s.poll() == "hard_cap" and s.ended


def test_wake_ends_and_late_playback_cannot_reopen_a_window() -> None:
    clock = _Clock()
    s = _session(clock)
    s.begin_turn()
    s.on_wake()
    assert s.ended and s.end_reason == "wake"
    assert s.on_playback_complete() is None  # a late node event must not re-arm
    assert s.poll() is None  # and polling an ended session re-triggers nothing
    assert s.end("silence") is False  # first end wins


def test_presence_loss_is_the_quiet_end() -> None:
    s = _session(_Clock())
    s.on_presence_lost()
    assert s.end_reason == "walk_away"


def test_no_turns_after_end_and_turn_ids_count() -> None:
    s = _session(_Clock())
    assert s.begin_turn().endswith("-t1")
    assert s.begin_turn().endswith("-t2")
    s.on_wake()
    with pytest.raises(RuntimeError):
        s.begin_turn()


def test_end_conversation_tool_is_required_kit() -> None:
    assert END_CONVERSATION_TOOL["name"] == END_CONVERSATION
    assert END_CONVERSATION_TOOL["type"] == "function"
    # ending is always safe: any tier may end (fail-closed direction is to END)
    assert end_conversation_rule().min_tier == "unknown"


# ---------------------------------------------------------------- turn runner


class _FakeEngine:
    """Scripted engine: yields events in order; submits may append follow-ons
    (the engine speaks again after a tool result — the real flow)."""

    def __init__(self, events: list[EngineEvent], *, on_submit: list[EngineEvent] | None = None,
                 hang_when_done: bool = False) -> None:
        self._events = events
        self._on_submit = on_submit or []
        self._hang = hang_when_done
        self.log: list[str] = []
        self.submitted: list[tuple[str, str]] = []

    async def events(self) -> AsyncIterator[EngineEvent]:
        i = 0
        while True:
            if i < len(self._events):
                evt = self._events[i]
                i += 1
                self.log.append(f"evt:{type(evt).__name__}")
                yield evt
            elif self._hang:
                await asyncio.sleep(3600)  # a silent engine — the timeout's case
            else:
                return

    async def submit_tool_result(self, call_id: str, output: str) -> None:
        self.submitted.append((call_id, output))
        self.log.append(f"submit:{call_id}")
        self._events.extend(self._on_submit)
        self._on_submit = []

    async def cancel(self) -> None:
        self.log.append("cancel")


class _Rig:
    def __init__(self, engine: _FakeEngine, *, tier: str = "recognized",
                 rules: dict[str, ToolRule] | None = None, detach: bool = False) -> None:
        self.engine = engine
        self.session = ConversationSession(clock=_Clock())
        self.gate = TurnGate(
            self.session.begin_turn(),
            identity=lambda: Speaker("Alex", tier),
            tool_rules=rules or {
                "set_light": ToolRule(min_tier="recognized"),
                "check_mail": ToolRule(min_tier="recognized", detach=True),
                END_CONVERSATION: end_conversation_rule(),
            },
            audit=lambda _r: None,
        )
        self.executed: list[str] = []
        self.detached: list[str] = []
        self.delivered: list[bytes] = []

        async def execute(call: ToolCall) -> str:
            self.executed.append(call.name)
            return "light on"

        async def detach_fn(call: ToolCall) -> str:
            self.detached.append(call.name)
            return "I'll work on that and let you know"

        async def deliver(pcm: bytes) -> None:
            self.delivered.append(pcm)

        self.runner = TurnRunner(
            engine, self.gate, self.session,
            execute=execute, deliver=deliver,
            detach=detach_fn if detach else None,
            transcript_timeout_s=0.05,
        )


def _call(name: str = "set_light") -> ToolCall:
    return ToolCall(call_id="c1", name=name, arguments_json="{}")


async def test_result_held_until_response_done_then_submitted() -> None:
    engine = _FakeEngine(
        [_call(), InputTranscript("turn on the light", late=False), ResponseDone("done", 0, 0)],
        on_submit=[ResponseDone("done", 0, 0)],
    )
    rig = _Rig(engine)
    result = await rig.runner.run()
    assert result.status == "ok" and result.results_submitted == 1
    assert rig.executed == ["set_light"]
    # divergence 4: the submit happened only AFTER the calling response's done
    assert engine.log.index("submit:c1") > engine.log.index("evt:ResponseDone")


async def test_audio_delivered_and_reply_text_accumulated() -> None:
    engine = _FakeEngine([
        InputTranscript("hello", late=False),
        AudioDelta(b"\x01\x02"),
        ResponseDone("done", 0, 0),
    ])
    rig = _Rig(engine)
    result = await rig.runner.run()
    assert result.status == "ok"
    assert rig.delivered == [b"\x01\x02"]


async def test_end_conversation_ends_verbally_and_model_may_say_farewell() -> None:
    engine = _FakeEngine(
        [InputTranscript("end the session", late=False), _call(END_CONVERSATION),
         ResponseDone("done", 0, 0)],
        on_submit=[AudioDelta(b"\x0f"), ResponseDone("done", 0, 0)],  # the farewell
    )
    rig = _Rig(engine)
    result = await rig.runner.run()
    assert result.status == "ok" and result.session_ended
    assert rig.session.end_reason == "verbal"
    assert rig.executed == []  # the end tool is the conversation layer's, not the executor's
    assert "conversation ended" in engine.submitted[0][1]
    assert rig.delivered == [b"\x0f"]  # the farewell still played after the end


async def test_denied_verdict_is_reported_honestly() -> None:
    engine = _FakeEngine(
        [InputTranscript("turn on the light", late=False), _call(), ResponseDone("done", 0, 0)],
        on_submit=[ResponseDone("done", 0, 0)],
    )
    rig = _Rig(engine, tier="unknown")  # below set_light's min_tier
    result = await rig.runner.run()
    assert result.status == "ok"
    assert rig.executed == []  # fail-closed: nothing ran
    assert engine.submitted[0][1].startswith("denied:")  # the model is told, never lied to


async def test_detach_verdict_routes_to_the_ledger_hand_off() -> None:
    engine = _FakeEngine(
        [InputTranscript("check my mail", late=False), _call("check_mail"),
         ResponseDone("done", 0, 0)],
        on_submit=[ResponseDone("done", 0, 0)],
    )
    rig = _Rig(engine, detach=True)
    await rig.runner.run()
    assert rig.detached == ["check_mail"] and rig.executed == []
    assert "I'll work on that" in engine.submitted[0][1]


async def test_late_transcript_race_resolves_and_then_submits() -> None:
    # the measured cloud race: response.done BEFORE the input transcript exists
    engine = _FakeEngine(
        [_call(), ResponseDone("done", 0, 0), InputTranscript("turn on the light", late=True),
         ResponseDone("done", 0, 0)],
        on_submit=[],
    )
    rig = _Rig(engine)
    result = await rig.runner.run()
    assert result.status == "ok" and result.results_submitted == 1
    assert engine.log.index("submit:c1") > engine.log.index("evt:InputTranscript")


async def test_transcript_never_arrives_fails_the_turn_closed() -> None:
    engine = _FakeEngine([_call(), ResponseDone("done", 0, 0)], hang_when_done=True)
    rig = _Rig(engine)
    result = await rig.runner.run()
    assert result.status == "no_transcript"
    assert rig.executed == [] and engine.submitted == []  # the gate held everything
