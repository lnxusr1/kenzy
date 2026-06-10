"""
Home Assistant skill — device control via YAML device map + LLM resolution.

On each home control request:
  1. Reads device_ids.yaml (human-readable alias hierarchy) as text
  2. Sends the YAML + user request to a sub-LLM call to resolve device aliases
  3. Looks up each alias in device_ids.json to get the actual HA entity ID
  4. Calls the HA REST API to execute the action(s)

Requires: HA_API_KEY in .env
Config in llm.yaml under skills.home_assistant:
  url:              "http://homeassistant.local:8123"
  model:            "gpt-4o"          # model for the sub-LLM call; defaults to gpt-4o
  base_url:         null              # only needed for local providers (Ollama, etc.)
  device_ids_yaml:  "device_ids.yaml" # relative to project root
  device_ids_json:  "device_ids.json"
  default_room:     ""               # used when the user doesn't specify a room
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

from kenzy.llm.skills import get_config, skill  # type: ignore[import]

log = logging.getLogger(__name__)

_THERMO_MIN = 65.0
_THERMO_MAX = 85.0

# Actions that require a recognized (non-unknown) speaker.
_SECURE_ACTIONS = {"lock", "unlock", "open_cover", "close_cover"}

_SYSTEM_PROMPT = """\
You are a home automation resolver.  Given a device map (YAML) and a user \
request, identify which devices to act on and what action to perform.

Device actions by type:
- light / switch : turn_on | turn_off | toggle
- fan            : turn_on | turn_off | toggle
- cover          : open_cover | close_cover
- lock           : lock | unlock
- climate        : set_temperature  (°F, must be 65–85)

Selection rules:
- If the user names a specific device (e.g. "the lamp", "ceiling fan"), select
  only that one device.
- Plural type ("the lights", "the fans") means all devices of that type in the
  room.
- If no specific device or type is mentioned and the room has a "default" list,
  use those devices.
- YAML line comments (# ...) provide helpful context — read them.
- For a status query use action "get_status".
- For relative temperature changes ("warmer", "cooler") use "get_status" first
  to learn the current setpoint, then set_temperature.

Respond with a JSON object — no markdown, no extra text:
{
  "devices": [
    {"id": "<alias from YAML>", "action": "<action>"},
    {"id": "<alias>", "action": "set_temperature", "temperature": <float>}
  ],
  "response_text": "<short 1–2 sentence spoken response>",
  "clarify_text":  "<ask for clarification only if genuinely needed, else empty>"
}"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_root() -> Path:
    for path in [Path.cwd(), *Path.cwd().parents]:
        if (path / "pyproject.toml").exists():
            return path
    return Path.cwd()


def _load_device_files() -> tuple[str, dict[str, str]]:
    root      = _project_root()
    yaml_path = root / get_config("home_assistant", "device_ids_yaml", "data/home_assistant/device_ids.yaml")
    json_path = root / get_config("home_assistant", "device_ids_json", "data/home_assistant/device_ids.json")
    yaml_text: str           = yaml_path.read_text()
    device_map: dict[str, str] = json.loads(json_path.read_text())
    return yaml_text, device_map


def _extract_json(content: str) -> dict[str, Any]:
    """Parse JSON from an LLM response, tolerating markdown code fences."""
    try:
        return json.loads(content)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if m:
        return json.loads(m.group(1))  # type: ignore[no-any-return]
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if m:
        return json.loads(m.group(0))  # type: ignore[no-any-return]
    raise ValueError(f"No JSON in LLM response: {content[:200]}")


def _conn() -> tuple[str, dict[str, str]]:
    base  = get_config("home_assistant", "url", "http://homeassistant.local:8123")
    token = os.environ.get("HA_API_KEY", "")
    return base.rstrip("/"), {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


async def _ha_service(entity_id: str, service: str, extra: dict[str, Any] | None = None) -> None:
    domain = entity_id.split(".")[0]
    base, headers = _conn()
    payload: dict[str, Any] = {"entity_id": entity_id}
    if extra:
        payload.update(extra)
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{base}/api/services/{domain}/{service}",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()


async def _ha_state(entity_id: str) -> dict[str, Any]:
    base, headers = _conn()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{base}/api/states/{entity_id}", headers=headers)
        resp.raise_for_status()
    return resp.json()  # type: ignore[no-any-return]


async def _resolve(request: str, yaml_text: str) -> dict[str, Any]:
    """Sub-LLM call: map the user request to device aliases + actions."""
    from litellm import acompletion  # type: ignore[import-untyped]

    model        = get_config("home_assistant", "model",        "gpt-4o")
    base_url     = get_config("home_assistant", "base_url")     or None
    default_room = get_config("home_assistant", "default_room") or ""

    user_content = f"Device map:\n{yaml_text}\n\nUser request: {request}"
    if default_room:
        user_content += f"\n\nDefault room if none specified: {default_room}"

    kwargs: dict[str, Any] = {
        "model":    model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_content},
        ],
    }
    if base_url:
        kwargs["base_url"] = base_url

    # json_object mode is supported by OpenAI and most hosted providers.
    # Omit for local/unknown providers — the system prompt instructs JSON output.
    try:
        kwargs["response_format"] = {"type": "json_object"}
        response = await acompletion(**kwargs)
    except Exception:
        kwargs.pop("response_format", None)
        response = await acompletion(**kwargs)

    return _extract_json(response.choices[0].message.content or "")


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


