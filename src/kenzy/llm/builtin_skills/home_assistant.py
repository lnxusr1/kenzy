"""
Home Assistant skill — device control over the HA REST API.

Device topology (entities, names, domains, area/floor placement) is pulled
**live from Home Assistant** by :mod:`kenzy.llm.builtin_skills.ha_model` and
cached; the only hand-authored input is ``curation.yaml`` (aliases, per-device
notes, room group-defaults, voice-control exclusions). ``_ensure_view`` builds a
``_DeviceIndex`` + resolver text from that merged view; if HA is unreachable
with nothing cached, requests fail honestly ("could not load device map") —
there is no static fallback (retired 3.5.1: a stale hand-built map can resolve
but never actuate when HA is down).

On each home control request:
  1. Fast path: padacioso intent parse + local resolution to entity IDs, executed
     directly against HA (no LLM). Hard cases defer.
  2. LLM fallback: the live topology is rendered as a floor>area>type>entity
     outline and sent to a sub-LLM that picks entity IDs + action.
  3. The HA REST API executes the action(s); device *state* is read live only
     when a request needs it (status queries, relative temperature changes).

Requires: HA_API_KEY in .env
Config in llm.yaml under skills.home_assistant:
  url:           "http://homeassistant.local:8123"
  model:         "gpt-4o"     # model for the sub-LLM resolver; defaults to gpt-4o
  base_url:      null         # only needed for local providers (Ollama, etc.)
  curation_file: "data/home_assistant/curation.yaml"   # optional
  cache_ttl:     300          # seconds to cache the HA topology
  media_volume_steps: 3       # device notches per spoken "turn the TV up/down" (1-6)
  domains:       [light, switch, fan, cover, lock, climate, scene,
                  script, button, input_button, input_boolean, vacuum, media_player]
  default_room:  ""           # used when the user doesn't specify a room
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from kenzy.llm.builtin_skills import ha_model
from kenzy.llm.skills import (  # type: ignore[import]
    FastResult,
    acompletion_with_fallback,
    endpoint_kwargs,
    fast_intent,
    get_config,
    get_request,
    skill,
)

log = logging.getLogger(__name__)

# Comfort clamp for relative "make it warmer/cooler" adjustments — the packaged
# defaults; overridable per-home via skills.home_assistant.thermo_min/max.
_THERMO_MIN = 65.0
_THERMO_MAX = 85.0


def _thermo_min() -> float:
    return float(get_config("home_assistant", "thermo_min", _THERMO_MIN))


def _thermo_max() -> float:
    return float(get_config("home_assistant", "thermo_max", _THERMO_MAX))


def _media_volume_steps() -> int:
    """Device notches per spoken volume command. One HA volume_up = one notch,
    which is painfully slow by voice — default to 3 (clamped 1–6)."""
    try:
        steps = int(get_config("home_assistant", "media_volume_steps", 3))
    except (TypeError, ValueError):
        steps = 3
    return max(1, min(6, steps))


# Actions that require a recognized (non-unknown) speaker.
_SECURE_ACTIONS = {"lock", "unlock", "open_cover", "close_cover"}

_SYSTEM_PROMPT = """\
You are a home automation resolver.  Given a device map (YAML) and a user \
request, identify which devices to act on and what action to perform.

The device map is structured as: floor > area > type > device.
Top-level keys are floors (e.g. downstairs, upstairs, outside).
Second-level keys are areas (rooms) within that floor.
Third-level keys are device types (lights, fans, locks, covers, climate,
scenes, scripts, buttons, toggles, media).

Device actions by type:
- light / switch : turn_on | turn_off | toggle
- fan            : turn_on | turn_off | toggle
- cover          : open_cover | close_cover
- lock           : lock | unlock
- climate        : set_temperature  (°F, must be 65–85)
- scene          : turn_on   ("activate" / "run" / "start" a scene)
- script         : turn_on  (run it) | turn_off  (stop a running script)
- button         : press
- toggle         : turn_on | turn_off | toggle  (input_boolean helpers, e.g. "guest mode")
- vacuum         : start | stop | return_to_base  ("send it home" / "back to the dock")
- media          : media_play | media_pause | media_next_track | media_previous_track |
                   volume_up | volume_down | media_mute | media_unmute | turn_on | turn_off
                   (transport only — starting NEW music by name is not supported yet;
                   say so if asked to play a specific song/artist)

Selection rules:
- A location context (floor + area) may be provided below the request. When it
  is, ALL ambiguous device references must be resolved within that area first.
  A device name that appears in multiple areas (e.g. "the lamp") refers to the
  one in the context area — do not match devices in other areas.
- If the user explicitly names a different area or floor in their request, that
  overrides the context area for that reference only.
- Plural type with an area ("the lights", "the fans") means all devices of that
  type in the context area (or the explicitly named area).
- Floor-level requests ("the lights downstairs", "all the lights downstairs",
  "upstairs fans") select all matching devices across EVERY area on that floor,
  even when the context area is on that same floor.
- If no specific device or type is mentioned and the room has a "default" list,
  use those devices.
- Scenes, scripts, buttons, and toggles usually have no area and appear under
  home > unplaced. Match them by NAME across the whole map — the location
  context does not scope them.
- Each "- " line is a Home Assistant entity_id (e.g. light.kitchen_island).
  Use it verbatim as the "id" value. The text after "#" is the friendly name
  and optional context — read it to match the user's wording, but never return
  it as the id.
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


def _area_to_floor(yaml_text: str) -> dict[str, str]:
    """Build a child→parent lookup from the resolver text's top two levels.

    Against the live resolver text (floor > area > type > entity) this maps
    **area → floor** — exactly how ``_resolve`` uses it for the
    location-context line. (The historic name ``_room_to_area`` described the
    old static-file nesting and lied about the live shape.)"""
    import yaml as _yaml

    mapping: dict[str, str] = {}
    try:
        data = _yaml.safe_load(yaml_text)
        if isinstance(data, dict):
            for area, rooms in data.items():
                if isinstance(rooms, dict):
                    for room in rooms:
                        mapping[str(room)] = str(area)
    except Exception:
        pass
    return mapping


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
    base = get_config("home_assistant", "url", "http://homeassistant.local:8123")
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


async def _ma_play(entity_id: str, query: str, *, radio: bool = False) -> None:
    """``music_assistant.play_media`` — the play-by-name passthrough. MA does
    ALL name resolution (artist/album/track/playlist); Kenzy only carries the
    spoken phrase and the target player."""
    base, headers = _conn()
    payload: dict[str, Any] = {"entity_id": entity_id, "media_id": query}
    if radio:
        payload["radio_mode"] = True
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{base}/api/services/music_assistant/play_media",
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


