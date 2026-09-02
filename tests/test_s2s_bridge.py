"""Follow-up bridge tests — routing, fallback, lifecycle, and barge-in pinned
(spec: kenzy-design/app/s2s-design.md, the follow-up feature entry). Every
server-shaped dependency is a recording fake; the engine is scripted."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from kenzy.s2s.conversation import END_CONVERSATION, WindowPolicy
from kenzy.s2s.engine import (
    AudioDelta,
    EngineEvent,
    InputTranscript,
    ResponseDone,
    ToolCall,
)
from kenzy.s2s.gate import Speaker
from kenzy.server.s2s_bridge import BridgeDeps, S2SBridge

NODE = "node-1"
ROOM = "office"
_PCM = b"\x00\x40" * 160  # 320 bytes of loud int16 — never a phantom
_PCM2 = b"\x00\x40" * 320  # 640 bytes


class _FakeEngine:
    """Scripted engine shared across a conversation's turns (one cursor)."""

    def __init__(self) -> None:
        self.script: list[EngineEvent] = []
        self.log: list[str] = []
        self.closed = False
        self._i = 0

    async def events(self) -> AsyncIterator[EngineEvent]:
        # A real socket BLOCKS when there's nothing to read — so does this
        # (the abandoned generator is collected when the runner returns).
        while True:
            if self._i < len(self.script):
                evt = self.script[self._i]
                self._i += 1
                yield evt
            else:
                await asyncio.sleep(0.001)

    async def configure(self, *, instructions: str, voice: str,
                        tools: list[dict[str, Any]] | None = None, rate: int = 24000) -> None:
        self.log.append("configure")
        self.tools = tools or []

    async def append(self, pcm: bytes) -> None:
        self.log.append(f"append:{len(pcm)}")

    async def commit(self) -> None:
        self.log.append("commit")

    async def cancel(self) -> None:
        self.log.append("cancel")

    async def submit_tool_result(
        self, call_id: str, output: str, *, respond: bool = True
    ) -> None:
        self.log.append(f"submit:{call_id}" + ("" if respond else ":norespond"))
        if respond:
            self.script.append(ResponseDone("done", 0, 0))

    async def add_context(self, text: str) -> None:
        self.log.append(f"context:{text}")

    async def respond(self) -> None:
        self.log.append("respond")

    async def aclose(self) -> None:
        self.closed = True


