"""End-to-end tests for the timers/alarms/reminders feature: the schedule skill's
parsing + fast intents (kenzy-llm side) and the server's action dispatch, request
injection, and fire/ring delivery."""

from __future__ import annotations

import asyncio

from kenzy.llm import skills as sk
from kenzy.llm.builtin_skills import schedule as sched
from kenzy.server import tones
from kenzy.server.server import LlmReply, NodeSession, TranscribingServer


class _WS:
    pass


def _ctx(schedules=None, rooms=None):
    sk.begin_actions()
    sk.begin_request({"schedules": schedules or [], "rooms": rooms or [], "room_id": "office"})


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def test_parse_duration():
    assert sched.parse_duration("10 minutes") == 600
    assert sched.parse_duration("90 seconds") == 90
    assert sched.parse_duration("an hour") == 3600
    assert sched.parse_duration("an hour and a half") == 5400
    assert sched.parse_duration("one and a half minutes") == 90
    assert sched.parse_duration("half an hour") == 1800
    assert sched.parse_duration("1 hour and 20 minutes") == 4800
    assert sched.parse_duration("2 hours and 15 minutes") == 8100
    assert sched.parse_duration("ten minutes") == 600
    assert sched.parse_duration("my roast") is None
    assert sched.parse_duration("0 minutes") is None


def test_parse_clock():
    assert sched.parse_clock("6:30 pm") == "18:30"
    assert sched.parse_clock("6:30 a.m.") == "06:30"
    assert sched.parse_clock("noon") == "12:00"
    assert sched.parse_clock("midnight") == "00:00"
    assert sched.parse_clock("18:30") == "18:30"
    assert sched.parse_clock("7", wake=True) == "07:00"  # wake phrasing → morning
    assert sched.parse_clock("7") in ("07:00", "19:00")  # nearest future occurrence
    assert sched.parse_clock("sunrise") is None


def test_parse_days():
    assert sched.parse_days("") == []
    assert sched.parse_days("every weekday") == ["mon", "tue", "wed", "thu", "fri"]
    assert sched.parse_days("every day") == list(sched.DAY_NAMES)
    assert sched.parse_days("every saturday") == ["sat"]
    assert sched.parse_days("every so often") is None


# ---------------------------------------------------------------------------
# Fast intents
# ---------------------------------------------------------------------------


async def test_fast_set_timer():
    _ctx()
    r = await sched.fast_schedule("Set a timer for 10 minutes.", "office", None)
    assert r.is_handled and "10 minutes" in r.text
    assert sk.take_actions() == [
        {"type": "set_schedule", "kind": "timer", "seconds": 600, "label": ""}
    ]

    _ctx()
    r = await sched.fast_schedule("start a pizza timer for an hour and a half", "office", None)
    assert "Pizza timer" in r.text
    assert sk.take_actions()[0] == {
        "type": "set_schedule", "kind": "timer", "seconds": 5400, "label": "pizza",
    }  # fmt: skip

    _ctx()
    r = await sched.fast_schedule("set a 5 minute timer", "office", None)
    assert r.is_handled and sk.take_actions()[0]["seconds"] == 300

    _ctx()  # unparseable duration → miss (LLM's problem)
    r = await sched.fast_schedule("set a timer for my roast", "office", None)
    assert r.status == "miss" and sk.take_actions() == []


async def test_fast_timer_status_and_list():
    entries = [
        {"id": "t1", "kind": "timer", "label": "pizza", "seconds_left": 200, "at": "", "days": []},
        {"id": "t2", "kind": "timer", "label": "", "seconds_left": 90, "at": "", "days": []},
    ]
    _ctx(schedules=entries)
    r = await sched.fast_schedule("how much time is left", "office", None)
    assert "pizza timer" in r.text and "3 minutes and 20 seconds" in r.text

    _ctx(schedules=entries)
    r = await sched.fast_schedule("what timers do I have?", "office", None)
    assert r.is_handled and "You have 2" in r.text

    _ctx()
    r = await sched.fast_schedule("how much time is left on the timer", "office", None)
    assert "don't have" in r.text


