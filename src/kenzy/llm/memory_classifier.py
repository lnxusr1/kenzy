"""The write-path classifier (4.1, absorbs F2.4) — quarantine's judge.

Every memory write is born quarantined (owner-only, invisible to models).
This module decides each fact's fate:

- **vault** — it's a secret: moved to the lockbox (encrypted, model-free
  recall), removed from the plain ledger.
- **release** — ordinary memory: quarantine cleared, normal tier semantics
  resume.
- **hold** — ambiguous with no local model to consult: stays quarantined for
  dashboard review. Rare by design; over-protection, never a leak.
- **split** (local model only) — only a span is secret: the ledger keeps a
  redacted text, the lockbox holds the secret span.

The chicken-and-egg rule (locked): a CLOUD model never sees suspect content
to judge its own secrecy. Order: cheap heuristics → a LOCAL model when one
is configured (``memory.classifier_model``, defaulting to the service's
model only when that is local) → conservative hold.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from kenzy.llm import lockbox
from kenzy.llm import skills as skill_registry
from kenzy.llm.locality import model_is_local
from kenzy.llm.memory import Fact, MemoryStore
from kenzy.redact import PAYLOAD_RE as _PAYLOAD_RE
from kenzy.redact import SECRET_WORDS_RE

log = logging.getLogger(__name__)

# Configured at service startup (mirrors memory_semantic).
_CFG: dict[str, Any] = {}


def configure(
    model: str,
    base_url: str | None,
    *,
    classifier_model: str = "",
    classifier_url: str | None = None,
    keep_alive: str = "",
) -> None:
    """The classifier's model = explicit ``classifier_model`` if set, else the
    service model — but only ever USED when local. ``keep_alive`` rides each
    Ollama call (e.g. "-1", "30m") so the model stays resident — the pin
    travels with the deployment instead of the Ollama host's env."""
    _CFG["model"] = classifier_model or model
    _CFG["base_url"] = classifier_url if classifier_model else base_url
    _CFG["keep_alive"] = keep_alive


def ollama_keep_alive_kwargs(model: str) -> dict[str, Any]:
    """The keep_alive kwarg for an Ollama call, empty otherwise (other
    providers would reject the parameter)."""
    ka = str(_CFG.get("keep_alive") or "")
    if ka and str(model).startswith("ollama"):
        return {"keep_alive": ka}
    return {}


def _local_model() -> tuple[str, str | None] | None:
    model, url = str(_CFG.get("model", "")), _CFG.get("base_url")
    return (model, url) if model and model_is_local(model, url) else None


# ---------------------------------------------------------------------------
# Heuristics — instant, local, no model
# ---------------------------------------------------------------------------

#: Strong secret signals live in kenzy.redact (the one shared definition);
#: aliased here for the heuristics and tests.
_SECRET_WORDS_RE = SECRET_WORDS_RE
#: A code-like payload: digit runs, or mixed letter-digit blobs (min length 4
#: so years alone don't trip it without a secret word).

#: Mundane date-ish contexts that legitimately carry digits.
_DATEISH_RE = re.compile(
    r"\b(birthday|anniversary|born|arrives?|leaving|due|appointment|meeting|"
    r"o'?clock|am\b|pm\b|january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\b",
    re.IGNORECASE,
)


def heuristic(text: str) -> str:
    """'secret' | 'clear' | 'unsure' — deliberately simple and fast.

    secret-word + payload ⇒ secret. secret-word alone ⇒ unsure (a model or a
    human should look). No secret-words ⇒ clear (dates/quantities are normal
    memory content; digits alone aren't suspicion).
    """
    has_word = bool(_SECRET_WORDS_RE.search(text))
    if not has_word:
        return "clear"
    if _PAYLOAD_RE.search(text) and not _DATEISH_RE.search(text):
        return "secret"
    return "unsure"


#: Label derivation is shared with the lockbox's own add() auto-labelling.
derive_label = lockbox.derive_label


# ---------------------------------------------------------------------------
# Local-model pass (the unsure/split tier)
# ---------------------------------------------------------------------------

