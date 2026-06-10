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
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from kenzy.llm import skills as skill_registry

log = logging.getLogger(__name__)


def _find_project_root() -> Path:
    """Walk up from CWD until pyproject.toml is found."""
    for path in [Path.cwd(), *Path.cwd().parents]:
        if (path / "pyproject.toml").exists():
            return path
    return Path.cwd()

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ProcessRequest(BaseModel):
    text: str
    room_id: str | None = None
    session_id: str | None = None
    speaker: str | None = None


class ProcessResponse(BaseModel):
    text: str
    voice_prompt: str


# ---------------------------------------------------------------------------
# App + module-level state
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------

@dataclass
class _Turn:
    timestamp:      float
    speaker:        str    # raw value — "unknown" if not identified
    user_text:      str
    assistant_text: str    # spoken text only (no JSON wrapper, no tool internals)


class ConversationHistory:
    """Per-room rolling history of the last N turns within a time window."""

    MAX_TURNS: int   = 10
    MAX_AGE:   float = 180.0   # seconds (3 minutes)

    def __init__(self) -> None:
        self._rooms: dict[str, list[_Turn]] = {}

    def _prune(self, room_id: str) -> None:
        turns   = self._rooms.get(room_id, [])
        cutoff  = time.time() - self.MAX_AGE
        turns   = [t for t in turns if t.timestamp >= cutoff]
        if len(turns) > self.MAX_TURNS:
            turns = turns[-self.MAX_TURNS:]
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
            out.append({"role": "user",      "content": f"[{label}] {turn.user_text}"})
            out.append({"role": "assistant", "content": turn.assistant_text})
        return out


_history: ConversationHistory = ConversationHistory()


# ---------------------------------------------------------------------------
# App + module-level config
# ---------------------------------------------------------------------------

app = FastAPI(title="Kenzy LLM Service", version="0.1.0")

_model:               str = "gpt-4o"
_base_url:            str | None = None
_system_prompt:       str = "You are Kenzy, a helpful home assistant. Be concise."
_voice_prompt:        str = "Respond in a friendly, conversational tone."
_max_tool_iterations: int = 5
_location:            str = ""   # "City, State, Country" assembled at startup
_timezone:            str = ""   # IANA timezone string e.g. "America/Chicago"

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
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/process", response_model=ProcessResponse)
async def process(req: ProcessRequest) -> ProcessResponse:
    # Preserve raw speaker for history; derive display name for logging.
    raw_speaker     = req.speaker or "unknown"
    display_speaker = raw_speaker if raw_speaker.lower() != "unknown" else None
    log.info("[%s/%s] %s", req.room_id or "?", display_speaker or "?", req.text)
    text, voice_prompt = await _run_llm(req.text, raw_speaker, req.room_id)
    return ProcessResponse(text=text, voice_prompt=voice_prompt)


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


async def _run_llm(text: str, speaker: str, room_id: str | None) -> tuple[str, str]:
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

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": f"{_system_prompt}\n\n{_build_context()}\n{_JSON_INSTRUCTION}"},
        *history_messages,
        {"role": "user",   "content": user_content},
    ]

    tools = skill_registry.get_tools()
    kwargs: dict[str, Any] = {
        "model":    _model,
        "messages": messages,
    }
    if _base_url:
        kwargs["base_url"] = _base_url
    if tools:
        kwargs["tools"]       = tools
        kwargs["tool_choice"] = "auto"

    for iteration in range(_max_tool_iterations):
        response = await acompletion(**kwargs)
        message  = response.choices[0].message

        # Serialise the assistant turn back into the message list.
        assistant_msg: dict[str, Any] = {
            "role":    "assistant",
            "content": message.content or "",
        }
        if message.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id":       tc.id,
                    "type":     "function",
                    "function": {
                        "name":      tc.function.name,
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
        log.debug("Tool calls (iteration %d): %s",
                  iteration + 1,
                  [tc.function.name for tc in message.tool_calls])

        for tc in message.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await skill_registry.execute(tc.function.name, args)
            log.debug("  %s(%s) → %s", tc.function.name, args, result[:120])
            messages.append({
                "role":         "tool",
                "tool_call_id": tc.id,
                "content":      result,
            })

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
    import yaml  # type: ignore[import-untyped]
    from dotenv import load_dotenv  # type: ignore[import-untyped]

    load_dotenv()

    config_path = sys.argv[1] if len(sys.argv) > 1 else "configs/llm.yaml"
    with open(config_path) as fh:
        cfg: dict[str, Any] = yaml.safe_load(fh)

    log_level: int = getattr(logging, str(cfg.get("log_level", "info")).upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=logging.WARNING, format=fmt)
    logging.getLogger("kenzy").setLevel(log_level)

    global _model, _base_url, _system_prompt, _voice_prompt, _max_tool_iterations, _location, _timezone
    _model               = str(cfg.get("model", "gpt-4o"))
    _base_url            = cfg.get("base_url") or None
    _system_prompt       = str(cfg.get("system_prompt", _system_prompt))
    _voice_prompt        = str(cfg.get("voice_prompt", _voice_prompt))
    _max_tool_iterations = int(cfg.get("max_tool_iterations", 5))

    loc = cfg.get("location", {})
    _location = ", ".join(filter(None, [
        loc.get("city"), loc.get("state"), loc.get("country")
    ]))
    _timezone = str(loc.get("timezone", ""))
    if _location:
        log.info("Location: %s (tz=%s)", _location, _timezone or "system local")

    log.info("LLM: model=%s base_url=%s", _model, _base_url or "(provider default)")

    # Load skills from the configured directory (default: skills/ at project root).
    # The framework (__init__.py) stays in the package; skill files live outside it.
    skills_cfg = cfg.get("skills", {})
    # Make top-level location available to skills via get_config("location", ...)
    skills_cfg["location"] = cfg.get("location", {})
    disabled = skills_cfg.get("disabled", [])

    raw_dir  = skills_cfg.get("dir", "skills")
    user_dir = Path(raw_dir) if Path(raw_dir).is_absolute() else _find_project_root() / raw_dir

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
