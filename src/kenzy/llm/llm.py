"""
Kenzy LLM service.

Accepts POST /process with transcribed text and returns a response plus a
voice prompt for kenzy-tts.

Skills are auto-discovered from the skills/ directory alongside this file.
Any async function decorated with @skill is registered automatically — no
extra config needed.  Add skills.disabled in llm.yaml to turn individual
skills off without deleting them.

Tool-calling loop
-----------------
1. Build messages: system prompt + user message (with speaker if known).
2. Call LiteLLM with all registered tools.
3. If the response contains tool calls, execute each skill and append the
   results as tool messages, then call again.
4. Repeat until a plain text response is returned (or max_tool_iterations).
5. Return the final text + configured voice_prompt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from kenzy import version_info
from kenzy.fastapi_auth import (
    install_backup_endpoint,
    install_logs_endpoint,
    install_restart_endpoint,
    install_service_auth,
    install_unit_endpoint,
    install_upgrade_endpoint,
)
from kenzy.llm import memory
from kenzy.llm import skills as skill_registry
from kenzy.logutil import quiet_health_access_log

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ProcessRequest(BaseModel):
    text: str
    room_id: str | None = None
    session_id: str | None = None
    speaker: str | None = None
    # Names of the rooms currently connected (sent by the server) so the model can
    # target real rooms for announcements / intercom and use their canonical names.
    rooms: list[str] = []
    # The asking node's active timers/alarms/reminders (sent by the server) so the
    # schedule skill / fast intents can answer status and cancel by id locally.
    schedules: list[dict[str, Any]] = []
    # Rooms whose speakers lack echo cancellation (hardware_aec: false) — the
    # alarm/intercom skills refuse these targets in the reply itself, instead of
    # confirming and then failing when the server actuates the action.
    no_aec_rooms: list[str] = []
    # Person records (id/name/voiceprints), server-injected like rooms/schedules —
    # lets skills resolve spoken names to people (enrollment's person-first flow).
    people: list[dict[str, Any]] = []
    # The speaker service base URL (server-resolved: static ← auto-registered),
    # for the enrollment skill's /enroll calls.
    speaker_url: str | None = None
    # Identity core (F1): the resolved person id (None = no record / unknown), the
    # confidence tier ("unknown"/"recognized"), and the raw voiceprint confidence.
    # Skills read these via get_request to gate on who's asking.
    person_id: str | None = None
    speaker_tier: str | None = None
    confidence: float | None = None
    # Which front door the request came through (F3): "voice" (a room node) or
    # "assist" (HA Assist — no asking node exists). Node-bound skills refuse
    # gracefully on non-voice channels instead of silently targeting nothing.
    channel: str = "voice"
    # Whether the server's TTS keeps audio on-box (kokoro). Gates SPEAKING lockbox
    # values (founder decision 2026-07-18) — absent/False ⇒ deflect to the dashboard.
    tts_local: bool = False
    # F7.4 "don't remember me": the resolved person opted out of memory —
    # no context injection, no writes, no reads (the server sets this from
    # the person record; skills read it via the request context).
    memory_opt_out: bool = False
    # 4.1 capture mode (per person): explicit | suggest | auto. Only "auto"
    # changes runtime behavior today (proactive capture instruction + source
    # tagging); "suggest" becomes real on 4.2's ask().
    memory_capture: str = "explicit"


class ProcessResponse(BaseModel):
    text: str
    voice_prompt: str
    # Set by a fast intent (or, later, the LLM) to ask the server to re-open the
    # mic for a follow-up without requiring the wake word. Honoured by the server.
    expect_response: bool = False
    # Server-side actions a skill asked for (e.g. broadcast an announcement) that the
    # LLM service can't perform itself. The server actuates each after speaking `text`.
    actions: list[dict[str, Any]] = []
    # True when the deterministic fast path handled this (no LLM call) — surfaced for
    # the dashboard's fast-path hit-rate metric.
    fast: bool = False
    # True when lockbox content shaped this exchange (secret stored/spoken/erased):
    # the server must redact its logs and the Activity record for this turn.
    secret: bool = False
    # Timing breakdown inside this service for the Activity tab: ordered
    # [{kind: "fast"|"model"|"tool", name, ms}] — names and durations ONLY,
    # never arguments or content (safe even on secret exchanges).
    spans: list[dict[str, Any]] = []
    # ask() (4.2): set when a skill parked on a question. text IS the prompt;
    # expect_response is forced true. The server speaks it, arms the reply
    # window, routes the next utterance to POST /process/continue with this id
    # (wake word / window expiry → POST /process/cancel).
    continuation: str | None = None
    # Optional per-question override of the node's reply window (seconds).
    ask_timeout_s: float | None = None
    # What the parked skill wants back: "text" (the transcript — default) or
    # "audio" (the RAW captured PCM, base64 on the wire; STT never runs).
    ask_capture: str = "text"
    # Whether the node should play its record-tone when the window opens.
    ask_cue: bool = False
    # Cross-room ask: speak/answer the question in THIS room instead of the
    # asker's; `text` is then the asker-side announcement and `ask_prompt`
    # the question itself.
    ask_room: str | None = None
    ask_prompt: str = ""
    # Whether the server's processing-cue ladder may speak over this question's
    # ANSWER turn while the skill resumes (ask(busy_cues=False) opts out —
    # conversational skills keeping their turnarounds clean).
    ask_busy_cues: bool = True


# ---------------------------------------------------------------------------
# App + module-level state
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------


@dataclass
class _Turn:
    timestamp: float
    speaker: str  # raw value — "unknown" if not identified
    user_text: str
    assistant_text: str  # spoken text only (no JSON wrapper, no tool internals)
    # F2 privacy: when this turn's answer was built from private/personal-tier
    # memory, it replays ONLY for this person id — never for another voice in
    # the room-history window (a stranger 30s later must not hear the echo).
    private_to: str | None = None


class ConversationHistory:
    """Per-room rolling history of the last N turns within a time window."""

    MAX_TURNS: int = 10
    MAX_AGE: float = 180.0  # seconds (3 minutes)

    def __init__(self) -> None:
        self._rooms: dict[str, list[_Turn]] = {}

    def _prune(self, room_id: str) -> None:
        turns = self._rooms.get(room_id, [])
        cutoff = time.time() - self.MAX_AGE
        turns = [t for t in turns if t.timestamp >= cutoff]
        if len(turns) > self.MAX_TURNS:
            turns = turns[-self.MAX_TURNS :]
        self._rooms[room_id] = turns

    def add(
        self,
        room_id: str,
        speaker: str,
        user_text: str,
        assistant_text: str,
        *,
        private_to: str | None = None,
    ) -> None:
        if room_id not in self._rooms:
            self._rooms[room_id] = []
        self._prune(room_id)
        self._rooms[room_id].append(
            _Turn(
                timestamp=time.time(),
                speaker=speaker,
                user_text=user_text,
                assistant_text=assistant_text,
                private_to=private_to,
            )
        )

    def get_messages(self, room_id: str, viewer: str | None = None) -> list[dict[str, Any]]:
        """History as alternating user/assistant dicts, as ``viewer`` (a person
        id, or None for an unrecognized voice) is allowed to see it: turns
        tagged private to someone else are withheld."""
        self._prune(room_id)
        out: list[dict[str, Any]] = []
        for turn in self._rooms.get(room_id, []):
            if turn.private_to and turn.private_to != viewer:
                continue
            label = turn.speaker if turn.speaker.lower() != "unknown" else "unidentified speaker"
            out.append({"role": "user", "content": f"[{label}] {turn.user_text}"})
            out.append({"role": "assistant", "content": turn.assistant_text})
        return out


_history: ConversationHistory = ConversationHistory()

# F2.1: per-PERSON rolling context (cross-room, cross-session, hours-scale) —
# complements the per-room minutes-scale history above.
_short_term: memory.ShortTermContext = memory.ShortTermContext()


def _llm_history_tag() -> str | None:
    """Tag for LLM-path history turns: the person id when private/personal
    memory shaped the answer (mirrors process()'s fast-path tag)."""
    pid = skill_registry.get_request("person_id")
    return str(pid) if (pid and memory.private_touched()) else None


def _memory_context(utterance: str) -> str:
    """The memory block injected into the system prompt for a recognized person:
    tier-scoped relevant facts (F2.5 auto-recall) + their recent cross-session
    exchanges (F2.1). Empty for unrecognized voices — the F1.3 contract."""
    person_id = skill_registry.get_request("person_id")
    if not person_id or skill_registry.current_tier() == skill_registry.TIER_UNKNOWN:
        return ""
    if skill_registry.get_request("memory_opt_out"):
        return ""  # F7.4 "don't remember me" — no ledger context for them
    parts: list[str] = []
    store = memory.store()
    if store is not None:
        facts = store.recall(str(person_id), utterance, limit=5)
        # 4.1 quarantine: an unclassified fact never enters ANY model context
        # (even local) — the fast path still answers it for its owner.
        facts = [f for f in facts if f.state != "quarantined"]
        if facts and not _private_to_cloud:
            from kenzy.llm.locality import model_is_local

            if not model_is_local(_model, _base_url):
                # 4.0.2: a private fact must not leave the house just because
                # the brain is a cloud model. Personal-public/shared tiers are
                # household-visible by design and still inject; the fast-path
                # recall (no model) still answers private facts by voice.
                facts = [f for f in facts if f.tier != memory.TIER_PRIVATE]
        if facts:
            memory.mark_if_sensitive(facts)  # private facts ⇒ tag this history turn
            parts.append(
                "Remembered facts relevant to this request (already tier-scoped to "
                "this speaker — do not reveal to others):\n"
                + "\n".join(f"- {f.text}" for f in facts)
            )
    recent = _short_term.recent(str(person_id), limit=4)
    if recent:
        lines = "\n".join(f"- they said: {u!r} — you replied: {a!r}" for u, a in recent)
        parts.append(f"Earlier exchanges with this speaker (may be from other rooms):\n{lines}")
    # 4.1 lockbox identification block: the KEYS are the non-sensitive index
    # the model may see; the VALUES are substituted deterministically after
    # the model has answered and never enter its context.
    from kenzy.llm import lockbox

    box = lockbox.store()
    if box is not None:
        keys = list(box.keymap(str(person_id)))
        if keys:
            parts.append(
                "This speaker's lockbox holds encrypted secrets under these keys "
                "(you can never see the values): " + ", ".join(keys) + ". "
                "When they ask for one, reply naturally and write the placeholder "
                "[[lockbox:<key>]] exactly where the value belongs — it is filled in "
                "after your reply, for their ears only. Never use a placeholder for "
                "anyone other than this speaker, and never invent keys."
            )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# App + module-level config
# ---------------------------------------------------------------------------

app = FastAPI(title="Kenzy LLM Service", version="0.1.0")

_model: str = "gpt-4o"
_base_url: str | None = None
#: 4.0.2 privacy slice — operator opt-OUT of the protection (default: private
#: facts never ride into a cloud model's context or consolidation).
_private_to_cloud: bool = False
_system_prompt: str = "You are Kenzy, a helpful home assistant. Be concise."
_voice_prompt: str = "Respond in a friendly, conversational tone."
_max_tool_iterations: int = 10
# Extra LiteLLM parameters merged into every primary model call (llm.yaml
# `params:`) — the latency knobs live here: reasoning_effort ("none"/"minimal"
# stops a reasoning-capable model thinking before a two-sentence reply),
# service_tier, temperature, max_tokens, … Credential/routing keys are
# stripped: endpoint_kwargs stays the only authority on those (F-14).
_params: dict[str, Any] = {}
_PARAMS_BLOCKED = frozenset({"api_key", "base_url", "api_base", "model", "messages", "tools"})
_location: str = ""  # "City, State, Country" assembled at startup
_timezone: str = ""  # IANA timezone string e.g. "America/Chicago"
_skills_dir: Path | None = None  # resolved user skills overlay (backup slice)


def _backup_items() -> list[tuple[Path, str]]:
    """This host's backup slice: the user skills overlay + HA curation data.

    Both live with the LLM service (not the server), so the server pulls them
    via ``GET /backup`` to keep a multi-host deployment's archive complete.
    """
    from kenzy.config import kenzy_data_root

    items: list[tuple[Path, str]] = [
        (kenzy_data_root() / "data" / "home_assistant", "data/home_assistant"),
        (kenzy_data_root() / "data" / "memory", "data/memory"),  # the F2 fact ledger
    ]
    if _skills_dir is not None:
        items.append((_skills_dir, "skills"))
    return items


# Appended to every system prompt — instructs the LLM to return structured output.
# voice_prompt tells kenzy-tts how to speak the response (tone, pace, style).
# The config voice_prompt is used as fallback if the model doesn't comply.
_JSON_INSTRUCTION = """
Tool usage: if a tool is available that can fulfill the user's request, you \
MUST call it. Do not describe an action as done or assume it happened — call \
the tool. Only after all tool calls are complete should you produce your \
final reply.

Final reply format: a single raw JSON object and nothing else — no markdown, \
no code fences, no explanation, no trailing characters after the closing brace.

The object must contain these fields, emitted in exactly this order:
  "voice_prompt": a short TTS instruction describing tone, pace, and style
  "expect_response" (default false): whether to keep the microphone open
      for the user's reply without requiring the wake word again.
  "text": your spoken response (read aloud — plain prose, no markdown) — LAST,
      so speech can begin while you are still writing.

Set "expect_response" to true when your reply cannot be complete without the user's
immediate answer — in either of these cases:
  (a) your reply is deliberately incomplete and sets up their line — e.g. the opening
      of a knock-knock joke ("Knock knock." expects "Who's there?"); or
  (b) your reply is itself a genuine question you are actively waiting for them to
      answer as part of what they asked for — they asked you to quiz them, to ask them
      a question, to play a back-and-forth game, or you offered a real either/or choice
      that needs their pick. If you just asked the user a question and want their reply,
      hold the floor — do not end the exchange as if the task were only to ask it.
In every other case set it to false, and when in doubt use false. Do NOT set it true
merely to offer more help ("Is there anything else?", "Let me know if you need
anything"), for pleasantries or sign-offs, or to ask the user to clarify a request you
could otherwise just carry out.

Example (output this format exactly):
{"voice_prompt": "Speak naturally.", "expect_response": false, "text": "The lights are on."}
Example holding the floor (incomplete setup):
{"voice_prompt": "Playful tone.", "expect_response": true, "text": "Knock knock."}
Example holding the floor (a question you're waiting on):
{"voice_prompt": "Curious.", "expect_response": true, "text": "What's your favorite color?"}"""


# The same three-field contract as a JSON schema. Passed as `response_format` so
# providers that support structured outputs (OpenAI/gpt-5.x, etc.) MUST emit all
# three fields — including expect_response, which the model otherwise drops
# intermittently, silently defaulting the floor-hold to false. This fixes the
# reliability at the output mechanism instead of parsing the reply text. The
# prompt instruction above stays as the fallback for providers that don't support
# it (drop_params removes response_format for them; _parse_response still copes).
_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    # Property order is part of the contract (4.4): structured-output providers
    # emit fields in schema order, and streaming needs the two header fields
    # BEFORE the (long) spoken text so TTS can start on the first sentence.
    "properties": {
        "voice_prompt": {
            "type": "string",
            "description": "A short TTS style instruction (tone, pace).",
        },
        "expect_response": {
            "type": "boolean",
            "description": (
                "Keep the mic open for the user's immediate reply without the wake "
                "word. True only when the reply is deliberately incomplete (a joke "
                "setup) or is itself a question you are waiting for them to answer; "
                "false otherwise, and when in doubt."
            ),
        },
        "text": {"type": "string", "description": "The spoken response, read aloud."},
    },
    "required": ["voice_prompt", "expect_response", "text"],
    "additionalProperties": False,
}


