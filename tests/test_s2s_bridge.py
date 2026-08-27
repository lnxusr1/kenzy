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

    async def submit_tool_result(self, call_id: str, output: str) -> None:
        self.log.append(f"submit:{call_id}")
        self.script.append(ResponseDone("done", 0, 0))

    async def aclose(self) -> None:
        self.closed = True


class _Rig:
    def __init__(self, *, enabled: bool = True, capable: bool = True,
                 url: str = "ws://engine/v1/realtime", open_fails: bool = False,
                 speaker: Speaker | None = None,
                 policy: WindowPolicy | None = None) -> None:
        self.enabled = enabled
        self.capable = capable
        self.url = url
        self.open_fails = open_fails
        self.speaker = speaker if speaker is not None else Speaker("Alex", "recognized")
        self.engines: list[_FakeEngine] = []
        self.classic: list[tuple[str, str, str | None, int]] = []
        self.frames: list[bytes] = []
        self.starts = 0
        self.ends = 0
        self.holds = 0
        self.hold_result = True
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

        async def fetch_tools(_tier: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
            return (
                [{"type": "function", "name": "set_light", "parameters": {}}],
                {"set_light": "recognized"},
            )

        async def identify(_pcm: bytes, _room: str) -> tuple[Speaker, float]:
            return self.speaker, 0.9

        async def execute_tool(call: ToolCall, _n: str, _r: str, _s: Speaker) -> str:
            self.executed.append(call.name)
            return "done"

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

        async def hold_floor(_n: str) -> bool:
            self.holds += 1
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
            policy=policy or WindowPolicy(),
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
    engine.script.append(ResponseDone("cancelled", 0, 0))
    await task
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