async def test_fast_cancel_single_multiple_and_clarify():
    one = [{"id": "t1", "kind": "timer", "label": "", "seconds_left": 60, "at": "", "days": []}]
    _ctx(schedules=one)
    r = await sched.fast_schedule("cancel the timer", "office", None)
    assert r.is_handled
    assert sk.take_actions() == [{"type": "cancel_schedule", "ids": ["t1"]}]

    two = [
        {"id": "t1", "kind": "timer", "label": "pizza", "seconds_left": 60, "at": "", "days": []},
        {"id": "t2", "kind": "timer", "label": "eggs", "seconds_left": 30, "at": "", "days": []},
    ]
    _ctx(schedules=two)  # ambiguous singular → clarify (mic re-opens)
    r = await sched.fast_schedule("cancel the timer", "office", None)
    assert r.status == "clarify" and r.expect_response and sk.take_actions() == []

    _ctx(schedules=two)  # label picks one
    r = await sched.fast_schedule("cancel the eggs timer", "office", None)
    assert sk.take_actions() == [{"type": "cancel_schedule", "ids": ["t2"]}]

    _ctx(schedules=two)  # plural cancels all
    r = await sched.fast_schedule("cancel my timers", "office", None)
    assert sorted(sk.take_actions()[0]["ids"]) == ["t1", "t2"]


async def test_fast_set_alarm_with_days_and_room():
    _ctx(rooms=["office", "bedroom"])
    r = await sched.fast_schedule("wake me up at 7 every weekday", "office", None)
    assert "7:00 AM" in r.text and "every weekday" in r.text
    (a,) = sk.take_actions()
    assert a["kind"] == "alarm" and a["at"] == "07:00"
    assert a["days"] == ["mon", "tue", "wed", "thu", "fri"] and "room" not in a

    _ctx(rooms=["office", "bedroom"])
    r = await sched.fast_schedule("set an alarm for 6:30 pm in the bedroom", "office", None)
    (a,) = sk.take_actions()
    assert a["at"] == "18:30" and a["room"] == "bedroom"

    _ctx(rooms=["office"])  # unknown room → treated as unparsed → LLM
    r = await sched.fast_schedule("set an alarm for 6:30 pm in the garage", "office", None)
    assert r.status == "miss"


async def test_fast_reminders_both_orders():
    _ctx()
    r = await sched.fast_schedule("remind me to flip the bread in 20 minutes", "office", None)
    assert "20 minutes" in r.text
    (a,) = sk.take_actions()
    assert a == {
        "type": "set_schedule", "kind": "reminder", "label": "to flip the bread",
        "days": [], "seconds": 1200,
    }  # fmt: skip

    _ctx()
    r = await sched.fast_schedule("remind me at 6:15 pm to take out the trash", "office", None)
    (a,) = sk.take_actions()
    assert a["at"] == "18:15" and a["label"] == "to take out the trash"

    _ctx()  # duration-first order; a that-clause keeps its joiner too
    r = await sched.fast_schedule(
        "remind me in an hour and a half that the game is starting", "office", None
    )
    (a,) = sk.take_actions()
    assert a["seconds"] == 5400 and a["label"] == "that the game is starting"

    _ctx()  # fuzzy time → LLM
    r = await sched.fast_schedule("remind me to call mom sometime tomorrow", "office", None)
    assert r.status == "miss"


async def test_fast_deferred_command():
    _ctx()
    r = await sched.fast_schedule("turn on the office lights in 30 seconds", "office", None)
    assert r.is_handled and "30 seconds" in r.text and "turn on the office lights" in r.text
    (a,) = sk.take_actions()
    assert a == {
        "type": "set_schedule", "kind": "command", "label": "turn on the office lights",
        "seconds": 30, "days": [],
    }  # fmt: skip

    _ctx()  # a room phrase before the duration survives (greedy split on the LAST " in ")
    r = await sched.fast_schedule("turn on the lights in the bedroom in 5 minutes", "office", None)
    (a,) = sk.take_actions()
    assert a["label"] == "turn on the lights in the bedroom" and a["seconds"] == 300

    _ctx()  # clock form needs an unambiguous marker
    r = await sched.fast_schedule("lock the front door at 10:30 pm", "office", None)
    (a,) = sk.take_actions()
    assert a["kind"] == "command" and a["at"] == "22:30" and a["label"] == "lock the front door"