class _Rig:
    def __init__(self, *, enabled: bool = True, capable: bool = True,
                 url: str = "ws://engine/v1/realtime", open_fails: bool = False,
                 speaker: Speaker | None = None,
                 policy: WindowPolicy | None = None,
                 policy_on_demand: WindowPolicy | None = None) -> None:
        self.enabled = enabled
        self.capable = capable
        self.url = url
        self.open_fails = open_fails
        self.speaker = speaker if speaker is not None else Speaker("Alex", "recognized")
        self.pace: dict[str, str] = {"set_light": "instant"}
        self.detached: list[tuple[str, bool]] = []  # (tool name, had running work)
        self.pickup_lines: list[str] = []
        self.pickup_delivered_ids: list[str] = []
        self.stashed: list[tuple[str, str, list[tuple[str, str]]]] = []
        self.resume_line: str | None = None
        self.exec_gate: asyncio.Event | None = None  # set = execute_tool blocks on it
        self.playback_signal = False  # node reports tts_done (>=5.1.3)
        self.engines: list[_FakeEngine] = []
        self.classic: list[tuple[str, str, str | None, int]] = []
        self.frames: list[bytes] = []
        self.starts = 0
        self.ends = 0
        self.holds = 0
        self.hold_result = True
        self.hold_windows: list[float | None] = []
        self.floor_ends = 0
        self.executed: list[str] = []
        self.order: list[str] = []  # cross-dep ordering (the buffered-order rule)
        self.activities: list[dict[str, Any]] = []

        async def factory(_url: str) -> _FakeEngine:
            if self.open_fails:
                raise ConnectionError("engine down")
            engine = _FakeEngine()
            self.engines.append(engine)
            return engine

        async def fetch_tools(
            _tier: str,
        ) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
            return (
                [{"type": "function", "name": "set_light", "parameters": {}}],
                {"set_light": "recognized"},
                dict(self.pace),
            )

        async def identify(_pcm: bytes, _room: str) -> tuple[Speaker, float]:
            return self.speaker, 0.9

        async def execute_tool(call: ToolCall, _n: str, _r: str, _s: Speaker) -> str:
            if self.exec_gate is not None:
                await self.exec_gate.wait()
            self.executed.append(call.name)
            return "done"

        async def detach(
            call: ToolCall, _n: str, _r: str, _s: Speaker, work: asyncio.Task[str] | None
        ) -> str:
            self.detached.append((call.name, work is not None))
            if work is not None:
                work.cancel()
            return f"Started in the background: {call.name}."

        def pickup(owner: str) -> list[tuple[str, str]]:
            lines, self.pickup_lines = list(self.pickup_lines), []
            return [(f"t{i}", ln) for i, ln in enumerate(lines)]

        def pickup_delivered(task_id: str) -> None:
            self.pickup_delivered_ids.append(task_id)

        def stash(node: str, name: str, history: list[tuple[str, str]]) -> None:
            self.stashed.append((node, name, history))

        def resume(node: str, name: str) -> str | None:
            return self.resume_line

        async def deliver_start(_n: str) -> None:
            self.starts += 1
            self.order.append("tts_start")

        async def deliver_frame(_n: str, pcm: bytes) -> bool:
            self.frames.append(pcm)
            return True

        async def deliver_end(_n: str) -> None:
            self.ends += 1
            self.order.append("tts_end")

        async def listen_now(_n: str) -> None:
            self.order.append("listen_now")

        async def hold_floor(_n: str, window_s: float | None) -> bool:
            self.holds += 1
            self.hold_windows.append(window_s)
            self.order.append("hold_floor")
            return self.hold_result

        async def classic(n: str, r: str, sid: str | None, pcm: bytes) -> None:
            self.classic.append((n, r, sid, len(pcm)))

        self.bridge = S2SBridge(BridgeDeps(
            enabled=lambda: self.enabled,
            engine_url=lambda: self.url,
            node_capable=lambda _n: self.capable,
            engine_factory=factory,
            fetch_tools=fetch_tools,
            identify=identify,
            execute_tool=execute_tool,
            deliver_start=deliver_start,
            deliver_frame=deliver_frame,
            deliver_end=deliver_end,
            hold_floor=hold_floor,
            listen_now=listen_now,
            activity=self.activities.append,
            end_floor=lambda _n: setattr(self, "floor_ends", self.floor_ends + 1),
            classic=classic,
            instructions=lambda room: f"be kenzy in the {room}",
            audit=lambda _r: None,
            detach=detach,
            pickup=pickup,
            pickup_delivered=pickup_delivered,
            playback_signal=lambda _n: self.playback_signal,
            policy=policy or WindowPolicy(),
            policy_on_demand=policy_on_demand,
            stash=stash,
            resume=resume,
        ))

    @property
    def engine(self) -> _FakeEngine:
        return self.engines[-1]


def _turn_ok() -> list[EngineEvent]:
    return [InputTranscript("hello", late=False), AudioDelta(b"\x01"), ResponseDone("done", 0, 0)]


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


# ------------------------------------------------------------------- routing


def test_half_duplex_and_disabled_stay_classic() -> None:
    assert not _Rig(capable=False).bridge.should_take(NODE)  # the AEC gate
    assert not _Rig(enabled=False).bridge.should_take(NODE)  # the toggle
    assert not _Rig(url="").bridge.should_take(NODE)  # no engine known
    rig = _Rig()
    assert rig.bridge.should_take(NODE)
    assert rig.bridge.node_mode(NODE) == "follow-up"


async def test_open_on_demand_opens_speaks_and_arms_the_sticky_window() -> None:
    # on_demand (auto-open off): "start a conversation" opens the conversation
    # NOW and speaks the greeting through the bridge's own delivery path.
    rig = _Rig(
        enabled=False,
        policy=WindowPolicy(followup_s=8.0),
        policy_on_demand=WindowPolicy(followup_s=30.0),
    )
    assert not rig.bridge.should_take(NODE)  # fresh capture would stay classic
    ok = await rig.bridge.open_on_demand(NODE, ROOM, b"\x01" * 32, "Okay, let's talk.")
    assert ok and rig.bridge.active(NODE)
    assert rig.bridge.should_take(NODE)  # the open conversation now routes turns
    # the greeting went out via the deliver path, floor-holding order intact
    assert rig.starts == 1 and rig.ends == 1 and rig.frames
    assert rig.order.index("hold_floor") < rig.order.index("tts_end")
    assert rig.hold_windows == [30.0]  # the sticky window rode expect_utterance
    # the engine was told it greeted (no double greeting on turn 1)
    assert any("context:You just opened this conversation" in c for c in rig.engine.log)
    await rig.bridge.close(NODE, "ended")
    assert not rig.bridge.should_take(NODE)  # closed = classic again


