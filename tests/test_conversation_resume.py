"""The on-demand 3-min resume cache — identity-gated, ephemeral, one-shot."""

from __future__ import annotations

from kenzy.server.server import TranscribingServer


def _srv() -> TranscribingServer:
    return TranscribingServer({"s2s": {"mode": "on_demand"}})


def test_resume_returns_context_for_the_same_person() -> None:
    s = _srv()
    s._s2s_stash("office", "Alex", [("what's the weather", "It's sunny."), ("thanks", "")])
    line = s._s2s_resume("office", "Alex")
    assert line and "Alex: what's the weather" in line and "You: It's sunny." in line


def test_resume_is_identity_gated_and_discards_on_mismatch() -> None:
    s = _srv()
    s._s2s_stash("office", "Alex", [("hi", "hello")])
    assert s._s2s_resume("office", "Bob") is None  # a different person never resumes
    assert s._s2s_resume("office", "Alex") is None  # ...and the slot is discarded


def test_unknown_speaker_does_not_resume() -> None:
    s = _srv()
    s._s2s_stash("office", "Alex", [("hi", "hello")])
    assert s._s2s_resume("office", "") is None  # no identity → no resume


def test_resume_consumes_the_slot() -> None:
    s = _srv()
    s._s2s_stash("office", "Alex", [("hi", "hello")])
    assert s._s2s_resume("office", "Alex")  # first works
    assert s._s2s_resume("office", "Alex") is None  # one-shot


def test_zero_window_disables_resume() -> None:
    s = _srv()
    s._s2s_resume_window_s = 0.0
    s._s2s_stash("office", "Alex", [("hi", "hello")])
    assert s._s2s_resume("office", "Alex") is None  # nothing kept warm


def test_expired_slot_does_not_resume() -> None:
    s = _srv()
    s._s2s_stash("office", "Alex", [("hi", "hello")])
    s._s2s_warm["office"]["expires"] = 0.0  # force expiry
    assert s._s2s_resume("office", "Alex") is None
