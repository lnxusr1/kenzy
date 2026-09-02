"""On-demand conversation entry — the "start a conversation" fast intent."""

from __future__ import annotations

from kenzy.llm.builtin_skills.conversation_control import fast_start_conversation
from kenzy.llm.skills import begin_actions, begin_request, take_actions


async def _run(utterance, *, mode="on_demand", room="office", no_aec=None):
    begin_request({"s2s_mode": mode, "no_aec_rooms": no_aec or []})
    begin_actions()
    res = await fast_start_conversation(utterance, room, None)
    return res, take_actions()


async def test_on_demand_escalates_silently_via_the_action():
    # Deliberately SILENT: the server action opens the conversation and speaks
    # the entry cue through the bridge (option-2 redesign, 2026-09-01) — the
    # classic reply says nothing and holds nothing.
    res, actions = await _run("start a conversation")
    assert res.status == "handled"
    assert res.text == ""  # silence by choice
    assert res.expect_response is False
    assert {"type": "start_conversation"} in actions


async def test_continue_and_lets_talk_also_escalate():
    # "continue OUR conversation" was live-missed 2026-09-01 (STT's natural
    # possessive wasn't in the article list) — pinned here with its siblings.
    for phrase in (
        "continue a conversation",
        "continue our conversation",
        "resume our chat",
        "continue this conversation",
        "let's talk",
        "let's have a chat",
    ):
        res, actions = await _run(phrase)
        assert res.status == "handled" and {"type": "start_conversation"} in actions, phrase


async def test_off_declines_with_a_reason_and_no_action():
    res, actions = await _run("start a conversation", mode="off")
    assert res.status == "handled" and res.expect_response is False
    assert "aren't turned on" in res.text.lower()
    assert actions == []  # nothing armed


async def test_always_mode_defers_to_the_model():
    res, actions = await _run("start a conversation", mode="always")
    assert res.status != "handled"  # a conversation already opens on every wake
    assert actions == []


async def test_half_duplex_room_declines_with_the_reason():
    res, actions = await _run("start a conversation", no_aec=["office"])
    assert res.status == "handled" and res.expect_response is False
    assert "can hear while i talk" in res.text.lower()
    assert actions == []  # never armed on a half-duplex node


async def test_unrelated_speech_misses():
    res, actions = await _run("let's talk about dinner")
    assert res.status != "handled"
    assert actions == []


def test_process_request_carries_s2s_mode():
    # Regression (caught live 2026-09-01): s2s_mode must be a declared
    # ProcessRequest field or pydantic silently drops it between the server and
    # the llm — so on_demand read as "off" and the skill declined. Same class as
    # the 5.0.0 occupancy field.
    from kenzy.llm.llm import ProcessRequest

    req = ProcessRequest(text="start a conversation", s2s_mode="on_demand")
    assert req.s2s_mode == "on_demand"