async def test_open_on_demand_refuses_half_duplex_and_engine_down() -> None:
    rig = _Rig(enabled=False, capable=False)
    assert not await rig.bridge.open_on_demand(NODE, ROOM, b"", "hi")
    rig2 = _Rig(enabled=False, url="")
    assert not await rig2.bridge.open_on_demand(NODE, ROOM, b"", "hi")
    rig3 = _Rig(enabled=False, open_fails=True)
    assert not await rig3.bridge.open_on_demand(NODE, ROOM, b"", "hi")
    assert not rig3.bridge.active(NODE)


async def test_open_on_demand_unarmed_floor_closes_honestly() -> None:
    rig = _Rig(enabled=False)
    rig.hold_result = False  # the node never armed its window
    ok = await rig.bridge.open_on_demand(NODE, ROOM, b"\x01" * 8, "hi")
    assert not ok and not rig.bridge.active(NODE)  # no zombie conversation


async def test_always_mode_open_keeps_the_default_window() -> None:
    rig = _Rig(  # always: auto-open, not on-demand
        enabled=True,
        policy=WindowPolicy(followup_s=8.0),
        policy_on_demand=WindowPolicy(followup_s=30.0),
    )
    task = asyncio.get_running_loop().create_task(rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM))
    await _settle()
    rig.engine.script.extend(_turn_ok())
    await task
    assert rig.bridge._convs[NODE].session._policy.followup_s == 8.0  # default, not sticky
    assert rig.hold_windows == [None]  # always-mode turns use the node's default window


async def test_on_demand_turn_arms_the_sticky_window() -> None:
    rig = _Rig(
        enabled=False,
        policy=WindowPolicy(followup_s=8.0),
        policy_on_demand=WindowPolicy(followup_s=30.0),
    )
    assert await rig.bridge.open_on_demand(NODE, ROOM, b"", "hi")
    task = asyncio.get_running_loop().create_task(rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM))
    await _settle()
    rig.engine.script.extend(_turn_ok())
    await task
    assert rig.bridge._convs[NODE].session._policy.followup_s == 30.0
    assert rig.hold_windows[-1] == 30.0  # every on-demand turn re-arms sticky


async def test_on_demand_close_stashes_the_transcript() -> None:
    rig = _Rig(enabled=False, policy_on_demand=WindowPolicy())
    assert await rig.bridge.open_on_demand(NODE, ROOM, b"", "Okay, let's talk.")
    task = asyncio.get_running_loop().create_task(rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM))
    await _settle()
    rig.engine.script.extend(_turn_ok())
    await task
    await rig.bridge.close(NODE, "ended")
    assert rig.stashed and rig.stashed[0][0] == NODE and rig.stashed[0][1] == "Alex"
    # the greeting + the turn's (transcript, reply)
    assert rig.stashed[0][2] == [("", "Okay, let's talk."), ("hello", "")]


async def test_always_close_does_not_stash() -> None:
    rig = _Rig(enabled=True)  # always mode: not on-demand, never rests between wakes
    task = asyncio.get_running_loop().create_task(rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM))
    await _settle()
    rig.engine.script.extend(_turn_ok())
    await task
    await rig.bridge.close(NODE, "ended")
    assert rig.stashed == []


async def test_on_demand_resume_line_injected_as_context() -> None:
    rig = _Rig(enabled=False, policy_on_demand=WindowPolicy())
    rig.resume_line = "Resuming your recent conversation. Earlier: hi / hello"
    assert await rig.bridge.open_on_demand(NODE, ROOM, b"", "hi")
    task = asyncio.get_running_loop().create_task(rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM))
    await _settle()
    rig.engine.script.extend(_turn_ok())
    await task
    assert any("context:Resuming your recent conversation" in c for c in rig.engine.log)


