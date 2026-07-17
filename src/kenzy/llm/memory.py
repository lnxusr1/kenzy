"""Long-term memory — the fact ledger (v4 F2.2/F2.3).

One JSONL file (``data/memory/facts.jsonl`` in the config home), one JSON
object per line, loaded into memory at startup. **Deliberately not a
database**: household-scale facts (hundreds to low thousands) fit in memory
with room to spare, and a text ledger matches Kenzy's data culture
(people.yaml, schedules.json — tolerant loading, atomic rewrites, readable
and hand-fixable, rides the backup as plain text, no migration chain for
version-skipping upgrades). Each record carries a ``v`` field so the loader
can up-convert old shapes individually, forever. Storage stays an
implementation detail behind this module + the ``/memory`` HTTP contract.

**Tiers are the ACL** (F2.3): ``private`` — only the owner reads;
``personal-public`` — a fact *about* a person that others may read
(birthday); ``shared`` — household facts anyone recognized reads/writes
(trash day, the plumber). Owners are **person ids** (F1's stable join key,
never display names). The unrecognized tier gets no memory at all —
enforcement lives in the skill layer (``min_tier="recognized"``) and in the
caller passing an asker id; this store fails closed when the asker is empty.

Phase 1 writes are EXPLICIT ("remember …") — the auto-learning classifier is
F2.4 (phase 2). Consolidation (F2.7) will distill this ledger into a curated
markdown layer; the ledger stays the source of truth.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Private-touch marker (request-scoped). Set whenever a request's answer was
# built from PRIVATE/PERSONAL-tier facts (auto-injection or a memory skill);
# the LLM service then tags the room-history turn with the owner's person id so
# the echo of a private fact is never replayed to a different voice within the
# room history's TTL window (field finding: a stranger asking 30s after the
# owner heard the fact back verbatim — every tier wall held, history leaked).
# ---------------------------------------------------------------------------

_private_touch: contextvars.ContextVar[bool] = contextvars.ContextVar("kenzy_memory_private")


def begin_touch() -> None:
    """Reset the marker for a new request (called next to begin_request)."""
    _private_touch.set(False)


def mark_private_touch() -> None:
    """Record that private/personal-tier memory shaped this request's answer."""
    try:
        _private_touch.set(True)
    except LookupError:  # outside a request — nothing to mark
        pass


def private_touched() -> bool:
    try:
        return _private_touch.get()
    except LookupError:
        return False


def mark_if_sensitive(facts: list[Fact]) -> None:
    """Mark the request when any of these facts is private/personal-tier."""
    if any(f.tier in (TIER_PRIVATE, TIER_PERSONAL) for f in facts):
        mark_private_touch()


TIER_PRIVATE = "private"
TIER_PERSONAL = "personal-public"
TIER_SHARED = "shared"
TIERS = (TIER_PRIVATE, TIER_PERSONAL, TIER_SHARED)

_RECORD_V = 1  # bump when the record shape changes; the loader up-converts per record

_WORD_RE = re.compile(r"[a-z0-9']+")

#: Recall words too common to signal relevance on their own.
_STOPWORDS = frozenset(
    "a an and are be did do does for from had has have how i in is it me my of on or "
    "s t that the their them they this to was we what when where which who will you your".split()
)


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS}


@dataclass
class Fact:
    """One remembered fact. ``owner`` is the *author's* person id — provenance
    even for shared facts; ``tier`` decides who else can read it."""

    id: str
    owner: str
    tier: str
    text: str
    source: str = "voice"
    created: float = 0.0
    updated: float = 0.0
    confidence: float = 1.0
    expires: float | None = None  # decay policy lives in the schema now, logic in F2.7
    superseded_by: str | None = None
    v: int = _RECORD_V

    def visible_to(self, asker: str) -> bool:
        """The tier ACL. Empty asker ⇒ nothing (fail closed)."""
        if not asker:
            return False
        if self.tier == TIER_PRIVATE:
            return self.owner == asker
        return self.tier in (TIER_PERSONAL, TIER_SHARED)

    def erasable_by(self, asker: str) -> bool:
        """Forget rights: your own facts at any tier; shared facts are
        household-writable, so any recognized asker may erase them."""
        if not asker:
            return False
        return self.owner == asker or self.tier == TIER_SHARED

    def live(self, now: float) -> bool:
        if self.superseded_by:
            return False
        return self.expires is None or self.expires > now


