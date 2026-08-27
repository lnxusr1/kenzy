"""Session identity tests — decision 6 pinned: monotonic-add, speaker-change
vs drop, current-speaker binding (spec: kenzy-design/app/s2s-design.md)."""

from __future__ import annotations

from kenzy.s2s.identity import SessionIdentity


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_fail_closed_before_any_voice_resolves() -> None:
    ident = SessionIdentity(clock=_Clock())
    assert ident.current.name == "" and ident.current.tier == "unknown"
    assert ident.heard == ()


def test_hear_adds_and_binds_current() -> None:
    ident = SessionIdentity(clock=_Clock())
    speaker = ident.hear("Alex", "recognized", 0.8)
    assert speaker.name == "Alex" and speaker.tier == "recognized"
    assert ident.current == speaker
    assert [p.name for p in ident.heard] == ["Alex"]


def test_second_voice_is_a_speaker_change_never_a_drop() -> None:
    ident = SessionIdentity(clock=_Clock())
    ident.hear("Alex", "recognized", 0.8)
    ident.hear("Alice", "recognized", 0.7)
    assert ident.current.name == "Alice"  # the action gate binds to the current speaker
    assert sorted(p.name for p in ident.heard) == ["Alex", "Alice"]  # additive, both stay


def test_refinement_is_monotonic_within_a_person() -> None:
    ident = SessionIdentity(clock=_Clock())
    ident.hear("Alex", "recognized", 0.8)
    ident.hear("Alex", "unknown", 0.3)  # a noisy later segment must not downgrade
    (alex,) = ident.heard
    assert alex.tier == "recognized" and alex.confidence == 0.8
    assert ident.current.tier == "recognized"


def test_stranger_moves_current_to_unknown_without_dropping_anyone() -> None:
    ident = SessionIdentity(clock=_Clock())
    ident.hear("Alex", "recognized", 0.8)
    stranger = ident.hear_stranger()
    # a stranger's commands must never ride the prior speaker's tier
    assert stranger.tier == "unknown" and ident.current.tier == "unknown"
    assert [p.name for p in ident.heard] == ["Alex"]  # the set is untouched
    assert ident.strangers_heard == 1


def test_recency_clocks_are_per_person() -> None:
    clock = _Clock()
    ident = SessionIdentity(clock=clock)
    ident.hear("Alex", "recognized", 0.8)
    clock.t += 30.0
    ident.hear("Alex", "recognized", 0.9)
    (alex,) = ident.heard
    assert alex.first_heard == 1000.0 and alex.last_heard == 1030.0
    assert alex.confidence == 0.9  # confidence refines upward