async def test_engine_down_falls_back_to_classic_with_the_same_pcm() -> None:
    rig = _Rig(open_fails=True)
    await rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM2)
    assert rig.classic == [(NODE, ROOM, "sid1", 640)]  # nothing lost
    assert not rig.bridge.active(NODE)


# --------------------------------------------------------------------- turns


async def test_happy_turn_delivers_audio_and_holds_the_floor() -> None:
    rig = _Rig()
    # first turn: conversation opens, turn runs
    task = asyncio.get_running_loop().create_task(
        rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM)
    )
    await _settle()
    rig.engine.script.extend(_turn_ok())
    await task
    assert "configure" in rig.engine.log and "commit" in rig.engine.log
    assert rig.frames == [b"\x01"] and rig.starts == 1 and rig.ends == 1
    assert rig.holds == 1  # the follow-up window armed (expect_utterance path)
    # the buffered-order rule: expect_utterance BEFORE tts_end, or the node
    # never treats the reply as floor-holding (no barge-in, no window)
    assert rig.order == ["listen_now", "tts_start", "hold_floor", "tts_end"]
    # the household-visible trail: one Activity record per turn
    assert rig.activities and rig.activities[0]["transcript"] == "hello"
    assert rig.bridge.active(NODE)  # the conversation persists across turns


async def test_followup_turn_reuses_the_same_engine_session() -> None:
    rig = _Rig()
    for sid in ("sid1", "sid2"):
        task = asyncio.get_running_loop().create_task(
            rig.bridge.take_turn(NODE, ROOM, sid, _PCM)
        )
        await _settle()
        rig.engine.script.extend(_turn_ok())
        await task
    assert len(rig.engines) == 1  # one session, one history — never a fork
    assert rig.holds == 2


async def test_recognized_speaker_unlocks_a_gated_tool() -> None:
    rig = _Rig()
    task = asyncio.get_running_loop().create_task(
        rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM)
    )
    await _settle()  # identity task has fed the session by now
    rig.engine.script.extend([
        InputTranscript("turn on the light", late=False),
        ToolCall("c1", "set_light", "{}"),
        ResponseDone("done", 0, 0),  # submit appends the follow-on done
    ])
    await task
    assert rig.executed == ["set_light"]  # gate allowed: identity was current
    assert "submit:c1" in rig.engine.log


async def test_end_conversation_closes_everything_and_never_rearms() -> None:
    rig = _Rig()
    task = asyncio.get_running_loop().create_task(
        rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM)
    )
    await _settle()
    rig.engine.script.extend([
        InputTranscript("end the conversation", late=False),
        ToolCall("c2", END_CONVERSATION, "{}"),
        ResponseDone("done", 0, 0),
    ])
    await task
    assert not rig.bridge.active(NODE)
    assert rig.engine.closed and rig.floor_ends == 1
    assert rig.holds == 0  # a finished conversation never re-opens the mic


async def test_followup_timeout_is_the_silence_end() -> None:
    rig = _Rig()
    task = asyncio.get_running_loop().create_task(
        rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM)
    )
    await _settle()
    rig.engine.script.extend(_turn_ok())
    await task
    engine = rig.engine
    assert rig.bridge.on_followup_timeout(NODE) is True  # ours — classic skips
    await _settle()
    assert engine.closed and not rig.bridge.active(NODE)
    assert rig.bridge.on_followup_timeout(NODE) is False  # nothing left to end


async def test_barge_in_cancels_in_engine_and_skips_the_rearm() -> None:
    rig = _Rig()
    task = asyncio.get_running_loop().create_task(
        rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM)
    )
    await _settle()
    engine = rig.engine
    engine.script.append(InputTranscript("hello", late=False))
    await _settle()  # the reply now hangs mid-generation (script drained)
    await rig.bridge.on_capture_start(NODE)  # the user spoke over the reply
    assert "cancel" in engine.log
    # The engine's buffered tail arrives AFTER the cancel (measured live on
    # the cloud engine: seconds of speech land instantly post-cancel). The
    # runner consumes it so the socket stays clean — but none of it may
    # reach the room the user just interrupted.
    frames_at_barge = list(rig.frames)
    engine.script.append(AudioDelta(b"\x99"))
    engine.script.append(ResponseDone("cancelled", 0, 0))
    await task
    assert rig.frames == frames_at_barge  # stale tail never delivered
    assert rig.holds == 0  # the new capture owns the floor — no re-arm over it
    assert rig.bridge.active(NODE)  # the conversation (and history) survives