def _response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": "kenzy_reply", "strict": True, "schema": _RESPONSE_SCHEMA},
    }


def _wants_cache_breakpoint(model: str) -> bool:
    """Anthropic-family models need an explicit cache_control marker; OpenAI-style
    providers cache the prompt prefix automatically (the message layout alone does
    the work there)."""
    m = model.lower()
    return m.startswith(("claude", "anthropic/")) or "/claude" in m


def _system_message(static_head: str, dynamic_ctx: str) -> dict[str, Any]:
    """The system message, laid out for provider prompt caching: the byte-stable
    static head (system prompt + reply contract) first, the per-request context
    (clock, rooms, schedules, memory) after it — so the cacheable prefix is never
    invalidated by the minute-granularity clock. For Anthropic-family models the
    head rides its own content block with a cache_control breakpoint; everyone
    else gets a plain string (safest shape for Ollama-class templates)."""
    if _wants_cache_breakpoint(_model):
        return {
            "role": "system",
            "content": [
                {"type": "text", "text": static_head, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": dynamic_ctx},
            ],
        }
    return {"role": "system", "content": f"{static_head}\n\n{dynamic_ctx}"}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, object]:
    return {"status": "ok", **version_info(), "model": _model}


class SkillToggle(BaseModel):
    # Full replacement set of disabled skill names (live, no restart).
    disabled: list[str] = []


