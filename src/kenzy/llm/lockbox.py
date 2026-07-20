"""The lockbox — encrypted at-rest storage for secret facts (v4.1 F-lockbox).

Secrets get different MECHANICS than tiers, not just a stricter ACL:

- **Never enter a model.** Not context injection, not consolidation, not the
  LLM recall tool. Retrieval is the deterministic fast path only — verbatim,
  owner-only.
- **Encrypted at rest.** ``data/memory/lockbox.enc`` (Fernet), key in
  ``data/memory/lockbox.key`` (chmod 600). The key is DATA, not host
  material: it rides backups by default (founder call 2026-07-18 — a
  backup's job is to restore everything; untick "Include the lockbox key"
  for a shareable ciphertext-only archive). A stolen disk that includes the
  key still loses; that's appliance reality without a boot passphrase, and
  the docs say so.
- **Whole-file rewrite** per mutation (the schedules.json/facts.jsonl
  pattern): decrypt → mutate → encrypt → atomic replace. Household-scale
  secret counts make this free.

The ``cryptography`` package provides Fernet (in the ``llm`` extra). Absent
(a pip-upgraded older venv), the lockbox degrades honestly: disabled, one
clear log line, and callers see ``available() == False`` — the same
feature-chip pattern as Wyoming/Kokoro.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kenzy.redact import SECRET_WORDS_RE

log = logging.getLogger(__name__)

#: Strong secret signals — the ONE definition lives in kenzy.redact (shared
#: with every service's log redaction); re-exported here for the classifier
#: and the fast paths.


_LABEL_STOPWORDS = {"the", "my", "our", "his", "her", "their", "your", "that", "this", "for"}


def derive_label(text: str) -> str:
    """A speakable handle for a vaulted secret: the secret-word phrase plus
    its qualifier when present ("shed key code", "wifi password" — the
    qualifier is what keeps two codes' keys distinct), else the first few
    content words."""
    m = SECRET_WORDS_RE.search(text)
    if m:
        pre = [
            w.lower()
            for w in re.findall(r"[a-zA-Z]+", text[: m.start()])
            if len(w) > 2 and w.lower() not in _LABEL_STOPWORDS
        ]
        return " ".join(pre[-1:] + [m.group(0).lower()])
    # Fallback labels must never CONTAIN the value: the label reaches the
    # model (key index), the masked dashboard list, and logs. When no value
    # splits off, the whole text IS the value ("swordfish", "4412",
    # "correct horse battery staple") — only the opaque label is safe.
    value = extract_value(text)
    if not value:
        return "secret"
    value_tokens = {w.lower() for w in re.findall(r"[a-zA-Z0-9]+", value)}
    toks = [
        w
        for w in re.findall(r"[a-zA-Z]+", text)
        if len(w) > 2 and w.lower() not in value_tokens and w.lower() not in _LABEL_STOPWORDS
    ][:3]
    return " ".join(toks).lower() or "secret"


#: Verb forms that introduce a value ("is", "has been updated to", …).
#: Longest alternatives first; greedy prefix keeps the LAST verb so "the code
#: for the shed is 8642" splits at the right place.
_VALUE_VERB_RE = re.compile(
    r"^.*\b(?:is now|is|are|was|were|equals|"
    r"(?:changed|updated|set|reset|switched)(?:\s+back)?\s+to|now)\b[:\s]+(.+?)[.!?]?$",
    re.IGNORECASE,
)
#: Fallback: a payload-shaped chunk (digit/code run, or a mixed letter-digit
#: blob) — the LAST one in the sentence is the value in verbless phrasings
#: ("door code 4593").
_PAYLOAD_SHAPE_RE = re.compile(r"(?:\d[\s,.\-]?){3,}\d|\d+|\b(?=\w*\d)(?=\w*[a-zA-Z])\w{4,}\b")


def extract_value(text: str) -> str:
    """The VALUE half of the key/value pair, pulled from a spoken sentence:
    "the shed key code is 8642" → "8642", "the door code has been updated to
    4593" → "4593". Verb split first; else the last payload-shaped chunk. No
    extractable payload ⇒ empty (callers fall back to the verbatim text)."""
    flat = " ".join(text.split())
    m = _VALUE_VERB_RE.match(flat)
    if m:
        return m.group(1).strip()
    hits = list(_PAYLOAD_SHAPE_RE.finditer(flat))
    if hits:
        return hits[-1].group(0).strip()
    return ""


def slug(label: str) -> str:
    """A machine key for a label: "shed key code" → ``shed_key_code``."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", label.lower())).strip("_") or "secret"


