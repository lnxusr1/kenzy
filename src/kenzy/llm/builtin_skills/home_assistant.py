"""
Home Assistant skill — device control over the HA REST API.

Device topology (entities, names, domains, area/floor placement) is pulled
**live from Home Assistant** by :mod:`kenzy.llm.builtin_skills.ha_model` and
cached; the only hand-authored input is ``curation.yaml`` (aliases, per-device
notes, room group-defaults, voice-control exclusions). ``_ensure_view`` builds a
``_DeviceIndex`` + resolver text from that merged view; if HA is unreachable and
the legacy ``device_ids.yaml`` / ``device_ids.json`` files exist, it falls back
to them.

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
  domains:       [light, switch, fan, cover, lock, climate]
  default_room:  ""           # used when the user doesn't specify a room
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from kenzy.llm.builtin_skills import ha_model
from kenzy.llm.skills import FastResult, fast_intent, get_config, skill  # type: ignore[import]

log = logging.getLogger(__name__)

_THERMO_MIN = 65.0
_THERMO_MAX = 85.0

# Actions that require a recognized (non-unknown) speaker.
_SECURE_ACTIONS = {"lock", "unlock", "open_cover", "close_cover"}

_SYSTEM_PROMPT = """\
You are a home automation resolver.  Given a device map (YAML) and a user \
request, identify which devices to act on and what action to perform.

The device map is structured as: floor > area > type > device.
Top-level keys are floors (e.g. downstairs, upstairs, outside).
Second-level keys are areas (rooms) within that floor.
Third-level keys are device types (lights, fans, locks, covers, climate).

Device actions by type:
- light / switch : turn_on | turn_off | toggle
- fan            : turn_on | turn_off | toggle
- cover          : open_cover | close_cover
- lock           : lock | unlock
- climate        : set_temperature  (°F, must be 65–85)

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


def _project_root() -> Path:
    """Operational-tree root that holds ``data/home_assistant/`` (KENZY_HOME-aware)."""
    from kenzy.config import kenzy_data_root
    return kenzy_data_root()


def _room_to_area(yaml_text: str) -> dict[str, str]:
    """Build a room→area lookup from the area>room>type>device YAML structure."""
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


async def _resolve(request: str, yaml_text: str, room: str | None = None) -> dict[str, Any]:
    """Sub-LLM call: map the user request to device aliases + actions."""
    from litellm import acompletion  # type: ignore[import-untyped]

    model    = get_config("home_assistant", "model",    "gpt-4o")
    base_url = get_config("home_assistant", "base_url") or None

    resolved_room = room or get_config("home_assistant", "default_room") or ""
    resolved_floor = _room_to_area(yaml_text).get(resolved_room, "") if resolved_room else ""

    user_content = f"Device map:\n{yaml_text}\n\nUser request: {request}"
    if resolved_room:
        loc = f"Floor: {resolved_floor}, Area: {resolved_room}" if resolved_floor else f"Area: {resolved_room}"
        user_content += (
            f"\n\nLocation context: {loc}"
            "\nScope all ambiguous device references to this area unless the"
            " request explicitly names a different area or floor."
        )

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
async def handle_home_control(request: str, speaker: str | None = None, room: str | None = None) -> str:
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

    if (blocked := _secure_blocked(devices, speaker)):
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


async def _apply_devices(
    devices: list[dict[str, Any]], device_map: dict[str, str]
) -> list[str]:
    """Execute resolved device actions against Home Assistant.

    Returns status lines for any get_status actions (empty for pure control).
    Shared by the LLM skill and the deterministic fast path.
    """
    status_lines: list[str] = []

    for dev in devices:
        alias  = str(dev.get("id", ""))
        action = str(dev.get("action", ""))
        # In the live-HA path the id is already an entity_id (device_map is an
        # identity map); in the static path it's a friendly code mapped to one.
        ha_id  = device_map.get(alias) or (alias if "." in alias else None)

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
    "light": "lights", "lights": "lights",
    "fan": "fans", "fans": "fans",
    "door": "lock", "doors": "lock", "lock": "lock", "locks": "lock",
    "cover": "covers", "covers": "covers", "blind": "covers", "blinds": "covers",
    "shade": "covers", "shades": "covers", "curtain": "covers", "curtains": "covers",
}

