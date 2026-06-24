"""Tests for voice speaker enrollment: the enroll_speaker skill, the server-side
session (gating, prompt/capture loop, /enroll routing), and disconnect cleanup."""

from __future__ import annotations

import asyncio
import json

from kenzy.llm import skills as sk
from kenzy.llm.builtin_skills.enroll import enroll_speaker
from kenzy.server.server import NodeSession, TranscribingServer


class _RecWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, m):  # noqa: ANN001, ANN201
        self.sent.append(m)


def _server() -> TranscribingServer:
    return TranscribingServer({"speaker": {"url": "http://spk:8768/identify"}})


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


async def test_enroll_skill_queues_action():
    sk.begin_actions()
    out = await enroll_speaker("Alice")
    assert "Alice" in out
    assert sk.take_actions() == [{"type": "start_enrollment", "name": "Alice"}]


async def test_enroll_skill_rejects_empty():
    sk.begin_actions()
    await enroll_speaker("   ")
    assert sk.take_actions() == []


# ---------------------------------------------------------------------------
# Server: gating + session lifecycle
# ---------------------------------------------------------------------------


async def test_enroll_disabled_by_default(monkeypatch):
    srv = _server()
    monkeypatch.setattr(srv, "_voice_enroll_allowed", lambda: False)
    srv._nodes["k"] = NodeSession(ws=_RecWS(), node_id="k", room_id="kitchen")  # type: ignore[arg-type]
    said: list[str] = []

    async def say(node, room, text):  # noqa: ANN001, ANN202
        said.append(text)

    monkeypatch.setattr(srv, "_say", say)
    await srv.start_enrollment("k", "kitchen", "Alice")
    assert "k" not in srv._enroll_sessions
    assert said and "off" in said[0].lower()


