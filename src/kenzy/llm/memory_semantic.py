"""Incremental semantic consolidation (F2.7's model-assisted half).

Exact dedupe is plumbing hygiene; people restate the same THOUGHT in
different words ("the pool guy comes Thursdays" / "pool service is on
Thursday"). This pass runs the ``memory-consolidation-semantic`` job:
**kicked by every write** (via the MemoryStore write hook), rate-limited by
the job's cooldown, retried in minutes on failure, swept daily as a backstop.

Scope-first and conservative by construction:

- Only facts added since the pending **high-water mark** are examined (a tiny
  state file next to the ledger; advances only on a successful run — so a
  failed run leaves everything pending for the retry/backstop). Cost scales
  with new-facts-per-day, never corpus size.
- Each pending fact is compared against keyword-prefiltered neighbors of the
  SAME owner + tier only — tiers are the ACL and consolidation never crosses
  it.
- The model returns constrained JSON validated against real fact ids;
  anything malformed, unknown, or cross-scope degrades to "keep" (skipping is
  always safe: still pending? no — processed-as-keep; the periodic GLOBAL
  pass (phase 2) inherits residue by design).
- **Supersede, never delete**: a merge writes the consolidated fact and marks
  sources ``superseded_by`` — instantly out of recall, physically present for
  the mechanical sweep's 30-day retention window. Every model decision is
  reversible for a month, and every application is logged.

The model is the service's configured one (with the local fallback) — memory
never has to leave the house unless the operator pointed it elsewhere.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from kenzy.llm import memory
from kenzy.llm import skills as skill_registry
from kenzy.llm.memory import Fact, MemoryStore

log = logging.getLogger(__name__)

_MAX_NEIGHBORS = 8
_MAX_BATCH = 20  # pending facts per run (backstop after an outage may have many)
_MAX_MERGED_LEN = 300

# Configured at service startup (avoids a circular import of llm.py).
_MODEL: dict[str, Any] = {}


def configure(
    model: str,
    base_url: str | None = None,
    *,
    private_to_cloud: bool = False,
    classifier_model: str = "",
    classifier_url: str | None = None,
) -> None:
    _MODEL["model"] = model
    _MODEL["base_url"] = base_url
    _MODEL["private_to_cloud"] = private_to_cloud
    _MODEL["classifier_model"] = classifier_model
    _MODEL["classifier_url"] = classifier_url


def _pass_model() -> tuple[str, str | None, bool]:
    """(model, base_url, is_local) for this consolidation pass. A cloud brain
    with a LOCAL ``memory.classifier_model`` configured runs the pass on the
    classifier's model instead — so private-tier facts consolidate without a
    local service model. Strictly more private, never less."""
    from kenzy.llm.locality import model_is_local

    model, url = str(_MODEL.get("model", "")), _MODEL.get("base_url")
    if model_is_local(model, url):
        return model, url, True
    cmodel, curl = str(_MODEL.get("classifier_model", "")), _MODEL.get("classifier_url")
    if cmodel and model_is_local(cmodel, curl):
        return cmodel, curl, True
    return model, url, False


# ---------------------------------------------------------------------------
# Pending high-water mark (data/memory/consolidation-state.json)
# ---------------------------------------------------------------------------


def _state_path(store: MemoryStore) -> Path:
    return store.path.parent / "consolidation-state.json"


def _load_mark(store: MemoryStore) -> float:
    try:
        return float(json.loads(_state_path(store).read_text()).get("mark", 0.0))
    except Exception:
        return 0.0  # no/unreadable state ⇒ everything is pending (safe: idempotent)


def _save_mark(store: MemoryStore, mark: float) -> None:
    path = _state_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"mark": mark}))
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# The pass, decomposed for testability
# ---------------------------------------------------------------------------


def _stamp(f: Fact) -> float:
    """A fact's position in the pending window. ``updated`` counts too:
    release() stamps it, so a fact that sat QUARANTINED while the mark
    advanced past its ``created`` re-enters the window the moment the
    classifier clears it — otherwise it would be stranded behind the mark
    and never coalesce."""
    return max(f.created, f.updated or 0.0)


def pending_facts(store: MemoryStore, mark: float) -> list[Fact]:
    now = time.time()
    out = [
        f
        for f in store.all_facts()
        if _stamp(f) > mark and f.live(now) and f.state != "quarantined"
    ]
    out.sort(key=lambda f: _stamp(f))
    return out[:_MAX_BATCH]


def neighbors_for(store: MemoryStore, fact: Fact) -> list[Fact]:
    """Candidate merge partners: live facts of the SAME owner + tier, ranked by
    keyword overlap with the pending fact (other pending facts included — a
    burst of restatements must see each other)."""
    now = time.time()
    q = memory._tokens(fact.text)
    scored = []
    for other in store.all_facts():
        if other.id == fact.id or not other.live(now):
            continue
        if other.owner != fact.owner or other.tier != fact.tier:
            continue
        overlap = len(q & memory._tokens(other.text))
        if overlap:
            scored.append((overlap, other))
    scored.sort(key=lambda s: (s[0], s[1].updated), reverse=True)
    return [f for _n, f in scored[:_MAX_NEIGHBORS]]


_SYSTEM = """You maintain a household voice assistant's memory ledger.
You receive NEW facts (just spoken) and, for each, EXISTING facts by the same
person at the same privacy tier. Decide, for each new fact:

