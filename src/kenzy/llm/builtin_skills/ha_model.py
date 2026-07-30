"""
Home Assistant live topology model.

HA is the source of truth for *facts* — which entities exist, their names,
domains, and their area/floor placement.  This module pulls that topology and
normalizes it into a flat list of :class:`Entity` records that the
``home_assistant`` skill projects into its resolver index.

Design notes
------------
* **Topology vs. state.**  This module fetches only the slow-changing topology
  (names / areas / floors / domains) and caches it.  Volatile device *state*
  (on/off, current temperature) is never cached here — the skill reads that live
  via the HA REST ``/api/states`` endpoint only when a request actually needs it
  (status queries, relative-temperature changes).

* **REST, not WebSocket.**  Area/floor placement isn't available from the plain
  ``/api/states`` REST endpoint, so we render a single Jinja template through
  ``POST /api/template`` that returns the whole ``area > entity`` tree as JSON.
  This keeps the ``llm`` extra dependency-free (reuses ``httpx`` + ``HA_API_KEY``).
  The trade-off: the template engine can't see registry-only flags
  (``entity_category`` / ``hidden_by``), so the automatic filter here is limited
  to controllable domains; finer exclusion is done with curation rules.

* **Curation.**  The one thing HA can't store — aliases, per-device notes, room
  group-defaults, and voice exclusions — lives in ``curation.yaml``.  This module
  owns loading that file and applies its *exclusion* rules at model-build time so
  an excluded entity is absent from the model entirely (unreachable by any path).
  Aliases / notes / defaults are consumed later by the skill's index builder.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from kenzy.llm.skills import get_config  # type: ignore[import]

log = logging.getLogger(__name__)

# Domains we can voice-control.  Anything else HA exposes is ignored.
_DEFAULT_DOMAINS = (
    "light",
    "switch",
    "fan",
    "cover",
    "lock",
    "climate",
    "scene",
    "script",
    "button",
    "input_button",
    "input_boolean",
    "vacuum",
    "media_player",
)

# Name-first domains: voice-reachable even with NO area assignment (scenes and
# scripts usually have none).  The topology template only walks areas, so these
# are additionally merged in from /api/states when unplaced; spatial domains
# stay placed-only (an area-less light is deliberately not voice-addressable).
NAME_FIRST_DOMAINS = frozenset(
    {"scene", "script", "button", "input_button", "input_boolean", "vacuum"}
)

# Device buttons with these device_classes are hardware maintenance (Identify /
# Restart / Update), not voice targets — dropped automatically. In a real HA
# they dominate the button domain (~30 of 39 observed).
_DIAGNOSTIC_BUTTON_CLASSES = {"identify", "restart", "update"}

# Synthetic top level used when an area has no floor (floors are optional in HA).
_NO_FLOOR = "home"

# One render pass returns the whole area>entity tree as a JSON array.  We resolve
# the floor per-entity (entity -> area -> floor) so floorless areas yield null.
_TEMPLATE = (
    "{% set ns = namespace(items=[]) %}"
    "{% for a in areas() %}"
    "{% for e in area_entities(a) %}"
    "{% set ns.items = ns.items + [{"
    "'entity_id': e,"
    "'name': state_attr(e, 'friendly_name'),"
    "'area': area_name(a),"
    "'floor': floor_name(e)"
    "}] %}"
    "{% endfor %}"
    "{% endfor %}"
    "{{ ns.items | tojson }}"
)


_MA_TEMPLATE = "{{ integration_entities('music_assistant') | list | tojson }}"


@dataclass(frozen=True)
class Entity:
    """A single voice-controllable HA entity, placed in the area/floor tree."""

    entity_id: str
    domain: str
    name: str  # spoken-friendly name (HA friendly_name, or derived)
    area: str  # room slug (lowercased, underscored); "" if unplaced
    area_name: str  # room display name
    floor: str  # floor slug; _NO_FLOOR when the area has no floor
    floor_name: str  # floor display name
    # Owned by the Music Assistant integration (entity-REGISTRY fact, so it
    # survives any rename — never inferred from names or entity_ids). MA
    # players are the only valid targets for play-by-name.
    ma: bool = False


@dataclass
class HAModel:
    """A cached snapshot of the HA topology."""

    entities: list[Entity] = field(default_factory=list)
    fetched_at: float = 0.0

    def by_id(self) -> dict[str, Entity]:
        return {e.entity_id: e for e in self.entities}


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def ha_conn() -> tuple[str, dict[str, str]]:
    """Return the HA base URL and auth headers (shared with the skill executor)."""
    base = get_config("home_assistant", "url", "http://homeassistant.local:8123")
    token = os.environ.get("HA_API_KEY", "")
    return base.rstrip("/"), {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def _slug(text: str) -> str:
    """Normalize a display name to a lowercase, underscore-joined slug."""
    cleaned = re.sub(r"[^\w\s]", "", text or "").strip().lower()
    return re.sub(r"\s+", "_", cleaned)


def _domains() -> tuple[str, ...]:
    configured = get_config("home_assistant", "domains", None)
    if isinstance(configured, list) and configured:
        return tuple(str(d).strip().lower() for d in configured)
    return _DEFAULT_DOMAINS


# ---------------------------------------------------------------------------
# Curation
# ---------------------------------------------------------------------------


def _curation_path() -> Path:
    from kenzy.config import kenzy_data_root

    rel = get_config("home_assistant", "curation_file", "data/home_assistant/curation.yaml")
    return kenzy_data_root() / rel


def load_curation() -> dict[str, Any]:
    """Load curation.yaml (aliases / notes / defaults / excludes), or {} if absent."""
    path = _curation_path()
    if not path.exists():
        return {}
    import yaml as _yaml

    try:
        data = _yaml.safe_load(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # malformed file shouldn't take the skill down
        log.warning("Could not load curation file %s: %s", path, exc)
        return {}


def _is_kenzy_entity(entity_id: str) -> bool:
    """Kenzy's OWN HA entities (the MQTT bridge's per-node buttons/switches).

    Never voice targets, and never occupancy evidence: her published sensors
    changing state is her own echo, and believing it would mean every room reads
    "occupied" the moment she speaks. Code rule, not curation — deliberately not
    overridable in either consumer.
    """
    return entity_id.split(".", 1)[-1].startswith("kenzy_")


def _exclude_reason(entity_id: str, domain: str, area: str, curation: dict[str, Any]) -> str:
    """Return the curation rule excluding an entity from voice control, or "".

    ``area`` is the area *slug*.  Single source of truth shared by the model
    builder and the ``kenzy-ha-devices`` discovery CLI.
    """
    # Built-in rule: Kenzy's own HA entities (the MQTT bridge's per-node
    # trigger/stop buttons and mute switch) are never voice targets — voice-
    # controlling your own control surface is a loop, and the mute switch
    # would otherwise ride the switch domain into "turn on the lights".
    if _is_kenzy_entity(entity_id):
        return "kenzy internal"

    exclude = curation.get("exclude", {}) or {}

    if entity_id in set(exclude.get("entities", []) or []):
        return "exclude.entities"
    if domain in set(exclude.get("domains", []) or []):
        return f"exclude.domains: {domain}"
    if area and area in {_slug(a) for a in (exclude.get("areas", []) or [])}:
        return f"exclude.areas: {area}"
    for pattern in exclude.get("patterns", []) or []:
        if fnmatch.fnmatch(entity_id, str(pattern)):
            return f"exclude.patterns: {pattern}"

    # Per-device shorthand: devices.<entity_id>.hidden = true
    dev = (curation.get("devices", {}) or {}).get(entity_id, {}) or {}
    return "devices.hidden" if dev.get("hidden") else ""


def _excluded(entity_id: str, domain: str, area: str, curation: dict[str, Any]) -> bool:
    """True if an entity is hard-excluded from voice control by curation rules."""
    return bool(_exclude_reason(entity_id, domain, area, curation))


_ALLOWED_TOP = {"exclude", "devices", "rooms", "lists", "occupancy"}
_EXCLUDE_LISTS = ("entities", "patterns", "domains", "areas")


def validate_curation(data: dict[str, Any]) -> dict[str, Any]:
    """Validate + normalize a curation dict, dropping empties. Raises ValueError."""
    if not isinstance(data, dict):
        raise ValueError("curation must be a mapping")
    unknown = set(data) - _ALLOWED_TOP
    if unknown:
        raise ValueError(f"unknown curation keys: {sorted(unknown)}")

    out: dict[str, Any] = {}

    ex = data.get("exclude") or {}
    if not isinstance(ex, dict):
        raise ValueError("exclude must be a mapping")
    clean_ex: dict[str, list[str]] = {}
    for key in _EXCLUDE_LISTS:
        val = ex.get(key)
        if val is None:
            continue
        if not isinstance(val, list):
            raise ValueError(f"exclude.{key} must be a list")
        items = [str(x).strip() for x in val if str(x).strip()]
        if items:
            clean_ex[key] = items
    if clean_ex:
        out["exclude"] = clean_ex

    devs = data.get("devices") or {}
    if not isinstance(devs, dict):
        raise ValueError("devices must be a mapping")
    clean_devs: dict[str, Any] = {}
    for eid, dev in devs.items():
        if not isinstance(dev, dict):
            raise ValueError(f"devices.{eid} must be a mapping")
        cd: dict[str, Any] = {}
        aliases = dev.get("aliases")
        if aliases is not None:
            if not isinstance(aliases, list):
                raise ValueError(f"devices.{eid}.aliases must be a list")
            al = [str(x).strip() for x in aliases if str(x).strip()]
            if al:
                cd["aliases"] = al
        if dev.get("note"):
            cd["note"] = str(dev["note"]).strip()
        if dev.get("in_group") is False:
            cd["in_group"] = False
        if dev.get("hidden"):
            cd["hidden"] = True
        if cd:
            clean_devs[str(eid)] = cd
    if clean_devs:
        out["devices"] = clean_devs

    rooms = data.get("rooms") or {}
    if not isinstance(rooms, dict):
        raise ValueError("rooms must be a mapping")
    clean_rooms: dict[str, Any] = {}
    for area, rv in rooms.items():
        if not isinstance(rv, dict):
            raise ValueError(f"rooms.{area} must be a mapping")
        defaults = rv.get("defaults") or {}
        if not isinstance(defaults, dict):
            raise ValueError(f"rooms.{area}.defaults must be a mapping")
        cd_defaults: dict[str, list[str]] = {}
        for type_key, lst in defaults.items():
            if not isinstance(lst, list):
                raise ValueError(f"rooms.{area}.defaults.{type_key} must be a list")
            vals = [str(x).strip() for x in lst if str(x).strip()]
            if vals:
                cd_defaults[str(type_key)] = vals
        if cd_defaults:
            clean_rooms[str(area)] = {"defaults": cd_defaults}
    if clean_rooms:
        out["rooms"] = clean_rooms

    # lists: the shopping/to-do voice layer — which todo entity is "the list",
    # plus spoken aliases per list ("the groceries" → todo.shopping_list).
    ls = data.get("lists") or {}
    if not isinstance(ls, dict):
        raise ValueError("lists must be a mapping")
    unknown_ls = set(ls) - {"default", "aliases"}
    if unknown_ls:
        raise ValueError(f"unknown lists keys: {sorted(unknown_ls)}")
    clean_lists: dict[str, Any] = {}
    default_list = str(ls.get("default") or "").strip()
    if default_list:
        clean_lists["default"] = default_list
    la = ls.get("aliases") or {}
    if not isinstance(la, dict):
        raise ValueError("lists.aliases must be a mapping")
    clean_la: dict[str, list[str]] = {}
    for eid, al in la.items():
        if not isinstance(al, list):
            raise ValueError(f"lists.aliases.{eid} must be a list")
        vals = [str(x).strip() for x in al if str(x).strip()]
        if vals:
            clean_la[str(eid).strip()] = vals
    if clean_la:
        clean_lists["aliases"] = clean_la
    if clean_lists:
        out["lists"] = clean_lists

    # occupancy (v5): which entities count as EVIDENCE that a room holds a
    # person. Deliberately separate from `exclude` — voice-control exclusion and
    # evidence exclusion are different judgments (a sensor can be hidden from
    # voice and still be good evidence, or voice-visible and terrible evidence,
    # like the one aimed through a window at the street). "Pets exist": the
    # pet-tripped PIR is the reason this is configurable at all.
    occ = data.get("occupancy") or {}
    if not isinstance(occ, dict):
        raise ValueError("occupancy must be a mapping")
    unknown_occ = set(occ) - {"exclude", "include"}
    if unknown_occ:
        raise ValueError(f"unknown occupancy keys: {sorted(unknown_occ)}")
    clean_occ: dict[str, list[str]] = {}
    for key in ("exclude", "include"):
        val = occ.get(key)
        if val is None:
            continue
        if not isinstance(val, list):
            raise ValueError(f"occupancy.{key} must be a list")
        items = [str(x).strip() for x in val if str(x).strip()]
        if items:
            clean_occ[key] = items
    if clean_occ:
        out["occupancy"] = clean_occ

    return out


def save_curation(data: dict[str, Any]) -> None:
    """Validate and write curation.yaml, then drop the cached topology."""
    cleaned = validate_curation(data)
    import yaml as _yaml

    path = _curation_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Home Assistant curation — managed by the Kenzy dashboard.\n"
        "# Aliases, notes, room defaults, and voice-control exclusions; keys are\n"
        "# HA entity_ids. See docs/skills/home-assistant.md.\n\n"
    )
    path.write_text(header + _yaml.safe_dump(cleaned, sort_keys=False, default_flow_style=False))
    reset_cache()


# ---------------------------------------------------------------------------
# Fetch + build
# ---------------------------------------------------------------------------


async def fetch_todo_lists() -> list[dict[str, str]]:
    """The `todo` entities HA currently has (the available lists), name + id.

    Read from ``/api/states`` (todo entities need no area/floor placement, so
    the heavier template pull isn't needed). Used by the lists skill and by the
    dashboard's curation editor for its default-list picker.
    """
    base, headers = ha_conn()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{base}/api/states", headers=headers)
        resp.raise_for_status()
    out: list[dict[str, str]] = []
    for st in resp.json():
        eid = str(st.get("entity_id", ""))
        if eid.startswith("todo."):
            attrs = st.get("attributes") or {}
            name = str(attrs.get("friendly_name") or eid.split(".", 1)[1].replace("_", " "))
            out.append({"entity_id": eid, "name": name})
    return sorted(out, key=lambda x: x["entity_id"])


async def fetch_persons() -> list[dict[str, str]]:
    """The ``person`` entities HA currently has, name + id — the People page's
    "HA person" dropdown (F3): mapping a household member to their HA login
    picks from real entities instead of typing one."""
    base, headers = ha_conn()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{base}/api/states", headers=headers)
        resp.raise_for_status()
    out: list[dict[str, str]] = []
    for st in resp.json():
        eid = str(st.get("entity_id", ""))
        if eid.startswith("person."):
            attrs = st.get("attributes") or {}
            name = str(attrs.get("friendly_name") or eid.split(".", 1)[1].replace("_", " "))
            out.append({"entity_id": eid, "name": name})
    return sorted(out, key=lambda x: x["entity_id"])


async def fetch_raw() -> list[dict[str, Any]]:
    """Render the topology template through HA and return the raw entity rows.

    The template only sees entities placed in an area, so entities of the
    name-first domains (scenes/scripts/buttons/input_booleans, which usually
    have no area) are merged in from ``/api/states`` as unplaced rows.
    """
    base, headers = ha_conn()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{base}/api/template", headers=headers, json={"template": _TEMPLATE}
        )
        resp.raise_for_status()
        parsed = json.loads(resp.text)
        if not isinstance(parsed, list):
            raise ValueError("HA template did not return a list")

        merge_domains = set(_domains()) & NAME_FIRST_DOMAINS
        if merge_domains:
            states = await client.get(f"{base}/api/states", headers=headers)
            states.raise_for_status()
            parsed = merge_unplaced(parsed, states.json(), merge_domains)

        # Music Assistant ownership (play-by-name targets). Registry-based and
        # rename-proof; a failure (older HA, no MA) degrades to an empty set.
        try:
            ma_resp = await client.post(
                f"{base}/api/template", headers=headers, json={"template": _MA_TEMPLATE}
            )
            ma_resp.raise_for_status()
            ma_ids = set(json.loads(ma_resp.text))
        except Exception:
            ma_ids = set()
        if ma_ids:
            for row in parsed:
                if str(row.get("entity_id", "")) in ma_ids:
                    row["ma"] = True
    return parsed


def merge_unplaced(
    rows: list[dict[str, Any]], states: list[dict[str, Any]], domains: set[str]
) -> list[dict[str, Any]]:
    """Append area-less entities of the given domains as unplaced rows.

    Also drops diagnostic-class buttons (identify/restart/update) everywhere —
    including placed ones that came through the template — since the template
    can't see ``device_class`` but ``/api/states`` can.
    """
    diagnostic: set[str] = set()
    for st in states:
        entity_id = str(st.get("entity_id", ""))
        if entity_id.split(".", 1)[0] in ("button", "input_button"):
            attrs = st.get("attributes") or {}
            if attrs.get("device_class") in _DIAGNOSTIC_BUTTON_CLASSES:
                diagnostic.add(entity_id)

    seen = {str(r.get("entity_id", "")) for r in rows}
    out = [r for r in rows if str(r.get("entity_id", "")) not in diagnostic]
    for st in states:
        entity_id = str(st.get("entity_id", ""))
        if "." not in entity_id or entity_id in seen or entity_id in diagnostic:
            continue
        if entity_id.split(".", 1)[0] not in domains:
            continue
        attrs = st.get("attributes") or {}
        out.append(
            {
                "entity_id": entity_id,
                "name": attrs.get("friendly_name"),
                "area": None,
                "floor": None,
            }
        )
    return out


def build_model(raw: list[dict[str, Any]], curation: dict[str, Any]) -> HAModel:
    """Normalize raw template rows into a filtered, placed :class:`HAModel`."""
    domains = _domains()
    entities: list[Entity] = []
    seen: set[str] = set()

    for row in raw:
        entity_id = str(row.get("entity_id", "")).strip()
        if not entity_id or "." not in entity_id or entity_id in seen:
            continue
        domain = entity_id.split(".", 1)[0]
        if domain not in domains:
            continue

        area_name = str(row.get("area") or "")
        if _excluded(entity_id, domain, _slug(area_name), curation):
            continue

        floor_name = str(row.get("floor") or "")
        friendly = row.get("name")
        name = str(friendly) if friendly else _name_from_id(entity_id)

        seen.add(entity_id)
        entities.append(
            Entity(
                entity_id=entity_id,
                domain=domain,
                name=name,
                area=_slug(area_name),
                area_name=area_name,
                floor=_slug(floor_name) or _NO_FLOOR,
                floor_name=floor_name or _NO_FLOOR.title(),
                ma=bool(row.get("ma")),
            )
        )

    return HAModel(entities=entities, fetched_at=time.time())


def _name_from_id(entity_id: str) -> str:
    """Fallback spoken name from an entity_id (``light.office_lamp`` -> ``office lamp``)."""
    return entity_id.split(".", 1)[1].replace("_", " ")


@dataclass(frozen=True)
class ClassifiedEntity:
    """A controllable entity tagged with its voice-control inclusion status."""

    entity_id: str
    domain: str
    name: str
    area_name: str
    floor_name: str
    included: bool
    reason: str  # the excluding curation rule, or "" when included


def classify(raw: list[dict[str, Any]], curation: dict[str, Any]) -> list[ClassifiedEntity]:
    """Tag every controllable-domain entity as included/excluded (for diagnostics).

    Unlike :func:`build_model`, excluded entities are *kept* here and flagged with
    the reason, so the discovery CLI can show the whole picture.
    """
    domains = _domains()
    out: list[ClassifiedEntity] = []
    seen: set[str] = set()

    for row in raw:
        entity_id = str(row.get("entity_id", "")).strip()
        if not entity_id or "." not in entity_id or entity_id in seen:
            continue
        domain = entity_id.split(".", 1)[0]
        if domain not in domains:
            continue
        seen.add(entity_id)

        area_name = str(row.get("area") or "")
        floor_name = str(row.get("floor") or _NO_FLOOR.title())
        friendly = row.get("name")
        name = str(friendly) if friendly else _name_from_id(entity_id)
        reason = _exclude_reason(entity_id, domain, _slug(area_name), curation)

        out.append(
            ClassifiedEntity(
                entity_id=entity_id,
                domain=domain,
                name=name,
                area_name=area_name or "(no area)",
                floor_name=floor_name,
                included=not reason,
                reason=reason,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_MODEL: HAModel | None = None
_LOCK = asyncio.Lock()


def _cache_ttl() -> float:
    try:
        return float(get_config("home_assistant", "cache_ttl", 300.0))
    except (TypeError, ValueError):
        return 300.0


async def get_model(force: bool = False) -> HAModel:
    """Return the cached topology, refreshing from HA when stale or forced.

    Raises on fetch failure only when there is no cached snapshot to fall back
    on; otherwise a stale snapshot is returned and the error is logged.
    """
    global _MODEL
    fresh = _MODEL is not None and (time.time() - _MODEL.fetched_at) < _cache_ttl()
    if _MODEL is not None and fresh and not force:
        return _MODEL

    async with _LOCK:
        # Another waiter may have refreshed while we blocked on the lock.
        fresh = _MODEL is not None and (time.time() - _MODEL.fetched_at) < _cache_ttl()
        if _MODEL is not None and fresh and not force:
            return _MODEL
        try:
            raw = await fetch_raw()
            _MODEL = build_model(raw, load_curation())
            log.info("HA topology refreshed: %d entities", len(_MODEL.entities))
        except Exception as exc:
            if _MODEL is not None:
                log.warning("HA topology refresh failed (%s); using stale cache", exc)
                return _MODEL
            raise
    return _MODEL


def reset_cache() -> None:
    """Test hook: drop the cached topology snapshot."""
    global _MODEL
    _MODEL = None


# ---------------------------------------------------------------------------
# Occupancy evidence map (v5 spine, Slice A)
# ---------------------------------------------------------------------------
#
# The server owns the HA event socket and the occupancy tracker, but it has no
# area knowledge — that lives here. So it fetches this MAP (slow, small) and
# consumes the event stream itself (fast, large). The map is the only thing
# that crosses the service boundary; see v5-aware-era.md "Scoping the spine".
#
# NOTE this cannot reuse build_model(): the voice topology covers _DEFAULT_DOMAINS,
# which contains neither `binary_sensor` nor `person` — there is not a single
# presence entity in it. Hence a dedicated template.

#: device_class → evidence kind. PULSE spikes and decays immediately (a PIR
#: fires on motion and goes quiet while you sit still); LEVEL asserts
#: continuously and must NOT drain while it still says "occupied" (mmWave
#: still-presence, and person home/away).
_OCCUPANCY_CLASSES: dict[str, str] = {
    "motion": "pulse",
    "moving": "pulse",
    "occupancy": "level",
    "presence": "level",
}

_OCCUPANCY_TEMPLATE = (
    "{% set ns = namespace(items=[]) %}"
    "{% for a in areas() %}"
    "{% for e in area_entities(a) %}"
    "{% if e.startswith('binary_sensor.') %}"
    "{% set ns.items = ns.items + [{"
    "'entity_id': e,"
    "'name': state_attr(e, 'friendly_name'),"
    "'area': area_name(a),"
    "'device_class': state_attr(e, 'device_class')"
    "}] %}"
    "{% endif %}"
    "{% endfor %}"
    "{% endfor %}"
    "{{ ns.items | tojson }}"
)


def build_occupancy_map(
    rows: list[dict[str, Any]],
    persons: list[dict[str, Any]],
    curation: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """entity_id → {room, room_name, kind, scope} for occupancy-relevant entities.

    Pure (no I/O) so it is exhaustively testable. Room-scope evidence comes from
    area-placed ``binary_sensor``s with a presence-ish device_class; house-scope
    evidence is ``person.*`` (home/away has no room). Curation's ``occupancy``
    block adds entities the class heuristic missed (``include``) and removes
    ones that lie (``exclude`` — the cat-crossed hallway, the window-facing PIR).
    """
    occ = curation.get("occupancy") or {}
    excluded = {str(x).strip() for x in (occ.get("exclude") or [])}
    included = {str(x).strip() for x in (occ.get("include") or [])}

    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        eid = str(row.get("entity_id") or "").strip()
        if not eid or eid in excluded or _is_kenzy_entity(eid):
            continue
        dev_class = str(row.get("device_class") or "").strip().lower()
        kind = _OCCUPANCY_CLASSES.get(dev_class)
        if kind is None:
            # Not a presence class — only an explicit include rescues it, and a
            # hand-picked sensor is treated as a pulse (the conservative choice:
            # it spikes and decays rather than pinning a room "occupied").
            if eid not in included:
                continue
            kind = "pulse"
        area_name = str(row.get("area") or "").strip()
        if not area_name:
            continue  # unplaced sensor: no room to credit
        out[eid] = {
            "room": _slug(area_name),
            "room_name": area_name,
            "kind": kind,
            "scope": "room",
        }

    for person in persons:
        eid = str(person.get("entity_id") or "").strip()
        if not eid.startswith("person.") or eid in excluded or _is_kenzy_entity(eid):
            continue
        out[eid] = {
            "room": "",
            "room_name": "",
            "kind": "level",  # home/away asserts continuously
            "scope": "house",
            "name": str(person.get("name") or _name_from_id(eid)),
        }
    return out


async def fetch_occupancy_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch the raw presence candidates from HA: (binary_sensor rows, persons).

    Shared by the evidence map (what the server consumes) and the curation
    editor's candidate list (what the operator toggles), so the two can never
    disagree about which entities exist.
    """
    base, headers = ha_conn()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{base}/api/template", headers=headers, json={"template": _OCCUPANCY_TEMPLATE}
        )
        resp.raise_for_status()
        rows = json.loads(resp.text)
        if not isinstance(rows, list):
            raise ValueError("HA occupancy template did not return a list")

        states = await client.get(f"{base}/api/states", headers=headers)
        states.raise_for_status()
        persons = [
            {
                "entity_id": s.get("entity_id"),
                "name": (s.get("attributes") or {}).get("friendly_name"),
            }
            for s in states.json()
            if str(s.get("entity_id", "")).startswith("person.")
        ]
    return rows, persons


