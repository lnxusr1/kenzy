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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from kenzy.llm.skills import FastResult, fast_intent, get_config, skill  # type: ignore[import]

log = logging.getLogger(__name__)

_THERMO_MIN = 65.0
_THERMO_MAX = 85.0

# Actions that require a recognized (non-unknown) speaker.
_SECURE_ACTIONS = {"lock", "unlock", "open_cover", "close_cover"}

_SYSTEM_PROMPT = """\
You are a home automation resolver.  Given a device map (YAML) and a user \
request, identify which devices to act on and what action to perform.

The device map is structured as: area > room > type > device.
Top-level keys are areas (e.g. downstairs, upstairs, outside).
Second-level keys are rooms within that area.
Third-level keys are device types (lights, fans, locks, covers, climate).

Device actions by type:
- light / switch : turn_on | turn_off | toggle
- fan            : turn_on | turn_off | toggle
- cover          : open_cover | close_cover
- lock           : lock | unlock
- climate        : set_temperature  (°F, must be 65–85)

Selection rules:
- A location context (area + room) may be provided below the request. When it
  is, ALL ambiguous device references must be resolved within that room first.
  A device name that appears in multiple rooms (e.g. "the lamp") refers to the
  one in the context room — do not match devices in other rooms.
- If the user explicitly names a different room or area in their request, that
  overrides the context room for that reference only.
- Plural type with a room ("the lights", "the fans") means all devices of that
  type in the context room (or the explicitly named room).
- Area-level requests ("all the lights downstairs", "upstairs fans") select all
  matching devices across every room in that area.
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
    resolved_area = _room_to_area(yaml_text).get(resolved_room, "") if resolved_room else ""

    user_content = f"Device map:\n{yaml_text}\n\nUser request: {request}"
    if resolved_room:
        loc = f"Area: {resolved_area}, Room: {resolved_room}" if resolved_area else f"Room: {resolved_room}"
        user_content += (
            f"\n\nLocation context: {loc}"
            "\nScope all ambiguous device references to this room unless the"
            " request explicitly names a different room or area."
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
        yaml_text, device_map = _load_device_files()
    except Exception as exc:
        return f"Could not load device map: {exc}"

    try:
        result = await _resolve(request, yaml_text, room)
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

    for _area, rms in data.items():
        if not isinstance(rms, dict):
            continue
        for room, groups in rms.items():
            if not isinstance(groups, dict):
                continue
            room_phrases[room.replace("_", " ")] = room
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

    return _DeviceIndex(rooms, defaults, spoken, aliases, exclude, room_phrases, device_map)


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
    global _INDEX
    if _INDEX is None:
        yaml_text, device_map = _load_device_files()
        _INDEX = _index_from(yaml_text, device_map, _load_overlay())
    return _INDEX


def _get_intents() -> Any:
    global _INTENTS
    if _INTENTS is None:
        _INTENTS = _build_intents()
    return _INTENTS


def _reset_cache() -> None:
    """Test hook: drop the cached index + intent container."""
    global _INDEX, _INTENTS
    _INDEX = None
    _INTENTS = None


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
    toks = [t for t in toks if t not in _FILLERS]
    phrase = " ".join(toks).strip()
    if not phrase:
        return None

    # Overlay alias (room-scoped, then global).
    codes = idx.aliases.get((room, phrase)) or idx.aliases.get(("", phrase))
    if codes is not None:
        return codes

    # Bare group ("the lights", "fans").
    if phrase in _GROUP_WORDS:
        gtype = _GROUP_WORDS[phrase]
        if gtype not in types or action in _EXPLICIT_ONLY:
            return None
        rt = idx.rooms.get(room, {})
        if direction == "activate" and not has_all:
            codes = idx.defaults.get(room, {}).get(gtype) or rt.get(gtype, [])
        else:
            codes = rt.get(gtype, [])
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
    device_phrase = " ".join(w for w in rest if w not in _FILLERS).strip()
    if len(codes) == 1:
        device_phrase = idx.spoken.get(codes[0], device_phrase)

    room_phrase = room.replace("_", " ") if room else ""
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