@app.get("/skills")
async def list_skills() -> dict[str, Any]:
    """Loaded skills + fast intents with disabled state and invocation counts."""
    return skill_registry.registry_info()


@app.post("/skills")
async def set_skills(body: SkillToggle) -> dict[str, Any]:
    """Replace the runtime-disabled set live (the server persists it separately)."""
    skill_registry.set_disabled(body.disabled)
    log.info("Skills disabled set updated: %s", sorted(body.disabled))
    return skill_registry.registry_info()


# ---------------------------------------------------------------------------
# Memory (F2) — the token-gated wire contract over the fact ledger. The
# dashboard proxies these; the voice path uses the skills directly. Tiers gate
# *voices* — these endpoints are a credentialed admin/service surface (behind
# install_service_auth), so list/forget take an explicit asker or none at all.
# ---------------------------------------------------------------------------


class RememberBody(BaseModel):
    owner: str  # person id (F1) — never a display name
    text: str
    tier: str = "private"
    source: str = "api"


class ForgetBody(BaseModel):
    # With an asker: voice-style scoped forget (erase rights apply). Without:
    # admin erase by id — this surface is already token-gated, and the
    # dashboard manager deletes any fact.
    id: str
    asker: str = ""


def _fact_dict(f: Any) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(f)


def _memory_or_503() -> Any:
    store = memory.store()
    if store is None:
        raise HTTPException(status_code=503, detail="memory is disabled")
    return store


@app.get("/memory")
async def memory_list() -> dict[str, Any]:
    """The whole ledger (dashboard manager view / F7.2)."""
    store = _memory_or_503()
    from kenzy.llm import memory_classifier

    # local_model: a LOCAL model is available to judge memory (classifier_model
    # or a local service model) — drives both auto-review of held facts and
    # private-fact consolidation. The dashboard banners on False.
    return {
        "facts": [_fact_dict(f) for f in store.all_facts()],
        "count": len(store),
        "local_model": memory_classifier._local_model() is not None,
    }


@app.post("/memory/remember")
async def memory_remember(body: RememberBody) -> dict[str, Any]:
    store = _memory_or_503()
    try:
        fact = store.remember(body.owner, body.text, tier=body.tier, source=body.source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"fact": _fact_dict(fact)}


@app.get("/memory/recall")
async def memory_recall(asker: str, q: str = "", limit: int = 5) -> dict[str, Any]:
    """Tier-scoped recall as ``asker`` would see it."""
    store = _memory_or_503()
    return {"facts": [_fact_dict(f) for f in store.recall(asker, q, limit=limit)]}


@app.post("/memory/forget")
async def memory_forget(body: ForgetBody) -> dict[str, Any]:
    store = _memory_or_503()
    ok = store.forget(body.asker, body.id) if body.asker else store.erase(body.id)
    if not ok:
        raise HTTPException(status_code=404, detail="no such fact (or not yours to forget)")
    return {"status": "ok"}


class ErasePersonBody(BaseModel):
    person: str
    include_shared: bool = False


@app.post("/memory/erase_person")
async def memory_erase_person(body: ErasePersonBody) -> dict[str, Any]:
    """Revoke-all (F7.4): hard-delete every fact the person owns — and their
    lockbox secrets (4.1). Shared facts stay with the house unless
    ``include_shared``. Credentialed surface only."""
    from kenzy.llm import lockbox

    store = _memory_or_503()
    if not body.person.strip():
        raise HTTPException(status_code=400, detail="a person id is required")
    pid = body.person.strip()
    erased = store.erase_person(pid, include_shared=body.include_shared)
    box = lockbox.store()
    secrets = box.erase_person(pid) if box is not None else 0
    return {"erased": erased, "secrets_erased": secrets}


@app.get("/memory/lockbox")
async def memory_lockbox(person: str = "") -> dict[str, Any]:
    """Masked lockbox metadata (label/owner/age — never the text) for the
    dashboard. ``person`` filters to one owner."""
    from kenzy.llm import lockbox

    _memory_or_503()
    box = lockbox.store()
    if box is None:
        return {"available": lockbox.available(), "secrets": []}
    return {"available": True, "secrets": box.masked(person.strip() or None)}


class ReviewBody(BaseModel):
    id: str
    action: str  # release | vault


@app.post("/memory/review")
async def memory_review(body: ReviewBody) -> dict[str, Any]:
    """Resolve a held-for-review (quarantined) fact from the credentialed
    dashboard: release it to normal tiering, or vault it into the lockbox."""
    from kenzy.llm import lockbox as _lb
    from kenzy.llm import memory_classifier as _mcls

    store = _memory_or_503()
    fact = store.get_fact(body.id.strip())
    if fact is None or fact.state != "quarantined":
        raise HTTPException(status_code=404, detail="no such held fact")
    if body.action == "release":
        store.release(fact.id)
        return {"status": "released"}
    if body.action == "vault":
        box = _lb.store()
        if box is None:
            raise HTTPException(status_code=503, detail="lockbox unavailable")
        box.add(fact.owner, fact.text, label=_mcls.derive_label(fact.text), source="review")
        store.erase(fact.id)
        return {"status": "vaulted"}
    raise HTTPException(status_code=400, detail="action must be release or vault")


@app.get("/memory/lockbox/reveal")
async def memory_lockbox_reveal(id: str) -> dict[str, Any]:
    """Credentialed click-to-reveal: the one path that returns secret text.
    Token-gated like every route; the dashboard additionally gates it on
    `controls` and never stores what it shows."""
    from kenzy.llm import lockbox

    _memory_or_503()
    box = lockbox.store()
    sec = box.reveal(id.strip()) if box is not None else None
    if sec is None:
        raise HTTPException(status_code=404, detail="no such secret")
    return {"id": sec.id, "label": sec.label, "text": sec.text, "value": sec.payload}


class MemoryUpdateBody(BaseModel):
    """Admin edit (dashboard): any subset of wording / tier / retention."""

    id: str
    text: str | None = None
    tier: str | None = None
    expires_days: float | None = None
    clear_expiry: bool = False


@app.post("/memory/update")
async def memory_update(body: MemoryUpdateBody) -> dict[str, Any]:
    store = _memory_or_503()
    if body.tier is not None and body.tier not in memory.TIERS:
        raise HTTPException(status_code=400, detail=f"unknown tier {body.tier!r}")
    fact = store.update(
        body.id,
        text=body.text,
        tier=body.tier,
        expires_days=body.expires_days,
        clear_expiry=body.clear_expiry,
    )
    if fact is None:
        raise HTTPException(status_code=404, detail="no such fact")
    return {"fact": _fact_dict(fact)}


