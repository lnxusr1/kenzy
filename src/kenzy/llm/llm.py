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

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from kenzy import version_info
from kenzy.fastapi_auth import (
    install_backup_endpoint,
    install_logs_endpoint,
    install_restart_endpoint,
    install_service_auth,
    install_upgrade_endpoint,
)
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

    def add(self, room_id: str, speaker: str, user_text: str, assistant_text: str) -> None:
        if room_id not in self._rooms:
            self._rooms[room_id] = []
        self._prune(room_id)
        self._rooms[room_id].append(
            _Turn(
                timestamp=time.time(),
                speaker=speaker,
                user_text=user_text,
                assistant_text=assistant_text,
            )
        )

    def get_messages(self, room_id: str) -> list[dict[str, Any]]:
        """Return history as alternating role:user / role:assistant dicts."""
        self._prune(room_id)
        out: list[dict[str, Any]] = []
        for turn in self._rooms.get(room_id, []):
            label = turn.speaker if turn.speaker.lower() != "unknown" else "unidentified speaker"
            out.append({"role": "user", "content": f"[{label}] {turn.user_text}"})
            out.append({"role": "assistant", "content": turn.assistant_text})
        return out


_history: ConversationHistory = ConversationHistory()


# ---------------------------------------------------------------------------
# App + module-level config
# ---------------------------------------------------------------------------

app = FastAPI(title="Kenzy LLM Service", version="0.1.0")

_model: str = "gpt-4o"
_base_url: str | None = None
_system_prompt: str = "You are Kenzy, a helpful home assistant. Be concise."
_voice_prompt: str = "Respond in a friendly, conversational tone."
_max_tool_iterations: int = 5
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
        (kenzy_data_root() / "data" / "home_assistant", "data/home_assistant")
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

The object must contain two required fields and one optional field:
  "text": your spoken response (read aloud — plain prose, no markdown)
  "voice_prompt": a short TTS instruction describing tone, pace, and style
  "expect_response" (optional, default false): whether to keep the microphone open
      for the user's reply without requiring the wake word again.

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
{"text": "The lights are on.", "voice_prompt": "Speak naturally at a conversational pace."}
Example holding the floor (incomplete setup):
{"text": "Knock knock.", "voice_prompt": "Playful tone.", "expect_response": true}
Example holding the floor (a question you're waiting on):
{"text": "What's your favorite color?", "voice_prompt": "Curious.", "expect_response": true}"""


# The same three-field contract as a JSON schema. Passed as `response_format` so
# providers that support structured outputs (OpenAI/gpt-5.x, etc.) MUST emit all
# three fields — including expect_response, which the model otherwise drops
# intermittently, silently defaulting the floor-hold to false. This fixes the
# reliability at the output mechanism instead of parsing the reply text. The
# prompt instruction above stays as the fallback for providers that don't support
# it (drop_params removes response_format for them; _parse_response still copes).
_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "The spoken response, read aloud."},
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
    },
    "required": ["text", "voice_prompt", "expect_response"],
    "additionalProperties": False,
}


def _response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": "kenzy_reply", "strict": True, "schema": _RESPONSE_SCHEMA},
    }


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


class CurationUpdate(BaseModel):
    # The full curation document (exclude / devices / rooms) to write.
    curation: dict[str, Any]


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


@app.post("/process", response_model=ProcessResponse)
async def process(req: ProcessRequest) -> ProcessResponse:
    # Preserve raw speaker for history; derive display name for logging.
    raw_speaker = req.speaker or "unknown"
    display_speaker = raw_speaker if raw_speaker.lower() != "unknown" else None
    log.info("[%s/%s] %s", req.room_id or "?", display_speaker or "?", req.text)

    # Request-scoped accumulator for any server-side actions a skill queues, and
    # the server-injected context (rooms, active schedules) skills may read.
    skill_registry.begin_actions()
    skill_registry.begin_request(
        {
            "rooms": req.rooms,
            "schedules": req.schedules,
            "room_id": req.room_id,
            "no_aec_rooms": req.no_aec_rooms,
        }
    )

    # Deterministic fast path: try local/instant matchers before the LLM.
    fast = await skill_registry.dispatch_fast(req.text, req.room_id, raw_speaker)
    if fast is not None:
        vp = fast.voice_prompt or _voice_prompt
        _history.add(req.room_id or "", raw_speaker, req.text, fast.text)
        return ProcessResponse(
            text=fast.text,
            voice_prompt=vp,
            expect_response=fast.expect_response,
            actions=skill_registry.take_actions(),
            fast=True,
        )

    text, voice_prompt, expect_response = await _run_llm(
        req.text, raw_speaker, req.room_id, available_rooms=req.rooms, schedules=req.schedules
    )
    return ProcessResponse(
        text=text,
        voice_prompt=voice_prompt,
        expect_response=expect_response,
        actions=skill_registry.take_actions(),
        fast=False,
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
      1. Strict json.loads on the stripped content.
      2. raw_decode — parses the first valid JSON object, ignores trailing garbage
         (e.g. extra characters the model appended after the closing brace).
      3. Regex search for any {...} block — handles leading/trailing prose or
         markdown code fences wrapping the JSON.
    """
    stripped = content.strip()

    # Strategy 1: clean JSON.
    try:
        parsed = json.loads(stripped)
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


async def _run_llm(
    text: str,
    speaker: str,
    room_id: str | None,
    available_rooms: list[str] | None = None,
    schedules: list[dict[str, Any]] | None = None,
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
    history_messages = _history.get_messages(room_id or "")

    system_content = f"{_system_prompt}\n\n{_build_context()}"
    if available_rooms:
        system_content += "\nConnected rooms: " + ", ".join(available_rooms)
    if schedules:
        system_content += "\n" + _schedule_context(schedules)
    system_content += f"\n{_JSON_INSTRUCTION}"

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_content},
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
        response = await skill_registry.acompletion_with_fallback(kwargs, fb_state)
        message = response.choices[0].message

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
            _history.add(room_id or "", speaker, text, spoken)
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
            result = await skill_registry.execute(tc.function.name, args)
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
    _history.add(room_id or "", speaker, text, spoken)
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
    _max_tool_iterations = int(cfg.get("max_tool_iterations", 5))

    loc = cfg.get("location", {})
    _location = ", ".join(filter(None, [loc.get("city"), loc.get("state"), loc.get("country")]))
    _timezone = str(loc.get("timezone", ""))
    if _location:
        log.info("Location: %s (tz=%s)", _location, _timezone or "system local")

    log.info("LLM: model=%s base_url=%s", _model, _base_url or "(provider default)")

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
        dropped = set(raw_params) - set(_params)
        if dropped:
            log.warning("llm params: ignoring reserved keys %s", sorted(dropped))
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