def _fernet_cls() -> Any | None:
    try:
        from cryptography.fernet import Fernet  # type: ignore[import-untyped]

        return Fernet
    except ImportError:
        return None


def available() -> bool:
    """Whether the crypto dependency exists (feature-chip signal)."""
    return _fernet_cls() is not None


@dataclass
class Secret:
    """One vaulted secret — a key/value pair. ``label`` is the speakable
    handle ("shed key code"; its :func:`slug` is the key the LLM sees in the
    identification block); ``value`` is the extracted payload ("8642") used
    for placeholder substitution; ``text`` keeps the verbatim sentence for
    fast-path read-back and the dashboard's Reveal."""

    id: str
    owner: str  # person id (F1) — never a display name
    text: str
    label: str = ""
    value: str = ""
    created: float = field(default_factory=time.time)
    source: str = "voice"

    @property
    def payload(self) -> str:
        """What a placeholder substitutes to: the stored value, extracted on
        the fly for pre-4.1-k/v entries, else the whole verbatim text."""
        return self.value or extract_value(self.text) or self.text


class LockboxStore:
    """Owner-scoped encrypted secret store. All reads/writes decrypt/encrypt
    the whole file; mutations rewrite atomically."""

    def __init__(self, enc_path: Path, key_path: Path | None = None) -> None:
        self._path = Path(enc_path)
        self._key_path = Path(key_path) if key_path else self._path.with_suffix(".key")
        fernet = _fernet_cls()
        if fernet is None:
            log.error(
                "The lockbox needs the 'cryptography' package — run: pip install "
                "cryptography (or the service's Upgrade button). Lockbox DISABLED."
            )
            self._fernet = None
            return
        self._fernet = fernet(self._load_or_create_key())

    # -- key handling ------------------------------------------------------

    def _load_or_create_key(self) -> bytes:
        if self._key_path.is_file():
            return self._key_path.read_bytes().strip()
        fernet = _fernet_cls()
        assert fernet is not None  # only called when the lib exists
        if self._path.is_file():
            # Ciphertext exists but its key is gone (deleted key, or a restored
            # "shareable" archive built without it). A fresh key can never
            # read it, and the next write would CLOBBER it — preserve it aside
            # so the data survives if the key ever resurfaces.
            self._preserve_aside("key regenerated")
        key: bytes = fernet.generate_key()
        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        self._key_path.touch(mode=0o600)
        self._key_path.write_bytes(key)
        os.chmod(self._key_path, 0o600)
        log.info("Lockbox: generated a new key at %s (rides backups by default)", self._key_path)
        return key

    def _preserve_aside(self, why: str) -> None:
        """Move unreadable ciphertext out of the write path instead of letting
        the next write destroy it."""
        aside = self._path.with_name(self._path.name + time.strftime(".orphaned-%Y%m%d-%H%M%S"))
        try:
            os.replace(self._path, aside)
            log.error(
                "Lockbox: %s — existing secrets are UNREADABLE and were preserved at %s "
                "(restore the matching lockbox.key beside it to recover them)",
                why,
                aside,
            )
        except OSError as exc:
            log.error("Lockbox: could not preserve unreadable file (%s)", exc)

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    # -- persistence -------------------------------------------------------

    def _read_all(self) -> list[Secret]:
        if self._fernet is None or not self._path.is_file():
            return []
        try:
            raw = self._fernet.decrypt(self._path.read_bytes())
            out = []
            for rec in json.loads(raw.decode()):
                if isinstance(rec, dict) and rec.get("id") and rec.get("owner"):
                    out.append(
                        Secret(
                            id=str(rec["id"]),
                            owner=str(rec["owner"]),
                            text=str(rec.get("text", "")),
                            label=str(rec.get("label", "")),
                            value=str(rec.get("value", "")),
                            created=float(rec.get("created", 0.0)),
                            source=str(rec.get("source", "voice")),
                        )
                    )
            return out
        except Exception as exc:  # wrong key / corrupt file — never crash the service
            log.error("Lockbox unreadable (%s: %s) — treating as empty", type(exc).__name__, exc)
            self._degraded = True  # the next write preserves the file aside first
            return []

    def _write_all(self, secrets: list[Secret]) -> None:
        assert self._fernet is not None
        if getattr(self, "_degraded", False):
            self._preserve_aside("stored file undecryptable with the current key")
            self._degraded = False
        payload = self._fernet.encrypt(json.dumps([asdict(s) for s in secrets]).encode())
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".enc.tmp")
        tmp.write_bytes(payload)
        os.replace(tmp, self._path)
        # Same live-refresh poke as the fact ledger (the push is data-less).
        from kenzy.llm import memory as _memory

        _memory._fire_change_hook()

    # -- API ---------------------------------------------------------------

    def add(
        self,
        owner: str,
        text: str,
        *,
        label: str = "",
        value: str = "",
        source: str = "voice",
    ) -> Secret:
        """Store a key/value secret. ``label``/``value`` are auto-derived from
        the text when not given, so every entry has a usable key."""
        if self._fernet is None:
            raise RuntimeError("lockbox unavailable (cryptography not installed)")
        owner, text = owner.strip(), " ".join(text.split())
        if not owner or not text:
            raise ValueError("a secret needs an owner and text")
        secret = Secret(
            id=uuid.uuid4().hex[:12],
            owner=owner,
            text=text,
            label=label.strip() or derive_label(text),
            value=value.strip() or extract_value(text),
        )
        secret.source = source
        items = self._read_all()
        # Same owner + same key ⇒ UPDATE, not a second entry ("the door code
        # has changed to…" replaces the old door code). Deterministic secret
        # coalescing — no model, instant, newest wins. The opaque fallback
        # label is exempt: two label-less secrets ("4412", "9981") share the
        # key "secret" without being the same thing — replacing would destroy
        # the first behind a false success ack (review finding). They coexist;
        # keymap() suffixes them.
        key = slug(secret.label)
        stale = (
            []
            if key == "secret"
            else [
                s.id
                for s in items
                if s.owner == owner and slug(s.label or derive_label(s.text)) == key
            ]
        )
        if stale:
            items = [s for s in items if s.id not in stale]
            log.info("Lockbox: key %r for %s updated (replaced %d)", key, owner, len(stale))
        items.append(secret)
        self._write_all(items)
        log.info("Lockbox: stored secret %s for %s (label=%r)", secret.id, owner, secret.label)
        return secret

    def list_for(self, owner: str) -> list[Secret]:
        """The owner's secrets, oldest first. Owner-scoped — there is no
        cross-owner read; the dashboard shows masked metadata only."""
        return sorted(
            (s for s in self._read_all() if s.owner == owner), key=lambda s: s.created
        )

    def find(self, owner: str, query: str, *, require_all: bool = False) -> list[Secret]:
        """Deterministic owner-only lookup for the voice fast path: stopword-
        filtered token overlap against label + text, reusing the memory
        ledger's tokenizer ("wifi" ≡ "Wi-Fi"). No model anywhere near this.

        ``require_all``: every query content-token must appear in the secret
        (the generic "what's my X" matcher uses this so a question that merely
        MENTIONS a secret-adjacent word can't trigger a read-back)."""
        from kenzy.llm.memory import _tokens

        want = _tokens(query)
        if not want:
            return []
        hits = []
        for s in self.list_for(owner):
            have = _tokens(s.label + " " + s.text)
            if (want <= have) if require_all else (want & have):
                hits.append(s)
        return hits

    def keymap(self, owner: str) -> dict[str, Secret]:
        """The owner's secrets keyed by machine key (label slug), oldest
        first; a collision gets a numeric suffix so every entry stays
        addressable. Deterministic given file order — the identification
        block and the substitution pass build the SAME map."""
        out: dict[str, Secret] = {}
        for s in self.list_for(owner):
            key = slug(s.label or derive_label(s.text))
            if key in out:
                n = 2
                while f"{key}_{n}" in out:
                    n += 1
                key = f"{key}_{n}"
            out[key] = s
        return out

    def erase(self, owner: str, secret_id: str) -> bool:
        """Owner-scoped hard delete."""
        items = self._read_all()
        keep = [s for s in items if not (s.id == secret_id and s.owner == owner)]
        if len(keep) == len(items):
            return False
        self._write_all(keep)
        log.info("Lockbox: erased secret %s (owner=%s)", secret_id, owner)
        return True

    def erase_admin(self, secret_id: str) -> bool:
        """Credentialed-surface delete by id (the dashboard)."""
        items = self._read_all()
        keep = [s for s in items if s.id != secret_id]
        if len(keep) == len(items):
            return False
        self._write_all(keep)
        log.info("Lockbox: erased secret %s (admin)", secret_id)
        return True

    def erase_person(self, owner: str) -> int:
        """Revoke-all integration: every secret the person owns."""
        items = self._read_all()
        keep = [s for s in items if s.owner != owner]
        removed = len(items) - len(keep)
        if removed:
            self._write_all(keep)
            log.info("Lockbox: erased %d secret(s) for %s (revoke-all)", removed, owner)
        return removed

    def masked(self, owner: str | None = None) -> list[dict[str, Any]]:
        """Metadata for the dashboard: label/owner/age — never the text."""
        items = self._read_all() if owner is None else self.list_for(owner)
        return [
            {"id": s.id, "owner": s.owner, "label": s.label or "(unlabeled)",
             "created": s.created, "source": s.source}  # fmt: skip
            for s in sorted(items, key=lambda s: s.created)
        ]

    def reveal(self, secret_id: str) -> Secret | None:
        """Credentialed-surface click-to-reveal (the dashboard)."""
        for s in self._read_all():
            if s.id == secret_id:
                return s
        return None