async def _resolve(request: str, yaml_text: str, room: str | None = None) -> dict[str, Any]:
    """Sub-LLM call: map the user request to device aliases + actions."""
    model = get_config("home_assistant", "model", "gpt-4o")
    base_url = get_config("home_assistant", "base_url") or None

    resolved_room = room or get_config("home_assistant", "default_room") or ""
    resolved_floor = _area_to_floor(yaml_text).get(resolved_room, "") if resolved_room else ""

    user_content = f"Device map:\n{yaml_text}\n\nUser request: {request}"
    if resolved_room:
        loc = (
            f"Floor: {resolved_floor}, Area: {resolved_room}"
            if resolved_floor
            else f"Area: {resolved_room}"
        )
        user_content += (
            f"\n\nLocation context: {loc}"
            "\nScope all ambiguous device references to this area unless the"
            " request explicitly names a different area or floor."
        )

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    }
    # Custom endpoint: OPENAI_API_KEY never rides to it (F-14; CUSTOM_LLM_API_KEY
    # is the opt-in credential for hosted proxies).
    kwargs.update(endpoint_kwargs(base_url))

    # json_object mode is supported by OpenAI and most hosted providers.
    # Omit for local/unknown providers — the system prompt instructs JSON output.
    # Both attempts ride the silent local-fallback path (skills.set_fallback).
    try:
        kwargs["response_format"] = {"type": "json_object"}
        response = await acompletion_with_fallback(kwargs)
    except Exception:
        kwargs.pop("response_format", None)
        response = await acompletion_with_fallback(kwargs)

    return _extract_json(response.choices[0].message.content or "")


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


@skill
async def handle_home_control(
    request: str, speaker: str | None = None, room: str | None = None
) -> str:
    """Control or query smart home devices: lights, fans, locks, covers,
    thermostats, scenes, scripts, buttons, and toggle helpers.

    Use for any request involving home devices — turning lights on or off,
    adjusting fans, locking or unlocking doors, opening or closing covers,
    setting the thermostat, checking the status of any device, activating a
    scene ("movie night"), running a script or routine, pressing a button, or
    flipping an input_boolean helper ("guest mode").

    Pass the user's complete request text verbatim.  This skill consults the
    home device map to identify the correct devices and executes the action.

    request: the user's full home control request, e.g. "turn on the lamp in
             the office" or "what's the temperature in the living room?"
    speaker: the speaker identifier from context (e.g. "john", "unknown").
             Always pass this when available — it is required to authorize
             lock/unlock and cover open/close commands.
    room:    the room the request originated from (e.g. "office", "living_room").
             Always pass this from context — it is used as the default room when
             the user's request does not name a specific room.
    """
    try:
        await _ensure_view()
        resolver_text = _get_resolver_text()
        device_map = _get_index().device_map
    except Exception as exc:
        return f"Could not load device map: {exc}"

    try:
        result = await _resolve(request, resolver_text, room)
    except Exception as exc:
        log.error("Device resolution failed: %s", exc, exc_info=True)
        return f"Could not resolve devices for that request: {exc}"

    if result.get("clarify_text"):
        return str(result["clarify_text"])

    devices: list[dict[str, Any]] = result.get("devices", [])
    if not devices:
        return result.get("response_text") or "No matching devices found."

    if blocked := _secure_blocked(devices, speaker):
        return blocked

    status_lines = await _apply_devices(devices, device_map)
    if status_lines:
        return "\n".join(status_lines)

    return result.get("response_text") or "Done."


def _secure_blocked(devices: list[dict[str, Any]], speaker: str | None) -> str | None:
    """Refusal message if a secure action is requested by an unknown speaker, else None.

    Shared by the LLM skill and the deterministic fast path.
    """
    if any(dev.get("action") in _SECURE_ACTIONS for dev in devices):
        if not speaker or speaker.lower() == "unknown":
            return (
                "I'm sorry, I don't recognize who is speaking and can't "
                "perform lock or cover operations for security reasons."
            )
    return None


async def _apply_devices(devices: list[dict[str, Any]], device_map: dict[str, str]) -> list[str]:
    """Execute resolved device actions against Home Assistant.

    Returns status lines for any get_status actions (empty for pure control).
    Shared by the LLM skill and the deterministic fast path.
    """
    status_lines: list[str] = []

    for dev in devices:
        alias = str(dev.get("id", ""))
        action = str(dev.get("action", ""))
        # In the live-HA path the id is already an entity_id (device_map is an
        # identity map); in the static path it's a friendly code mapped to one.
        ha_id = device_map.get(alias) or (alias if "." in alias else None)

        if not ha_id:
            log.warning("Unknown device alias %r — skipping", alias)
            continue

        try:
            if action == "get_status":
                state = await _ha_state(ha_id)
                s = state.get("state", "unknown")
                attrs = state.get("attributes", {})
                name = attrs.get("friendly_name", ha_id)
                temp = attrs.get("current_temperature")
                target = attrs.get("temperature")
                if temp is not None:
                    status_lines.append(f"{name}: {s}, current {temp}°F, target {target}°F")
                else:
                    status_lines.append(f"{name}: {s}")

            elif action in ("volume_up", "volume_down"):
                # Relative steps (Roku-class players report no volume_level, so
                # absolute volume_set can't be trusted); one notch per call.
                for _ in range(_media_volume_steps()):
                    await _ha_service(ha_id, action)

            elif action in ("media_mute", "media_unmute"):
                await _ha_service(ha_id, "volume_mute", {"is_volume_muted": action == "media_mute"})

            elif action == "set_temperature":
                temp = max(_thermo_min(), min(_thermo_max(), float(dev.get("temperature", 70))))
                await _ha_service(ha_id, "set_temperature", {"temperature": temp})

            else:
                await _ha_service(ha_id, action)

        except Exception as exc:
            log.error("HA error for %s (%s %s): %s", ha_id, action, alias, exc)

    return status_lines


# ---------------------------------------------------------------------------
# Deterministic fast path (no LLM): padacioso intent parse + local resolution
# ---------------------------------------------------------------------------
#
# Handles the high-frequency imperative commands instantly, with no remote model
# call.  Anything ambiguous (relative temps, status queries, unrecognised
# devices, bare unlock/open) returns FastResult.miss() and falls through to the
# LLM skill above, which keeps its sub-LLM resolver for the hard cases.

_FAST_VOICE = "Speak naturally and briefly."

# Spoken group words → yaml type key.
_GROUP_WORDS = {
    "light": "lights",
    "lights": "lights",
    "fan": "fans",
    "fans": "fans",
    "door": "lock",
    "doors": "lock",
    "lock": "lock",
    "locks": "lock",
    "cover": "covers",
    "covers": "covers",
    "blind": "covers",
    "blinds": "covers",
    "shade": "covers",
    "shades": "covers",
    "curtain": "covers",
    "curtains": "covers",
}

# action name → (HA service, target type keys, direction).
# direction "activate" uses the room's curated default subset for a bare group;
# "deactivate" always acts on every device of that type in the room.
_CONTROL: dict[str, tuple[str, set[str], str]] = {
    "turn_on": ("turn_on", {"lights", "fans"}, "activate"),
    "turn_off": ("turn_off", {"lights", "fans"}, "deactivate"),
    "toggle": ("toggle", {"lights", "fans"}, "activate"),
    "lock": ("lock", {"lock"}, "deactivate"),
    "unlock": ("unlock", {"lock"}, "activate"),
    "open_cover": ("open_cover", {"covers"}, "activate"),
    "close_cover": ("close_cover", {"covers"}, "deactivate"),
}
# Unsafe directions must name a specific device — never act on a bare group.
_EXPLICIT_ONLY = {"unlock", "open_cover"}