@skill
async def handle_home_control(request: str, speaker: str | None = None) -> str:
    """Control or query smart home devices: lights, fans, locks, covers, thermostats.

    Use for any request involving home devices — turning lights on or off,
    adjusting fans, locking or unlocking doors, opening or closing covers,
    setting the thermostat, or checking the status of any device.

    Pass the user's complete request text verbatim.  This skill consults the
    home device map to identify the correct devices and executes the action.

    request: the user's full home control request, e.g. "turn on the lamp in
             the office" or "what's the temperature in the living room?"
    speaker: the speaker identifier from context (e.g. "john", "unknown").
             Always pass this when available — it is required to authorize
             lock/unlock and cover open/close commands.
    """
    try:
        yaml_text, device_map = _load_device_files()
    except Exception as exc:
        return f"Could not load device map: {exc}"

    try:
        result = await _resolve(request, yaml_text)
    except Exception as exc:
        log.error("Device resolution failed: %s", exc, exc_info=True)
        return f"Could not resolve devices for that request: {exc}"

    if result.get("clarify_text"):
        return str(result["clarify_text"])

    devices: list[dict[str, Any]] = result.get("devices", [])
    if not devices:
        return result.get("response_text") or "No matching devices found."

    # Refuse secure actions (lock/unlock, open/close cover) for unknown speakers.
    if any(dev.get("action") in _SECURE_ACTIONS for dev in devices):
        if not speaker or speaker.lower() == "unknown":
            return (
                "I'm sorry, I don't recognize who is speaking and can't "
                "perform lock or cover operations for security reasons."
            )

    status_lines: list[str] = []

    for dev in devices:
        alias  = str(dev.get("id", ""))
        action = str(dev.get("action", ""))
        ha_id  = device_map.get(alias)

        if not ha_id:
            log.warning("Unknown device alias %r — skipping", alias)
            continue

        try:
            if action == "get_status":
                state  = await _ha_state(ha_id)
                s      = state.get("state", "unknown")
                attrs  = state.get("attributes", {})
                name   = attrs.get("friendly_name", ha_id)
                temp   = attrs.get("current_temperature")
                target = attrs.get("temperature")
                if temp is not None:
                    status_lines.append(
                        f"{name}: {s}, current {temp}°F, target {target}°F"
                    )
                else:
                    status_lines.append(f"{name}: {s}")

            elif action == "set_temperature":
                temp = max(_THERMO_MIN, min(_THERMO_MAX, float(dev.get("temperature", 70))))
                await _ha_service(ha_id, "set_temperature", {"temperature": temp})

            else:
                await _ha_service(ha_id, action)

        except Exception as exc:
            log.error("HA error for %s (%s %s): %s", ha_id, action, alias, exc)

    if status_lines:
        return "\n".join(status_lines)

    return result.get("response_text") or "Done."
