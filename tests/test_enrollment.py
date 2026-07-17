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


async def test_enroll_starts_and_prompts(monkeypatch, tmp_path):
    from kenzy.server.people import PeopleStore

    srv = _server()
    srv._people = PeopleStore(tmp_path / "nope.yaml")  # hermetic: no person records
    monkeypatch.setattr(srv, "_voice_enroll_allowed", lambda: True)
    ws = _RecWS()
    srv._nodes["k"] = NodeSession(ws=ws, node_id="k", room_id="kitchen")  # type: ignore[arg-type]
    tts: list[str] = []

    async def run_tts(node, room, sid, text, vp):  # noqa: ANN001, ANN202
        tts.append(text)

    monkeypatch.setattr(srv, "_run_tts", run_tts)
    await srv.start_enrollment("k", "kitchen", "Alice")
    try:
        # Person-first: no person named Alice exists, so the profile is keyed by
        # the slug her record will get, while the spoken flow uses her name.
        assert srv._enroll_sessions["k"]["name"] == "alice"
        assert srv._enroll_sessions["k"]["display"] == "Alice"
        # The node was armed to capture the next utterance, and prompted by name.
        assert any(json.loads(m).get("type") == "expect_utterance" for m in ws.sent)
        assert tts and "Alice" in tts[0]
    finally:
        srv._end_enroll_session("k")  # cancel the timeout task


async def test_enroll_capture_loop_collects_and_finishes(monkeypatch, tmp_path):
    from kenzy.server.people import PeopleStore

    srv = _server()
    srv._people = PeopleStore(tmp_path / "people.yaml")  # hermetic: adoption writes here
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
    # Person-first invariant: the first stored sample adopted the voice into a
    # person record (created here, since no person named Alice existed).
    owner = srv._people.by_voiceprint("Alice")
    assert owner is not None and owner.name == "Alice"


async def test_enroll_timeout_rearmed_on_capture(monkeypatch, tmp_path):
    """The session timeout is inactivity-based: every capture re-arms it, so a
    slow-but-progressing enrollment (5 prompts through local TTS) never dies
    mid-flow. Field finding: a real run blew the old 120s TOTAL cap."""
    from kenzy.server.people import PeopleStore

    srv = _server()
    srv._people = PeopleStore(tmp_path / "people.yaml")
    srv._nodes["k"] = NodeSession(ws=_RecWS(), node_id="k", room_id="kitchen")  # type: ignore[arg-type]

    async def call_enroll(pcm, name):  # noqa: ANN001, ANN202
        return True

    async def prompt(node, room, text):  # noqa: ANN001, ANN202
        pass

    monkeypatch.setattr(srv, "_call_enroll", call_enroll)
    monkeypatch.setattr(srv, "_enroll_prompt", prompt)
    original = asyncio.create_task(asyncio.sleep(0))
    srv._enroll_sessions["k"] = {
        "name": "Alice",
        "room": "kitchen",
        "collected": 0,
        "attempts": 0,
        "prompts": ["one", "two"],
        "timeout": original,
    }
    await srv._handle_enroll_capture("k", "kitchen", b"\x01\x02" * 20000)
    await asyncio.sleep(0)  # let the old task's cancellation land
    session = srv._enroll_sessions["k"]
    assert session["timeout"] is not original  # fresh inactivity window
    assert original.cancelled() or original.done()
    srv._end_enroll_session("k")