_DOMAIN_TO_TYPE = {
    "light": "lights",
    "switch": "lights",
    "fan": "fans",
    "cover": "covers",
    "lock": "lock",
    "climate": "climate",
    "scene": "scenes",
    "script": "scripts",
    "button": "buttons",
    "input_button": "buttons",
    "input_boolean": "toggles",
    "vacuum": "vacuums",
    "media_player": "media",
}

# --- Tier 1 name-first domains (scene/script/button/input_boolean) ----------
# Single-verb, resolved by NAME across the whole house (they usually have no
# area, so room scoping doesn't apply). None of their type keys are group
# words, so "turn on the lights" can never sweep up a scene or a helper.

# Trailing qualifier words ("the movie night scene") tried stripped when the
# full phrase doesn't match — tried second, since real names may contain them
# ("Guest Mode", "Bedtime Scene").
_QUALIFIERS = {"scene", "script", "routine", "automation", "button"}

# Domains the "activate/run/press" verb family may act on; anything else
# ("run the blinds") defers to the LLM. Service = press for buttons, turn_on
# for the rest.
_ACTIVATE_DOMAINS = {
    "scene",
    "script",
    "button",
    "input_button",
    "input_boolean",
    "switch",
    "light",
    "fan",
    "vacuum",
}

_ACTIVATE_VERB = {
    "scene": "Activated",
    "script": "Ran",
    "button": "Pressed",
    "input_button": "Pressed",
    "vacuum": "Started",
}

# Spoken type-words for the vacuum ("start the vacuum" names no device) — the
# room's vacuum wins, else the house's only one, else defer to a clarify.
_VACUUM_WORDS = {"vacuum", "vacuum cleaner", "robot vacuum"}

# The activate family's HA service, chosen by the matched entity's domain.
_ACTIVATE_SERVICE = {"button": "press", "input_button": "press", "vacuum": "start"}

# Legacy verbs translated per domain ("turn on the vacuum" means start it).
_SERVICE_ALIAS = {("vacuum", "turn_on"): "start", ("vacuum", "turn_off"): "stop"}

# "stop the X" services by domain; anything else defers to the LLM.
_STOP_SERVICES = {
    "vacuum": "stop",
    "fan": "turn_off",
    "script": "turn_off",
    "media_player": "media_pause",
}

# --- media_player transport (Tier 2) ----------------------------------------
# Transport verbs only: act on what's already playing. Starting NEW music by
# name is the Music Assistant integration (later — see design/backlog.md).
# intent name → (HA service, live states that make a player the obvious
# target, spoken confirmation verb).
_MEDIA_INTENTS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "media_pause": ("media_pause", ("playing", "on", "buffering"), "Paused"),
    "media_resume": ("media_play", ("paused", "idle", "on"), "Resumed"),
    "media_next": ("media_next_track", ("playing", "on", "buffering"), "Skipped"),
    "media_previous": ("media_previous_track", ("playing", "on", "buffering"), "Went back on"),
    "media_vol_up": ("volume_up", ("playing", "on", "buffering"), "Turned up"),
    "media_vol_down": ("volume_down", ("playing", "on", "buffering"), "Turned down"),
    "media_mute": ("media_mute", ("playing", "on", "buffering"), "Muted"),
    "media_unmute": ("media_unmute", ("playing", "on", "buffering", "paused"), "Unmuted"),
}

# Fast-path service compatibility for the name-first domains: a scene can't
# turn_off, a button only presses — mismatches miss to the LLM instead of
# 500ing against HA. Legacy domains keep their existing paths untouched.
_DOMAIN_SERVICES = {
    "scene": {"turn_on"},
    "script": {"turn_on", "turn_off", "toggle"},
    "button": {"press"},
    "input_button": {"press"},
    "input_boolean": {"turn_on", "turn_off", "toggle"},
    "vacuum": {"start", "stop", "return_to_base"},
    "media_player": {
        "turn_on",
        "turn_off",
        "toggle",
        "media_play",
        "media_pause",
        "media_next_track",
        "media_previous_track",
        "volume_up",
        "volume_down",
        "media_mute",
        "media_unmute",
    },
}


def _service_ok(entity_id: str, service: str) -> bool:
    allowed = _DOMAIN_SERVICES.get(entity_id.split(".", 1)[0])
    return allowed is None or service in allowed


_ARTICLES = {"the", "a", "an", "my", "our", "your", "some", "please"}
_FILLERS = {"in", "at", "of", "inside", "to"}
_ALL_WORDS = {"all", "every", "everything", "any"}
_FUZZ_CUTOFF = 82
# Token-coverage gate: every spoken word must partially match SOME word of the
# device name. Without it, WRatio's partial tricks let a generic type word do
# all the scoring — a garbled "hot light" put a dozen unrelated lights over
# the cutoff (and even a perfect "hall light" lost to a device named "Light").
# Tolerance stays WITHIN words (prefixes, plurals, and substrings score 100);
# strictness applies ACROSS words (unaccounted-for words disqualify). 85 —
# partial_ratio's edge alignment is generous with short tokens ("hot" scores
# 80 against "light"), so anything lower lets garbage back in.
_TOKEN_COVERAGE = 85

# Past tense: the HA REST call completes before TTS playback even begins, so the
# device is already on/off/locked by the time the confirmation is spoken.
_VERB = {
    "turn_on": "Turned on",
    "turn_off": "Turned off",
    "toggle": "Toggled",
    "lock": "Locked",
    "unlock": "Unlocked",
    "open_cover": "Opened",
    "close_cover": "Closed",
}


@dataclass
class _DeviceIndex:
    rooms: dict[str, dict[str, list[str]]]  # room → type_key → [codes]
    defaults: dict[str, dict[str, list[str]]]  # room → type_key → [codes]
    spoken: dict[str, str]  # code → spoken name
    aliases: dict[tuple[str, str], list[str]]  # (room, phrase) → [codes]
    exclude: set[str]  # codes barred from groups
    room_phrases: dict[str, str]  # "living room" → "living_room"
    device_map: dict[str, str]  # code → entity_id
    # Floor scope: a spoken floor name ("downstairs") expands a bare-group command
    # to every room on that floor. Empty when the topology has no floors.
    floor_phrases: dict[str, str] = field(default_factory=dict)  # "downstairs" → "downstairs"
    floor_rooms: dict[str, list[str]] = field(default_factory=dict)  # floor → [rooms]
    # Music Assistant players: music + transport targets ONLY. Control-verb
    # resolution (on/off, name/type/group matching) must never see them — MA
    # imports arrive named after the devices they wrap ("Office TV" twice), and
    # "turn on the TV" hitting a queue frontend is the 4.3.0 field bug.
    music_only: set[str] = field(default_factory=set)


def _norm(text: str) -> str:
    return re.sub(r"[^\w\s]", "", text).strip().lower()


def _auto_spoken(code: str) -> str:
    """Derive a spoken name from a device code, stripping the room prefix.

    "lr_floor_lamp" → "floor lamp"; "kt_island_pendant_lights" → "island pendant lights".
    """
    parts = code.split("_")
    if len(parts) > 1 and len(parts[0]) <= 3:
        parts = parts[1:]
    return " ".join(parts)