async def test_fast_deferred_command_guards():
    _ctx()  # "in the bedroom" is a room, not a duration → miss (HA intent's problem)
    r = await sched.fast_schedule("turn on the lights in the bedroom", "office", None)
    assert r.status == "miss" and sk.take_actions() == []

    _ctx()  # bare number after "at" is a level, not a time → miss (LLM disambiguates)
    r = await sched.fast_schedule("set the brightness at 5", "office", None)
    assert r.status == "miss" and sk.take_actions() == []

    _ctx()  # schedule phrasings still win over deferral (handler order)
    r = await sched.fast_schedule("set a timer for 10 minutes", "office", None)
    assert sk.take_actions()[0]["kind"] == "timer"

    _ctx()
    r = await sched.fast_schedule("remind me to flip the bread in 20 minutes", "office", None)
    assert sk.take_actions()[0]["kind"] == "reminder"


# ---------------------------------------------------------------------------
# LLM tools
# ---------------------------------------------------------------------------


async def test_skill_run_later():
    _ctx()
    out = await sched.run_later("turn on the porch light", in_seconds=90)
    assert out.startswith("Scheduled")
    (a,) = sk.take_actions()
    assert a == {
        "type": "set_schedule", "kind": "command", "label": "turn on the porch light",
        "days": [], "seconds": 90,
    }  # fmt: skip

    _ctx()
    assert "Error" in await sched.run_later("")  # empty command
    assert "Error" in await sched.run_later("x")  # neither in_seconds nor time
    assert "Error" in await sched.run_later("x", in_seconds=5, time="07:00")  # both
    assert "Error" in await sched.run_later("x", time="7 pm")  # not HH:MM
    assert sk.take_actions() == []


async def test_skill_set_alarm_and_reminder_validation():
    _ctx(rooms=["office"])
    out = await sched.set_alarm("07:00", days="weekdays")
    assert out.startswith("Scheduled") and sk.take_actions()[0]["days"] == [
        "mon", "tue", "wed", "thu", "fri",
    ]  # fmt: skip

    _ctx(rooms=["office"])
    assert "Error" in await sched.set_alarm("7 am")  # not HH:MM
    assert "Error" in await sched.set_alarm("07:00", room="garage")  # unknown room
    assert "Error" in await sched.set_reminder("x")  # neither in_seconds nor time
    assert "Error" in await sched.set_reminder("x", in_seconds=60, time="07:00")  # both
    assert sk.take_actions() == []


async def test_skill_list_and_cancel():
    entries = [{"id": "a1", "kind": "alarm", "label": "", "at": "07:00",
                "days": ["mon"], "seconds_left": 900, "room": "office"}]  # fmt: skip
    _ctx(schedules=entries)
    listing = await sched.list_schedules()
    assert "a1" in listing and "7:00 AM" in listing

    out = await sched.cancel_schedules(["a1", "bogus"])
    assert sk.take_actions() == [{"type": "cancel_schedule", "ids": ["a1"]}]
    assert "1" in out

    _ctx()
    assert "Error" in await sched.cancel_schedules(["bogus"])


# ---------------------------------------------------------------------------
# Server side: action dispatch, injection payload, firing
# ---------------------------------------------------------------------------


def _server(tmp_path, monkeypatch) -> TranscribingServer:
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = TranscribingServer({})
    s._nodes["n-off"] = NodeSession(ws=_WS(), node_id="n-off", room_id="office")
    s._nodes["n-bed"] = NodeSession(ws=_WS(), node_id="n-bed", room_id="bedroom")
    return s


