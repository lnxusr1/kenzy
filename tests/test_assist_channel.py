"""Tests for the F3 assist-channel contract on skills: `request_channel()` /
`is_node_bound_refused()`, node-bound skill refusals, and the schedule skill's
roomless-set behavior on a nodeless channel (fast tier misses to the LLM; LLM
tools ask for a room)."""

from __future__ import annotations

import pytest

from kenzy.llm import skills as sk
from kenzy.llm.builtin_skills import calibrate, enroll, intercom, volume
from kenzy.llm.builtin_skills import schedule as sched


@pytest.fixture(autouse=True)
def _fresh_request_context():
    """Reset the request/action contextvars after each test so an "assist"
    channel (or a live actions list) set here can't leak into tests in other
    files that rely on running outside a request scope."""
    t_req = sk._request_ctx.set({})
    t_act = sk._actions.set([])
    yield
    sk._actions.reset(t_act)
    sk._request_ctx.reset(t_req)


def _ctx(channel: str = "voice", rooms=None):
    sk.begin_actions()
    sk.begin_request(
        {
            "channel": channel,
            "rooms": rooms or ["office", "kitchen"],
            "room_id": "office",
            "schedules": [],
        }
    )


# ---------------------------------------------------------------------------
# Channel helpers
# ---------------------------------------------------------------------------


def test_request_channel_defaults_to_voice():
    _ctx()
    assert sk.request_channel() == "voice"


def test_request_channel_reflects_assist():
    _ctx("assist")
    assert sk.request_channel() == "assist"


def test_channel_defaults_outside_request():
    # A fresh context with no begin_request must read as voice (backward compat
    # for callers that never send the field).
    sk.begin_request({})
    assert sk.request_channel() == "voice"


def test_is_node_bound_refused():
    _ctx()
    assert sk.is_node_bound_refused() is None
    _ctx("assist")
    refusal = sk.is_node_bound_refused()
    assert refusal and "room" in refusal


# ---------------------------------------------------------------------------
# Node-bound skills refuse on assist (and keep working on voice)
# ---------------------------------------------------------------------------


async def test_volume_refuses_on_assist():
    _ctx("assist")
    res = await volume.fast_volume("turn it up", "office", None)
    assert res.is_handled and res.text
    assert "room" in res.text
    assert sk.take_actions() == []  # nothing queued


async def test_volume_still_works_on_voice():
    _ctx()
    res = await volume.fast_volume("turn it up", "office", None)
    assert res.is_handled
    assert any(a["type"] == "set_volume" for a in sk.take_actions())


async def test_calibrate_refuses_on_assist():
    _ctx("assist")
    reply = await calibrate.calibrate_audio()
    assert "room" in reply
    res = await calibrate.fast_calibrate("calibrate", "office", None)
    assert res.is_handled and res.text and "room" in res.text
    assert sk.take_actions() == []


async def test_enroll_refuses_on_assist():
    _ctx("assist")
    reply = await enroll.enroll_speaker("Alice")
    assert "room" in reply or "dashboard" in reply
    assert sk.take_actions() == []


async def test_intercom_refuses_on_assist():
    _ctx("assist", rooms=["office", "kitchen"])
    reply = await intercom.connect_room("kitchen")
    assert "room speaker" in reply
    assert sk.take_actions() == []


# ---------------------------------------------------------------------------
# Schedule: roomless sets on assist
# ---------------------------------------------------------------------------


async def test_fast_timer_misses_on_assist():
    _ctx("assist")
    res = await sched.fast_schedule("set a timer for 10 minutes", None, None)
    assert not res.is_handled  # falls to the LLM tier, which can ask for a room
    assert sk.take_actions() == []


async def test_fast_reminder_with_room_works_on_assist():
    _ctx("assist")
    res = await sched.fast_schedule("remind me to stretch in 10 minutes in the kitchen", None, None)
    assert res.is_handled
    acts = sk.take_actions()
    assert acts and acts[0]["room"] == "kitchen"


async def test_fast_roomless_reminder_misses_on_assist():
    _ctx("assist")
    res = await sched.fast_schedule("remind me to stretch in 10 minutes", None, None)
    assert not res.is_handled
    assert sk.take_actions() == []


async def test_fast_deferred_misses_on_assist():
    _ctx("assist")
    res = await sched.fast_schedule("turn on the lights in 30 seconds", None, None)
    assert not res.is_handled
    assert sk.take_actions() == []


async def test_set_timer_tool_asks_for_room_on_assist():
    _ctx("assist")
    reply = await sched.set_timer(600)
    assert reply.startswith("Error:") and "room" in reply
    assert sk.take_actions() == []


async def test_set_timer_tool_with_room_works_on_assist():
    _ctx("assist")
    reply = await sched.set_timer(600, room="kitchen")
    assert reply.startswith("Scheduled:")
    acts = sk.take_actions()
    assert acts and acts[0]["room"] == "kitchen"


async def test_set_timer_roomless_still_fine_on_voice():
    _ctx()
    reply = await sched.set_timer(600)
    assert reply.startswith("Scheduled:")
    acts = sk.take_actions()
    assert acts and "room" not in acts[0]


async def test_set_alarm_tool_asks_for_room_on_assist():
    _ctx("assist")
    reply = await sched.set_alarm("07:00")
    assert reply.startswith("Error:") and "room" in reply
    assert sk.take_actions() == []


async def test_set_reminder_tool_asks_for_room_on_assist():
    _ctx("assist")
    reply = await sched.set_reminder("stretch", in_seconds=600)
    assert reply.startswith("Error:") and "room" in reply
    assert sk.take_actions() == []


async def test_run_later_tool_asks_for_room_on_assist():
    _ctx("assist")
    reply = await sched.run_later("turn on the lights", in_seconds=30)
    assert reply.startswith("Error:") and "room" in reply
    assert sk.take_actions() == []


async def test_run_later_with_room_works_on_assist():
    _ctx("assist")
    reply = await sched.run_later("turn on the lights", in_seconds=30, room="kitchen")
    assert reply.startswith("Scheduled:")
    acts = sk.take_actions()
    assert acts and acts[0]["room"] == "kitchen"
