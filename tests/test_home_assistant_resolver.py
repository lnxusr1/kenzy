"""Tests for the deterministic Home Assistant resolver (parsing + resolution).

These exercise the offline resolution logic against the real device_ids files.
The Home Assistant network boundary (_apply_devices) is mocked.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

from kenzy.llm import skills as reg

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def ha(monkeypatch):
    """Load the bundled home_assistant skill by path with the index from real files."""
    # Resolve paths relative to project root regardless of cwd.
    reg.set_config(
        {
            "home_assistant": {
                "device_ids_yaml": str(ROOT / "data/home_assistant/device_ids.yaml"),
                "device_ids_json": str(ROOT / "data/home_assistant/device_ids.json"),
                "device_overlay": str(ROOT / "data/home_assistant/device_overlay.yaml"),
            }
        }
    )
    path = ROOT / "src" / "kenzy" / "llm" / "builtin_skills" / "home_assistant.py"
    spec = importlib.util.spec_from_file_location("home_assistant", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["home_assistant"] = mod
    spec.loader.exec_module(mod)
    mod._reset_cache()
    # _load_device_files joins get_config paths onto the project root; the config
    # above already gives absolute paths, so build the index directly from files.
    yaml_text = (ROOT / "data/home_assistant/device_ids.yaml").read_text()
    device_map = json.loads((ROOT / "data/home_assistant/device_ids.json").read_text())
    overlay_path = ROOT / "data/home_assistant/device_overlay.yaml"
    overlay = yaml.safe_load(overlay_path.read_text()) if overlay_path.exists() else {}
    mod._INDEX = mod._index_from(yaml_text, device_map, overlay or {})
    mod._RESOLVER_TEXT = yaml_text

    # These tests exercise the offline resolution engine against the static
    # files, so bypass the live-HA topology pull entirely.
    async def _noop_view():
        return None

    monkeypatch.setattr(mod, "_ensure_view", _noop_view)
    return mod


def _intent(ha, utterance):
    return ha._get_intents().calc_intent(utterance)


# ---------------------------------------------------------------------------
# Intent parsing (padacioso)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utterance,name",
    [
        ("turn on the office lights", "turn_on"),
        ("please turn off the kitchen lights", "turn_off"),
        ("switch on the floor lamp", "turn_on"),
        ("toggle the office fan", "toggle"),
        ("lock the front door", "lock"),
        ("unlock the front door", "unlock"),
        ("open the garage door", "open_cover"),
        ("close the garage door", "close_cover"),
        ("set the thermostat to 72 degrees", "set_temperature"),
    ],
)
def test_intent_parsing(ha, utterance, name):
    assert _intent(ha, utterance)["name"] == name


def test_non_command_is_not_parsed(ha):
    assert _intent(ha, "what time is it")["name"] is None


# ---------------------------------------------------------------------------
# Group resolution + on/off asymmetry
# ---------------------------------------------------------------------------


def test_turn_on_lights_uses_room_default(ha):
    # living_room default = [lr_floor_lamp, lr_decorative_table_lamp]
    codes = ha._resolve_target(ha._get_index(), "turn_on", "lights", "living_room")
    assert set(codes) == {"lr_floor_lamp", "lr_decorative_table_lamp"}


def test_turn_off_lights_means_all(ha):
    codes = ha._resolve_target(ha._get_index(), "turn_off", "lights", "living_room")
    # All living_room lights, a superset of the default subset.
    assert "lr_recessed_ceiling_lights" in codes
    assert "lr_floor_lamp" in codes
    assert len(codes) > 2


def test_turn_on_all_lights_overrides_default(ha):
    codes = ha._resolve_target(ha._get_index(), "turn_on", "all the lights", "living_room")
    assert "lr_recessed_ceiling_lights" in codes
    assert len(codes) > 2


def test_explicit_room_in_phrase_overrides_origin(ha):
    codes = ha._resolve_target(ha._get_index(), "turn_off", "kitchen lights", "living_room")
    assert all(c.startswith("kt_") for c in codes)


def test_unlock_bare_group_defers(ha):
    # "unlock the doors" must not act on a group — defer to the LLM.
    assert ha._resolve_target(ha._get_index(), "unlock", "doors", "foyer") is None


def test_lock_bare_group_allowed(ha):
    codes = ha._resolve_target(ha._get_index(), "lock", "doors", "foyer")
    assert codes == ["fy_front_door"]


# ---------------------------------------------------------------------------
# Specific device resolution (rapidfuzz)
# ---------------------------------------------------------------------------


def test_specific_device_fuzzy_in_room(ha):
    codes = ha._resolve_target(ha._get_index(), "turn_on", "floor lamp", "office")
    assert codes == ["of_floor_lamps"]


def test_unknown_device_defers(ha):
    assert ha._resolve_target(ha._get_index(), "turn_on", "disco ball", "office") is None


def test_explicit_room_in_phrase_scopes_specific_device(ha):
    # Regression: "the lamps in the living room" from the office node must act on
    # the LIVING ROOM lamps, never wander to the office's similarly-named device.
    codes = ha._resolve_target(ha._get_index(), "turn_on", "lamps in the living room", "office")
    assert set(codes) == {"lr_floor_lamp", "lr_decorative_table_lamp"}


def test_plural_stem_group_in_origin_room(ha):
    # "lamps" (no room named) → all lamp-named lights in the origin room.
    codes = ha._resolve_target(ha._get_index(), "turn_on", "lamps", "living_room")
    assert set(codes) == {"lr_floor_lamp", "lr_decorative_table_lamp"}


def test_explicit_room_no_match_does_not_wander(ha):
    # Named room + no in-room match → defer (None), not a house-wide guess.
    assert (
        ha._resolve_target(ha._get_index(), "turn_on", "disco ball in the kitchen", "office")
        is None
    )


def test_cross_room_device_without_named_room_uses_house_wide(ha):
    # No room named → house-wide fallback is allowed (device lives elsewhere).
    codes = ha._resolve_target(ha._get_index(), "turn_on", "christmas tree", "master_bedroom")
    assert codes == ["lr_christmas_tree_light"]


# ---------------------------------------------------------------------------
# Overlay (aliases + exclude)
# ---------------------------------------------------------------------------


def test_overlay_bare_singular_alias(ha):
    # "lamp" in the bedroom resolves to the chair lamp via the overlay.
    codes = ha._resolve_target(ha._get_index(), "turn_on", "lamp", "master_bedroom")
    assert codes == ["mb_chair_lamp"]


def test_overlay_synonym_alias(ha):
    codes = ha._resolve_target(ha._get_index(), "turn_on", "nightstand lamp", "master_bedroom")
    assert codes == ["mb_nicki_nightstand_lamp"]


def test_overlay_exclude_drops_from_group(ha):
    # kt_sink_light is excluded from bare-group commands.
    codes = ha._resolve_target(ha._get_index(), "turn_off", "lights", "kitchen")
    assert "kt_sink_light" not in codes
    assert "kt_island_pendant_lights" in codes


def test_excluded_device_still_addressable_directly(ha):
    # Excluded only from groups — naming it directly still works.
    codes = ha._resolve_target(ha._get_index(), "turn_on", "sink light", "kitchen")
    assert codes == ["kt_sink_light"]


def test_turn_off_all_overrides_in_group_exclude(ha):
    # "turn off ALL the lights" means literally all — the in_group:false floor
    # (kt_sink_light) is bypassed for an explicit deactivate-all.
    codes = ha._resolve_target(ha._get_index(), "turn_off", "all the lights", "kitchen")
    assert "kt_sink_light" in codes


def test_turn_on_all_still_honors_in_group_exclude(ha):
    # "turn on all the lights" still respects in_group:false — a name-only light
    # shouldn't blaze on with a bare "all".
    codes = ha._resolve_target(ha._get_index(), "turn_on", "all the lights", "kitchen")
    assert "kt_sink_light" not in codes


# ---------------------------------------------------------------------------
# Climate
# ---------------------------------------------------------------------------


def test_climate_resolves_room_thermostat(ha):
    codes = ha._resolve_climate(ha._get_index(), "thermostat", "living_room")
    assert codes == ["lr_thermostat"]


# ---------------------------------------------------------------------------
# End-to-end fast intent (HA boundary mocked)
# ---------------------------------------------------------------------------


async def test_fast_intent_turn_on_handles(ha, monkeypatch):
    applied = {}

    async def fake_apply(devices, device_map):
        applied["devices"] = devices
        return []

    monkeypatch.setattr(ha, "_apply_devices", fake_apply)
    result = await ha.fast_home_control("turn off the kitchen lights", "living_room", "john")
    assert result.is_handled
    assert applied["devices"]
    assert all(d["action"] == "turn_off" for d in applied["devices"])
    assert all(d["id"].startswith("kt_") for d in applied["devices"])


async def test_fast_intent_unlock_blocked_for_unknown(ha, monkeypatch):
    called = False

    async def fake_apply(devices, device_map):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(ha, "_apply_devices", fake_apply)
    result = await ha.fast_home_control("unlock the front door", "foyer", "unknown")
    assert result.is_handled
    assert "don't recognize" in result.text
    assert called is False  # never reached HA


async def test_fast_intent_misses_status_query(ha):
    # Status queries aren't in the fast grammar — must defer to the LLM.
    result = await ha.fast_home_control("is the office fan on", "office", "john")
    assert result.status == "miss"


async def test_fast_intent_misses_non_home_request(ha):
    result = await ha.fast_home_control("tell me a joke", "office", "john")
    assert result.status == "miss"


# ---------------------------------------------------------------------------
# Confirmation phrasing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,target,origin,expected",
    [
        # Room rendered first, never "...the lamps in living room". Past tense:
        # the device is already changed before the confirmation is spoken.
        ("turn_off", "lamps in the living room", "office", "Turned off the living room lamps."),
        ("turn_off", "kitchen lights", "office", "Turned off the kitchen lights."),
        ("turn_on", "all the lights", "living_room", "Turned on the lights."),
        ("turn_off", "lights", "living_room", "Turned off the lights."),
        ("turn_on", "lamp", "master_bedroom", "Turned on the chair lamp."),
    ],
)
def test_confirmation_phrasing(ha, action, target, origin, expected):
    idx = ha._get_index()
    codes = ha._resolve_target(idx, action, target, origin)
    assert ha._confirm(action, idx, codes, target) == expected