# action name → (HA service, target type keys, direction).
# direction "activate" uses the room's curated default subset for a bare group;
# "deactivate" always acts on every device of that type in the room.
_CONTROL: dict[str, tuple[str, set[str], str]] = {
    "turn_on":     ("turn_on",     {"lights", "fans"}, "activate"),
    "turn_off":    ("turn_off",    {"lights", "fans"}, "deactivate"),
    "toggle":      ("toggle",      {"lights", "fans"}, "activate"),
    "lock":        ("lock",        {"lock"},   "deactivate"),
    "unlock":      ("unlock",      {"lock"},   "activate"),
    "open_cover":  ("open_cover",  {"covers"}, "activate"),
    "close_cover": ("close_cover", {"covers"}, "deactivate"),
}
# Unsafe directions must name a specific device — never act on a bare group.
_EXPLICIT_ONLY = {"unlock", "open_cover"}

_DOMAIN_TO_TYPE = {
    "light": "lights", "switch": "lights", "fan": "fans",
    "cover": "covers", "lock": "lock", "climate": "climate",
}

_ARTICLES = {"the", "a", "an", "my", "our", "your", "some", "please"}
_FILLERS = {"in", "at", "of", "inside", "to"}
_ALL_WORDS = {"all", "every", "everything", "any"}
_FUZZ_CUTOFF = 82

# Past tense: the HA REST call completes before TTS playback even begins, so the
# device is already on/off/locked by the time the confirmation is spoken.
_VERB = {
    "turn_on": "Turned on", "turn_off": "Turned off", "toggle": "Toggled",
    "lock": "Locked", "unlock": "Unlocked",
    "open_cover": "Opened", "close_cover": "Closed",
}


@dataclass
class _DeviceIndex:
    rooms:        dict[str, dict[str, list[str]]]      # room → type_key → [codes]
    defaults:     dict[str, dict[str, list[str]]]      # room → type_key → [codes]
    spoken:       dict[str, str]                       # code → spoken name
    aliases:      dict[tuple[str, str], list[str]]     # (room, phrase) → [codes]
    exclude:      set[str]                             # codes barred from groups
    room_phrases: dict[str, str]                       # "living room" → "living_room"
    device_map:   dict[str, str]                       # code → entity_id
    # Floor scope: a spoken floor name ("downstairs") expands a bare-group command
    # to every room on that floor. Empty when the topology has no floors.
    floor_phrases: dict[str, str] = field(default_factory=dict)   # "downstairs" → "downstairs"
    floor_rooms:   dict[str, list[str]] = field(default_factory=dict)  # floor → [rooms]


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


def _index_from(
    yaml_text: str, device_map: dict[str, str], overlay: dict[str, Any]
) -> _DeviceIndex:
    import yaml as _yaml

    data = _yaml.safe_load(yaml_text) or {}
    rooms: dict[str, dict[str, list[str]]] = {}
    defaults: dict[str, dict[str, list[str]]] = {}
    room_phrases: dict[str, str] = {}
    floor_phrases: dict[str, str] = {}
    floor_rooms: dict[str, list[str]] = {}

    for _area, rms in data.items():
        if not isinstance(rms, dict):
            continue
        # In the static files the top level is the floor (downstairs/upstairs/outside).
        floor = _norm(str(_area)).replace(" ", "_")
        floor_phrases[str(_area).replace("_", " ")] = floor
        for room, groups in rms.items():
            if not isinstance(groups, dict):
                continue
            room_phrases[room.replace("_", " ")] = room
            if room not in floor_rooms.setdefault(floor, []):
                floor_rooms[floor].append(room)
            rt = rooms.setdefault(room, {})
            dflt = defaults.setdefault(room, {})
            for type_key, codes in groups.items():
                if not isinstance(codes, list):
                    continue
                if type_key == "default":
                    # Legacy room-level default: bucket each code under its type.
                    for c in codes:
                        domain = (device_map.get(c, "") or "").split(".")[0]
                        tk = _DOMAIN_TO_TYPE.get(domain)
                        if tk:
                            dflt.setdefault(tk, []).append(c)
                else:
                    bucket = rt.setdefault(type_key, [])
                    for c in codes:
                        if c not in bucket:
                            bucket.append(c)

    spoken = {code: _auto_spoken(code) for code in device_map}
    aliases: dict[tuple[str, str], list[str]] = {}
    exclude: set[str] = set()

    for room, ov in (overlay.get("rooms", {}) if overlay else {}).items():
        if not isinstance(ov, dict):
            continue
        for phrase, target in (ov.get("aliases", {}) or {}).items():
            codes = [target] if isinstance(target, str) else list(target)
            aliases[(room, _norm(phrase))] = codes
        for tk, codes in (ov.get("defaults", {}) or {}).items():
            defaults.setdefault(room, {})[tk] = list(codes)
        for c in ov.get("exclude", []) or []:
            exclude.add(c)

    return _DeviceIndex(
        rooms, defaults, spoken, aliases, exclude, room_phrases, device_map,
        floor_phrases, floor_rooms,
    )