def _add_intent(c: Any, name: str, phrases: list[str]) -> None:
    """add_intent with "[the] {device}" expanded to explicit variants.

    padacioso quirk: an optional word immediately before a capture ("[the]
    {device}") fails to match when the word is absent — so "turn on guest
    mode" would miss while "turn on the guest mode" hits. Expanding the
    article into two explicit patterns restores the bare form.
    """
    expanded: list[str] = []
    for ph in phrases:
        if "[the] " in ph:
            expanded.append(ph.replace("[the] ", "the "))
            expanded.append(ph.replace("[the] ", ""))
        else:
            expanded.append(ph)
    c.add_intent(name, expanded)


def _build_intents() -> Any:
    from padacioso import IntentContainer  # type: ignore[import-untyped]

    c = IntentContainer()
    _add_intent(
        c,
        "turn_on",
        [
            "[please] turn on [the] {device}",
            "[please] switch on [the] {device}",
            "[please] cut on [the] {device}",
        ],
    )
    _add_intent(
        c,
        "turn_off",
        [
            "[please] turn off [the] {device}",
            "[please] switch off [the] {device}",
            "[please] cut off [the] {device}",
            "[please] kill [the] {device}",
        ],
    )
    _add_intent(c, "toggle", ["[please] toggle [the] {device}"])
    _add_intent(c, "stop_device", ["[please] stop [the] {device}"])
    _MEDIA_WORD = "(music|tv|television|movie|show|media)"
    _add_intent(
        c,
        "media_pause",
        [
            "[please] pause",
            "[please] pause it",
            "[please] pause that",
            f"[please] pause [the] {_MEDIA_WORD}",
            f"[please] pause the {_MEDIA_WORD} in the {{room}}",
        ],
    )
    _add_intent(
        c,
        "media_resume",
        [
            "[please] resume",
            "[please] unpause",
            "[please] keep playing",
            "[please] continue playing",
            "play",
            f"[please] resume [the] {_MEDIA_WORD}",
            f"[please] play the {_MEDIA_WORD}",
            f"[please] resume the {_MEDIA_WORD} in the {{room}}",
        ],
    )
    _add_intent(
        c,
        "media_next",
        [
            "[please] next",
            "[please] next (song|track)",
            "[please] skip (this|the) (song|track)",
            "[please] skip it",
            "[please] play the next (song|track)",
        ],
    )
    _add_intent(
        c,
        "media_previous",
        [
            "[please] previous (song|track)",
            "[please] play the (previous|last) (song|track)",
            "[please] go back a (song|track)",
        ],
    )
    _add_intent(
        c,
        "media_vol_up",
        [
            f"[please] turn the {_MEDIA_WORD} up",
            f"[please] turn up the {_MEDIA_WORD}",
            f"[please] turn the {_MEDIA_WORD} volume up",
        ],
    )
    _add_intent(
        c,
        "media_vol_down",
        [
            f"[please] turn the {_MEDIA_WORD} down",
            f"[please] turn down the {_MEDIA_WORD}",
            f"[please] turn the {_MEDIA_WORD} volume down",
            f"[please] lower the {_MEDIA_WORD} [volume]",
        ],
    )
    _add_intent(c, "media_mute", [f"[please] mute the {_MEDIA_WORD}"])
    _add_intent(c, "media_unmute", [f"[please] unmute the {_MEDIA_WORD}"])
    _add_intent(
        c,
        "return_home",
        [
            "[please] send [the] {device} home",
            "[please] send [the] {device} [back] to the (dock|base)",
            "[please] tell [the] {device} to go home",
        ],
    )
    _add_intent(
        c,
        "activate",
        [
            "[please] activate [the] {device}",
            "[please] run [the] {device}",
            "[please] start [the] {device}",
            "[please] execute [the] {device}",
            "[please] launch [the] {device}",
            "[please] press [the] {device}",
            "[please] push [the] {device}",
        ],
    )
    _add_intent(c, "lock", ["[please] lock [the] {device}"])
    _add_intent(c, "unlock", ["[please] unlock [the] {device}"])
    _add_intent(
        c,
        "open_cover",
        [
            "[please] open [the] {device}",
            "[please] raise [the] {device}",
        ],
    )
    _add_intent(
        c,
        "close_cover",
        [
            "[please] close [the] {device}",
            "[please] shut [the] {device}",
            "[please] lower [the] {device}",
        ],
    )
    _add_intent(
        c,
        "set_temperature",
        [
            "set [the] {device} to {value} [degrees]",
            "change [the] {device} to {value} [degrees]",
            "set [the] {device} [to] {value} degrees",
        ],
    )
    return c


# Lazily-built, process-wide caches (rebuilt via _reset_cache in tests).
_INDEX: _DeviceIndex | None = None
_INTENTS: Any = None
_RESOLVER_TEXT: str | None = None
# Timestamp of the HA topology the cached view was built from; None for the
# static-file fallback (so a later live pull always supersedes it).
_VIEW_TS: float | None = None


# ---------------------------------------------------------------------------
# Live-HA view: build the resolver index + text from ha_model + curation
# ---------------------------------------------------------------------------


def _index_from_model(model: ha_model.HAModel, curation: dict[str, Any]) -> _DeviceIndex:
    """Build the resolver index from live HA topology + curation.

    Entity_ids are used directly as the device "codes"; ``device_map`` is an
    identity map so the shared executor path is unchanged.
    """
    devices_cur = curation.get("devices", {}) or {}
    rooms_cur = curation.get("rooms", {}) or {}

    rooms: dict[str, dict[str, list[str]]] = {}
    spoken: dict[str, str] = {}
    aliases: dict[tuple[str, str], list[str]] = {}
    exclude: set[str] = set()
    music_only: set[str] = set()
    room_phrases: dict[str, str] = {}
    device_map: dict[str, str] = {}
    floor_phrases: dict[str, str] = {}
    floor_rooms: dict[str, list[str]] = {}

    for e in model.entities:
        type_key = _DOMAIN_TO_TYPE.get(e.domain, e.domain)
        room = e.area or "unplaced"
        room_phrases[room.replace("_", " ")] = room
        # A real floor scope ("downstairs"); skip the synthetic no-floor bucket so
        # unplaced areas don't become a spoken "home" floor.
        if e.floor and e.floor != ha_model._NO_FLOOR:
            floor_phrases[_norm(e.floor_name)] = e.floor
            if room not in floor_rooms.setdefault(e.floor, []):
                floor_rooms[e.floor].append(room)
        bucket = rooms.setdefault(room, {}).setdefault(type_key, [])
        if e.entity_id not in bucket:
            bucket.append(e.entity_id)
        if getattr(e, "ma", False) and e.domain == "media_player":
            music = rooms.setdefault(room, {}).setdefault("music", [])
            if e.entity_id not in music:
                music.append(e.entity_id)
            music_only.add(e.entity_id)
        spoken[e.entity_id] = e.name
        device_map[e.entity_id] = e.entity_id

        dc = devices_cur.get(e.entity_id, {}) or {}
        for phrase in dc.get("aliases", []) or []:
            aliases.setdefault((room, _norm(str(phrase))), []).append(e.entity_id)
        if dc.get("in_group") is False:
            exclude.add(e.entity_id)

    defaults: dict[str, dict[str, list[str]]] = {}
    for area, ov in rooms_cur.items():
        rslug = ha_model._slug(str(area))
        for tk, eids in ((ov or {}).get("defaults", {}) or {}).items():
            if isinstance(eids, list):
                defaults.setdefault(rslug, {})[str(tk)] = [str(x) for x in eids]

    return _DeviceIndex(
        rooms,
        defaults,
        spoken,
        aliases,
        exclude,
        room_phrases,
        device_map,
        floor_phrases,
        floor_rooms,
        music_only,
    )