async def test_hard_cap_rolls_the_conversation_over() -> None:
    rig = _Rig(policy=WindowPolicy(hard_cap_s=0.0))  # capped immediately
    for sid in ("sid1", "sid2"):
        task = asyncio.get_running_loop().create_task(
            rig.bridge.take_turn(NODE, ROOM, sid, _PCM)
        )
        await _settle()
        rig.engine.script.extend(_turn_ok())
        await task
    assert len(rig.engines) == 2  # turn 2 found the cap expired: fresh session
    assert rig.engines[0].closed


async def test_phantom_silence_never_spends_a_turn() -> None:
    rig = _Rig()
    await rig.bridge.take_turn(NODE, ROOM, "sid1", b"\x00" * 640)  # digital silence
    assert rig.engines == [] and rig.classic == []  # no engine turn, no fallback noise
    assert not rig.bridge.active(NODE)


def test_near_silence_scans_the_whole_capture() -> None:
    """A slow start (the pause after the wake phrase) followed by real speech
    is a COMMAND, not a phantom — found live: a first-second-only scan threw
    away 'what time is it'."""
    from kenzy.server.s2s_bridge import _near_silence

    assert _near_silence(b"\x00" * 64000)  # true silence, any length
    assert not _near_silence(b"\x00" * 64000 + b"\x00\x40" * 8)  # speech after 2s of quiet


async def test_group_claim_closes_the_old_owners_conversation() -> None:
    """Layer 1 meets the follow-up feature: a sibling node winning the wake
    ends the old owner's conversation — engine closed, floor cleared, its
    turn never re-arms a mic in a room the group has left. (Found by asking
    whether the two-butlers problem still resolves under s2s: it didn't —
    the old conversation lingered.)"""
    rig = _Rig()
    task = asyncio.get_running_loop().create_task(
        rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM)
    )
    await _settle()
    engine = rig.engine
    engine.script.append(InputTranscript("hello", late=False))
    await _settle()  # the turn now hangs mid-generation — a sibling wakes
    await rig.bridge.close(NODE, "group claimed by sibling")
    assert not rig.bridge.active(NODE) and engine.closed
    engine.script.append(ResponseDone("cancelled", 0, 0))
    await task  # the dying turn drains without re-arming
    assert rig.holds == 0  # the floor was never re-opened over the sibling
    assert rig.floor_ends >= 1  # and the node's dialog surface was cleared


async def test_identity_answer_is_fed_to_the_model_once() -> None:
    """OQ3 slice 1: the resolved speaker lands as session context (so the
    model can address people and shape actions), injected on CHANGE only —
    a second turn by the same speaker adds no duplicate item."""
    rig = _Rig()
    for sid in ("sid1", "sid2"):
        task = asyncio.get_running_loop().create_task(
            rig.bridge.take_turn(NODE, ROOM, sid, _PCM)
        )
        await _settle()
        rig.engine.script.extend(_turn_ok())
        await task
    injected = [e for e in rig.engine.log if e.startswith("context:")]
    assert len(injected) == 1
    assert "Alex" in injected[0] and "recognized" in injected[0]


async def test_a_stranger_injects_no_identity_context() -> None:
    rig = _Rig(speaker=Speaker("", "unknown"))
    task = asyncio.get_running_loop().create_task(
        rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM)
    )
    await _settle()
    rig.engine.script.extend(_turn_ok())
    await task
    assert not any(e.startswith("context:") for e in rig.engine.log)


async def test_an_errored_turn_replays_classic_when_nothing_happened() -> None:
    """A session rejection (bad config, auth) errors the turn before anything
    runs — the utterance must still get an answer, not dead air (lived
    2026-08-29 against the cloud engine)."""
    from kenzy.s2s.engine import EngineError

    rig = _Rig()
    task = asyncio.get_running_loop().create_task(
        rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM)
    )
    await _settle()
    rig.engine.script.append(EngineError("session.update rejected: bad tool shape"))
    await task
    assert not rig.bridge.active(NODE)  # the conversation closed honestly
    assert rig.classic == [(NODE, ROOM, "sid1", len(_PCM))]  # replayed, answered