# ---------------------------------------------------------------------------
# Placeholder substitution (the deterministic value path)
# ---------------------------------------------------------------------------

#: The placeholder the LLM writes where a secret value belongs. Primary form
#: ``[[lockbox:shed_key_code]]``; tolerant of the ``{{…}}`` spelling and the
#: prose form ``*check lockbox for shed_key_code*`` since models drift.
_PLACEHOLDER_RE = re.compile(
    r"\[\[\s*lockbox\s*:\s*([A-Za-z0-9_\-]+)\s*\]\]"
    r"|\{\{\s*lockbox\s*:\s*([A-Za-z0-9_\-]+)\s*\}\}"
    r"|\*+\s*check(?:\s+the)?\s+lockbox\s+for\s+([A-Za-z0-9_\-]+)\s*\*+",
    re.IGNORECASE,
)

_MISS_TEXT = "(not in your lockbox)"

#: Spoken when a secret WOULD be read back but the TTS provider is cloud
#: (founder decision 2026-07-18: lockbox values never transit cloud speech).
DEFLECT_TEXT = (
    "That's in your lockbox, but my voice runs through a cloud service right now, "
    "so I won't say it out loud. You can see it on the dashboard, or switch to "
    "local speech."
)


def substitute(text: str, owner: str | None, *, speak_values: bool = True) -> tuple[str, int]:
    """Replace lockbox placeholders in a finished reply with the ASKER's
    values — the deterministic half of the key/value design: the model only
    ever handles keys; values ride output post-processing and never enter
    model context (the pre-substitution text is what history/short-term
    keep). Every placeholder is consumed: an unknown key, a non-owner, or no
    lockbox at all substitutes to a safe miss — nothing leaks, nothing
    model-shaped survives into speech. Returns ``(new_text, real_hits)``.

    ``speak_values=False`` (cloud TTS): an owned hit deflects the WHOLE reply
    to :data:`DEFLECT_TEXT` instead of speaking the value — the value must not
    ride a reply that transits a cloud speech provider."""
    if not _PLACEHOLDER_RE.search(text):
        return text, 0
    box = store()
    keys = box.keymap(owner) if (box is not None and owner) else {}
    hits = 0

    def _sub(m: re.Match[str]) -> str:
        nonlocal hits
        key = slug(next(g for g in m.groups() if g))
        secret = keys.get(key)
        if secret is None:
            return _MISS_TEXT
        hits += 1
        return secret.payload

    out = _PLACEHOLDER_RE.sub(_sub, text)
    if hits and not speak_values:
        return DEFLECT_TEXT, hits
    return out, hits


# ---------------------------------------------------------------------------
# Module-level store (mirrors kenzy.llm.memory's pattern)
# ---------------------------------------------------------------------------

_store: LockboxStore | None = None


def init_store(enc_path: Path) -> LockboxStore:
    global _store
    _store = LockboxStore(enc_path)
    if _store.enabled:
        log.info("Lockbox: %d secret(s) loaded from %s", len(_store._read_all()), enc_path)
    return _store


def store() -> LockboxStore | None:
    """The live lockbox, or None when memory is disabled / crypto missing."""
    return _store if (_store is not None and _store.enabled) else None