class LockboxEraseBody(BaseModel):
    id: str


@app.post("/memory/lockbox/erase")
async def memory_lockbox_erase(body: LockboxEraseBody) -> dict[str, Any]:
    """Admin delete of one secret (the dashboard's Forget)."""
    from kenzy.llm import lockbox

    _memory_or_503()
    box = lockbox.store()
    if box is None or not box.erase_admin(body.id.strip()):
        raise HTTPException(status_code=404, detail="no such secret")
    return {"status": "ok"}


@app.get("/memory/export")
async def memory_export(person: str, secrets: int = 1) -> dict[str, Any]:
    """Everything OWNED by a person — the "what does Kenzy know about me"
    surface (F7.4). Includes their lockbox entries by default (founder call:
    the export answers "what does Kenzy know about me" COMPLETELY, and the
    requesting surface is already credentialed to Reveal); ``secrets=0``
    yields a shareable export without them."""
    store = _memory_or_503()
    out: dict[str, Any] = {"facts": [_fact_dict(f) for f in store.export(person)]}
    from kenzy.llm import lockbox

    box = lockbox.store()
    if box is not None:
        if secrets:
            out["secrets"] = [
                {"label": x.label, "value": x.payload, "text": x.text,
                 "created": x.created, "source": x.source}  # fmt: skip
                for x in box.list_for(person)
            ]
        else:
            out["secrets_excluded"] = len(box.list_for(person))
    return out


class CurationUpdate(BaseModel):
    # The full curation document (exclude / devices / rooms) to write.
    curation: dict[str, Any]


@app.get("/ha/persons")
async def get_ha_persons() -> dict[str, Any]:
    """HA person entities + HA-availability flags — the People page's "HA
    person" dropdown and the dashboard's HA-surface gating (F3). Cheap when HA
    isn't configured (no HA call at all)."""
    import os

    from kenzy.llm.builtin_skills import ha_model

    configured = bool(os.environ.get("HA_API_KEY"))
    skill_disabled = skill_registry.is_disabled("home_assistant")
    persons: list[dict[str, str]] = []
    reachable = False
    if configured and not skill_disabled:
        try:
            persons = await ha_model.fetch_persons()
            reachable = True
        except Exception as exc:
            log.warning("HA persons unavailable: %s", exc)
    return {
        "configured": configured,
        "skill_disabled": skill_disabled,
        "reachable": reachable,
        "persons": persons,
    }


@app.get("/ha/curation")
async def get_ha_curation() -> dict[str, Any]:
    """Current Home Assistant curation + the live device tree (for the editor)."""
    from kenzy.llm.builtin_skills import ha_model

    curation = ha_model.load_curation()
    devices: list[dict[str, Any]] = []
    lists: list[dict[str, str]] = []
    reachable = True
    try:
        raw = await ha_model.fetch_raw()
        devices = [vars(e) for e in ha_model.classify(raw, curation)]
    except Exception as exc:
        reachable = False
        log.warning("HA device list unavailable for curation editor: %s", exc)
    try:
        lists = await ha_model.fetch_todo_lists()
    except Exception as exc:
        log.warning("HA todo lists unavailable for curation editor: %s", exc)
    import os

    return {
        "curation": curation,
        "devices": devices,
        "lists": lists,
        "reachable": reachable,
        # Editor state hints: the tab stays editable when the skill is off
        # (curation is stageable config), but it says so honestly; with no HA
        # credential at all it shows onboarding guidance instead of an error.
        "skill_disabled": skill_registry.is_disabled("home_assistant"),
        "configured": bool(os.environ.get("HA_API_KEY")),
    }


@app.post("/ha/curation")
async def set_ha_curation(body: CurationUpdate) -> dict[str, Any]:
    """Validate + persist the curation document; drops the topology cache."""
    from kenzy.llm.builtin_skills import ha_model

    try:
        ha_model.save_curation(body.curation)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    log.info("Home Assistant curation updated")
    return {"ok": True, "curation": ha_model.load_curation()}


def _ask_prompt_response(outcome: Any, *, fast: bool) -> ProcessResponse:
    """A skill parked on ask(): the reply IS the question. The server speaks
    it, holds the floor (expect_response), and routes the answer to
    /process/continue. Actions queued BEFORE the ask are actuated now."""
    from kenzy.llm import asking

    parked = outcome.parked
    assert isinstance(parked, asking.Parked)
    ch = parked.channel
    # Actions ship from the CHANNEL's shared list and are DRAINED on every
    # turn — one carrier, one lifecycle. (take_actions() here would double-
    # dispatch pre-ask actions on the finished turn, and continue-turns have
    # no request-context accumulator at all — review findings M1/M2.)
    acts = list(ch.actions or [])
    if ch.actions:
        ch.actions.clear()
    return ProcessResponse(
        # Same-room: the reply IS the question. Cross-room: the asker hears the
        # announcement (may be empty) while the question travels to ask_room.
        text=ch.prompt if ch.room is None else ch.announce,
        voice_prompt=_voice_prompt,
        expect_response=True,
        actions=acts,
        fast=fast,
        secret=bool(ch.touch and ch.touch.get("lockbox")),
        spans=list(parked.meta.get("spans") or []),
        continuation=parked.id,
        ask_timeout_s=ch.timeout,
        ask_capture=ch.capture,
        ask_cue=ch.cue,
        ask_room=ch.room,
        ask_prompt=ch.prompt,
        ask_busy_cues=ch.busy_cues,
    )


class ContinueRequest(BaseModel):
    """The user's answer to a parked ask() — identity is the ANSWERER's.
    ``audio_b64`` carries the raw capture for audio-mode asks (text ignored)."""

    continuation: str
    text: str = ""
    audio_b64: str | None = None
    speaker: str | None = None
    person_id: str | None = None
    speaker_tier: str | None = None
    confidence: float | None = None
    tts_local: bool = False
    memory_opt_out: bool = False


@app.post("/process/continue", response_model=ProcessResponse)
async def process_continue(req: ContinueRequest) -> ProcessResponse:
    from kenzy import redact
    from kenzy.llm import asking, lockbox

    parked = asking.pending(req.continuation)
    if parked is None:
        raise HTTPException(status_code=404, detail="no such continuation")
    ch, meta, kind = parked.channel, parked.meta, parked.kind
    log.info("[continue/%s] %s", req.speaker or "?", redact.loggable(req.text))
    answerer = {
        "person_id": req.person_id,
        "speaker_tier": req.speaker_tier or "unknown",
        "confidence": req.confidence,
        "speaker": req.speaker,
        "tts_local": bool(req.tts_local),
        "memory_opt_out": bool(req.memory_opt_out),
    }
    # Snapshot the ANSWERED question's capture mode before resume — a chained
    # ask would overwrite the channel's fields with the NEXT question's.
    was_audio = parked.channel.capture == "audio"
    reply: Any = req.text
    if was_audio:
        import base64

        # b"" is a REAL value here (window expiry = empty sample → the skill's
        # retry path); None is reserved for cancel, which never reaches continue.
        reply = base64.b64decode(req.audio_b64 or "")
    outcome = await asking.resume(req.continuation, reply, answerer)
    if not outcome.finished:
        return _ask_prompt_response(outcome, fast=(kind == "fast"))

    actions = list(ch.actions or [])
    if ch.actions:
        ch.actions.clear()
    touched_lockbox = bool(ch.touch and ch.touch.get("lockbox"))
    touched_private = bool(ch.touch and ch.touch.get("private"))
    room = str(meta.get("room_id") or "")
    speaker = req.speaker or "unknown"

    if kind == "fast":
        fast_res = outcome.value
        # A resumed matcher that ultimately MISSES is a skill bug (the prompt
        # already went out) — close the dialog honestly rather than re-dispatch.
        text = fast_res.text if fast_res is not None else "Sorry — I lost track of that."
        vp = (fast_res.voice_prompt if fast_res else None) or _voice_prompt
        expect = bool(fast_res.expect_response) if fast_res else False
    else:
        text, vp, expect = outcome.value

    if not touched_lockbox and not was_audio:
        _history.add(
            room, speaker, req.text, text,
            private_to=(req.person_id if (req.person_id and touched_private) else None),
        )  # fmt: skip
        if not req.memory_opt_out:
            _short_term.add(req.person_id or "", req.text, text)
    secret_hits = 0
    if kind == "llm":
        text, secret_hits = lockbox.substitute(
            text, req.person_id, speak_values=bool(req.tts_local)
        )
    return ProcessResponse(
        text=text,
        voice_prompt=vp,
        expect_response=expect,
        actions=actions,
        fast=(kind == "fast"),
        secret=touched_lockbox or bool(secret_hits),
        spans=list(meta.get("spans") or []),
    )