async def test_an_errored_turn_with_a_submitted_tool_never_replays() -> None:
    """If a tool result was already submitted before the engine died, a
    classic replay would run the same command twice. Dead air is the lesser
    harm — the conversation still closes with the reason logged."""
    from kenzy.s2s.engine import EngineError

    rig = _Rig()
    task = asyncio.get_running_loop().create_task(
        rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM)
    )
    await _settle()
    rig.engine.script.extend([
        InputTranscript("turn on the light", late=False),
        ToolCall("c1", "set_light", "{}"),
        EngineError("engine died mid-turn"),
    ])
    await task
    assert rig.executed == ["set_light"]
    assert rig.classic == []  # no replay — the action already ran
    assert not rig.bridge.active(NODE)


# ------------------------------------------------ the async tool contract


async def test_a_deferred_tool_detaches_instead_of_executing() -> None:
    """pace=deferred -> the gate's detach verdict -> the executor, never the
    inline path. The model hears the hand-off string."""
    rig = _Rig()
    rig.pace = {"set_light": "deferred"}
    task = asyncio.get_running_loop().create_task(
        rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM)
    )
    await _settle()
    rig.engine.script.extend([
        InputTranscript("do the thing", late=False),
        ToolCall("c1", "set_light", "{}"),
        ResponseDone("done", 0, 0),
    ])
    await task
    assert rig.detached == [("set_light", False)]  # fresh detach, no inline run
    assert rig.executed == []
    assert "submit:c1" in rig.engine.log  # the hand-off went back to the model


async def test_a_stalling_working_tool_promotes_one_rung() -> None:
    """A working-class tool past the promotion threshold is ADOPTED: the
    in-flight work moves to the executor and the turn gets the hand-off."""
    rig = _Rig(policy=WindowPolicy(working_promote_s=0.05))
    rig.exec_gate = asyncio.Event()  # never set: the tool hangs
    task = asyncio.get_running_loop().create_task(
        rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM)
    )
    await _settle()
    rig.engine.script.extend([
        InputTranscript("slow thing", late=False),
        ToolCall("c1", "set_light", "{}"),
        ResponseDone("done", 0, 0),
    ])
    await task
    assert rig.detached == [("set_light", True)]  # adopted mid-flight
    assert "submit:c1" in rig.engine.log


async def test_a_barge_over_a_working_tool_is_a_downgrade_request() -> None:
    """The user speaking over the wait promotes the tool immediately — the
    ruled barge-as-downgrade, no separate mechanism."""
    rig = _Rig(policy=WindowPolicy(working_promote_s=30.0))  # only the barge ends it
    rig.exec_gate = asyncio.Event()
    task = asyncio.get_running_loop().create_task(
        rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM)
    )
    await _settle()
    rig.engine.script.extend([
        InputTranscript("slow thing", late=False),
        ToolCall("c1", "set_light", "{}"),
    ])
    await _settle()
    await rig.bridge.on_capture_start(NODE)  # the barge
    await _settle()
    rig.engine.script.append(ResponseDone("cancelled", 0, 0))
    await task
    assert rig.detached == [("set_light", True)]


async def test_delivery_turn_speaks_a_completion_into_the_live_conversation() -> None:
    """A finished task's result becomes a DELIVERY TURN: context item +
    respond, audio delivered, the follow-up window re-armed."""
    rig = _Rig()
    task = asyncio.get_running_loop().create_task(
        rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM)
    )
    await _settle()
    rig.engine.script.extend(_turn_ok())
    await task
    frames_before = len(rig.frames)
    dtask = asyncio.get_running_loop().create_task(
        rig.bridge.deliver_completion(NODE, "the app build finished")
    )
    await _settle()
    rig.engine.script.extend([AudioDelta(b"\x07"), ResponseDone("done", 0, 0)])
    assert any(e.startswith("context:A background task update") for e in rig.engine.log)
    assert "respond" in rig.engine.log
    assert await dtask is True
    assert len(rig.frames) == frames_before + 1  # the completion reached the room
    assert rig.holds == 2  # window re-armed after the delivery turn
    assert rig.bridge.active(NODE)