class MemoryStore:
    """The JSONL-backed ledger. Single-writer by design (kenzy-llm owns the
    file; the dashboard goes through kenzy-llm's HTTP endpoints)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._facts: dict[str, Fact] = {}
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        self._facts = {}
        if not self._path.is_file():
            return
        bad = 0
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                fact = self._from_record(rec)
            except Exception:
                bad += 1  # a corrupt line must never take memory down — skip it
                continue
            if fact is not None:
                self._facts[fact.id] = fact
        if bad:
            log.warning("Memory: skipped %d unreadable line(s) in %s", bad, self._path)
        log.info("Memory: loaded %d fact(s) from %s", len(self._facts), self._path)

    @staticmethod
    def _from_record(rec: dict[str, Any]) -> Fact | None:
        """Tolerant, per-record up-conversion: missing fields default, unknown
        fields are dropped, any past ``v`` is readable. This is the whole
        upgrade story — no migration chain."""
        if not isinstance(rec, dict):
            return None
        text = str(rec.get("text", "")).strip()
        owner = str(rec.get("owner", "")).strip()
        if not text or not owner:
            return None
        tier = str(rec.get("tier", TIER_PRIVATE))
        if tier not in TIERS:
            tier = TIER_PRIVATE  # unknown tier degrades to the most restrictive
        expires = rec.get("expires")
        superseded = rec.get("superseded_by")
        return Fact(
            id=str(rec.get("id") or uuid.uuid4().hex[:12]),
            owner=owner,
            tier=tier,
            text=text,
            source=str(rec.get("source", "voice")),
            created=float(rec.get("created", 0.0)),
            updated=float(rec.get("updated") or rec.get("created", 0.0)),
            confidence=float(rec.get("confidence", 1.0)),
            expires=float(expires) if expires is not None else None,
            superseded_by=str(superseded) if superseded else None,
        )

    def _append(self, fact: Fact) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(fact), ensure_ascii=False) + "\n")

    def _rewrite(self) -> None:
        """Atomic full rewrite (the schedules.json pattern) after a mutation."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for fact in self._facts.values():
                f.write(json.dumps(asdict(fact), ensure_ascii=False) + "\n")
        os.replace(tmp, self._path)

    # -- the contract --------------------------------------------------------

    def remember(
        self,
        owner: str,
        text: str,
        *,
        tier: str = TIER_PRIVATE,
        source: str = "voice",
        confidence: float = 1.0,
    ) -> Fact:
        owner = owner.strip()
        text = " ".join(text.split())
        if not owner:
            raise ValueError("memory needs an owner (a recognized person)")
        if not text:
            raise ValueError("nothing to remember")
        if tier not in TIERS:
            raise ValueError(f"tier must be one of {TIERS}: {tier!r}")
        now = time.time()
        fact = Fact(
            id=uuid.uuid4().hex[:12],
            owner=owner,
            tier=tier,
            text=text,
            source=source,
            created=now,
            updated=now,
            confidence=confidence,
        )
        self._facts[fact.id] = fact
        self._append(fact)
        log.info("Memory: remembered %s (%s, owner=%s)", fact.id, tier, owner)
        return fact

    def recall(self, asker: str, query: str = "", *, limit: int = 5) -> list[Fact]:
        """Scope-first (the ACL), then relevance: keyword overlap with a recency
        tiebreak. An empty query returns the newest visible facts."""
        now = time.time()
        visible = [f for f in self._facts.values() if f.live(now) and f.visible_to(asker)]
        q = _tokens(query)
        if not q:
            if query.strip():
                return []  # words, but no content tokens — no signal, no matches
            visible.sort(key=lambda f: f.updated, reverse=True)
            return visible[: max(1, limit)]  # empty query = browse newest
        scored: list[tuple[float, Fact]] = []
        for f in visible:
            overlap = len(q & _tokens(f.text))
            if overlap:
                scored.append((overlap / len(q), f))
        scored.sort(key=lambda s: (s[0], s[1].updated), reverse=True)
        return [f for _score, f in scored[: max(1, limit)]]

    def forget(self, asker: str, fact_id: str) -> bool:
        """Erase a fact (hard delete — "forget" is a privacy promise)."""
        fact = self._facts.get(fact_id)
        if fact is None or not fact.erasable_by(asker):
            return False
        del self._facts[fact_id]
        self._rewrite()
        log.info("Memory: forgot %s (asker=%s)", fact_id, asker)
        return True

    def set_tier(self, asker: str, fact_id: str, tier: str) -> Fact | None:
        """Promote/demote — only the owner moves a fact between tiers."""
        if tier not in TIERS:
            raise ValueError(f"tier must be one of {TIERS}: {tier!r}")
        fact = self._facts.get(fact_id)
        if fact is None or fact.owner != asker:
            return None
        fact.tier = tier
        fact.updated = time.time()
        self._rewrite()
        log.info("Memory: %s → %s (asker=%s)", fact_id, tier, asker)
        return fact

    def export(self, person_id: str) -> list[Fact]:
        """Everything owned BY a person (any tier) — the F7.4 "what does Kenzy
        know about me" surface. Distinct from recall: this is ownership, not
        visibility."""
        return sorted(
            (f for f in self._facts.values() if f.owner == person_id),
            key=lambda f: f.created,
        )

    def all_facts(self) -> list[Fact]:
        """The whole ledger, newest first — the dashboard manager view (the
        dashboard is a credentialed admin surface; tiers gate *voices*)."""
        return sorted(self._facts.values(), key=lambda f: f.updated, reverse=True)

    def __len__(self) -> int:
        return len(self._facts)