async def test_followup_timeout_retries_enrollment(monkeypatch, tmp_path):
    """An expired capture window during enrollment re-prompts (a missed sample),
    rather than silently stranding the session until the inactivity timeout —
    the node's followup_timeout must reach the enrollment loop."""
    from kenzy.server.people import PeopleStore

    srv = _server()
    srv._people = PeopleStore(tmp_path / "people.yaml")
    srv._nodes["k"] = NodeSession(ws=_RecWS(), node_id="k", room_id="kitchen")  # type: ignore[arg-type]
    prompts: list[str] = []

    async def prompt(node, room, text):  # noqa: ANN001, ANN202
        prompts.append(text)

    monkeypatch.setattr(srv, "_enroll_prompt", prompt)
    srv._enroll_sessions["k"] = {
        "name": "alice",
        "room": "kitchen",
        "collected": 0,
        "attempts": 0,
        "prompts": ["one", "two"],
        "timeout": asyncio.create_task(asyncio.sleep(0)),
    }
    srv._followup_timed_out("k")
    await asyncio.sleep(0)  # let the routed retry task run
    assert srv._enroll_sessions["k"]["attempts"] == 1
    assert prompts and "didn't catch" in prompts[0]
    srv._end_enroll_session("k")

    # Without an enrollment session it stays a dialog event (turn counter clear).
    srv._followup_turns["k"] = 2
    srv._followup_timed_out("k")
    assert "k" not in srv._followup_turns


async def test_enroll_resolves_existing_person(monkeypatch, tmp_path):
    """Person-first resolution: enrolling a known person appends to their existing
    voiceprint; a voiceless person gets a profile keyed by their stable id."""
    from kenzy.server.people import PeopleStore

    srv = _server()
    p = tmp_path / "people.yaml"
    p.write_text(
        "people:\n  john:\n    name: John\n    voiceprints: [johnmark]\n  nicki:\n    name: Nicki\n"
    )
    srv._people = PeopleStore(p)
    monkeypatch.setattr(srv, "_voice_enroll_allowed", lambda: True)
    srv._nodes["k"] = NodeSession(ws=_RecWS(), node_id="k", room_id="kitchen")  # type: ignore[arg-type]

    async def run_tts(node, room, sid, text, vp):  # noqa: ANN001, ANN202
        pass

    monkeypatch.setattr(srv, "_run_tts", run_tts)

    # Spoken name → existing person: more samples for their existing profile.
    await srv.start_enrollment("k", "kitchen", "John")
    sess = srv._enroll_sessions["k"]
    assert (sess["name"], sess["display"], sess["person_id"]) == ("johnmark", "John", "john")
    srv._end_enroll_session("k")

    # Dashboard picks a voiceless person by id: fresh profile keyed by the id.
    await srv.start_enrollment("k", "kitchen", "Nicki", operator=True, person_id="nicki")
    sess = srv._enroll_sessions["k"]
    assert (sess["name"], sess["display"], sess["person_id"]) == ("nicki", "Nicki", "nicki")
    srv._end_enroll_session("k")

    # Unknown person_id refuses (spoken feedback, no session).
    said: list[str] = []

    async def say(node, room, text):  # noqa: ANN001, ANN202
        said.append(text)

    monkeypatch.setattr(srv, "_say", say)
    await srv.start_enrollment("k", "kitchen", "X", operator=True, person_id="ghost")
    assert "k" not in srv._enroll_sessions and said


async def test_adopt_enrolled_voice_paths(tmp_path):
    """The adopt hook: link to the picked person, else the name match, else create."""
    from kenzy.server.people import PeopleStore
    from kenzy.server.server import AudioServer

    srv = AudioServer({})
    p = tmp_path / "people.yaml"
    p.write_text("people:\n  nicki:\n    name: Nicki\n")
    srv._people = PeopleStore(p)

    srv.adopt_enrolled_voice("nicki", "Nicki", "nicki")  # picked person
    assert srv._people.by_voiceprint("nicki").id == "nicki"
    srv.adopt_enrolled_voice("nicki", "Nicki", "nicki")  # idempotent
    assert srv._people.get("nicki").voiceprints == ["nicki"]

    srv.adopt_enrolled_voice("nicki2", "NICKI")  # name match, no person_id
    assert srv._people.by_voiceprint("nicki2").id == "nicki"

    srv.adopt_enrolled_voice("alice", "Alice")  # no match → person created
    owner = srv._people.by_voiceprint("alice")
    assert owner is not None and owner.name == "Alice" and owner.id == "alice"


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