async def test_delivery_needs_a_live_conversation() -> None:
    rig = _Rig()
    assert await rig.bridge.deliver_completion(NODE, "x") is False


async def test_pickup_lines_ride_the_identity_injection() -> None:
    """Results the gate declined earlier land as session context the moment
    the owner's next conversation knows who they are."""
    rig = _Rig()
    rig.pickup_lines = ["'build the app' is finished. It went well."]
    task = asyncio.get_running_loop().create_task(
        rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM)
    )
    await _settle()
    rig.engine.script.extend(_turn_ok())
    await task
    picked = [e for e in rig.engine.log if "background task finished" in e]
    assert len(picked) == 1 and "build the app" in picked[0]


async def test_delivery_waits_for_the_previous_reply_to_finish_playing() -> None:
    """The audible-idle anchor: audio sent while the node still plays the
    prior reply is DISCARDED by its player — lived 2026-08-29 as a delivery
    that logged ok and was never heard. On a tts_done-capable node the
    delivery turn holds until playback completes."""
    rig = _Rig()
    rig.playback_signal = True
    task = asyncio.get_running_loop().create_task(
        rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM)
    )
    await _settle()
    rig.engine.script.extend(_turn_ok())
    await task  # reply DISPATCHED — but the node is still playing it

    dtask = asyncio.get_running_loop().create_task(
        rig.bridge.deliver_completion(NODE, "the search finished")
    )
    await _settle()
    # Still audibly busy: the delivery turn must not have started speaking.
    assert not any(e.startswith("context:A background task update") for e in rig.engine.log)

    rig.bridge.on_tts_done(NODE)  # the node reports playback complete
    await _settle()
    rig.engine.script.extend([AudioDelta(b"\x07"), ResponseDone("done", 0, 0)])
    assert await dtask is True
    assert any(e.startswith("context:A background task update") for e in rig.engine.log)


async def test_a_detach_always_gets_a_followon_response() -> None:
    """Founder ruling 2026-08-29: a detached tool must never start silently,
    so its hand-off ALWAYS gets a follow-on response to be spoken about —
    even when the calling response already said something else. The occasional
    "already acknowledged" double-say is prevented by the hand-off WORDING,
    not by suppressing the response (which lost the acknowledgment when the
    turn answered a different sub-request)."""
    from kenzy.s2s.engine import ReplyTranscriptDelta

    rig = _Rig()
    rig.pace = {"set_light": "deferred"}
    task = asyncio.get_running_loop().create_task(
        rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM)
    )
    await _settle()
    rig.engine.script.extend([
        InputTranscript("what time is it and go do the thing", late=False),
        ReplyTranscriptDelta("It's three o'clock."),  # answered something ELSE
        AudioDelta(b"\x01"),
        ToolCall("c1", "set_light", "{}"),
        ResponseDone("done", 0, 0),
    ])
    await task
    # respond=True on the submit (no :norespond suffix) — a follow-on is
    # requested so the detach gets acknowledged.
    assert "submit:c1" in rig.engine.log
    assert "submit:c1:norespond" not in rig.engine.log
    assert rig.detached == [("set_light", False)]
    assert rig.holds == 1  # the turn completed normally and re-armed


async def test_a_dead_cached_engine_falls_back_to_classic_and_closes() -> None:
    """A cached conversation whose engine socket died between turns raises at
    the append/commit handoff — before a TurnRunner exists. The utterance must
    still be answered (classic replay) and the dead conversation closed, never
    dead air + a stuck conv (the fire-and-forget task would swallow the raise)."""
    rig = _Rig()
    # Open a conversation with a first good turn.
    task = asyncio.get_running_loop().create_task(rig.bridge.take_turn(NODE, ROOM, "sid1", _PCM))
    await _settle()
    rig.engine.script.extend(_turn_ok())
    await task
    assert rig.bridge.active(NODE)

    # Now the engine's socket is dead: append raises.
    async def dead_append(pcm: bytes) -> None:
        raise ConnectionError("socket closed")

    rig.engine.append = dead_append  # type: ignore[assignment]
    rig.classic.clear()
    await rig.bridge.take_turn(NODE, ROOM, "sid2", _PCM)
    assert rig.classic == [(NODE, ROOM, "sid2", len(_PCM))]  # answered, not dead air
    assert not rig.bridge.active(NODE)  # the dead conversation was closed
