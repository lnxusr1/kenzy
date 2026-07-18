"""Tests for the LLM→server actions channel and the voice broadcast (announce)."""

from __future__ import annotations

from kenzy.llm import skills as sk
from kenzy.llm.builtin_skills.announce import announce
from kenzy.server.server import NodeSession, TranscribingServer


class _WS:
    pass


# ---------------------------------------------------------------------------
# Skill side: the announce skill queues a server-side action
# ---------------------------------------------------------------------------


async def test_announce_skill_queues_action():
    sk.begin_actions()
    out = await announce("Dinner is ready")
    assert "every room" in out.lower()
    assert sk.take_actions() == [{"type": "announce", "text": "Dinner is ready", "rooms": None}]


async def test_announce_skill_parses_rooms():
    sk.begin_actions()
    await announce("Movie time", "living room, den")
    assert sk.take_actions()[0]["rooms"] == ["living room", "den"]


async def test_announce_skill_rejects_empty():
    sk.begin_actions()
    out = await announce("   ")
    assert "no announcement" in out.lower()
    assert sk.take_actions() == []


def test_add_action_outside_scope_is_noop():
    # No begin_actions() in this (sync) context → no error, nothing collected.
    sk.add_action({"type": "announce", "text": "x", "rooms": None})
    assert sk.take_actions() == []


# ---------------------------------------------------------------------------
# Server side: actions are actuated, room names resolved to node_ids
# ---------------------------------------------------------------------------


async def test_dispatch_announce_resolves_rooms_and_excludes_source(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = TranscribingServer({})
    s._nodes["n-src"] = NodeSession(ws=_WS(), node_id="n-src", room_id="office")
    s._nodes["n-kit"] = NodeSession(ws=_WS(), node_id="n-kit", room_id="kitchen")
    s._nodes["n-den"] = NodeSession(ws=_WS(), node_id="n-den", room_id="den")

    calls: list[tuple[str, list[str] | None]] = []

    async def fake_announce(text: str, rooms: list[str] | None = None) -> int:
        calls.append((text, rooms))
        return len(rooms or [])

    monkeypatch.setattr(s, "announce", fake_announce)

    # Named target → resolved to that room's node_id (case-insensitive).
    await s._dispatch_actions(
        [{"type": "announce", "text": "hi", "rooms": ["Kitchen"]}], "n-src", "office"
    )
    assert calls == [("hi", ["n-kit"])]

    # No rooms → everyone except the asking node.
    calls.clear()
    await s._dispatch_actions(
        [{"type": "announce", "text": "all", "rooms": None}], "n-src", "office"
    )
    assert calls[0][0] == "all"
    assert set(calls[0][1] or []) == {"n-kit", "n-den"}


async def test_dispatch_ignores_unknown_action(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = TranscribingServer({})
    s._nodes["n-1"] = NodeSession(ws=_WS(), node_id="n-1", room_id="office")
    # Should not raise on an unrecognized action type.
    await s._dispatch_actions([{"type": "teleport", "to": "moon"}], "n-1", "office")


async def test_dispatch_skips_node_bound_actions_without_source_node(tmp_path, monkeypatch):
    # The assist lane (F3) dispatches with source_node_id="" — node-bound
    # actions must be skipped, not crash (the skills refuse them already;
    # this is the server-side backstop for custom skills).
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = TranscribingServer({})
    called: list[str] = []

    async def boom(*a, **k):
        called.append("x")

    for name in ("set_node_volume", "set_node_muted", "start_intercom",
                 "start_enrollment", "start_calibration"):
        monkeypatch.setattr(s, name, boom)
    await s._dispatch_actions(
        [
            {"type": "set_volume", "delta": 10},
            {"type": "set_volume", "muted": True},
            {"type": "start_intercom", "room": "office"},
            {"type": "start_enrollment", "name": "Bob"},
            {"type": "start_calibration"},
            {"type": "set_schedule", "kind": "timer", "seconds": 60, "label": ""},
        ],
        "",
        "assist:john",
    )
    assert called == []


async def test_dispatch_room_targeted_schedule_works_without_source_node(tmp_path, monkeypatch):
    # An explicit-room schedule from the assist lane must still be stored.
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    s = TranscribingServer({})
    s._nodes["n-kit"] = NodeSession(ws=_WS(), node_id="n-kit", room_id="kitchen")
    stored: list[dict] = []
    monkeypatch.setattr(
        s, "_action_set_schedule", lambda a, nid, room, spk: stored.append(a)
    )
    await s._dispatch_actions(
        [{"type": "set_schedule", "kind": "timer", "seconds": 60, "label": "", "room": "kitchen"}],
        "",
        "assist:john",
    )
    assert len(stored) == 1 and stored[0]["room"] == "kitchen"