def _build_intents() -> Any:
    from padacioso import IntentContainer  # type: ignore[import-untyped]

    c = IntentContainer()
    c.add_intent("turn_on", [
        "[please] turn on [the] {device}", "[please] switch on [the] {device}",
        "[please] cut on [the] {device}",
    ])
    c.add_intent("turn_off", [
        "[please] turn off [the] {device}", "[please] switch off [the] {device}",
        "[please] cut off [the] {device}", "[please] kill [the] {device}",
    ])
    c.add_intent("toggle", ["[please] toggle [the] {device}"])
    c.add_intent("lock", ["[please] lock [the] {device}"])
    c.add_intent("unlock", ["[please] unlock [the] {device}"])
    c.add_intent("open_cover", [
        "[please] open [the] {device}", "[please] raise [the] {device}",
    ])
    c.add_intent("close_cover", [
        "[please] close [the] {device}", "[please] shut [the] {device}",
        "[please] lower [the] {device}",
    ])
    c.add_intent("set_temperature", [
        "set [the] {device} to {value} [degrees]",
        "change [the] {device} to {value} [degrees]",
        "set [the] {device} [to] {value} degrees",
    ])
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
        rooms, defaults, spoken, aliases, exclude, room_phrases, device_map,
        floor_phrases, floor_rooms,
    )