def _resolver_text(model: ha_model.HAModel, curation: dict[str, Any]) -> str:
    """Render the live topology as the floor>area>type>entity text the LLM reads."""
    notes = {
        eid: str(dev.get("note") or "") for eid, dev in (curation.get("devices", {}) or {}).items()
    }
    tree: dict[str, dict[str, dict[str, list[ha_model.Entity]]]] = {}
    for e in model.entities:
        type_key = _DOMAIN_TO_TYPE.get(e.domain, e.domain)
        area = e.area or "unplaced"
        tree.setdefault(e.floor, {}).setdefault(area, {}).setdefault(type_key, []).append(e)

    lines: list[str] = []
    for floor in sorted(tree):
        lines.append(f"{floor}:")
        for area in sorted(tree[floor]):
            lines.append(f"  {area}:")
            for tk in sorted(tree[floor][area]):
                lines.append(f"    {tk}:")
                for e in tree[floor][area][tk]:
                    ctx = e.name
                    note = notes.get(e.entity_id)
                    if note:
                        ctx = f"{ctx} — {note}"
                    lines.append(f"      - {e.entity_id}  # {ctx}")
    return "\n".join(lines)


async def _ensure_view() -> None:
    """Refresh the cached resolver index + text from live HA topology.

    Falls back to the static device_ids files when HA is unreachable and nothing
    is cached yet. A successful live pull always supersedes a static fallback.
    """
    global _INDEX, _RESOLVER_TEXT, _VIEW_TS
    try:
        model = await ha_model.get_model()
    except Exception as exc:
        if _INDEX is not None:
            return  # keep the existing (stale) view — better than nothing mid-outage
        # No cached topology and HA unreachable: fail honestly. (The old static
        # device_ids fallback was retired in 3.5.1 — resolving against a stale
        # hand-built map is pointless when actuation needs HA up anyway.)
        raise RuntimeError(f"Home Assistant topology unavailable: {exc}") from exc

    if _INDEX is None or _RESOLVER_TEXT is None or _VIEW_TS != model.fetched_at:
        curation = ha_model.load_curation()
        _INDEX = _index_from_model(model, curation)
        _RESOLVER_TEXT = _resolver_text(model, curation)
        _VIEW_TS = model.fetched_at


def _get_resolver_text() -> str:
    if _RESOLVER_TEXT is None:
        raise RuntimeError("resolver text not initialized; call _ensure_view() first")
    return _RESOLVER_TEXT


def _get_index() -> _DeviceIndex:
    if _INDEX is None:
        raise RuntimeError("device index not initialized; call _ensure_view() first")
    return _INDEX


def _get_intents() -> Any:
    global _INTENTS
    if _INTENTS is None:
        _INTENTS = _build_intents()
    return _INTENTS


def _reset_cache() -> None:
    """Test hook: drop the cached view + intent container."""
    global _INDEX, _INTENTS, _RESOLVER_TEXT, _VIEW_TS
    _INDEX = None
    _INTENTS = None
    _RESOLVER_TEXT = None
    _VIEW_TS = None
    ha_model.reset_cache()


def _room_key(origin: str | None) -> str:
    return (origin or "").strip().replace(" ", "_").lower()


def _extract_room(idx: _DeviceIndex, toks: list[str]) -> tuple[str | None, list[str]]:
    """Pull an explicit room out of the token list (longest match wins)."""
    for sp in sorted(idx.room_phrases, key=lambda p: -len(p.split())):
        words = sp.split()
        n = len(words)
        for i in range(len(toks) - n + 1):
            if toks[i : i + n] == words:
                return idx.room_phrases[sp], toks[:i] + toks[i + n :]
    return None, toks


def _extract_floor(idx: _DeviceIndex, toks: list[str]) -> tuple[str | None, list[str]]:
    """Pull an explicit floor name ("downstairs") out of the token list."""
    for sp in sorted(idx.floor_phrases, key=lambda p: -len(p.split())):
        words = sp.split()
        n = len(words)
        for i in range(len(toks) - n + 1):
            if toks[i : i + n] == words:
                return idx.floor_phrases[sp], toks[:i] + toks[i + n :]
    return None, toks


def _covers(tokens: list[str], name: str) -> bool:
    """True when every spoken token partially matches some word of the name."""
    from rapidfuzz import fuzz

    name_tokens = name.lower().split()
    return all(
        max((fuzz.partial_ratio(tok, nt) for nt in name_tokens), default=0) >= _TOKEN_COVERAGE
        for tok in tokens
    )


def _fuzzy(idx: _DeviceIndex, room: str | None, phrase: str, strict: bool = False) -> str | None:
    from rapidfuzz import fuzz, process, utils

    if room is not None:
        codes = [c for grp in idx.rooms.get(room, {}).values() for c in grp]
    else:
        codes = list(idx.device_map)
    codes = [c for c in codes if c not in idx.music_only]
    if not codes:
        return None
    # Coverage gate first (see _TOKEN_COVERAGE): a candidate is only eligible
    # when every spoken word is accounted for in its name.
    tokens = _norm(phrase).split()
    choices = {
        name: code
        for name, code in ((idx.spoken.get(c, c), c) for c in codes)
        if _covers(tokens, name)
    }
    if not choices:
        return None
    # default_process lowercases both sides — the utterance arrives lowercase
    # while HA names are Title Case, which alone costs ~18 WRatio points.
    # strict=True scores the WHOLE string (no partial matching): needed when the
    # phrase contains the room name, where WRatio's partials let the room word
    # alone put every "Office X" device over the cutoff in a tie.
    match = process.extractOne(
        phrase,
        list(choices),
        scorer=fuzz.ratio if strict else fuzz.WRatio,
        processor=utils.default_process,
        score_cutoff=_FUZZ_CUTOFF,
    )
    return choices[match[0]] if match else None


def _stem_group(idx: _DeviceIndex, room: str, phrase: str) -> list[str] | None:
    """Resolve a plural like "lamps" to every in-room device named with that stem.

    Lets "turn on the lamps" act on all lamp-type lights in the room without a
    dedicated group word.  Returns None for non-plural or no-match phrases.
    """
    if " " in phrase or not phrase.endswith("s") or len(phrase) < 4:
        return None
    stem = phrase[:-1]
    codes = [
        c
        for grp in idx.rooms.get(room, {}).values()
        for c in grp
        if stem in idx.spoken.get(c, c).lower().split()
    ]
    codes = [c for c in codes if c not in idx.exclude and c not in idx.music_only]
    return codes or None