_PROMPT = """You judge whether a remembered fact contains a SECRET (credential,
code, password, PIN, combination — something that unlocks or authenticates).
Reply with ONE JSON object, nothing else:
{"action": "release"}                          — no secret in it
{"action": "vault", "label": "<short handle>"} — the whole fact is a secret
{"action": "split", "public": "<non-secret text>", "secret": "<the secret part>",
 "label": "<short handle>"}                    — only part is secret
If unsure, prefer "vault". Fact: """


async def _model_decide(fact: Fact) -> dict[str, Any]:
    local = _local_model()
    assert local is not None
    model, url = local
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": _PROMPT + json.dumps(fact.text)}],
    }
    kwargs.update(ollama_keep_alive_kwargs(model))
    kwargs.update(skill_registry.endpoint_kwargs(url))
    # local_only: the suspect text must never reach a cloud model, even on a
    # fallback retry (the service-wide fallback is not guaranteed local).
    response = await skill_registry.acompletion_with_fallback(kwargs, local_only=True)
    raw = (response.choices[0].message.content or "").strip()
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        out = json.loads(raw[start:end])
        if isinstance(out, dict) and out.get("action") in ("release", "vault", "split"):
            return out
    except (ValueError, json.JSONDecodeError):
        pass
    log.warning("Classifier: unusable model output for %s — vaulting to be safe", fact.id)
    return {"action": "vault", "label": derive_label(fact.text)}


# ---------------------------------------------------------------------------
# The release-job body
# ---------------------------------------------------------------------------


async def classify_pending(store: MemoryStore) -> dict[str, int]:
    """Judge every quarantined fact. Returns a run summary for the job log."""
    box = lockbox.store()
    summary = {"released": 0, "vaulted": 0, "split": 0, "held": 0}
    for fact in store.quarantined():
        verdict = heuristic(fact.text)
        decision: dict[str, Any]
        if verdict == "secret":
            # Word + payload: certain enough to vault with no model call.
            decision = {"action": "vault", "label": derive_label(fact.text)}
        elif _local_model() is not None:
            # A LOCAL model judges EVERYTHING else — including heuristic
            # "clear". The word list must never gatekeep the smart tier: a
            # secret phrased outside it ("the thing that opens the garage is
            # 9931") would otherwise release as a plain memory without the
            # model ever seeing it (founder finding 2026-07-19). The word
            # list is an accelerator and the no-model fallback, not the
            # detector.
            try:
                decision = await _model_decide(fact)
            except Exception as exc:
                # Model down: leave the fact quarantined (owner-only, invisible)
                # — the job's retry/backstop re-judges when the model returns.
                log.warning("Classifier: local model unavailable (%s) — holding", exc)
                summary["held"] += 1
                continue
        elif verdict == "clear":
            # No local model: the heuristic is all we have. No secret-shaped
            # signal ⇒ release (the People page banners this degraded mode).
            decision = {"action": "release"}
        else:
            # Ambiguous, no local model: hold for dashboard review — the
            # conservative middle. Owner-only and model-invisible meanwhile.
            summary["held"] += 1
            continue

        action = decision.get("action")
        if action == "vault" and box is not None:
            box.add(
                fact.owner,
                fact.text,
                label=str(decision.get("label") or derive_label(fact.text)),
                source=f"classifier:{fact.source}",
            )
            store.erase(fact.id)  # out of the plain ledger entirely
            summary["vaulted"] += 1
        elif action == "split" and box is not None and decision.get("secret"):
            box.add(
                fact.owner,
                str(decision["secret"]),
                label=str(decision.get("label") or derive_label(fact.text)),
                source=f"classifier:{fact.source}",
            )
            public = " ".join(str(decision.get("public") or "").split())
            if public:
                fact.text = public
            store.release(fact.id)
            summary["split"] += 1
        else:
            # release — or a vault/split decision with no lockbox available
            # (crypto missing): releasing would leak nothing new (the text is
            # already in the plain ledger), but hold is safer and surfaces in
            # the dashboard's review queue instead.
            if action in ("vault", "split") and box is None:
                summary["held"] += 1
                continue
            store.release(fact.id)
            summary["released"] += 1
    if any(summary.values()):
        log.info(
            "Classifier: released %d, vaulted %d, split %d, held %d",
            summary["released"], summary["vaulted"], summary["split"], summary["held"],
        )  # fmt: skip
    return summary