class CancelRequest(BaseModel):
    continuation: str
    reason: str = "canceled"


@app.post("/process/cancel")
async def process_cancel(req: CancelRequest) -> dict[str, Any]:
    """Wake word / reply-window expiry / node disconnect: the parked skill
    gets None from ask() and its result is discarded. Unknown ids are fine
    (the answer won the race)."""
    from kenzy.llm import asking

    await asking.cancel(req.continuation, req.reason)
    return {"ok": True}


@app.post("/process", response_model=ProcessResponse)
async def process(req: ProcessRequest) -> ProcessResponse:
    return await _process_impl(req, None)


@app.post("/process/stream")
async def process_stream(req: ProcessRequest) -> Any:
    """Streaming twin of /process (4.4): newline-delimited JSON events.

    ``{"event":"tool","name":…}`` as tools run, ``{"event":"head",…}`` the
    moment the reply's header fields are known, ``{"event":"delta","text":…}``
    spoken-text previews, then ``{"event":"end", …}`` carrying the COMPLETE
    ProcessResponse — the authoritative record (identical semantics to
    /process: history, actions, lockbox substitution all happen there).
    Deltas are a prefix preview of end.text: the caller speaks deltas as they
    arrive and finishes with end.text's unspoken remainder. Fast-path replies
    and ask() questions arrive as a bare end event. A failure mid-stream is an
    ``{"event":"error"}`` line, never a broken pipe."""
    from fastapi.responses import StreamingResponse

    q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def sink(event: dict[str, Any]) -> None:
        await q.put(event)

    async def run() -> None:
        try:
            resp = await _process_impl(req, sink)
            await q.put({"event": "end", **resp.model_dump()})
        except Exception as exc:  # noqa: BLE001 — surfaced as an event line
            log.error("process/stream failed: %s", exc, exc_info=True)
            await q.put({"event": "error", "detail": str(exc)})
        finally:
            await q.put(None)

    task = asyncio.create_task(run())

    async def gen() -> Any:
        try:
            while True:
                ev = await q.get()
                if ev is None:
                    break
                yield (json.dumps(ev, ensure_ascii=False) + "\n").encode()
        finally:
            # Client hung up mid-reply (wake-word cancel): stop the pipeline.
            task.cancel()

    return StreamingResponse(gen(), media_type="application/x-ndjson")


async def _process_impl(
    req: ProcessRequest, sink: Callable[[dict[str, Any]], Awaitable[None]] | None
) -> ProcessResponse:
    # Preserve raw speaker for history; derive display name for logging.
    raw_speaker = req.speaker or "unknown"
    display_speaker = raw_speaker if raw_speaker.lower() != "unknown" else None
    from kenzy import redact
    from kenzy.llm import lockbox

    # A secret-store utterance carries the value — keep it out of this INFO
    # line (and therefore the ring buffer / Logs tab).
    log.info("[%s/%s] %s", req.room_id or "?", display_speaker or "?", redact.loggable(req.text))

    # Request-scoped accumulator for any server-side actions a skill queues, and
    # the server-injected context (rooms, active schedules) skills may read.
    skill_registry.begin_actions()
    memory.begin_touch()  # tracks whether private-tier memory shapes this answer
    skill_registry.begin_request(
        {
            "rooms": req.rooms,
            "schedules": req.schedules,
            "room_id": req.room_id,
            "no_aec_rooms": req.no_aec_rooms,
            "person_id": req.person_id,
            "speaker_tier": req.speaker_tier or "unknown",
            "confidence": req.confidence,
            "channel": req.channel or "voice",
            "memory_opt_out": bool(req.memory_opt_out),
            "memory_capture": req.memory_capture or "explicit",
            "tts_local": bool(req.tts_local),
            "people": req.people,
            "speaker_url": req.speaker_url,
        }
    )

    def _history_tag() -> str | None:
        # Turns built from private/personal memory replay only for their owner.
        return req.person_id if (req.person_id and memory.private_touched()) else None

    # Deterministic fast path: try local/instant matchers before the LLM.
    # Wrapped askable (4.2): a fast intent may await ask(...) — the dispatch
    # parks mid-matcher and the prompt goes back as a continuation.
    from kenzy.llm import asking

    _t0 = time.monotonic()
    outcome = await asking.run_askable(
        skill_registry.dispatch_fast(req.text, req.room_id, raw_speaker),
        kind="fast",
        meta={"room_id": req.room_id or "", "utterance": req.text, "spans": []},
    )
    if not outcome.finished:
        return _ask_prompt_response(outcome, fast=True)
    fast = outcome.value
    if fast is not None:
        fast_span = [
            {"kind": "fast", "name": fast.name, "ms": round((time.monotonic() - _t0) * 1000)}
        ]
        vp = fast.voice_prompt or _voice_prompt
        if memory.lockbox_touched():
            # The utterance or the reply carries a secret in the clear. History
            # and short-term feed future MODEL context (including cloud) — a
            # lockbox exchange never enters either (review finding H1).
            pass
        else:
            _history.add(
                req.room_id or "", raw_speaker, req.text, fast.text, private_to=_history_tag()
            )
            if not req.memory_opt_out:
                _short_term.add(req.person_id or "", req.text, fast.text)
        return ProcessResponse(
            text=fast.text,
            voice_prompt=vp,
            expect_response=fast.expect_response,
            actions=skill_registry.take_actions(),
            fast=True,
            secret=memory.lockbox_touched(),
            spans=fast_span,
        )

    spans: list[dict[str, Any]] = []
    outcome = await asking.run_askable(
        _run_llm(
            req.text,
            raw_speaker,
            req.room_id,
            available_rooms=req.rooms,
            schedules=req.schedules,
            spans=spans,
            sink=sink,
        ),
        kind="llm",
        meta={"room_id": req.room_id or "", "utterance": req.text, "spans": spans},
    )
    if not outcome.finished:
        return _ask_prompt_response(outcome, fast=False)
    text, voice_prompt, expect_response = outcome.value
    if not req.memory_opt_out:
        # Deliberately BEFORE substitution: short-term context (a future model
        # input) keeps the placeholder, never the secret value.
        _short_term.add(req.person_id or "", req.text, text)
    # 4.1 lockbox: deterministic value substitution, owner-scoped. Room
    # history (added inside _run_llm) and short-term above both pre-date this
    # — only the spoken reply carries the value.
    text, secret_hits = lockbox.substitute(
        text, req.person_id, speak_values=bool(req.tts_local)
    )
    return ProcessResponse(
        text=text,
        voice_prompt=voice_prompt,
        expect_response=expect_response,
        actions=skill_registry.take_actions(),
        fast=False,
        secret=bool(secret_hits) or memory.lockbox_touched(),
        spans=spans,
    )


# ---------------------------------------------------------------------------
# LLM + tool-calling loop
# ---------------------------------------------------------------------------


def _build_context() -> str:
    """Build a per-request context block with current date/time and location."""
    import datetime

    try:
        import zoneinfo

        tz: datetime.tzinfo | None = zoneinfo.ZoneInfo(_timezone) if _timezone else None
    except Exception:
        tz = None
    now = datetime.datetime.now(tz=tz)
    date_str = now.strftime("%A, %B %-d, %Y %-I:%M %p %Z").strip()
    lines = [f"Current date and time: {date_str}"]
    if _location:
        lines.append(f"Home location: {_location}")
    return "\n".join(lines)