def _group_codes(
    idx: _DeviceIndex, room: str, gtype: str, direction: str, has_all: bool
) -> list[str]:
    """Codes for a bare group in one room: curated default subset for an
    ``activate`` (unless "all"), every device of that type otherwise."""
    rt = idx.rooms.get(room, {})
    if direction == "activate" and not has_all:
        codes = list(idx.defaults.get(room, {}).get(gtype) or rt.get(gtype, []))
    else:
        codes = list(rt.get(gtype, []))
    return [c for c in codes if c not in idx.music_only]


def _resolve_target(
    idx: _DeviceIndex, action: str, target: str, origin_room: str | None
) -> list[str] | None:
    """Resolve a control command to device codes, or None to defer to the LLM."""
    _svc, types, direction = _CONTROL[action]

    raw = [t for t in _norm(target).split() if t]
    has_all = bool(set(raw) & _ALL_WORDS)
    toks = [t for t in raw if t not in _ARTICLES and t not in _ALL_WORDS]

    full_phrase = " ".join(t for t in toks if t not in _FILLERS).strip()
    extracted, toks = _extract_room(idx, toks)
    explicit_room = extracted is not None
    room = extracted or _room_key(origin_room)
    # A floor ("downstairs") only when no explicit room was named — a room is the
    # more specific scope and wins.
    floor: str | None = None
    if not explicit_room:
        floor, toks = _extract_floor(idx, toks)
    toks = [t for t in toks if t not in _FILLERS]
    phrase = " ".join(toks).strip()
    if not phrase:
        return None

    # Floor scope ("the lights downstairs") aggregates a bare group across every
    # room on that floor. Anything more specific than a bare group defers to the LLM
    # rather than mis-resolving against the origin room, which may be another floor.
    if floor is not None:
        if phrase not in _GROUP_WORDS:
            return None
        gtype = _GROUP_WORDS[phrase]
        if gtype not in types or action in _EXPLICIT_ONLY:
            return None
        codes = []
        for r in idx.floor_rooms.get(floor, []):
            for c in _group_codes(idx, r, gtype, direction, has_all):
                if c not in codes:
                    codes.append(c)
        if not (has_all and direction == "deactivate"):
            codes = [c for c in codes if c not in idx.exclude]
        return codes or None

    # Overlay alias (room-scoped, then global).
    codes = idx.aliases.get((room, phrase)) or idx.aliases.get(("", phrase))
    if codes is not None:
        return codes

    # "the vacuum" through the on/off verbs (the service alias maps them to
    # start/stop): positional resolution, same as the activate family.
    if phrase in _VACUUM_WORDS:
        code = _resolve_vacuum(idx, room)
        return [code] if code else None

    # A device NAMED exactly what was said beats the group word: "the office
    # light" means the fixture "Office Light", while the generic "the light" /
    # "the lights" keeps its room-group (curated defaults) semantics. Checked
    # with the room words kept AND stripped, so "office light" from anywhere
    # and "light" said in a room whose fixture is just "Light" both hit.
    for candidate in (full_phrase, phrase):
        exact = _exact_named(idx, room, candidate)
        if exact:
            return exact

    # Bare group ("the lights", "fans").
    if phrase in _GROUP_WORDS:
        gtype = _GROUP_WORDS[phrase]
        if gtype not in types or action in _EXPLICIT_ONLY:
            return None
        # SINGULAR prefers THE device: the one named "<Room> Light" (or just
        # "Light"), else the room's only one of the type. When no specific
        # referent exists, it degrades to the plural action below — the
        # curated default set is the operator's answer to "what does the
        # lighting mean here" ("all"/"every" always counts as plural).
        if not phrase.endswith("s") and not has_all:
            named = _room_named_device(idx, room, phrase)
            if named:
                return named
            lone = [c for c in idx.rooms.get(room, {}).get(gtype, []) if c not in idx.exclude]
            if len(lone) == 1:
                return lone
        codes = _group_codes(idx, room, gtype, direction, has_all)
        # The in_group:false floor (name-only devices) applies to every group
        # command EXCEPT an explicit deactivate-all ("turn off all the lights"),
        # where "all" means literally all — only hard-excluded devices, already
        # absent from the model, stay out. Hard floors still hold for on/all-on.
        if not (has_all and direction == "deactivate"):
            codes = [c for c in codes if c not in idx.exclude]
        return codes or None

    # Specific device, scoped to the resolved room.
    code = _fuzzy(idx, room, phrase)
    if code:
        return [code]

    # Plural like "lamps" → every matching device in the room.
    group = _stem_group(idx, room, phrase)
    if group:
        return group

    # Devices named WITH their room ("Office Lamp" in the office): the
    # room-stripped phrase can miss where the full phrase matches exactly.
    # Strict scoring only — a partial match on the room word must not win.
    if explicit_room and full_phrase != phrase:
        code = _fuzzy(idx, room, full_phrase, strict=True)
        if code:
            return [code]

    # Widen to a house-wide search ONLY when the user did not name a room.
    # If they explicitly said "in the <room>", stay there (or defer to the LLM)
    # rather than wandering into another room's similarly-named device.
    if not explicit_room:
        code = _fuzzy(idx, None, phrase)
        if code:
            return [code]

    # Trailing qualifier ("turn on the movie night scene" → "movie night").
    if len(toks) > 1 and toks[-1] in _QUALIFIERS:
        stripped = " ".join(toks[:-1])
        code = _fuzzy(idx, room, stripped)
        if not code and not explicit_room:
            code = _fuzzy(idx, None, stripped)
        if code:
            return [code]
    return None


def _resolve_climate(idx: _DeviceIndex, target: str, origin_room: str | None) -> list[str] | None:
    toks = [t for t in _norm(target).split() if t not in _ARTICLES]
    room, _toks = _extract_room(idx, toks)
    room = room or _room_key(origin_room)
    codes = idx.rooms.get(room, {}).get("climate", [])
    return codes or None


def _room_named_device(idx: _DeviceIndex, room: str | None, word: str) -> list[str]:
    """Room devices whose name IS the word once the room's own words are
    stripped: "the light" in the office matches "Office Light" (and a fixture
    named just "Light"). Searches every type bucket — a fan that lives as a
    switch entity is still THE fan. Twins sharing a name return together."""
    room_words = set((room or "").replace("_", " ").split())
    out: list[str] = []
    for grp in idx.rooms.get(room or "", {}).values():
        for code in grp:
            if code in idx.music_only:
                continue
            toks = _norm(idx.spoken.get(code, code)).split()
            local = [w for w in toks if w not in room_words]
            if toks == [word] or local == [word]:
                out.append(code)
    return out


def _exact_named(idx: _DeviceIndex, room: str | None, text: str) -> list[str]:
    """Devices in the room whose spoken name IS the text (normalized equality).

    Both representations of one fixture (a light entity and its switch twin
    often share a name) are returned together; curation usually excludes one.
    """
    if room is None:
        return []
    codes = [c for grp in idx.rooms.get(room, {}).values() for c in grp]
    return [
        c for c in codes if c not in idx.music_only and _norm(idx.spoken.get(c, c)) == text
    ]


def _resolve_vacuum(idx: _DeviceIndex, room: str | None) -> str | None:
    """The room's vacuum, else the house's ONLY vacuum, else None (clarify)."""
    room_codes = idx.rooms.get(room or "", {}).get("vacuums", [])
    if len(room_codes) == 1:
        return room_codes[0]
    all_codes = [c for c in idx.device_map if c.startswith("vacuum.")]
    return all_codes[0] if len(all_codes) == 1 else None


