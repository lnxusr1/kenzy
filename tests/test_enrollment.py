"""Voice enrollment on ask_audio() (4.2): the skill-driven conversation, the
server's audio-ask routing, the dashboard directive entry, and the person-first
adoption bookkeeping."""

from __future__ import annotations

import pytest

from kenzy.llm import asking
from kenzy.llm import skills as sk
from kenzy.llm.builtin_skills import enroll
from kenzy.server.server import LlmReply, NodeSession, TranscribingServer

GOOD = b"\x01\x02" * 20000  # > _MIN_PCM_BYTES
SHORT = b"\x01\x02" * 100


@pytest.fixture(autouse=True)
def _clean():
    yield
    for cid in list(asking._PENDING):
        asking._PENDING.pop(cid).task.cancel()


@pytest.fixture()
def speaker_svc(monkeypatch):
    """Fake speaker service: /enroll/info + /enroll capture."""
    state = {
        "info": {"prompts": ["one", "two", "three"], "allow_voice_enroll": True},
        "samples": [],
    }

    async def info(base):
        return dict(state["info"]) if state["info"] else None

    async def post(base, voiceprint, pcm):
        state["samples"].append((voiceprint, len(pcm)))
        return True

    monkeypatch.setattr(enroll, "_enroll_info", info)
    monkeypatch.setattr(enroll, "_post_sample", post)
    monkeypatch.setattr(enroll, "_speaker_base", lambda: "http://spk:8768")
    return state


async def _drive(coro, answers):
    """Run an enrollment coroutine, feeding scripted ask_audio answers.
    Returns (final_text_or_None, prompts)."""
    sk.begin_actions()
    sk.begin_request({"channel": "voice", "people": answers.pop("people", [])})
    outcome = await asking.run_askable(coro, kind="llm")
    prompts: list[str] = []
    script = answers["replies"]
    while not outcome.finished:
        prompts.append(outcome.parked.channel.prompt)
        if not script:
            await asking.cancel(outcome.parked.id)
            return None, prompts
        outcome = await asking.resume(outcome.parked.id, script.pop(0))
    return outcome.value, prompts


# ---------------------------------------------------------------------------
# The skill-driven conversation
# ---------------------------------------------------------------------------


async def test_enroll_happy_path(speaker_svc):
    text, prompts = await _drive(
        enroll._run_enrollment("Alice", operator=False),
        {"replies": [GOOD, GOOD, GOOD]},
    )
    assert text == "All done — I've enrolled Alice."
    assert len(prompts) == 3
    assert "After the tone, please say: one" in prompts[0]
    assert "Next, please say: two" in prompts[1]
    # New name → profile keyed by the slug the person record will get.
    assert speaker_svc["samples"] == [("alice", len(GOOD))] * 3
    # Person-first: the adopt action queued on the FIRST stored sample.
    actions = sk.take_actions()
    assert {"type": "adopt_voice", "voiceprint": "alice", "display": "Alice",
            "person_id": None} in actions  # fmt: skip


async def test_short_and_empty_samples_retry_same_prompt(speaker_svc):
    # An expired reply window arrives as b"" (the server maps expiry to an
    # empty sample) — both it and a too-short capture re-read the SAME prompt.
    text, prompts = await _drive(
        enroll._run_enrollment("Alice", operator=False),
        {"replies": [SHORT, b"", GOOD, GOOD, GOOD]},
    )
    assert text == "All done — I've enrolled Alice."
    assert "please say: one" in prompts[0]
    assert "I didn't catch that. Please say: one" in prompts[1]
    assert "I didn't catch that. Please say: one" in prompts[2]
    assert len(speaker_svc["samples"]) == 3


async def test_attempt_cap_gives_up(speaker_svc):
    text, prompts = await _drive(
        enroll._run_enrollment("Alice", operator=False),
        {"replies": [SHORT] * 10},
    )
    assert "couldn't get enough clear audio" in text
    assert len(prompts) == len(speaker_svc["info"]["prompts"]) + enroll._MAX_RETRIES


async def test_wake_cancel_mid_enrollment(speaker_svc):
    text, prompts = await _drive(
        enroll._run_enrollment("Alice", operator=False),
        {"replies": [GOOD]},  # one sample, then the script runs dry → cancel
    )
    assert text is None and len(prompts) == 2  # canceled while parked on prompt 2
    assert len(speaker_svc["samples"]) == 1  # the stored sample stays (adopted)


async def test_earshot_gate_and_operator_bypass(speaker_svc):
    speaker_svc["info"]["allow_voice_enroll"] = False
    sk.begin_actions()
    sk.begin_request({"channel": "voice", "people": []})
    out = await asking.run_askable(
        enroll._run_enrollment("Alice", operator=False), kind="llm"
    )
    assert out.finished and out.value == "Voice enrollment is turned off."

    # operator=True (dashboard) bypasses the gate.
    text, prompts = await _drive(
        enroll._run_enrollment("Alice", operator=True),
        {"replies": [GOOD, GOOD, GOOD]},
    )
    assert text and "enrolled Alice" in text