async def fetch_occupancy_map() -> dict[str, dict[str, Any]]:
    """The live occupancy evidence map (what the server's event filter uses)."""
    rows, persons = await fetch_occupancy_rows()
    return build_occupancy_map(rows, persons, load_curation())


@dataclass(frozen=True)
class OccupancyCandidate:
    """A presence-capable entity, tagged with whether it counts as evidence.

    The editor needs the ones that DON'T count as much as the ones that do —
    excluding a lying sensor (the cat-crossed hallway PIR) and opting in an
    unusual one both start from seeing the full list.
    """

    entity_id: str
    name: str
    area_name: str
    device_class: str
    kind: str  # "pulse" / "level" / "" when unused
    scope: str  # "room" / "house"
    used: bool
    reason: str  # why it is not used, or "" when it is
    #: Whether it WOULD be used with no curation rules at all. The editor stores
    #: only divergence from this, so the file stays small and a later change to
    #: the automatic behavior still reaches anyone who never touched the sensor.
    auto: bool = False


def classify_occupancy(
    rows: list[dict[str, Any]],
    persons: list[dict[str, Any]],
    curation: dict[str, Any],
) -> list[OccupancyCandidate]:
    """Every presence candidate + its status. Pure; mirrors ``classify``."""
    used_map = build_occupancy_map(rows, persons, curation)
    auto_map = build_occupancy_map(rows, persons, {})  # the no-curation baseline
    occ = curation.get("occupancy") or {}
    excluded = {str(x).strip() for x in (occ.get("exclude") or [])}

    out: list[OccupancyCandidate] = []
    for row in rows:
        eid = str(row.get("entity_id") or "").strip()
        if not eid or _is_kenzy_entity(eid):
            continue  # her own echo is never a candidate, so never a choice
        area_name = str(row.get("area") or "")
        dev_class = str(row.get("device_class") or "").strip().lower()
        entry = used_map.get(eid)
        if entry is not None:
            reason = ""
        elif eid in excluded:
            reason = "excluded"
        elif not area_name:
            reason = "no room"
        elif dev_class not in _OCCUPANCY_CLASSES:
            reason = "not a presence sensor"
        else:
            reason = "excluded"
        out.append(
            OccupancyCandidate(
                entity_id=eid,
                name=str(row.get("name") or _name_from_id(eid)),
                area_name=area_name,
                device_class=dev_class,
                kind=str(entry.get("kind")) if entry else "",
                scope="room",
                used=entry is not None,
                reason=reason,
                auto=eid in auto_map,
            )
        )
    for person in persons:
        eid = str(person.get("entity_id") or "").strip()
        if not eid.startswith("person.") or _is_kenzy_entity(eid):
            continue
        entry = used_map.get(eid)
        out.append(
            OccupancyCandidate(
                entity_id=eid,
                name=str(person.get("name") or _name_from_id(eid)),
                area_name="",
                device_class="person",
                kind=str(entry.get("kind")) if entry else "",
                scope="house",
                used=entry is not None,
                reason="" if entry else "excluded",
                auto=eid in auto_map,
            )
        )
    return sorted(out, key=lambda c: (c.scope == "house", c.area_name, c.name))


async def fetch_occupancy_candidates() -> list[OccupancyCandidate]:
    """The curation editor's presence list (all candidates, tagged)."""
    rows, persons = await fetch_occupancy_rows()
    return classify_occupancy(rows, persons, load_curation())