# ---------------------------------------------------------------------------
# Short-term per-person context (F2.1) — rolling, cross-session, auto-expiring.
# ---------------------------------------------------------------------------
# Complements (doesn't replace) the per-ROOM 3-minute ConversationHistory: the
# room history gives turn-by-turn continuity within one exchange; this follows
# the PERSON across rooms and sessions on an hours scale ("the thing we talked
# about this morning"). In-memory only — anything worth keeping past a service
# restart gets distilled into the ledger by F2.7 (phase 2).


@dataclass
class _Exchange:
    ts: float
    user_text: str
    assistant_text: str


class ShortTermContext:
    MAX_EXCHANGES = 20
    MAX_AGE = 4 * 3600.0  # seconds — a morning's worth, not a transcript archive

    def __init__(self) -> None:
        self._by_person: dict[str, list[_Exchange]] = {}

    def _prune(self, person_id: str, now: float) -> list[_Exchange]:
        cutoff = now - self.MAX_AGE
        kept = [e for e in self._by_person.get(person_id, []) if e.ts >= cutoff]
        self._by_person[person_id] = kept[-self.MAX_EXCHANGES :]
        return self._by_person[person_id]

    def add(self, person_id: str, user_text: str, assistant_text: str) -> None:
        if not person_id:
            return  # unrecognized voices leave no trail (the F1.3 contract)
        now = time.time()
        self._by_person.setdefault(person_id, []).append(_Exchange(now, user_text, assistant_text))
        self._prune(person_id, now)  # cap holds after the append

    def recent(self, person_id: str, *, limit: int = 6) -> list[tuple[str, str]]:
        """The person's latest exchanges (oldest→newest), for context injection."""
        if not person_id:
            return []
        kept = self._prune(person_id, time.time())
        return [(e.user_text, e.assistant_text) for e in kept[-limit:]]


# ---------------------------------------------------------------------------
# Module-level store (initialized by kenzy-llm at startup; skills reach it here)
# ---------------------------------------------------------------------------

_store: MemoryStore | None = None


def init_store(path: Path) -> MemoryStore:
    global _store
    _store = MemoryStore(path)
    return _store


def store() -> MemoryStore | None:
    """The live store, or None when memory is disabled/uninitialized."""
    return _store