async def test_dispatch_set_and_cancel_schedule(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    await s._dispatch_actions(
        [{"type": "set_schedule", "kind": "timer", "seconds": 600, "label": "pizza"}],
        "n-off",
        "office",
    )
    (entry,) = s._scheduler.entries()
    assert (entry.kind, entry.label, entry.node_id, entry.room) == (
        "timer", "pizza", "n-off", "office",
    )  # fmt: skip

    # Explicit room resolves to that room's node.
    await s._dispatch_actions(
        [
            {
                "type": "set_schedule",
                "kind": "alarm",
                "at": "07:00",
                "days": ["mon"],
                "room": "bedroom",
                "label": "",
            }
        ],  # fmt: skip
        "n-off",
        "office",
    )
    alarm = next(e for e in s._scheduler.entries() if e.kind == "alarm")
    assert alarm.node_id == "n-bed" and alarm.room == "bedroom"

    # Injection payload carries only the asking node's entries.
    payload = s._schedule_payload("n-off")
    assert [p["kind"] for p in payload] == ["timer"]
    assert payload[0]["seconds_left"] <= 600

    # Bad spec is logged, not raised (the reply was already spoken).
    await s._dispatch_actions(
        [{"type": "set_schedule", "kind": "chore", "seconds": 5}], "n-off", "office"
    )
    assert len(s._scheduler.entries()) == 2

    await s._dispatch_actions(
        [{"type": "cancel_schedule", "ids": [entry.id, alarm.id]}], "n-off", "office"
    )
    assert s._scheduler.entries() == []


async def test_dispatch_stores_speaker_and_command_replays_it(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    await s._dispatch_actions(
        [
            {
                "type": "set_schedule",
                "kind": "command",
                "label": "turn on the lights",
                "seconds": 60,
                "days": [],
            }
        ],  # fmt: skip
        "n-off",
        "office",
        "alice",
    )
    (entry,) = s._scheduler.entries()
    assert entry.kind == "command" and entry.speaker == "alice"
    # Survives a reload.
    from kenzy.server.scheduler import Scheduler

    async def _noop(e):
        pass

    s2 = Scheduler(tmp_path / "data" / "schedules.json", _noop)
    assert s2.entries()[0].speaker == "alice"

    # Fire: the command replays through the pipeline with the stored identity.
    llm_calls: list[tuple] = []
    tts_calls: list[str] = []
    dispatched: list[tuple] = []

    async def fake_llm(text, room, sid, speaker=None, node_id=None):
        llm_calls.append((text, room, speaker, node_id))
        return LlmReply(
            "The lights are on.", "vp", actions=[{"type": "set_volume", "level": 10}], fast=True
        )

    async def fake_tts(node_id, room, sid, text, vp):
        tts_calls.append(text)

    async def fake_dispatch(actions, node_id, room, speaker=None):
        dispatched.append((actions, speaker))

    monkeypatch.setattr(s, "_call_llm", fake_llm)
    monkeypatch.setattr(s, "_run_tts", fake_tts)
    monkeypatch.setattr(s, "_dispatch_actions", fake_dispatch)
    s._llm_url = "http://x/process"

    await s._fire_schedule(entry)
    await asyncio.sleep(0)
    assert llm_calls == [("turn on the lights", "office", "alice", "n-off")]
    assert tts_calls == ["The lights are on."]
    assert dispatched == [([{"type": "set_volume", "level": 10}], "alice")]


def test_scheduler_command_validation(tmp_path):
    from kenzy.server.scheduler import Scheduler

    async def _noop(e):
        pass

    s = Scheduler(tmp_path / "s.json", _noop)
    import pytest

    with pytest.raises(ValueError):
        s.add("command", "n", "r", seconds=60)  # no command text
    with pytest.raises(ValueError):  # recurring command = automation → HA's job
        s.add("command", "n", "r", label="turn on the light", at="08:00", days=["daily"])
    e = s.add("command", "n", "r", label="turn on the light", seconds=60, speaker="bob")
    assert e.kind == "command" and e.speaker == "bob"


async def test_fire_timer_prepends_tone_and_voice(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    streamed: list[tuple[str, bytes]] = []

    async def fake_synth(text: str, vp: str) -> bytes:
        return b"VOICE:" + text.encode()

    async def fake_stream(node_id: str, pcm: bytes) -> None:
        streamed.append((node_id, pcm))

    monkeypatch.setattr(s, "_synthesize", fake_synth)
    monkeypatch.setattr(s, "_stream_pcm", fake_stream)

    t = s._scheduler.add("timer", "n-off", "office", label="pizza", seconds=60)
    await s._fire_schedule(t)
    await asyncio.sleep(0)  # let the delivery task run
    ((node_id, pcm),) = streamed
    tone = tones.load_tone("timer.wav")
    assert node_id == "n-off" and tone
    assert pcm.startswith(tone) and pcm.endswith(b"VOICE:Your pizza timer is done.")


async def test_fire_reminder_speaks_expected_sentence(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    said: list[str] = []

    async def fake_say(node_id: str, room: str, text: str) -> None:
        said.append(text)

    monkeypatch.setattr(s, "_say", fake_say)  # reminders are voice-only (no tone)
    r = s._scheduler.add("reminder", "n-off", "office", label="to take the dog out", seconds=60)
    await s._fire_schedule(r)
    # A label stored without the joiner (e.g. from the LLM tool) gains "to".
    r2 = s._scheduler.add("reminder", "n-off", "office", label="call mom", seconds=60)
    await s._fire_schedule(r2)
    await asyncio.sleep(0)
    assert said == [
        "You asked me to remind you to take the dog out.",
        "You asked me to remind you to call mom.",
    ]


async def test_fire_timer_without_tone_uses_plain_say(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    s._node_defaults = {"sound_timer": ""}  # tone disabled → voice only
    said: list[str] = []

    async def fake_say(node_id: str, room: str, text: str) -> None:
        said.append(text)

    monkeypatch.setattr(s, "_say", fake_say)
    t = s._scheduler.add("timer", "n-off", "office", seconds=60)
    await s._fire_schedule(t)
    await asyncio.sleep(0)
    assert said == ["Your timer is done."]


async def test_alarm_tone_plays_even_when_tts_is_down(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    streamed: list[bytes] = []

    async def fake_synth(text: str, vp: str) -> None:
        return None  # TTS service unreachable

    async def fake_stream(node_id: str, pcm: bytes) -> None:
        streamed.append(pcm)

    monkeypatch.setattr(s, "_synthesize", fake_synth)
    monkeypatch.setattr(s, "_stream_pcm", fake_stream)
    await s._deliver_schedule("n-off", "office", "It's 7:00 AM. This is your alarm.", "alarm")
    assert streamed == [tones.load_tone("alarm.wav")]  # the tone alone still rings


async def test_fire_falls_back_to_room_resolution(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    s._node_defaults = {"sound_timer": ""}
    said: list[str] = []

    async def fake_say(node_id: str, room: str, text: str) -> None:
        said.append(node_id)

    monkeypatch.setattr(s, "_say", fake_say)
    # Entry set on a node that has since been replaced by another in the same room.
    e = s._scheduler.add("timer", "n-gone", "bedroom", seconds=60)
    await s._fire_schedule(e)
    await asyncio.sleep(0)
    assert said == ["n-bed"]


async def test_alarm_rings_until_acknowledged(tmp_path, monkeypatch):
    s = _server(tmp_path, monkeypatch)
    said: list[str] = []

    async def fake_deliver(node_id: str, room: str, text: str, kind: str) -> None:
        said.append(text)

    monkeypatch.setattr(s, "_deliver_schedule", fake_deliver)
    s._alarm_ring_interval = 0.01  # ring behavior is now instance config, not a module constant

    a = s._scheduler.add("alarm", "n-off", "office", at="07:00")
    await s._fire_schedule(a)
    await asyncio.sleep(0.05)
    assert len(said) >= 2 and "7:00 AM" in said[0]  # repeated, not a one-shot

    # Acknowledge the way a real node does. Saying the wake word while the alarm
    # is playing does NOT send a `wakeword` frame — the node stops its own audio
    # and opens a fresh session, so `audio_start` (on_session_start) is all the
    # server ever sees. Drive that, not the private helper: calling
    # _stop_ringing() directly passed for months while the feature was dead.
    await s.on_session_start(s._nodes["n-off"])
    await asyncio.sleep(0.03)
    count = len(said)
    await asyncio.sleep(0.03)
    assert len(said) == count  # ringing stopped
    assert "n-off" not in s._ring_tasks


async def test_alarm_ack_via_wakeword_frame_still_works(tmp_path, monkeypatch):
    """The other entry point: a wake word arriving mid-capture."""
    s = _server(tmp_path, monkeypatch)
    said: list[str] = []

    async def fake_deliver(node_id: str, room: str, text: str, kind: str) -> None:
        said.append(text)

    async def fake_stop_node(node_id: str) -> bool:
        return True

    monkeypatch.setattr(s, "_deliver_schedule", fake_deliver)
    monkeypatch.setattr(s, "stop_node", fake_stop_node)
    s._alarm_ring_interval = 0.01

    a = s._scheduler.add("alarm", "n-off", "office", at="07:00")
    await s._fire_schedule(a)
    await asyncio.sleep(0.05)
    assert len(said) >= 2

    await s.on_wakeword(s._nodes["n-off"], "hey_kenzy", 0.9)
    await asyncio.sleep(0.03)
    count = len(said)
    await asyncio.sleep(0.03)
    assert len(said) == count
    assert "n-off" not in s._ring_tasks


async def test_alarm_delivery_does_not_cancel_its_own_ring_loop(tmp_path, monkeypatch):
    """Guard: acknowledging on session-start must not be self-triggering.

    Alarm delivery streams PCM and never opens a capture session, so nothing in
    the ring loop can reach on_session_start. If a future change makes an alarm
    hold the floor, this test fails rather than the alarm silently ringing once.
    """
    s = _server(tmp_path, monkeypatch)
    said: list[str] = []

    async def fake_deliver(node_id: str, room: str, text: str, kind: str) -> None:
        said.append(text)

    monkeypatch.setattr(s, "_deliver_schedule", fake_deliver)
    s._alarm_ring_interval = 0.01

    a = s._scheduler.add("alarm", "n-off", "office", at="07:00")
    await s._fire_schedule(a)
    await asyncio.sleep(0.06)
    assert len(said) >= 3, "the ring loop stopped on its own"
    assert "n-off" in s._ring_tasks


# ---------------------------------------------------------------------------
# Tones (WAV → 24 kHz mono int16)
# ---------------------------------------------------------------------------


def _write_wav(path, channels: int, rate: int, seconds: float) -> None:
    import wave

    n = int(rate * seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x10\x00" * (n * channels))


def test_load_tone_bundled_and_disabled():
    for name in ("timer.wav", "alarm.wav"):
        pcm = tones.load_tone(name)
        assert pcm and len(pcm) % 2 == 0
    assert tones.load_tone("") is None
    assert tones.load_tone(None) is None
    assert tones.load_tone("no-such-file.wav") is None  # logged, not raised


def test_load_tone_converts_rate_and_channels(tmp_path):
    stereo48 = tmp_path / "stereo48.wav"
    _write_wav(stereo48, channels=2, rate=48000, seconds=0.5)
    pcm = tones.load_tone(str(stereo48))
    assert pcm is not None
    assert abs(len(pcm) // 2 - 12000) <= 2  # 0.5 s at 24 kHz mono

    mono44 = tmp_path / "mono44.wav"
    _write_wav(mono44, channels=1, rate=44100, seconds=1.0)
    pcm = tones.load_tone(str(mono44))
    assert pcm is not None
    assert abs(len(pcm) // 2 - 24000) <= 2


def test_alarm_ring_config_overrides_defaults(tmp_path, monkeypatch):
    """Promoted A-list constants: alarm ring repeats/interval read from config."""
    from kenzy.server.server import TranscribingServer

    s = TranscribingServer(
        {"alarm": {"ring_repeats": 3, "ring_interval": 10}, "dialog": {"max_turns": 9}}
    )
    assert s._alarm_ring_repeats == 3
    assert s._alarm_ring_interval == 10.0
    assert s._max_followup_turns == 9


def test_alarm_ring_defaults_when_unset():
    from kenzy.server.server import (
        _ALARM_RING_INTERVAL_S,
        _ALARM_RING_REPEATS,
        _MAX_FOLLOWUP_TURNS,
        TranscribingServer,
    )

    s = TranscribingServer({})
    assert s._alarm_ring_repeats == _ALARM_RING_REPEATS
    assert s._alarm_ring_interval == _ALARM_RING_INTERVAL_S
    assert s._max_followup_turns == _MAX_FOLLOWUP_TURNS
