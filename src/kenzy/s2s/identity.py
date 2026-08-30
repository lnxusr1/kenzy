"""Session identity — who has been heard, and who is speaking right now.

Spec: kenzy-design/app/s2s-design.md, decision 6. Identity evaluates on speech
segments and refines as more speech arrives, under one hard rule:
**monotonic-add** — this tracker can only ever NAME people, never drop them,
because absence of evidence is not evidence of absence (a head-turn or a noisy
patch must not downgrade someone mid-sentence). The boundary that keeps the
rule safe: a segment that *positively matches a different voice* is a
**speaker change** (``current`` moves), never a drop — so an unidentified
stranger cannot hide inside the monotonic rule.

The same segments feed two consumers with different semantics:

- The **action gate** binds to exactly one person — :attr:`current` — because
  tools, memory scope, and the lockbox are singular by necessity. A stranger's
  positive speech moves ``current`` to an unknown-tier speaker so their
  commands can never ride a prior speaker's tier.
- The **world model** is additive — :attr:`heard` is everyone identified in
  the session, each on their own recency clock, never replacing. This is the
  "everyone heard here" input the Layer 2 stranger-check consumes.

Inconclusive segments (no speaker verdict at all) are simply not reported to
this tracker — the no-op is by construction, not by branch.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from kenzy.s2s.gate import Speaker, _tier_rank


@dataclass(frozen=True)
class PersonHeard:
    """One identified voice in the session, on its own recency clock."""

    name: str
    tier: str
    confidence: float
    first_heard: float
    last_heard: float


class SessionIdentity:
    """The conversation's identity ledger — monotonic-add, current-speaker-bound."""

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock: Callable[[], float] = clock or time.monotonic
        self._heard: dict[str, PersonHeard] = {}
        self._current = Speaker("", "unknown")  # fail-closed until a voice resolves
        self._strangers = 0

    # ---------------------------------------------------------------- events

    def hear(self, name: str, tier: str, confidence: float = 0.0) -> Speaker:
        """A segment positively identified ``name``. Adds or refreshes them,
        and they become the current speaker.

        Refinement is monotonic within a person: tier only ever upgrades and
        confidence only ever rises — a later noisy segment cannot downgrade an
        identification already made this session.
        """
        now = self._clock()
        prev = self._heard.get(name)
        if prev is None:
            entry = PersonHeard(name, tier, confidence, first_heard=now, last_heard=now)
        else:
            entry = replace(
                prev,
                tier=tier if _tier_rank(tier) > _tier_rank(prev.tier) else prev.tier,
                confidence=max(prev.confidence, confidence),
                last_heard=now,
            )
        self._heard[name] = entry
        self._current = Speaker(entry.name, entry.tier)
        return self._current

    def hear_stranger(self) -> Speaker:
        """A segment was positively speech from a voice matching no one.

        That is a speaker CHANGE, not a drop: ``current`` moves to an
        unknown-tier speaker (the stranger's commands meet ``min_tier`` gates),
        while everyone already heard stays in the additive set.
        """
        self._strangers += 1
        self._current = Speaker("", "unknown")
        return self._current

    # ----------------------------------------------------------------- reads

    @property
    def current(self) -> Speaker:
        """The identity fact the action gate binds to — singular, by necessity."""
        return self._current

    @property
    def heard(self) -> tuple[PersonHeard, ...]:
        """Everyone identified this session (additive; presence evidence)."""
        return tuple(self._heard.values())

    @property
    def strangers_heard(self) -> int:
        """How many positively-unidentified voice segments occurred — the
        stranger-check's input ("is it safe to speak this here")."""
        return self._strangers


__all__ = ["PersonHeard", "SessionIdentity"]