async def _resolve_media(idx: _DeviceIndex, room: str | None, want: tuple[str, ...]) -> str | None:
    """One media player for a transport verb.

    The scoped room's only player wins outright (pausing an idle TV is a
    harmless no-op); with several in the room, the one whose LIVE state makes
    it the obvious target (playing, for most verbs). No room match ⇒ widen
    house-wide by state — "pause the music" from the kitchen stops the one
    thing playing anywhere. Still ambiguous ⇒ None (the LLM clarifies).
    """

    async def pick(codes: list[str], lone_wins: bool) -> str | None:
        if lone_wins and len(codes) == 1:
            # A room's only player wins even when idle (a no-op pause is
            # harmless) — but never a dead one; fall through to widen instead.
            try:
                state = str((await _ha_state(codes[0])).get("state", ""))
            except Exception:
                return None
            return codes[0] if state not in ("unavailable", "unknown") else None
        hits = []
        for c in codes:
            try:
                state = str((await _ha_state(c)).get("state", ""))
            except Exception:
                continue
            if state in want:
                hits.append(c)
        return hits[0] if len(hits) == 1 else None

    room_codes = idx.rooms.get(room or "", {}).get("media", [])
    if room_codes:
        code = await pick(room_codes, lone_wins=True)
        if code:
            return code
    house = [c for r in idx.rooms.values() for c in r.get("media", [])]
    house = [c for c in house if c not in room_codes]
    return await pick(house, lone_wins=False) if house else None


def _resolve_named(idx: _DeviceIndex, target: str, origin_room: str | None) -> str | None:
    """Name-first resolution for the activate verb family: curated aliases
    house-wide (unique hits only), then fuzzy across every device name,
    retrying with a trailing qualifier word stripped."""
    toks = [t for t in _norm(target).split() if t not in _ARTICLES and t not in _FILLERS]
    if not toks:
        return None

    # "the vacuum" is a type word, not a name ("Rosie" won't fuzzy-match it):
    # resolve it positionally — an explicit room, the asking room, or the only one.
    room, vac_toks = _extract_room(idx, toks)
    if " ".join(vac_toks) in _VACUUM_WORDS:
        return _resolve_vacuum(idx, room or _room_key(origin_room))
    phrases = [" ".join(toks)]
    if len(toks) > 1 and toks[-1] in _QUALIFIERS:
        phrases.append(" ".join(toks[:-1]))

    origin = _room_key(origin_room)
    for phrase in phrases:
        hits = idx.aliases.get((origin, phrase)) or idx.aliases.get(("unplaced", phrase))
        if not hits:
            all_hits = [codes for (_r, ph), codes in idx.aliases.items() if ph == phrase]
            if len(all_hits) == 1:
                hits = all_hits[0]
        if hits and len(hits) == 1:
            return hits[0]
        code = _fuzzy(idx, None, phrase)
        if code:
            return code
    return None


def _parse_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _confirm(action: str, idx: _DeviceIndex, codes: list[str], target: str) -> str:
    # Pull any room out of the spoken target and render it first, so a phrase
    # like "lamps in the living room" reads "the living room lamps" rather than
    # the awkward "the lamps in living room".
    toks = [w for w in _norm(target).split() if w not in _ARTICLES and w not in _ALL_WORDS]
    room, rest = _extract_room(idx, toks)
    floor = None
    if room is None:
        floor, rest = _extract_floor(idx, rest)
    device_phrase = " ".join(w for w in rest if w not in _FILLERS).strip()
    if len(codes) == 1:
        device_phrase = idx.spoken.get(codes[0], device_phrase)

    scope = room or floor
    room_phrase = scope.replace("_", " ") if scope else ""
    # HA devices often have their area baked into the name ("Master Bedroom Light"
    # in the Master Bedroom) — rendering room + device would say the room twice.
    # When the device name already starts with the room, it carries the room alone.
    if room_phrase and re.match(rf"{re.escape(room_phrase)}\b", device_phrase, re.IGNORECASE):
        room_phrase = ""
    what = " ".join(p for p in (room_phrase, device_phrase) if p).strip()
    if not what:
        what = "it" if len(codes) == 1 else "them"
    article = "" if what in ("it", "them") else "the "
    return f"{_VERB[action]} {article}{what}."


@fast_intent(priority=50)
async def fast_home_control(utterance: str, room_id: str | None, speaker: str | None) -> FastResult:
    """Deterministic home control: instant, no LLM, defers hard cases."""
    try:
        await _ensure_view()
        idx = _get_index()
        container = _get_intents()
    except Exception as exc:
        log.warning("HA fast path unavailable: %s", exc)
        return FastResult.miss()

    try:
        # STT transcripts carry capitalization + punctuation ("Mute the TV.").
        # Capture patterns absorb a trailing period into {device} (harmless —
        # resolution normalizes), but fixed-word patterns ("mute the (tv|…)")
        # would hard-miss on it, so parse a normalized copy.
        match = container.calc_intent(_norm(utterance)) or {}
    except Exception as exc:
        log.warning("HA intent parse failed: %s", exc)
        return FastResult.miss()

    name = match.get("name")
    if (
        name not in _CONTROL
        and name not in ("set_temperature", "activate", "stop_device", "return_home")
        and name not in _MEDIA_INTENTS
    ):
        return FastResult.miss()

    ents = match.get("entities", {}) or {}
    target = str(ents.get("device", ""))
    origin = room_id or get_config("home_assistant", "default_room") or ""

    if name == "set_temperature":
        value = _parse_int(ents.get("value"))
        codes = _resolve_climate(idx, target, origin) if value is not None else None
        if not codes:
            return FastResult.miss()
        temp = max(_thermo_min(), min(_thermo_max(), float(value)))  # type: ignore[arg-type]
        devices = [{"id": c, "action": "set_temperature", "temperature": temp} for c in codes]
        await _apply_devices(devices, idx.device_map)
        return FastResult.handled(f"Set the temperature to {int(temp)} degrees.", _FAST_VOICE)

    if name in _MEDIA_INTENTS:
        svc, want, verb = _MEDIA_INTENTS[name]
        spoken_room = _norm(str(ents.get("room", "")))
        room = idx.room_phrases.get(spoken_room) if spoken_room else _room_key(origin)
        if spoken_room and not room:
            return FastResult.miss()  # named a room we don't know
        code = await _resolve_media(idx, room, want)
        if not code:
            return FastResult.miss()
        await _apply_devices([{"id": code, "action": svc}], idx.device_map)
        return FastResult.handled(f"{verb} the {idx.spoken.get(code, code)}.", _FAST_VOICE)

    if name == "activate":
        code = _resolve_named(idx, target, origin)
        if not code or code.split(".", 1)[0] not in _ACTIVATE_DOMAINS:
            return FastResult.miss()
        domain = code.split(".", 1)[0]
        svc = _ACTIVATE_SERVICE.get(domain, "turn_on")
        await _apply_devices([{"id": code, "action": svc}], idx.device_map)
        verb = _ACTIVATE_VERB.get(domain, "Turned on")
        return FastResult.handled(f"{verb} {idx.spoken.get(code, code)}.", _FAST_VOICE)

    if name == "stop_device":
        code = _resolve_named(idx, target, origin)
        svc = _STOP_SERVICES.get(code.split(".", 1)[0]) if code else None
        if not code or not svc:
            return FastResult.miss()
        await _apply_devices([{"id": code, "action": svc}], idx.device_map)
        return FastResult.handled(f"Stopped {idx.spoken.get(code, code)}.", _FAST_VOICE)

    if name == "return_home":
        code = _resolve_named(idx, target, origin)
        if not code or not code.startswith("vacuum."):
            return FastResult.miss()
        await _apply_devices([{"id": code, "action": "return_to_base"}], idx.device_map)
        return FastResult.handled(f"Sent {idx.spoken.get(code, code)} home.", _FAST_VOICE)

    codes = _resolve_target(idx, name, target, origin)
    if not codes:
        return FastResult.miss()

    svc = _CONTROL[name][0]
    # A name-first entity resolved through the legacy verbs ("turn off movie
    # night"): translate per domain where the intent is obvious ("turn on the
    # vacuum" means start it), then only act when the domain supports the
    # service — else let the LLM untangle it.
    services = [_SERVICE_ALIAS.get((c.split(".", 1)[0], svc), svc) for c in codes]
    if not all(_service_ok(c, s) for c, s in zip(codes, services)):
        return FastResult.miss()
    devices = [{"id": c, "action": s} for c, s in zip(codes, services)]
    if blocked := _secure_blocked(devices, speaker):
        return FastResult.handled(blocked, _FAST_VOICE)
    await _apply_devices(devices, idx.device_map)
    return FastResult.handled(_confirm(name, idx, codes, target), _FAST_VOICE)


