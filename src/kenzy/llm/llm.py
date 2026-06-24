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

from kenzy import kenzy_version
from kenzy.fastapi_auth import (
    install_logs_endpoint,
    install_restart_endpoint,
    install_service_auth,
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
_location: str = ""  # "City, State, Country" assembled at startup
_timezone: str = ""  # IANA timezone string e.g. "America/Chicago"

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

The object must contain exactly two fields:
  "text": your spoken response (read aloud — plain prose, no markdown)
  "voice_prompt": a short TTS instruction describing tone, pace, and style

Example (output this format exactly):
{"text": "The lights are on.", "voice_prompt": "Speak naturally at a conversational pace."}"""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, object]:
    return {"status": "ok", "version": kenzy_version(), "model": _model}


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


@app.post("/process", response_model=ProcessResponse)
async def process(req: ProcessRequest) -> ProcessResponse:
    # Preserve raw speaker for history; derive display name for logging.
    raw_speaker = req.speaker or "unknown"
    display_speaker = raw_speaker if raw_speaker.lower() != "unknown" else None
    log.info("[%s/%s] %s", req.room_id or "?", display_speaker or "?", req.text)

    # Request-scoped accumulator for any server-side actions a skill queues.
    skill_registry.begin_actions()

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

    text, voice_prompt = await _run_llm(
        req.text, raw_speaker, req.room_id, available_rooms=req.rooms
    )
    return ProcessResponse(
        text=text, voice_prompt=voice_prompt, actions=skill_registry.take_actions(), fast=False
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


def _parse_response(content: str) -> tuple[str, str]:
    """Extract (text, voice_prompt) from a JSON response.

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
        return str(parsed["text"]), str(parsed.get("voice_prompt", _voice_prompt))
    except (json.JSONDecodeError, KeyError, TypeError):
        pass

    # Strategy 2: valid JSON followed by trailing garbage.
    try:
        parsed, _ = json.JSONDecoder().raw_decode(stripped)
        return str(parsed["text"]), str(parsed.get("voice_prompt", _voice_prompt))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass

    # Strategy 3: JSON buried inside prose or markdown fences.
    m = re.search(r"\{.*?\}", stripped, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(0))
            return str(parsed["text"]), str(parsed.get("voice_prompt", _voice_prompt))
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    log.warning("LLM response could not be parsed as JSON — speaking raw content")
    return stripped, _voice_prompt


async def _run_llm(
    text: str,
    speaker: str,
    room_id: str | None,
    available_rooms: list[str] | None = None,
) -> tuple[str, str]:
    from litellm import acompletion  # type: ignore[import-untyped]

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
    if _base_url:
        kwargs["base_url"] = _base_url
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    for iteration in range(_max_tool_iterations):
        response = await acompletion(**kwargs)
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
            spoken, vp = _parse_response(message.content or "")
            _history.add(room_id or "", speaker, text, spoken)
            return spoken, vp

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
    spoken, vp = _parse_response(message.content or "I wasn't able to complete that request.")
    _history.add(room_id or "", speaker, text, spoken)
    return spoken, vp


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import uvicorn  # type: ignore[import-untyped]
    from dotenv import load_dotenv  # type: ignore[import-untyped]

    load_dotenv()

    from kenzy.logutil import configure_logging, level_value
    from kenzy.serviceboot import load_service_config

    configure_logging(logging.INFO)  # provisional, so the config pull's retries are visible

    # Central config: pull from the server (blocking until it answers); an explicit
    # config path loads locally instead (dev/offline escape hatch).
    cfg: dict[str, Any] = load_service_config("llm")

    configure_logging(level_value(cfg.get("log_level"), logging.INFO))
    quiet_health_access_log()
    install_service_auth(app)
    install_logs_endpoint(
        app, capture_level=level_value(cfg.get("log_capture_level"), logging.DEBUG)
    )
    install_restart_endpoint(app)

    global \
        _model, \
        _base_url, \
        _system_prompt, \
        _voice_prompt, \
        _max_tool_iterations, \
        _location, \
        _timezone
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

    skill_registry.set_config(skills_cfg)
    skill_registry.load_skills(user_dir, disabled)

    uvicorn.run(
        app,
        host=cfg.get("host", "127.0.0.1"),
        port=int(cfg.get("port", 8766)),
        log_level=str(cfg.get("log_level", "info")).lower(),
    )


if __name__ == "__main__":
    main()