async def test_person_first_profile_keying(speaker_svc):
    people = [
        {"id": "nicki", "name": "Nicki", "voiceprints": ["nicki_old"]},
        {"id": "bob", "name": "Bob", "voiceprints": []},
    ]
    # Existing person by name → append to their existing profile.
    text, _ = await _drive(
        enroll._run_enrollment("Nicki", operator=False),
        {"replies": [GOOD, GOOD, GOOD], "people": list(people)},
    )
    assert speaker_svc["samples"][0][0] == "nicki_old"
    assert "enrolled Nicki" in text

    # person_id (dashboard) with a voiceless person → profile keyed by id.
    speaker_svc["samples"].clear()
    text, _ = await _drive(
        enroll._run_enrollment("", operator=True, person_id="bob"),
        {"replies": [GOOD, GOOD, GOOD], "people": list(people)},
    )
    assert speaker_svc["samples"][0][0] == "bob"

    # Unknown person_id → honest refusal, no asking.
    sk.begin_actions()
    sk.begin_request({"channel": "voice", "people": []})
    out = await asking.run_askable(
        enroll._run_enrollment("", operator=True, person_id="ghost"), kind="llm"
    )
    assert out.finished and "couldn't find that person" in out.value


async def test_directive_fast_intent(speaker_svc):
    m = enroll._DIRECTIVE_RE.match("[[enroll]] operator=1 person=bob name=")
    assert m and m.group("op") == "1" and m.group("pid") == "bob"
    m = enroll._DIRECTIVE_RE.match("[[enroll]] operator=0 person= name=Alice Smith")
    assert m and m.group("name") == "Alice Smith" and not m.group("pid")
    # Ordinary speech never matches.
    assert enroll._DIRECTIVE_RE.match("enroll me as Alice") is None
    r = await enroll.fast_enroll_directive("what time is it", "office", None)
    assert r.status == "miss"


# ---------------------------------------------------------------------------
# Server side: audio-ask routing + the dashboard entry
# ---------------------------------------------------------------------------


class _RecWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, m):  # noqa: ANN001, ANN201
        self.sent.append(m)


def _server() -> TranscribingServer:
    srv = TranscribingServer(
        {
            "stt": {"url": "http://x/transcribe"},
            "speaker": {"url": "http://x/identify"},
            "llm": {"url": "http://x/process"},
        }
    )
    srv._nodes["k"] = NodeSession(ws=_RecWS(), node_id="k", room_id="office")  # type: ignore[arg-type]
    return srv


async def test_audio_ask_routes_pcm_not_stt(monkeypatch):
    srv = _server()
    seen = {}

    async def stt(pcm, room, sid):  # noqa: ANN001, ANN202
        raise AssertionError("STT must never run on an enrollment sample")

    async def cont_audio(cont_id, pcm):  # noqa: ANN001, ANN202
        seen["cont"] = (cont_id, len(pcm))
        return LlmReply("Got it. Next, please say: two", "vp", fast=True,
                        expect_response=True, continuation="c2",
                        ask_capture="audio", ask_cue=True)  # fmt: skip

    async def tts(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return True

    monkeypatch.setattr(srv, "_call_stt", stt)
    monkeypatch.setattr(srv, "_call_llm_continue_audio", cont_audio)
    monkeypatch.setattr(srv, "_run_tts", tts)
    srv._pending_ask["k"] = {"id": "c1", "capture": "audio"}
    await srv._transcribe("k", "office", "s1", GOOD)
    assert seen["cont"] == ("c1", len(GOOD))
    # The chained audio ask re-registered and the cue-bearing floor hold armed.
    assert srv._pending_ask["k"]["id"] == "c2"
    assert srv._pending_ask["k"]["capture"] == "audio"
    ws = srv._nodes["k"].ws
    assert any('"cue": true' in m or '"cue":true' in m for m in ws.sent)  # type: ignore[attr-defined]


async def test_window_expiry_on_audio_ask_sends_empty_sample(monkeypatch):
    import asyncio

    srv = _server()
    seen = {}

    async def cont_audio(cont_id, pcm):  # noqa: ANN001, ANN202
        seen["cont"] = (cont_id, pcm)
        return LlmReply("I didn't catch that. Please say: one", "vp", fast=True,
                        expect_response=True, continuation="c9",
                        ask_capture="audio", ask_cue=True)  # fmt: skip

    async def tts(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return True

    monkeypatch.setattr(srv, "_call_llm_continue_audio", cont_audio)
    monkeypatch.setattr(srv, "_run_tts", tts)
    srv._pending_ask["k"] = {"id": "c1", "capture": "audio"}
    srv._followup_timed_out("k")
    await asyncio.sleep(0.05)
    assert seen["cont"] == ("c1", b"")  # empty sample = the retry path
    assert srv._pending_ask["k"]["id"] == "c9"


async def test_start_enrollment_sends_directive(monkeypatch):
    srv = _server()
    seen = {}

    async def llm(text, room, sid, speaker, node_id=None, identity=None):  # noqa: ANN001, ANN202
        seen["text"] = text
        return LlmReply("Okay, enrolling Bob. After the tone, please say: one", "vp",
                        fast=True, expect_response=True, continuation="c1",
                        ask_capture="audio", ask_cue=True)  # fmt: skip

    async def tts(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return True

    monkeypatch.setattr(srv, "_call_llm", llm)
    monkeypatch.setattr(srv, "_run_tts", tts)
    await srv.start_enrollment("k", "office", "", operator=True, person_id="bob")
    assert seen["text"] == "[[enroll]] operator=1 person=bob name="
    assert srv._pending_ask["k"]["id"] == "c1"
    assert srv._pending_ask["k"]["capture"] == "audio"


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


async def test_adopt_voice_action_dispatch(tmp_path, monkeypatch):
    from kenzy.server.people import PeopleStore

    srv = _server()
    p = tmp_path / "people.yaml"
    srv._people = PeopleStore(p)
    await srv._dispatch_actions(
        [{"type": "adopt_voice", "voiceprint": "alice", "display": "Alice", "person_id": None}],
        "k",
        "office",
    )
    owner = srv._people.by_voiceprint("alice")
    assert owner is not None and owner.name == "Alice"
