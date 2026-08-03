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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: A person id is a filesystem/YAML-safe slug (the join key downstream keys off).
_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")

#: Sentinel for save_person's three-state ha_user (omitted vs set vs cleared).
_UNSET: Any = object()
UNSET: Any = _UNSET  # public alias for callers outside this module


def slugify(name: str) -> str:
    """The slug a display name maps to — used for person ids AND for the
    voiceprint key of a person-first enrollment (so a person's voice profile is
    named after their id, and renaming the person never touches the file)."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "person"


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
    #: 5.0.3 slice D: other things this person is called — nicknames the fuzzy
    #: layer can never reach ("Bud" for Robert), or a spelling the transcriber
    #: insists on. Mirrors curation.yaml's device aliases. Editable from the
    #: People page; save_person treats it three-state (omitted ⇒ preserved), so
    #: callers that don't know about aliases can never silently drop them.
    aliases: list[str] = field(default_factory=list)
    ha_user: str | None = None
    phone: str | None = None
    #: F7.4 "don't remember me": memory refuses writes AND reads for this
    #: person — still a recognized voice (device control, Q&A), no ledger.
    memory_opt_out: bool = False
    #: 4.1 capture mode: explicit (default — only "remember…" signals) |
    #: suggest (model asks first; real in 4.2 on ask()) | auto (model stores
    #: proactively, always says so in the reply).
    memory_capture: str = "explicit"
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
            als = [str(a).strip() for a in (rec.get("aliases") or []) if str(a).strip()]
            person = Person(
                id=str(pid),
                name=str(rec.get("name") or pid),
                voiceprints=vps,
                aliases=als,
                ha_user=(str(rec["ha_user"]).strip() or None) if rec.get("ha_user") else None,
                phone=(str(rec["phone"]).strip() or None) if rec.get("phone") else None,
                memory_opt_out=bool(rec.get("memory_opt_out", False)),
                memory_capture=(
                    str(rec.get("memory_capture"))
                    if rec.get("memory_capture") in ("explicit", "suggest", "auto")
                    else "explicit"
                ),
                settings=rec["settings"] if isinstance(rec.get("settings"), dict) else {},
            )
            self._people[person.id] = person
        self._reindex()
        log.info("Loaded %d person record(s) from %s", len(self._people), self._path)

    def _reindex(self) -> None:
        """Rebuild the voiceprint→person index from the current records."""
        self._by_voiceprint = {}
        for person in self._people.values():
            for vp in person.voiceprints:
                self._by_voiceprint[vp.lower()] = person

    def by_voiceprint(self, name: str) -> Person | None:
        return self._by_voiceprint.get(name.lower())

    def by_name(self, name: str) -> Person | None:
        """Case-insensitive match on display name or id — how a spoken
        "enroll me as Alice" finds the existing Alice record."""
        want = name.strip().lower()
        for person in self._people.values():
            if person.name.lower() == want or person.id.lower() == want:
                return person
        return None

    def by_ha_user(self, entity_id: str) -> Person | None:
        """Match on the HA person entity id (F3 Assist channel) — the stable
        id the operator maps in people.yaml (e.g. ``person.alex``)."""
        want = entity_id.strip().lower()
        if not want:
            return None
        for person in self._people.values():
            if (person.ha_user or "").lower() == want:
                return person
        return None

    def get(self, person_id: str) -> Person | None:
        return self._people.get(person_id)

    def all(self) -> list[Person]:
        return list(self._people.values())

    # -- write path (dashboard People panel) --------------------------------
    #
    # The panel edits only ``name`` + ``voiceprints``; ``ha_user``/``phone``/
    # ``settings`` are preserved across a save so hand-edited links (or future
    # channels) survive. Every mutation reindexes and rewrites the file, so the
    # in-process resolver sees the change immediately (same object the pipeline
    # holds) — no reload needed.

    def _new_id(self, name: str) -> str:
        """A unique slug id derived from the display name."""
        base = slugify(name)
        pid, n = base, 2
        while pid in self._people:
            pid, n = f"{base}_{n}", n + 1
        return pid

    @staticmethod
    def _clean_voiceprints(voiceprints: list[str]) -> list[str]:
        """Strip, drop blanks, de-dup case-insensitively while keeping order."""
        seen: set[str] = set()
        out: list[str] = []
        for raw in voiceprints:
            vp = str(raw).strip()
            if vp and vp.lower() not in seen:
                seen.add(vp.lower())
                out.append(vp)
        return out

    def save_person(
        self,
        *,
        id: str | None,
        name: str,
        voiceprints: list[str],
        aliases: list[str] | None = None,
        ha_user: str | None = _UNSET,
        memory_opt_out: bool | None = None,
        memory_capture: str | None = None,
    ) -> Person:
        """Create (blank/unknown ``id``) or update a person. A voiceprint assigned
        here is removed from any *other* person, so a voice belongs to exactly one
        person (assigning it elsewhere moves it). ``ha_user`` (the HA person
        entity id, F3) is three-state: omitted ⇒ preserved, a string ⇒ set,
        ""/None ⇒ cleared. ``aliases`` likewise: ``None`` ⇒ preserved (a caller
        that doesn't know the field exists can't drop it), a list ⇒ set
        (``[]`` clears)."""
        name = name.strip()
        if not name:
            raise ValueError("a name is required")
        vps = self._clean_voiceprints(voiceprints)

        pid = (id or "").strip()
        person = self._people.get(pid) if pid else None
        if person is None:
            pid = pid if pid and _ID_RE.fullmatch(pid) else self._new_id(name)
            person = Person(id=pid, name=name)
            self._people[pid] = person

        # A voiceprint can name only one person — steal it from any prior owner.
        claimed = {vp.lower() for vp in vps}
        for other in self._people.values():
            if other is person:
                continue
            other.voiceprints = [v for v in other.voiceprints if v.lower() not in claimed]

        person.name = name
        person.voiceprints = vps
        if aliases is not None:  # None ⇒ preserve; [] ⇒ clear (same cleaning as voices)
            person.aliases = self._clean_voiceprints(aliases)
        if ha_user is not _UNSET:  # omitted ⇒ preserve; explicit value ⇒ set/clear
            person.ha_user = str(ha_user).strip() or None if ha_user else None
        if memory_opt_out is not None:  # None ⇒ preserve
            person.memory_opt_out = bool(memory_opt_out)
        if memory_capture in ("explicit", "suggest", "auto"):  # None/invalid ⇒ preserve
            person.memory_capture = memory_capture
        self._reindex()
        self._write()
        return person

    def delete_person(self, person_id: str) -> bool:
        if person_id not in self._people:
            return False
        del self._people[person_id]
        self._reindex()
        self._write()
        return True

    def rename_voiceprint(self, old: str, new: str) -> bool:
        """A voiceprint was renamed in the speaker service — follow it in the
        owning person's record so the link doesn't silently break. Returns
        whether any record changed."""
        person = self.by_voiceprint(old)
        if person is None:
            return False
        person.voiceprints = [new if v.lower() == old.lower() else v for v in person.voiceprints]
        self._reindex()
        self._write()
        return True

    def remove_voiceprint(self, name: str) -> bool:
        """A voiceprint was deleted from the speaker service — drop it from its
        owner (the person record itself stays). Returns whether any record changed."""
        person = self.by_voiceprint(name)
        if person is None:
            return False
        person.voiceprints = [v for v in person.voiceprints if v.lower() != name.lower()]
        self._reindex()
        self._write()
        return True

    def _serialize(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for p in self._people.values():
            rec: dict[str, Any] = {"name": p.name}
            if p.voiceprints:
                rec["voiceprints"] = list(p.voiceprints)
            if p.aliases:
                rec["aliases"] = list(p.aliases)
            if p.ha_user:
                rec["ha_user"] = p.ha_user
            if p.phone:
                rec["phone"] = p.phone
            if p.memory_opt_out:
                rec["memory_opt_out"] = True
            if p.memory_capture != "explicit":
                rec["memory_capture"] = p.memory_capture
            if p.settings:
                rec["settings"] = p.settings
            out[p.id] = rec
        return {"people": out}

    def _write(self) -> None:
        import yaml

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            yaml.safe_dump(self._serialize(), default_flow_style=False, sort_keys=True)
        )


def resolve_assist_identity(store: PeopleStore, ha_user: str, *, unknown_name: str) -> Identity:
    """Resolve an HA Assist request (F3 — the second front door) to the SAME
    person records the voice channel uses, by HA person entity id.

    A matched person is RECOGNIZED — an HA session is a credentialed login, at
    least as strong an identity signal as a voiceprint (VERIFIED stays reserved
    for multi-signal corroboration). No mapping ⇒ UNKNOWN, fail closed: no
    memory, gated skills withheld — exactly like an unrecognized voice.
    """
    person = store.by_ha_user(ha_user)
    if person is None:
        return Identity(display=unknown_name, tier=TIER_UNKNOWN, confidence=0.0)
    return Identity(
        display=person.name,
        tier=TIER_RECOGNIZED,
        confidence=1.0,  # not a similarity score — a credentialed match
        person_id=person.id,
        name=person.name,
        ha_user=person.ha_user,
    )


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