# ---------------------------------------------------------------------------
# Music Assistant play-by-name passthrough (4.3)
# ---------------------------------------------------------------------------
#
# Kenzy never resolves music names. MA's `play_media` takes the spoken phrase
# (media_id) and does the artist/album/track/playlist search itself; Kenzy's
# whole job is carrying the words and picking the right room's player. MA
# players are identified by entity-REGISTRY ownership (`Entity.ma`, via
# integration_entities) — never by name, so operator renames can't break it.

_PLAY_RE = re.compile(
    r"^\s*(?:please[,\s]+)?(?:play|put on|listen to)\s+(.+?)[.!?]*\s*$", re.IGNORECASE
)
# Queries that are really transport/ambient commands, not names to search for.
_PLAY_NOISE = {"music", "some music", "songs", "something", "it", "that", "this", "the music"}
_PLAY_TRANSPORT_HEADS = ("next", "previous", "the next", "the previous", "pause", "stop")


def _music_rooms(idx: _DeviceIndex) -> dict[str, list[str]]:
    return {room: t["music"] for room, t in idx.rooms.items() if t.get("music")}


def _resolve_music(
    idx: _DeviceIndex, spoken_room: str, origin: str | None
) -> tuple[str | None, str | None]:
    """Pick one MA player: the named room's, else the asking room's, else the
    house's only one. Returns (entity_id, room_key) — (None, room_key) means
    the NAMED room has no player (an honest reply beats a silent miss)."""
    music = _music_rooms(idx)
    if spoken_room:
        room = idx.room_phrases.get(spoken_room)
        if room is None:
            return None, None  # not a room we know — treat as part of the title
        codes = music.get(room, [])
        return (codes[0] if len(codes) == 1 else None), room
    origin_key = _room_key(origin) if origin else None
    codes = music.get(origin_key or "", [])
    if len(codes) == 1:
        return codes[0], origin_key
    house = [(r, c) for r, cs in music.items() for c in cs]
    if len(house) == 1:
        return house[0][1], house[0][0]
    return None, None


def _split_play_room(idx: _DeviceIndex, query: str) -> tuple[str, str]:
    """Strip a trailing "in the <room>" ONLY when <room> is a room we know —
    "Dancing in the Dark" must stay a title, not become a room lookup."""
    m = re.search(r"\s+in\s+the\s+([\w\s]+?)[.!?]*\s*$", query, re.IGNORECASE)
    if m and _norm(m.group(1)) in idx.room_phrases:
        return query[: m.start()].strip(), _norm(m.group(1))
    return query.strip(), ""


@fast_intent(priority=55)
async def fast_play(utterance: str, room_id: str | None, speaker: str | None) -> FastResult:
    """"Play <anything>" → Music Assistant, instantly. Misses to the LLM when
    no MA player fits or the phrase smells like transport, and stays entirely
    out of the way in MA-less homes."""
    m = _PLAY_RE.match(utterance)
    if m is None:
        return FastResult.miss()
    try:
        await _ensure_view()
        idx = _get_index()
    except Exception as exc:
        log.warning("HA fast path unavailable: %s", exc)
        return FastResult.miss()
    if not _music_rooms(idx):
        return FastResult.miss()  # no MA in this house — the LLM answers

    query, spoken_room = _split_play_room(idx, m.group(1))
    normed = _norm(query)
    if normed.startswith("some "):
        query, normed = query[5:].strip(), normed[5:]
    if not normed or normed in _PLAY_NOISE or normed.startswith(_PLAY_TRANSPORT_HEADS):
        return FastResult.miss()

    code, room = _resolve_music(idx, spoken_room, room_id)
    if code is None and spoken_room and room is not None:
        return FastResult.handled(
            f"There's no music player in the {spoken_room}.", _FAST_VOICE
        )
    if code is None:
        return FastResult.miss()  # ambiguous — the LLM can ask which room
    try:
        await _ma_play(code, query)
    except Exception as exc:
        log.warning("MA play_media failed for %r: %s", query, exc)
        return FastResult.miss()
    where = (room or "").replace("_", " ")
    return FastResult.handled(
        f"Playing {query}{f' in the {where}' if where else ''}.", _FAST_VOICE
    )


@skill
async def play_music(query: str, room: str = "") -> str:
    """Play music by name — an artist, album, song, playlist, or genre.

    Music Assistant resolves the name itself, so pass the user's words
    through as `query` (e.g. "miles davis", "workout playlist"). `room` is
    the room to play in; leave it empty for the room the user is speaking
    from. Use this for any "play …" music request; transport actions
    (pause, skip, volume) have their own handling.
    """
    try:
        await _ensure_view()
        idx = _get_index()
    except Exception as exc:
        return f"I couldn't reach Home Assistant: {exc}"
    if not _music_rooms(idx):
        return (
            "There are no Music Assistant players here — install and connect "
            "Music Assistant in Home Assistant to play music by name."
        )
    code, room_key = _resolve_music(idx, _norm(room) if room else "", get_request("room_id"))
    if code is None and room:
        return f"There's no music player in the {room}."
    if code is None:
        rooms = ", ".join(sorted(r.replace("_", " ") for r in _music_rooms(idx)))
        return f"Which room should I play that in? There are music players in: {rooms}."
    try:
        await _ma_play(code, query)
    except Exception as exc:
        log.warning("MA play_media failed for %r: %s", query, exc)
        return f"Music Assistant couldn't play {query!r} — it may not know that name."
    where = (room_key or "").replace("_", " ")
    return f"Playing {query}" + (f" in the {where}." if where else ".")