def _resolver_text(model: ha_model.HAModel, curation: dict[str, Any]) -> str:
    """Render the live topology as the floor>area>type>entity text the LLM reads."""
    notes = {
        eid: str(dev.get("note") or "")
        for eid, dev in (curation.get("devices", {}) or {}).items()
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


def _load_static_view() -> None:
    """Offline / legacy fallback: build the view from the static device_ids files."""
    global _INDEX, _RESOLVER_TEXT, _VIEW_TS
    yaml_text, device_map = _load_device_files()
    _INDEX = _index_from(yaml_text, device_map, _load_overlay())
    _RESOLVER_TEXT = yaml_text
    _VIEW_TS = None


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
            return  # keep the existing (live-stale or static) view
        log.warning("HA topology unavailable (%s); using static device files", exc)
        _load_static_view()
        return

    if _INDEX is None or _RESOLVER_TEXT is None or _VIEW_TS != model.fetched_at:
        curation = ha_model.load_curation()
        _INDEX = _index_from_model(model, curation)
        _RESOLVER_TEXT = _resolver_text(model, curation)
        _VIEW_TS = model.fetched_at


def _get_resolver_text() -> str:
    if _RESOLVER_TEXT is None:
        raise RuntimeError("resolver text not initialized; call _ensure_view() first")
    return _RESOLVER_TEXT


def _load_overlay() -> dict[str, Any]:
    root = _project_root()
    rel = get_config("home_assistant", "device_overlay", "data/home_assistant/device_overlay.yaml")
    path = root / rel
    if not path.exists():
        return {}
    import yaml as _yaml
    try:
        return _yaml.safe_load(path.read_text()) or {}
    except Exception as exc:
        log.warning("Could not load device overlay %s: %s", path, exc)
        return {}


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
            if toks[i:i + n] == words:
                return idx.room_phrases[sp], toks[:i] + toks[i + n:]
    return None, toks


def _extract_floor(idx: _DeviceIndex, toks: list[str]) -> tuple[str | None, list[str]]:
    """Pull an explicit floor name ("downstairs") out of the token list."""
    for sp in sorted(idx.floor_phrases, key=lambda p: -len(p.split())):
        words = sp.split()
        n = len(words)
        for i in range(len(toks) - n + 1):
            if toks[i:i + n] == words:
                return idx.floor_phrases[sp], toks[:i] + toks[i + n:]
    return None, toks


def _fuzzy(idx: _DeviceIndex, room: str | None, phrase: str) -> str | None:
    from rapidfuzz import fuzz, process

    if room is not None:
        codes = [c for grp in idx.rooms.get(room, {}).values() for c in grp]
    else:
        codes = list(idx.device_map)
    if not codes:
        return None
    choices = {idx.spoken.get(c, c): c for c in codes}
    match = process.extractOne(
        phrase, list(choices), scorer=fuzz.WRatio, score_cutoff=_FUZZ_CUTOFF
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
        if stem in idx.spoken.get(c, c).split()
    ]
    codes = [c for c in codes if c not in idx.exclude]
    return codes or None


def _group_codes(
    idx: _DeviceIndex, room: str, gtype: str, direction: str, has_all: bool
) -> list[str]:
    """Codes for a bare group in one room: curated default subset for an
    ``activate`` (unless "all"), every device of that type otherwise."""
    rt = idx.rooms.get(room, {})
    if direction == "activate" and not has_all:
        return list(idx.defaults.get(room, {}).get(gtype) or rt.get(gtype, []))
    return list(rt.get(gtype, []))


def _resolve_target(
    idx: _DeviceIndex, action: str, target: str, origin_room: str | None
) -> list[str] | None:
    """Resolve a control command to device codes, or None to defer to the LLM."""
    _svc, types, direction = _CONTROL[action]

    raw = [t for t in _norm(target).split() if t]
    has_all = bool(set(raw) & _ALL_WORDS)
    toks = [t for t in raw if t not in _ARTICLES and t not in _ALL_WORDS]

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

    # Bare group ("the lights", "fans").
    if phrase in _GROUP_WORDS:
        gtype = _GROUP_WORDS[phrase]
        if gtype not in types or action in _EXPLICIT_ONLY:
            return None
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

    # Widen to a house-wide search ONLY when the user did not name a room.
    # If they explicitly said "in the <room>", stay there (or defer to the LLM)
    # rather than wandering into another room's similarly-named device.
    if not explicit_room:
        code = _fuzzy(idx, None, phrase)
        if code:
            return [code]
    return None


def _resolve_climate(
    idx: _DeviceIndex, target: str, origin_room: str | None
) -> list[str] | None:
    toks = [t for t in _norm(target).split() if t not in _ARTICLES]
    room, _toks = _extract_room(idx, toks)
    room = room or _room_key(origin_room)
    codes = idx.rooms.get(room, {}).get("climate", [])
    return codes or None


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
    what = " ".join(p for p in (room_phrase, device_phrase) if p).strip()
    if not what:
        what = "it" if len(codes) == 1 else "them"
    article = "" if what in ("it", "them") else "the "
    return f"{_VERB[action]} {article}{what}."


@fast_intent(priority=50)
async def fast_home_control(
    utterance: str, room_id: str | None, speaker: str | None
) -> FastResult:
    """Deterministic home control: instant, no LLM, defers hard cases."""
    try:
        await _ensure_view()
        idx = _get_index()
        container = _get_intents()
    except Exception as exc:
        log.warning("HA fast path unavailable: %s", exc)
        return FastResult.miss()

    try:
        match = container.calc_intent(utterance) or {}
    except Exception as exc:
        log.warning("HA intent parse failed: %s", exc)
        return FastResult.miss()

    name = match.get("name")
    if name not in _CONTROL and name != "set_temperature":
        return FastResult.miss()

    ents = match.get("entities", {}) or {}
    target = str(ents.get("device", ""))
    origin = room_id or get_config("home_assistant", "default_room") or ""

    if name == "set_temperature":
        value = _parse_int(ents.get("value"))
        codes = _resolve_climate(idx, target, origin) if value is not None else None
        if not codes:
            return FastResult.miss()
        temp = max(_THERMO_MIN, min(_THERMO_MAX, float(value)))  # type: ignore[arg-type]
        devices = [{"id": c, "action": "set_temperature", "temperature": temp} for c in codes]
        await _apply_devices(devices, idx.device_map)
        return FastResult.handled(f"Set the temperature to {int(temp)} degrees.", _FAST_VOICE)

    codes = _resolve_target(idx, name, target, origin)
    if not codes:
        return FastResult.miss()

    svc = _CONTROL[name][0]
    devices = [{"id": c, "action": svc} for c in codes]
    if (blocked := _secure_blocked(devices, speaker)):
        return FastResult.handled(blocked, _FAST_VOICE)
    await _apply_devices(devices, idx.device_map)
    return FastResult.handled(_confirm(name, idx, codes, target), _FAST_VOICE)