- "keep": it is a distinct new fact (DEFAULT — use whenever unsure).
- "merge": it states the same thought as one or more existing facts, just
  worded differently. Provide the single best consolidated wording ("text")
  and list every fact id it replaces (the new fact's id and/or existing ids).
- "update": it changes/corrects an existing fact (new info wins — "the
  plumber is now Sam"). List the outdated existing ids it supersedes.

Rules: never invent ids; never combine unrelated facts; preserve concrete
details (numbers, names, days) exactly; when in doubt, "keep".
Respond with ONLY this JSON, no prose:
{"decisions": [{"id": "...", "action": "keep"}
             | {"id": "...", "action": "merge", "text": "...", "supersedes": ["..."]}
             | {"id": "...", "action": "update", "supersedes": ["..."]}]}"""


def build_messages(batch: list[tuple[Fact, list[Fact]]]) -> list[dict[str, Any]]:
    items = [
        {
            "new_fact": {"id": f.id, "text": f.text},
            "existing": [{"id": n.id, "text": n.text} for n in ns],
        }
        for f, ns in batch
    ]
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": json.dumps({"items": items}, ensure_ascii=False)},
    ]


def parse_decisions(raw: str) -> list[dict[str, Any]]:
    """Tolerant parse of the model's JSON; anything unusable ⇒ empty list
    (which applies as keep-everything)."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    try:
        data = json.loads(text)
        decisions = data.get("decisions", [])
        return decisions if isinstance(decisions, list) else []
    except Exception:
        log.warning("Semantic consolidation: unparseable model output — keeping all")
        return []


def apply_decisions(
    store: MemoryStore,
    pending: list[Fact],
    decisions: list[dict[str, Any]],
) -> dict[str, int]:
    """Validate and apply. Every guard degrades to 'keep' — the model can only
    ever narrow visibility (supersede), never destroy or cross a scope."""
    summary = {"kept": 0, "merged": 0, "updated": 0, "rejected": 0}
    by_id = {f.id: f for f in pending}
    decided: set[str] = set()

    for d in decisions:
        if not isinstance(d, dict):
            continue
        fact = by_id.get(str(d.get("id", "")))
        action = str(d.get("action", ""))
        if fact is None or fact.id in decided:
            summary["rejected"] += 1
            continue
        decided.add(fact.id)

        if action == "keep" or action not in ("merge", "update"):
            summary["kept"] += 1
            continue

        # Common validation for merge/update: every superseded id must exist,
        # be live, and share the pending fact's owner + tier (the ACL line).
        sup_ids = [str(s) for s in d.get("supersedes", []) if isinstance(s, (str, int))]
        sup_facts = []
        ok = bool(sup_ids)
        for sid in sup_ids:
            target = store.get_fact(sid)
            if (
                target is None
                or not target.live(time.time())
                or target.owner != fact.owner
                or target.tier != fact.tier
                or target.created > fact.created
            ):
                # The direction guard (created check) stops a confused model
                # from superseding a NEWER fact with an older one — which
                # could leave a tombstone outliving the only live copy.
                ok = False
                break
            sup_facts.append(target)

        if action == "merge":
            text = " ".join(str(d.get("text", "")).split())
            live_now = store.get_fact(fact.id)
            if not ok or not text or len(text) > _MAX_MERGED_LEN or live_now is None:
                # live_now: the user may have hard-deleted the pending fact
                # ("actually, forget that") while the model call was in flight
                # — a merge must not resurrect deleted content.
                summary["rejected"] += 1
                summary["kept"] += 1
                continue
            merged = store.remember(fact.owner, text, tier=fact.tier, source="consolidation")
            for target in sup_facts:
                store.supersede(target.id, by=merged.id)
            if fact.id not in {t.id for t in sup_facts}:
                store.supersede(fact.id, by=merged.id)
            summary["merged"] += 1
            log.info(
                "Semantic consolidation: merged %s (+%d) → %s: %r",
                fact.id,
                len(sup_facts),
                merged.id,
                text[:80],
            )
        else:  # update — the new fact supersedes stale ones
            if not ok or fact.id in {t.id for t in sup_facts}:
                summary["rejected"] += 1
                summary["kept"] += 1
                continue
            for target in sup_facts:
                store.supersede(target.id, by=fact.id)
            summary["updated"] += 1
            log.info("Semantic consolidation: %s updates %s", fact.id, [t.id for t in sup_facts])

    summary["kept"] += len([f for f in pending if f.id not in decided])
    return summary


async def run_pass(store: MemoryStore) -> dict[str, Any]:
    """The job body: process everything past the high-water mark in one model
    call; advance the mark only on success. Merged facts created here carry
    ``created > mark`` and are re-examined (and kept) on the follow-up kicked
    run — cheap, and it lets chained restatements converge."""
    mark = _load_mark(store)
    pending = pending_facts(store, mark)
    if not pending:
        return {"pending": 0}
    # The mark target covers the FULL pending set — including any facts the
    # privacy gate below withholds — so nothing loops back every run.
    mark_target = max(_stamp(f) for f in pending)

    # 4.0.2 privacy slice: when the configured model is CLOUD, private-tier
    # facts never enter the consolidation prompt — they stay unconsolidated
    # (the mechanical dedupe still covers exact repeats) until a local model
    # exists. Neighbors are same-owner+tier, so filtering pending private
    # facts removes every private exposure path.
    skipped_private = 0
    pass_model, pass_url, pass_local = _pass_model()
    if not _MODEL.get("private_to_cloud", False):
        if not pass_local:
            visible = [f for f in pending if f.tier != memory.TIER_PRIVATE]
            skipped_private = len(pending) - len(visible)
            if skipped_private:
                log.info(
                    "Semantic consolidation: %d private fact(s) withheld from the "
                    "cloud model (kept unconsolidated)",
                    skipped_private,
                )
            if not visible:
                _save_mark(store, mark_target)
                return {"pending": len(pending), "kept": len(pending),
                        "private_withheld": skipped_private}  # fmt: skip
            pending = visible

    batch = [(f, neighbors_for(store, f)) for f in pending]
    if all(not ns for _f, ns in batch):
        # Nothing to compare against — everything is trivially distinct.
        _save_mark(store, mark_target)
        return {"pending": len(pending), "kept": len(pending)}

    # No temperature pin: reasoning-family models (gpt-5.x) reject anything but
    # the default (field finding — the job failed and retried exactly as
    # designed). Determinism duty lives in the constrained prompt + validation.
    kwargs: dict[str, Any] = {
        "model": pass_model or "gpt-4o",
        "messages": build_messages(batch),
    }
    kwargs.update(skill_registry.endpoint_kwargs(pass_url))
    # local_only when this pass carries private-tier facts (pass_local): a
    # cloud fallback must not see what the cloud primary was denied.
    response = await skill_registry.acompletion_with_fallback(kwargs, local_only=pass_local)
    raw = response.choices[0].message.content or ""

    summary = apply_decisions(store, pending, parse_decisions(raw))
    _save_mark(store, mark_target)
    return {"pending": len(pending), **summary}
