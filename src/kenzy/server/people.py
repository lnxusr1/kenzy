"""Identity core (v4 F1) — the person record + the front-of-pipeline resolver.

A **person** is one household member as a single record that joins the ways
Kenzy can recognize them: one or more voiceprint names (from the speaker
service), and — all optional — an HA user, a phone id, and per-person settings.
The record is the join key everything downstream (memory, delivery, privacy)
will hang off.

The **resolver** turns a channel-specific signal into a uniform
:class:`Identity`. Today the only channel is voice: the speaker service's
``(name, confidence)`` maps to a person. The design is channel-agnostic on
purpose — an HA-Assist request (F3) will resolve the *same* person by their HA
user id — so this stays the one place identity is decided.

**Kenzy runs without any of this.** With no ``people.yaml`` the resolver is a
passthrough: the raw speaker name is the identity, exactly as before. HA links
are optional fields, so a voiceprint-only household is fully supported
(``[[ha-optional-principle]]``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Confidence tiers a downstream gate consumes as a contract (F1.3). Today the
#: resolver emits UNKNOWN (no/low-confidence match) and RECOGNIZED (a voiceprint
#: match). VERIFIED — a voiceprint corroborated by another signal (the person's
#: phone home, their HA session) — is reserved for later; a voiceprint alone is
#: replayable, so anything that sends or spends will require VERIFIED.
TIER_UNKNOWN = "unknown"
TIER_RECOGNIZED = "recognized"
TIER_VERIFIED = "verified"  # reserved (corroboration not built yet)


@dataclass
class Person:
    """One household member. ``voiceprints`` are the speaker-service names that
    resolve to this person; everything but ``id``/``name`` is optional."""

    id: str
    name: str
    voiceprints: list[str] = field(default_factory=list)
    ha_user: str | None = None
    phone: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass
class Identity:
    """The resolver's uniform output for a request, from any channel.

    ``display`` is what flows to skills as the ``speaker`` (a person's name, or
    the raw voiceprint name when no record matches, or the unknown-speaker name)
    — so existing skills and the secure-action gate keep working unchanged.
    """

    display: str
    tier: str
    confidence: float
    person_id: str | None = None
    name: str | None = None
    ha_user: str | None = None

    @property
    def recognized(self) -> bool:
        return self.tier in (TIER_RECOGNIZED, TIER_VERIFIED)


class PeopleStore:
    """Loads + indexes ``data/people.yaml`` from the config home. Absent file ⇒
    an empty store, which makes the resolver a passthrough (no behavior change)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._people: dict[str, Person] = {}
        self._by_voiceprint: dict[str, Person] = {}
        self.reload()

    def reload(self) -> None:
        self._people = {}
        self._by_voiceprint = {}
        if not self._path.is_file():
            return
        import yaml  # type: ignore[import-untyped]

        try:
            data = yaml.safe_load(self._path.read_text()) or {}
        except Exception as exc:  # a malformed file must not take the pipeline down
            log.warning("Could not load %s: %s — treating as no people", self._path, exc)
            return
        raw = data.get("people") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            return
        for pid, rec in raw.items():
            if not isinstance(rec, dict):
                continue
            vps = [str(v).strip() for v in (rec.get("voiceprints") or []) if str(v).strip()]
            person = Person(
                id=str(pid),
                name=str(rec.get("name") or pid),
                voiceprints=vps,
                ha_user=(str(rec["ha_user"]).strip() or None) if rec.get("ha_user") else None,
                phone=(str(rec["phone"]).strip() or None) if rec.get("phone") else None,
                settings=rec["settings"] if isinstance(rec.get("settings"), dict) else {},
            )
            self._people[person.id] = person
            for vp in vps:
                self._by_voiceprint[vp.lower()] = person
        log.info("Loaded %d person record(s) from %s", len(self._people), self._path)

    def by_voiceprint(self, name: str) -> Person | None:
        return self._by_voiceprint.get(name.lower())

    def get(self, person_id: str) -> Person | None:
        return self._people.get(person_id)

    def all(self) -> list[Person]:
        return list(self._people.values())


def resolve_voice_identity(
    store: PeopleStore, speaker_name: str, confidence: float, *, unknown_name: str
) -> Identity:
    """Map the speaker service's ``(name, confidence)`` to an :class:`Identity`.

    ``speaker_name`` is already the unknown-speaker name when the score was below
    the service's threshold, so this trusts that gate for UNKNOWN vs RECOGNIZED.
    A recognized voice with no matching person record is still RECOGNIZED — the
    raw name passes through as the display, so nothing breaks before people are
    enrolled into records.
    """
    if not speaker_name or speaker_name.lower() == unknown_name.lower():
        return Identity(display=unknown_name, tier=TIER_UNKNOWN, confidence=confidence)
    person = store.by_voiceprint(speaker_name)
    if person is None:
        return Identity(
            display=speaker_name,
            tier=TIER_RECOGNIZED,
            confidence=confidence,
            name=speaker_name,
        )
    return Identity(
        display=person.name,
        tier=TIER_RECOGNIZED,
        confidence=confidence,
        person_id=person.id,
        name=person.name,
        ha_user=person.ha_user,
    )