async def test_voice_enroll_flag_from_service_config(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    services = tmp_path / "configs" / "services"
    services.mkdir(parents=True)
    srv = _server()
    assert srv._voice_enroll_allowed() is False  # packaged default
    (services / "speaker.yaml").write_text("allow_voice_enroll: true\n")
    assert srv._voice_enroll_allowed() is True  # dashboard override enables it (live read)


def test_enroll_prompts_from_service_config(monkeypatch):
    from kenzy.speaker import DEFAULT_ENROLL_PROMPTS

    srv = _server()
    # The dashboard-editable speaker-service config is the single source of truth.
    monkeypatch.setattr(
        srv,
        "_effective_service_config",
        lambda svc: {"enroll_prompts": ["Read this.", "  ", "And this."]},
    )
    assert srv._enroll_prompts() == ["Read this.", "And this."]  # blanks dropped

    # Falls back to the bundled defaults when unset/empty.
    monkeypatch.setattr(srv, "_effective_service_config", lambda svc: {"enroll_prompts": []})
    assert srv._enroll_prompts() == DEFAULT_ENROLL_PROMPTS


async def test_enroll_starts_and_prompts(monkeypatch):
    srv = _server()
    monkeypatch.setattr(srv, "_voice_enroll_allowed", lambda: True)
    ws = _RecWS()
    srv._nodes["k"] = NodeSession(ws=ws, node_id="k", room_id="kitchen")  # type: ignore[arg-type]
    tts: list[str] = []

    async def run_tts(node, room, sid, text, vp):  # noqa: ANN001, ANN202
        tts.append(text)

    monkeypatch.setattr(srv, "_run_tts", run_tts)
    await srv.start_enrollment("k", "kitchen", "Alice")
    try:
        assert srv._enroll_sessions["k"]["name"] == "Alice"
        # The node was armed to capture the next utterance, and prompted by name.
        assert any(json.loads(m).get("type") == "expect_utterance" for m in ws.sent)
        assert tts and "Alice" in tts[0]
    finally:
        srv._end_enroll_session("k")  # cancel the timeout task


async def test_enroll_capture_loop_collects_and_finishes(monkeypatch):
    srv = _server()
    srv._nodes["k"] = NodeSession(ws=_RecWS(), node_id="k", room_id="kitchen")  # type: ignore[arg-type]
    enrolled: list[tuple[int, str]] = []
    prompts: list[str] = []
    done: list[str] = []

    async def call_enroll(pcm, name):  # noqa: ANN001, ANN202
        enrolled.append((len(pcm), name))
        return True

    async def prompt(node, room, text):  # noqa: ANN001, ANN202
        prompts.append(text)

    async def say(node, room, text):  # noqa: ANN001, ANN202
        done.append(text)

    monkeypatch.setattr(srv, "_call_enroll", call_enroll)
    monkeypatch.setattr(srv, "_enroll_prompt", prompt)
    monkeypatch.setattr(srv, "_say", say)

    srv._enroll_sessions["k"] = {
        "name": "Alice",
        "room": "kitchen",
        "collected": 0,
        "attempts": 0,
        "prompts": ["one", "two", "three"],
        "timeout": asyncio.create_task(asyncio.sleep(0)),
    }
    pcm = b"\x01\x02" * 20000  # comfortably over the min-bytes threshold
    for _ in range(3):
        await srv._handle_enroll_capture("k", "kitchen", pcm)

    assert len(enrolled) == 3  # three samples POSTed to /enroll (one per prompt)
    assert len(prompts) == 2  # re-prompted between samples, not after the last
    assert "k" not in srv._enroll_sessions  # session ended
    assert any("enrolled alice" in d.lower() for d in done)


async def test_enroll_short_capture_is_retried(monkeypatch):
    srv = _server()
    enrolled: list[str] = []

    async def call_enroll(pcm, name):  # noqa: ANN001, ANN202
        enrolled.append(name)
        return True

    async def prompt(node, room, text):  # noqa: ANN001, ANN202
        pass

    monkeypatch.setattr(srv, "_call_enroll", call_enroll)
    monkeypatch.setattr(srv, "_enroll_prompt", prompt)
    srv._enroll_sessions["k"] = {
        "name": "A",
        "room": "r",
        "collected": 0,
        "attempts": 0,
        "prompts": ["one", "two", "three"],
        "timeout": asyncio.create_task(asyncio.sleep(0)),
    }
    await srv._handle_enroll_capture("k", "r", b"\x00\x00")  # too short
    assert enrolled == []  # not sent
    assert srv._enroll_sessions["k"]["collected"] == 0
    srv._end_enroll_session("k")


# ---------------------------------------------------------------------------
# Server: routing + disconnect cleanup
# ---------------------------------------------------------------------------


async def test_transcribe_routes_to_enrollment(monkeypatch):
    srv = _server()
    handled: list[tuple[str, int]] = []
    stt_calls: list[object] = []

    async def handle(node, room, pcm):  # noqa: ANN001, ANN202
        handled.append((node, len(pcm)))

    async def stt(*a):  # noqa: ANN002, ANN202
        stt_calls.append(a)
        return "hello"

    monkeypatch.setattr(srv, "_handle_enroll_capture", handle)
    monkeypatch.setattr(srv, "_call_stt", stt)
    srv._enroll_sessions["k"] = {
        "name": "A",
        "room": "r",
        "collected": 0,
        "attempts": 0,
        "timeout": None,
    }

    await srv._transcribe("k", "kitchen", "sid", b"1234")
    assert handled == [("k", 4)]
    assert stt_calls == []  # enrollment capture must NOT go through STT/LLM


async def test_disconnect_clears_enroll_session():
    srv = _server()
    t = asyncio.create_task(asyncio.sleep(100))
    srv._enroll_sessions["k"] = {
        "name": "A",
        "room": "r",
        "collected": 0,
        "attempts": 0,
        "timeout": t,
    }
    srv._cleanup_on_disconnect("k")
    assert "k" not in srv._enroll_sessions
    await asyncio.gather(t, return_exceptions=True)
    assert t.cancelled()
