"""Spoken-name → person resolution (5.0.3 slice D).

The transcriber spells names phonetically and varies by speaker — the household
writes "Bobbie", Whisper hears "Bobby"; "Vicki" arrives as "Vikki"; a real
voice minted "Kinsey" for "Kenzy" in 5.0.2. An exact match kills the query
before the world model is even consulted, so this module resolves the *spoken*
rendering against the person records the server injects with every request.

Pure in the :mod:`kenzy.calibration` mould: no I/O, no request context — the
caller passes the people list in, and every judgment here is unit-testable.

Two rules are load-bearing (design: "Scoping the query tier", v5-aware-era):

* **This resolves the SUBJECT of a query, never the ASKER.** Asker identity
  comes from the voiceprint (``server/people.py``), and must keep doing so — if
  a spoken name could select the asker, mispronouncing a name becomes a way to
  read someone else's memory or lockbox. Nothing here may feed identity
  resolution; the module deliberately has no import path into it.
* **Ambiguity asks, never guesses.** Two household names inside the score
  margin come back as *ambiguous* with both candidates, so the skill can ask —
  a near-tie must never be settled by whichever record iterated first.

Matching is Jaro-Winkler (``rapidfuzz``), not plain ratio: it is
prefix-weighted and built for exactly this — short name strings whose endings
drift ("…ie"/"…y", Sara/Sarah) while their openings hold.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Fuzzy floor: a candidate below this is not a match at all. Tuned against the
#: variant classes in tests/test_names.py — high enough that non-name words
#: ("phone") and genuinely different names stay out, low enough that the
#: spelling drift STT actually produces gets in.
ACCEPT = 0.85

#: Two candidates whose scores land within this of each other are a tie the
#: resolver refuses to break. ("Jon" vs "John" when both exist.)
MARGIN = 0.04

_CLEAN_RE = re.compile(r"[^\w\s'-]+")


def _norm(text: str) -> str:
    return _CLEAN_RE.sub("", text).strip().casefold()


@dataclass(frozen=True)
class Resolution:
    """The three-way outcome: one person, a refused tie, or nothing.

    ``person`` is set only on a unique win. ``candidates`` carries the tied
    records (ambiguous ⇔ ``len(candidates) > 1``); a unique win repeats the
    winner there for uniform iteration. ``via`` records how the winner matched
    ("exact" | "alias" | "fuzzy") for logs and tests — never for behavior.
    """

    person: dict[str, Any] | None = None
    candidates: tuple[dict[str, Any], ...] = field(default=())
    via: str = ""

    @property
    def is_ambiguous(self) -> bool:
        return self.person is None and len(self.candidates) > 1

    @property
    def is_none(self) -> bool:
        return self.person is None and not self.candidates


def _labels(person: dict[str, Any]) -> list[tuple[str, str]]:
    """Every string this person may be called by, tagged with its kind."""
    out: list[tuple[str, str]] = []
    if person.get("name"):
        out.append((str(person["name"]), "exact"))
    if person.get("id"):
        out.append((str(person["id"]), "exact"))
    for alias in person.get("aliases") or []:
        if str(alias).strip():
            out.append((str(alias), "alias"))
    return out


def resolve_person(spoken: str, people: list[dict[str, Any]]) -> Resolution:
    """Resolve a spoken name against person records.

    Exact (name / id / alias, case-insensitive) beats fuzzy outright — the
    same rule HA device resolution learned in the field ("exact name beats
    group"): when someone says precisely what a record is called, near-misses
    on *other* records must not drag the answer into ambiguity. Only when
    nothing matches exactly does Jaro-Winkler arbitrate.
    """
    want = _norm(spoken)
    if not want:
        return Resolution()

    exact: list[tuple[dict[str, Any], str]] = []
    for p in people:
        for label, kind in _labels(p):
            if _norm(label) == want:
                exact.append((p, kind))
                break
    if len(exact) == 1:
        return Resolution(person=dict(exact[0][0]), candidates=(dict(exact[0][0]),),
                          via=exact[0][1])  # fmt: skip
    if len(exact) > 1:
        # Two records answer to the same string — a data problem the resolver
        # must surface as a question, never settle by iteration order.
        return Resolution(candidates=tuple(dict(p) for p, _ in exact))

    from rapidfuzz.distance import JaroWinkler

    scored: list[tuple[float, dict[str, Any]]] = []
    for p in people:
        best = 0.0
        for label, _ in _labels(p):
            best = max(best, JaroWinkler.normalized_similarity(_norm(label), want))
        if best >= ACCEPT:
            scored.append((best, p))
    if not scored:
        return Resolution()
    scored.sort(key=lambda t: t[0], reverse=True)
    top, winner = scored[0]
    tied = [p for s, p in scored if top - s < MARGIN]
    if len(tied) > 1:
        return Resolution(candidates=tuple(dict(p) for p in tied))
    return Resolution(person=dict(winner), candidates=(dict(winner),), via="fuzzy")