def _parse_response(content: str) -> tuple[str, str, bool]:
    """Extract (text, voice_prompt, expect_response) from a JSON response.

    Tries increasingly lenient strategies before falling back to raw text:
      1. json.loads on the stripped content (strict=False: raw control chars
         inside strings — a literal newline in the text — are routine from
         prompt-tier local models and must parse, not fall through to the raw
         blob; 4.4 review finding).
      2. raw_decode — parses the first valid JSON object, ignores trailing garbage
         (e.g. extra characters the model appended after the closing brace).
      3. Regex search for any {...} block — handles leading/trailing prose or
         markdown code fences wrapping the JSON.
    """
    stripped = content.strip()

    # Strategy 1: clean JSON (tolerating raw control chars in strings).
    try:
        parsed = json.loads(stripped, strict=False)
        return (
            str(parsed["text"]),
            str(parsed.get("voice_prompt", _voice_prompt)),
            bool(parsed.get("expect_response", False)),
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        pass

    # Strategy 2: valid JSON followed by trailing garbage.
    try:
        parsed, _ = json.JSONDecoder().raw_decode(stripped)
        return (
            str(parsed["text"]),
            str(parsed.get("voice_prompt", _voice_prompt)),
            bool(parsed.get("expect_response", False)),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass

    # Strategy 3: JSON buried inside prose or markdown fences.
    m = re.search(r"\{.*?\}", stripped, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(0))
            return (
                str(parsed["text"]),
                str(parsed.get("voice_prompt", _voice_prompt)),
                bool(parsed.get("expect_response", False)),
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    log.warning("LLM response could not be parsed as JSON — speaking raw content")
    return stripped, _voice_prompt, False


def _schedule_context(schedules: list[dict[str, Any]]) -> str:
    """Render the asking room's active schedule entries for the system prompt."""
    lines = ["Active timers/alarms/reminders in this room (cancel via cancel_schedules):"]
    for s in schedules:
        kind = s.get("kind", "?")
        label = s.get("label") or ""
        when = s.get("at") or f"in {int(s.get('seconds_left', 0))}s"
        days = ",".join(s.get("days") or [])
        desc = f"- id={s.get('id')} {kind}"
        if label:
            desc += f" '{label}'"
        desc += f" at {when}" if s.get("at") else f" fires {when}"
        if days:
            desc += f" (every {days})"
        lines.append(desc)
    return "\n".join(lines)


def _holdback(s: str) -> tuple[str, str]:
    """Split streamable text into (safe-to-emit, held). Held starts at the
    earliest possible lockbox placeholder opener — ``[[`` or a trailing ``[`` —
    so a placeholder can never stream out before substitution. Once held,
    everything after ships via the end event instead."""
    i = s.find("[[")
    if i >= 0:
        return s[:i], s[i:]
    if s.endswith("["):
        return s[:-1], "["
    return s, ""


async def _stream_one_call(
    kwargs: dict[str, Any],
    fb_state: dict[str, Any],
    sink: Callable[[dict[str, Any]], Awaitable[None]],
) -> tuple[Any | None, tuple[str, str, bool] | None]:
    """One model call with ``stream=True``.

    A tool-call response is collected whole and rebuilt (the tool loop then
    proceeds exactly as buffered) → ``(message, None)``. A content response
    streams text previews through the sink as they arrive → ``(None, (text,
    voice_prompt, expect_response))``. Deltas are suppressed for lockbox
    exchanges and held back at a possible placeholder (see _holdback).

    Mid-stream failure: with nothing emitted yet, re-call buffered with
    ``tool_choice="none"`` (tools must not re-execute) and let the caller parse
    that message; with previews already spoken, what streamed IS the reply —
    return it as the result (honest: it's what the room heard)."""
    from kenzy.llm import streamparse

    chunks: list[Any] = []
    extract = streamparse.StreamExtract()
    is_tools = False
    emitted = ""  # text already sent as deltas (post-holdback)
    pending = ""  # extracted but held back
    head_sent = False

    async def _pump() -> None:
        nonlocal is_tools, emitted, pending, head_sent
        stream = await skill_registry.acompletion_with_fallback(
            {**kwargs, "stream": True}, fb_state
        )
        async for chunk in stream:
            chunks.append(chunk)
            delta = chunk.choices[0].delta if getattr(chunk, "choices", None) else None
            if delta is None:
                continue
            if getattr(delta, "tool_calls", None):
                is_tools = True
            content = getattr(delta, "content", None)
            if not content or is_tools:
                continue
            piece = extract.feed(content)
            if not head_sent and extract.head is not None:
                head_sent = True
                await sink({"event": "head", **extract.head})
            if piece and not memory.lockbox_touched():
                pending += piece
                safe, pending = _holdback(pending)
                if safe:
                    emitted += safe
                    await sink({"event": "delta", "text": safe})

    try:
        await _pump()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — degrade, never break the reply
        if emitted:
            log.warning("Model stream broke mid-reply (%s) — keeping what was spoken", exc)
            head = extract.head or {}
            spoken = emitted + pending if "[[" not in pending else emitted
            return None, (
                spoken,
                str(head.get("voice_prompt") or ""),
                bool(head.get("expect_response", False)),
            )
        log.warning("Model stream failed before any output (%s) — re-calling buffered", exc)
        response = await skill_registry.acompletion_with_fallback(
            {**kwargs, "tool_choice": "none"}, fb_state
        )
        return response.choices[0].message, None

    if is_tools:
        import litellm  # type: ignore[import-untyped]

        resp: Any = litellm.stream_chunk_builder(chunks)
        if resp is None or not getattr(resp, "choices", None):
            raise RuntimeError("tool-call stream produced no reconstructable response")
        return resp.choices[0].message, None

    parsed = extract.finalize()
    if parsed is None:
        # Not contract JSON — the lenient parser owns it (same as buffered).
        return SimpleNamespace(content=extract.buf, tool_calls=None), None
    return None, parsed


async def _run_llm(
    text: str,
    speaker: str,
    room_id: str | None,
    available_rooms: list[str] | None = None,
    schedules: list[dict[str, Any]] | None = None,
    spans: list[dict[str, Any]] | None = None,
    sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> tuple[str, str, bool]:
    # Per-request fallback state: once the primary model fails, the whole tool
    # loop stays on the configured local fallback (see skills.set_fallback).
    fb_state: dict[str, Any] = {}

    # Build current user message — named speakers only in the prefix.
    parts = []
    if room_id:
        parts.append(f"[request from room: {room_id}]")
    if speaker.lower() != "unknown":
        parts.append(f"[speaker: {speaker}]")
    parts.append(text)
    user_content = " ".join(parts)

    # Inject conversation history between system message and current turn.
    viewer = skill_registry.get_request("person_id")
    history_messages = _history.get_messages(room_id or "", str(viewer) if viewer else None)

    # Static head + dynamic tail: everything per-request (the clock line first
    # among them) stays OUT of the cacheable prefix — see _system_message.
    static_head = f"{_system_prompt}\n{_JSON_INSTRUCTION}"
    dynamic_parts = [_build_context()]
    if available_rooms:
        dynamic_parts.append("Connected rooms: " + ", ".join(available_rooms))
    if schedules:
        dynamic_parts.append(_schedule_context(schedules))
    mem = _memory_context(text)
    if mem:
        dynamic_parts.append(mem)
    if (
        skill_registry.get_request("memory_capture") == "auto"
        and not skill_registry.get_request("memory_opt_out")
        and skill_registry.get_request("person_id")
    ):
        dynamic_parts.append(
            "This speaker enabled AUTO memory capture: when they state a durable "
            "personal fact (a preference, a name, a date, a code) even without "
            "saying 'remember', store it with the remember tool and briefly say "
            "you did. Never store small talk or transient states."
        )
    elif (
        skill_registry.get_request("memory_capture") == "suggest"
        and not skill_registry.get_request("memory_opt_out")
        and skill_registry.get_request("person_id")
        and skill_registry.get_request("channel") == "voice"
    ):
        dynamic_parts.append(
            "This speaker enabled SUGGEST memory capture: when they state a "
            "durable personal fact (a preference, a name, a date) even without "
            "saying 'remember', call offer_to_remember(fact) — it asks them "
            "aloud first and only stores on their yes. Never offer for small "
            "talk or transient states, and never more than once per exchange."
        )

    messages: list[dict[str, Any]] = [
        _system_message(static_head, "\n".join(dynamic_parts)),
        *history_messages,
        {"role": "user", "content": user_content},
    ]

    tools = skill_registry.get_tools()
    kwargs: dict[str, Any] = {
        "model": _model,
        "messages": messages,
    }
    kwargs.update(_params)  # operator latency/behavior knobs (llm.yaml params:)
    # Portability for those knobs: providers that don't support a param get it
    # dropped instead of erroring (e.g. reasoning_effort on Ollama or gpt-4o).
    kwargs.setdefault("drop_params", True)
    # Structured outputs: force the three-field reply schema so expect_response is
    # always present (reliable floor-holding). Dropped for providers that don't
    # support it, which fall back to the prompt-described JSON.
    kwargs.setdefault("response_format", _response_format())
    # Custom endpoint (Ollama/LM Studio/proxy): never lets OPENAI_API_KEY ride
    # to a dashboard-editable URL — see skills.endpoint_kwargs (F-14). Applied
    # AFTER params so credentials/routing can't be overridden from that block.
    kwargs.update(skill_registry.endpoint_kwargs(_base_url))
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    for iteration in range(_max_tool_iterations):
        _t = time.monotonic()
        triplet: tuple[str, str, bool] | None = None
        if sink is None:
            response = await skill_registry.acompletion_with_fallback(kwargs, fb_state)
            message = response.choices[0].message
        else:
            message, triplet = await _stream_one_call(kwargs, fb_state, sink)
        if spans is not None:
            # The name records which model ACTUALLY answered — once the primary
            # fails, fb_state pins the loop to the fallback and the spans show it.
            spans.append(
                {
                    "kind": "model",
                    "name": str(
                        skill_registry.fallback_model() if fb_state.get("fallback") else _model
                    ),
                    "ms": round((time.monotonic() - _t) * 1000),
                }
            )
        if triplet is not None:
            # Streamed content reply: the previews already went through the
            # sink; this is the same result, authoritatively parsed.
            spoken, vp, expect = triplet
            _history.add(room_id or "", speaker, text, spoken, private_to=_llm_history_tag())
            return spoken, vp, expect

        # Serialise the assistant turn back into the message list.
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": message.content or "",
        }
        if message.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in message.tool_calls
            ]
        messages.append(assistant_msg)

        if not message.tool_calls:
            spoken, vp, expect = _parse_response(message.content or "")
            _history.add(room_id or "", speaker, text, spoken, private_to=_llm_history_tag())
            return spoken, vp, expect

        # Execute each tool call and append results.
        log.debug(
            "Tool calls (iteration %d): %s",
            iteration + 1,
            [tc.function.name for tc in message.tool_calls],
        )

        for tc in message.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if sink is not None:
                await sink({"event": "tool", "name": tc.function.name})
            _t = time.monotonic()
            result = await skill_registry.execute(tc.function.name, args)
            if spans is not None:
                spans.append(
                    {
                        "kind": "tool",
                        "name": tc.function.name,
                        "ms": round((time.monotonic() - _t) * 1000),
                    }
                )
            log.debug("  %s(%s) → %s", tc.function.name, args, result[:120])
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                }
            )

        # Feed tool results back to the LLM.
        kwargs["messages"] = messages

    log.warning("Reached max tool iterations (%d) — returning last content", _max_tool_iterations)
    spoken, vp, expect = _parse_response(
        message.content or "I wasn't able to complete that request."
    )
    _history.add(room_id or "", speaker, text, spoken, private_to=_llm_history_tag())
    return spoken, vp, expect


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import uvicorn  # type: ignore[import-untyped]
    from dotenv import load_dotenv  # type: ignore[import-untyped]

    load_dotenv()

    from kenzy.logutil import configure_logging, level_value
    from kenzy.serviceboot import (
        effective_bind,
        load_service_config,
        populate_data,
        start_registration,
    )

    configure_logging(logging.INFO)  # provisional, so the config pull's retries are visible

    # Central config: pull from the server (blocking until it answers); an explicit
    # config path loads locally instead (dev/offline escape hatch).
    cfg: dict[str, Any] = load_service_config("llm")
    populate_data("llm")  # fetch skills + curation from the server if this host has none
    start_registration("llm", cfg)  # auto-announce to the server (dashboard + pipeline)

    configure_logging(level_value(cfg.get("log_level"), logging.INFO))
    quiet_health_access_log()
    install_service_auth(app)
    install_logs_endpoint(
        app, capture_level=level_value(cfg.get("log_capture_level"), logging.DEBUG)
    )
    install_restart_endpoint(app)
    install_upgrade_endpoint(app, "llm")
    install_unit_endpoint(app, "kenzy-llm.service")

    from kenzy.fastapi_auth import install_features_endpoint, install_fill_endpoint
    from kenzy.features import feature
    from kenzy.llm import lockbox as _lb
    from kenzy.llm.locality import model_is_local as _mil

    def _features() -> list[dict[str, Any]]:
        mcfg = cfg.get("memory", {}) if isinstance(cfg.get("memory"), dict) else {}
        cls_model = str(mcfg.get("classifier_model") or "") or _model
        cls_url = str(mcfg.get("classifier_url") or "") or _base_url
        return [
            feature(
                "lockbox",
                configured=bool(mcfg.get("enabled", True)),
                available=_lb.available(),
                active=_lb.store() is not None,
                note="Encrypted secret storage — needs the 'cryptography' package.",
            ),
            feature(
                "local-classifier",
                configured=bool(mcfg.get("enabled", True)),
                available=bool(cls_model and _mil(cls_model, cls_url)),
                active=bool(cls_model and _mil(cls_model, cls_url)),
                install="",
                note=(
                    "Ambiguous memory writes are auto-resolved by a local model."
                    if cls_model and _mil(cls_model, cls_url)
                    else "No local model — ambiguous writes are held for dashboard "
                    "review (set memory.classifier_model to a local model)."
                ),
            ),
        ]

    install_features_endpoint(app, _features)
    install_fill_endpoint(app, "llm")
    # Backup slice: this host's user skills overlay + HA curation (they live with
    # the LLM service, not the server), merged into the server's backup archive.
    install_backup_endpoint(app, _backup_items)

    global \
        _model, \
        _base_url, \
        _system_prompt, \
        _voice_prompt, \
        _max_tool_iterations, \
        _location, \
        _timezone, \
        _skills_dir
    _model = str(cfg.get("model", "gpt-4o"))
    _base_url = cfg.get("base_url") or None
    _system_prompt = str(cfg.get("system_prompt", _system_prompt))
    _voice_prompt = str(cfg.get("voice_prompt", _voice_prompt))
    _max_tool_iterations = int(cfg.get("max_tool_iterations", 10))

    loc = cfg.get("location", {})
    _location = ", ".join(filter(None, [loc.get("city"), loc.get("state"), loc.get("country")]))
    _timezone = str(loc.get("timezone", ""))
    if _location:
        log.info("Location: %s (tz=%s)", _location, _timezone or "system local")

    log.info("LLM: model=%s base_url=%s", _model, _base_url or "(provider default)")

    # Memory (F2): the fact ledger, on unless the operator turns it off. The
    # file lives in the config home's data/ tree (rides the llm backup slice).
    global _private_to_cloud
    mem_cfg = cfg.get("memory", {}) if isinstance(cfg.get("memory"), dict) else {}
    _private_to_cloud = bool(mem_cfg.get("private_to_cloud", False))
    if mem_cfg.get("enabled", True):
        from kenzy.config import kenzy_data_root as _kdr

        rel = str(mem_cfg.get("file", "data/memory/facts.jsonl"))
        memory.init_store(_kdr() / rel)
        # The lockbox (4.1): encrypted secret store beside the ledger. Degrades
        # honestly (disabled + one log line) when `cryptography` is absent.
        from kenzy.llm import lockbox as _lockbox

        _lockbox.init_store(_kdr() / "data" / "memory" / "lockbox.enc")

        # Live People-page refresh: any memory/lockbox mutation pokes the
        # server (debounced — classifier bursts coalesce), which pushes a
        # data-less {"type":"memory"} to dashboard browsers. Best-effort:
        # no server, no problem (the page's poll remains the fallback).
        import asyncio

        _poke_state: dict[str, Any] = {"pending": False}

        async def _poke_server() -> None:
            await asyncio.sleep(1.0)
            _poke_state["pending"] = False
            from kenzy import serviceboot, tlsutil
            from kenzy.serviceauth import service_token_from_env, sign_service_request

            base = serviceboot.server_base()
            if not base:
                log.debug("memory poke skipped (no server base yet)")
                return
            # Token-OPTIONAL, like every service-to-service call: a tokenless
            # mesh sends no auth header and the server's no-op gate admits it.
            token = service_token_from_env()
            headers = (
                {"X-Kenzy-Auth": sign_service_request(token, "GET", "/notify")} if token else {}
            )
            import httpx

            url = f"{base}/notify?what=memory"
            try:
                async with httpx.AsyncClient(verify=tlsutil.httpx_verify()) as client:
                    await client.get(url, timeout=5.0, headers=headers)
            except Exception as exc:
                log.debug("memory poke failed: %s", exc)

        def _on_memory_change() -> None:
            if _poke_state["pending"]:
                return
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # outside the service loop (CLI/tests) — nothing to poke
            _poke_state["pending"] = True
            loop.create_task(_poke_server())

        memory.set_change_hook(_on_memory_change)

    # Background jobs (F5.5 thin): the one runner for this service's periodic
    # work. GET /jobs shows every run; interval 0 disables a job (the runner
    # still mounts).
    from kenzy.jobs import Job, JobRunner, install_jobs_endpoint

    runner = JobRunner()
    interval = float(mem_cfg.get("maintenance_interval", 60))
    keep_days = float(mem_cfg.get("superseded_keep_days", 30))
    if memory.store() is not None and interval > 0:
        # Mechanical consolidation (F2.7's no-model half): expiry, supersession
        # retention, exact dedupe. Hourly, free.
        async def _consolidate() -> dict[str, Any] | None:
            store = memory.store()
            return store.consolidate(superseded_keep_days=keep_days) if store else None

        # Kicked after each release (below) with a 30s cooldown, so exact
        # repeats coalesce in seconds — the hourly interval is the backstop.
        runner.register(
            Job("memory-consolidation", interval, _consolidate, scope="memory", cooldown=30)
        )

    # Semantic consolidation (model-assisted): same-thought merging/updating.
    # Kicked by every remember (write hook), 30s cooldown coalesces bursts,
    # failed runs retry in ~15 min, the daily interval is only the backstop.
    sem_interval = float(mem_cfg.get("semantic_interval", 86400))
    sem_cooldown = float(mem_cfg.get("semantic_cooldown", 30))
    store_now = memory.store()
    if store_now is not None and sem_interval > 0:
        from kenzy.llm import memory_semantic

        memory_semantic.configure(
            _model,
            _base_url,
            private_to_cloud=_private_to_cloud,
            classifier_model=str(mem_cfg.get("classifier_model", "")),
            classifier_url=(
                str(mem_cfg.get("classifier_url")) if mem_cfg.get("classifier_url") else None
            ),
        )

        async def _semantic() -> dict[str, Any] | None:
            store = memory.store()
            return await memory_semantic.run_pass(store) if store else None

        runner.register(
            Job(
                "memory-consolidation-semantic",
                sem_interval,
                _semantic,
                scope="memory",
                cooldown=sem_cooldown,
                retry_after=900,
            )
        )
        store_now.on_write = lambda: runner.kick("memory-consolidation-semantic")

    # The release job (4.1 quarantine pipeline): fresh writes are born
    # quarantined — owner-only, invisible to every model — and the CLASSIFIER
    # judges each one: release / vault (→ lockbox) / split / hold-for-review.
    # Kicked by every write, ahead of consolidation (which skips quarantined
    # facts, so ordering is safe even when kicks race).
    if store_now is not None:
        from kenzy.llm import memory_classifier

        memory_classifier.configure(
            _model,
            _base_url,
            classifier_model=str(mem_cfg.get("classifier_model", "")),
            classifier_url=(
                str(mem_cfg.get("classifier_url")) if mem_cfg.get("classifier_url") else None
            ),
            keep_alive=str(mem_cfg.get("classifier_keep_alive", "") or ""),
        )

        async def _release() -> dict[str, Any] | None:
            store = memory.store()
            if store is None:
                return None
            summary = await memory_classifier.classify_pending(store)
            if summary.get("released") or summary.get("split"):
                runner.kick("memory-consolidation")  # exact dedupe, no model
                runner.kick("memory-consolidation-semantic")
            return summary

        runner.register(
            Job("memory-release", 60, _release, scope="memory", cooldown=2)
        )
        prev_kick = store_now.on_write

        def _on_write(prev: Callable[[], None] | None = prev_kick) -> None:
            runner.kick("memory-release")
            if prev is not None:
                prev()

        store_now.on_write = _on_write
    install_jobs_endpoint(app, runner)

    # Built-in skills ship inside the package; the configured directory is a
    # user overlay (default: skills/ under the config home — repo root in a dev
    # checkout, ~/.config/kenzy otherwise). A relative skills.dir resolves
    # against that operational-tree root.
    skills_cfg = cfg.get("skills", {})
    # Make top-level location available to skills via get_config("location", ...)
    skills_cfg["location"] = cfg.get("location", {})
    disabled = skills_cfg.get("disabled", [])

    from kenzy.config import kenzy_data_root

    raw_dir = skills_cfg.get("dir", "skills")
    user_dir = Path(raw_dir) if Path(raw_dir).is_absolute() else kenzy_data_root() / raw_dir
    _skills_dir = user_dir  # exposed via GET /backup (the server's merged archive)

    global _params
    raw_params = cfg.get("params") or {}
    if isinstance(raw_params, dict):
        # Empty string = "don't send this parameter at all" (the dashboard's
        # escape hatch — e.g. models where an explicit reasoning_effort routes
        # slower than omitting it).
        _params = {
            k: v for k, v in raw_params.items() if k not in _PARAMS_BLOCKED and v not in ("", None)
        }
        # Only genuinely reserved keys deserve a warning; an empty value is the
        # documented "don't send it" state (the packaged default), not a problem.
        blocked = sorted(k for k in raw_params if k in _PARAMS_BLOCKED)
        if blocked:
            log.warning("llm params: ignoring reserved keys %s", blocked)
        if _params:
            log.info("LLM params: %s", _params)

    # Optional local fallback model (silent retry on primary failure; if the
    # fallback also fails the user just gets the spoken error cue).
    fb = cfg.get("fallback") or {}
    skill_registry.set_fallback(fb.get("model"), fb.get("base_url"))
    if fb.get("model"):
        log.info("LLM fallback: %s (base_url=%s)", fb["model"], fb.get("base_url") or "default")

    skill_registry.set_config(skills_cfg)
    skill_registry.load_skills(user_dir, disabled)

    from kenzy import tlsutil

    uvicorn.run(
        app,
        host=effective_bind(cfg),
        port=int(cfg.get("port", 8766)),
        log_level=str(cfg.get("log_level", "info")).lower(),
        **tlsutil.uvicorn_tls_kwargs(cfg),
    )


if __name__ == "__main__":
    main()
