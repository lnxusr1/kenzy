"""Tests for the live Home Assistant topology model + its projection.

Covers ha_model.build_model (domain filter, curation excludes, floor/area
normalization) and the home_assistant skill's live-path builders
(_index_from_model / _resolver_text) plus an end-to-end fast intent driven by a
mocked HA topology. The HA network boundary is never touched.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from kenzy.llm import skills as reg
from kenzy.llm.builtin_skills import ha_model
from kenzy.llm.builtin_skills.ha_model import Entity, HAModel

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# ha_model.build_model
# ---------------------------------------------------------------------------


def test_build_model_filters_and_normalizes():
    raw = [
        {
            "entity_id": "light.lr_main",
            "name": "Main",
            "area": "Living Room",
            "floor": "Downstairs",
        },
        {"entity_id": "sensor.temp", "name": "Temp", "area": "Living Room", "floor": "Downstairs"},
        {
            "entity_id": "light.office_plug_led",
            "name": "LED",
            "area": "Office",
            "floor": "Downstairs",
        },
        {"entity_id": "light.garage_relay", "name": "Relay", "area": "Garage", "floor": None},
        {"entity_id": "fan.bp_fan", "name": None, "area": "Back Porch", "floor": None},
    ]
    curation = {
        "exclude": {"patterns": ["light.*_plug_led"]},
        "devices": {"light.garage_relay": {"hidden": True}},
    }
    model = ha_model.build_model(raw, curation)
    ids = {e.entity_id for e in model.entities}
    # sensor dropped (domain), plug LED dropped (pattern), relay dropped (hidden)
    assert ids == {"light.lr_main", "fan.bp_fan"}

    fan = model.by_id()["fan.bp_fan"]
    assert fan.name == "bp fan"  # derived from id (no friendly_name)
    assert fan.area == "back_porch"  # slugified
    assert fan.floor == "home"  # floorless fallback


def test_build_model_exclude_by_domain_and_area():
    raw = [
        {"entity_id": "switch.x", "name": "X", "area": "Shop", "floor": None},
        {"entity_id": "light.y", "name": "Y", "area": "Shop", "floor": None},
        {"entity_id": "light.z", "name": "Z", "area": "Den", "floor": None},
    ]
    curation = {"exclude": {"domains": ["switch"], "areas": ["Shop"]}}
    model = ha_model.build_model(raw, curation)
    assert {e.entity_id for e in model.entities} == {"light.z"}


# ---------------------------------------------------------------------------
# Curation validation + save
# ---------------------------------------------------------------------------


def test_validate_curation_normalizes_and_drops_empties():
    raw = {
        "exclude": {"patterns": ["light.*_led", " "], "domains": [], "entities": ["light.x"]},
        "devices": {
            "light.a": {
                "aliases": ["lamp", " "],
                "note": " hi ",
                "in_group": False,
                "hidden": False,
            },
            "light.b": {"aliases": []},  # empty -> dropped
        },
        "rooms": {"Living Room": {"defaults": {"lights": ["light.a"], "fans": []}}},
    }
    out = ha_model.validate_curation(raw)
    assert out["exclude"] == {"patterns": ["light.*_led"], "entities": ["light.x"]}
    assert out["devices"] == {"light.a": {"aliases": ["lamp"], "note": "hi", "in_group": False}}
    assert "light.b" not in out["devices"]
    assert out["rooms"] == {"Living Room": {"defaults": {"lights": ["light.a"]}}}


@pytest.mark.parametrize(
    "bad",
    [
        [],  # not a mapping
        {"bogus": 1},  # unknown top-level key
        {"exclude": {"patterns": "x"}},  # exclude list is a string
        {"devices": {"light.a": "x"}},  # device entry not a mapping
        {"rooms": {"den": {"defaults": {"lights": "x"}}}},  # defaults list is a string
    ],
)
def test_validate_curation_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        ha_model.validate_curation(bad)


def test_save_curation_roundtrip(tmp_path):
    path = tmp_path / "curation.yaml"
    reg.set_config({"home_assistant": {"curation_file": str(path)}})
    ha_model.reset_cache()
    ha_model.save_curation(
        {"devices": {"light.a": {"aliases": ["lamp"], "note": "x"}}, "exclude": {"domains": []}}
    )
    assert path.exists()
    loaded = ha_model.load_curation()
    assert loaded == {
        "devices": {"light.a": {"aliases": ["lamp"], "note": "x"}}
    }  # empty exclude dropped


# ---------------------------------------------------------------------------
# Skill live-path builders
# ---------------------------------------------------------------------------


@pytest.fixture
def ha(monkeypatch):
    """Load the skill by path and feed it a synthetic live HA model + curation."""
    reg.set_config({"home_assistant": {}})
    path = ROOT / "src" / "kenzy" / "llm" / "builtin_skills" / "home_assistant.py"
    spec = importlib.util.spec_from_file_location("home_assistant", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["home_assistant"] = mod
    spec.loader.exec_module(mod)
    mod._reset_cache()

    entities = [
        Entity(
            "light.lr_floor_lamp",
            "light",
            "Floor Lamp",
            "living_room",
            "Living Room",
            "downstairs",
            "Downstairs",
        ),
        Entity(
            "light.lr_ceiling",
            "light",
            "Living Room Ceiling",
            "living_room",
            "Living Room",
            "downstairs",
            "Downstairs",
        ),
        Entity(
            "light.lr_accent",
            "light",
            "Accent Light",
            "living_room",
            "Living Room",
            "downstairs",
            "Downstairs",
        ),
        Entity(
            "fan.lr_fan",
            "fan",
            "Ceiling Fan",
            "living_room",
            "Living Room",
            "downstairs",
            "Downstairs",
        ),
        Entity(
            "climate.lr_thermo",
            "climate",
            "Thermostat",
            "living_room",
            "Living Room",
            "downstairs",
            "Downstairs",
        ),
        Entity(
            "lock.front_door", "lock", "Front Door", "foyer", "Foyer", "downstairs", "Downstairs"
        ),
        Entity("vacuum.rosie", "vacuum", "Rosie", "living_room", "Living Room",
               "downstairs", "Downstairs"),
        Entity("media_player.living_room_tv", "media_player", "Living Room TV",
               "living_room", "Living Room", "downstairs", "Downstairs"),
        Entity("media_player.den_tv", "media_player", "Den TV",
               "den", "Den", "downstairs", "Downstairs"),
        Entity("media_player.den_speaker", "media_player", "Den Speaker",
               "den", "Den", "downstairs", "Downstairs"),
        # Name-first Tier 1 entities: no area (merged from /api/states in prod).
        Entity("scene.movie_night", "scene", "Movie Night", "", "", "home", "Home"),
        Entity("script.goodnight", "script", "Goodnight", "", "", "home", "Home"),
        Entity("button.coffee_maker", "button", "Coffee Maker", "", "", "home", "Home"),
        Entity("input_boolean.guest_mode", "input_boolean", "Guest Mode", "", "", "home", "Home"),
    ]
    model = HAModel(entities=entities, fetched_at=123.0)
    curation = {
        "devices": {
            "light.lr_floor_lamp": {
                "aliases": ["lamp"],
                "note": "black light on the table beside the chair",
            },
            "light.lr_accent": {"in_group": False},
        },
        "rooms": {"living_room": {"defaults": {"lights": ["light.lr_floor_lamp"]}}},
    }

    async def fake_get_model(force: bool = False):
        return model

    monkeypatch.setattr(mod.ha_model, "get_model", fake_get_model)
    monkeypatch.setattr(mod.ha_model, "load_curation", lambda: curation)
    return mod


async def _view(ha):
    await ha._ensure_view()
    return ha._get_index()


async def test_index_from_model_structure(ha):
    idx = await _view(ha)
    assert set(idx.rooms["living_room"]["lights"]) == {
        "light.lr_floor_lamp",
        "light.lr_ceiling",
        "light.lr_accent",
    }
    assert idx.spoken["light.lr_floor_lamp"] == "Floor Lamp"
    assert idx.device_map["light.lr_floor_lamp"] == "light.lr_floor_lamp"  # identity
    assert "light.lr_accent" in idx.exclude  # in_group: false
    assert idx.defaults["living_room"]["lights"] == ["light.lr_floor_lamp"]
    assert idx.aliases[("living_room", "lamp")] == ["light.lr_floor_lamp"]


async def test_alias_resolves_to_entity_id(ha):
    idx = await _view(ha)
    assert ha._resolve_target(idx, "turn_on", "lamp", "living_room") == ["light.lr_floor_lamp"]


async def test_activate_uses_room_default(ha):
    idx = await _view(ha)
    assert ha._resolve_target(idx, "turn_on", "lights", "living_room") == ["light.lr_floor_lamp"]


async def test_deactivate_means_all_minus_excluded(ha):
    idx = await _view(ha)
    codes = ha._resolve_target(idx, "turn_off", "lights", "living_room")
    assert set(codes) == {"light.lr_floor_lamp", "light.lr_ceiling"}  # accent excluded from group


async def test_excluded_device_still_addressable(ha):
    idx = await _view(ha)
    assert ha._resolve_target(idx, "turn_on", "accent light", "living_room") == ["light.lr_accent"]


async def test_climate_resolves(ha):
    idx = await _view(ha)
    assert ha._resolve_climate(idx, "thermostat", "living_room") == ["climate.lr_thermo"]


async def test_lock_bare_group(ha):
    idx = await _view(ha)
    assert ha._resolve_target(idx, "lock", "doors", "foyer") == ["lock.front_door"]


async def test_resolver_text_has_tree_and_notes(ha):
    await ha._ensure_view()
    text = ha._get_resolver_text()
    assert "downstairs:" in text
    assert "  living_room:" in text
    assert "    lights:" in text
    assert "- light.lr_floor_lamp" in text
    assert "black light on the table beside the chair" in text


async def test_fast_intent_over_live_model(ha, monkeypatch):
    applied = {}

    async def fake_apply(devices, device_map):
        applied["devices"] = devices
        return []

    monkeypatch.setattr(ha, "_apply_devices", fake_apply)
    result = await ha.fast_home_control("turn off the living room lights", "living_room", "john")
    assert result.is_handled
    ids = {d["id"] for d in applied["devices"]}
    assert ids == {"light.lr_floor_lamp", "light.lr_ceiling"}  # accent excluded
    assert all(d["action"] == "turn_off" for d in applied["devices"])


# ---------------------------------------------------------------------------
# Tier 1 name-first domains (scene / script / button / input_boolean)
# ---------------------------------------------------------------------------


def test_merge_unplaced_appends_only_name_first_domains():
    rows = [{"entity_id": "scene.placed", "name": "Placed", "area": "Den", "floor": None}]
    states = [
        {"entity_id": "scene.placed", "attributes": {"friendly_name": "Placed"}},
        {"entity_id": "scene.movie_night", "attributes": {"friendly_name": "Movie Night"}},
        {"entity_id": "light.orphan", "attributes": {"friendly_name": "Orphan"}},
        {"entity_id": "sensor.junk", "attributes": {}},
        {"entity_id": "nodot"},
    ]
    merged = ha_model.merge_unplaced(rows, states, {"scene", "script"})
    ids = [r["entity_id"] for r in merged]
    # placed scene not duplicated; unplaced light stays invisible (placed-only contract)
    assert ids == ["scene.placed", "scene.movie_night"]
    assert merged[1]["area"] is None and merged[1]["name"] == "Movie Night"


def test_kenzy_own_entities_are_never_voice_targets():
    raw = [
        {"entity_id": "switch.kenzy_office_mute", "name": "Kenzy Office Mute", "area": "Office"},
        {"entity_id": "button.kenzy_office_trigger", "name": "Kenzy Office Trigger", "area": "Office"},
        {"entity_id": "light.office_lamp", "name": "Office Lamp", "area": "Office"},
    ]
    model = ha_model.build_model(raw, {})
    assert [e.entity_id for e in model.entities] == ["light.office_lamp"]
    tagged = {c.entity_id: c for c in ha_model.classify(raw, {})}
    assert tagged["switch.kenzy_office_mute"].reason == "kenzy internal"
    assert not tagged["switch.kenzy_office_mute"].included


def test_merge_unplaced_drops_diagnostic_buttons():
    # A placed Identify button (came through the template) AND an unplaced one:
    # both must vanish; a real button (no diagnostic device_class) survives.
    rows = [
        {"entity_id": "button.sensor_identify", "name": "Sensor Identify", "area": "Garage"},
        {"entity_id": "light.garage", "name": "Garage Light", "area": "Garage"},
    ]
    states = [
        {"entity_id": "button.sensor_identify", "attributes": {"device_class": "identify"}},
        {"entity_id": "button.plug_restart", "attributes": {"device_class": "restart"}},
        {"entity_id": "button.doorbell_chime", "attributes": {"friendly_name": "Doorbell Chime"}},
        {"entity_id": "input_button.good_morning", "attributes": {"friendly_name": "Good Morning"}},
    ]
    merged = ha_model.merge_unplaced(rows, states, {"button", "input_button"})
    ids = [r["entity_id"] for r in merged]
    assert ids == ["light.garage", "button.doorbell_chime", "input_button.good_morning"]


async def test_activate_scene_by_name(ha, monkeypatch):
    applied = {}

    async def fake_apply(devices, device_map):
        applied["devices"] = devices
        return []

    monkeypatch.setattr(ha, "_apply_devices", fake_apply)
    result = await ha.fast_home_control("activate movie night", "living_room", "john")
    assert result.is_handled
    assert applied["devices"] == [{"id": "scene.movie_night", "action": "turn_on"}]
    assert "Activated" in result.text and "Movie Night" in result.text


async def test_run_script_strips_trailing_qualifier(ha):
    idx = await _view(ha)
    assert ha._resolve_named(idx, "the goodnight routine", "office") == "script.goodnight"


async def test_press_button_uses_press_service(ha, monkeypatch):
    applied = {}

    async def fake_apply(devices, device_map):
        applied["devices"] = devices
        return []

    monkeypatch.setattr(ha, "_apply_devices", fake_apply)
    result = await ha.fast_home_control("press the coffee maker button", "kitchen", "john")
    assert result.is_handled
    assert applied["devices"] == [{"id": "button.coffee_maker", "action": "press"}]
    assert "Pressed" in result.text


async def test_toggle_helper_via_plain_turn_on(ha, monkeypatch):
    applied = {}

    async def fake_apply(devices, device_map):
        applied["devices"] = devices
        return []

    monkeypatch.setattr(ha, "_apply_devices", fake_apply)
    result = await ha.fast_home_control("turn on guest mode", "office", "john")
    assert result.is_handled
    assert applied["devices"] == [{"id": "input_boolean.guest_mode", "action": "turn_on"}]


async def test_scene_turn_off_defers_to_llm(ha):
    idx = await _view(ha)
    # resolution finds the scene, but the service guard must miss it
    assert ha._resolve_target(idx, "turn_off", "movie night", "office") == ["scene.movie_night"]
    result = await ha.fast_home_control("turn off movie night", "office", "john")
    assert not result.is_handled


async def test_name_first_never_joins_groups(ha):
    idx = await _view(ha)
    # "turn on the lights" in any room must never sweep up scenes/helpers
    codes = ha._resolve_target(idx, "turn_on", "lights", "living_room") or []
    assert all(not c.startswith(("scene.", "script.", "input_boolean.")) for c in codes)


async def test_resolver_text_places_name_first_under_home(ha):
    await ha._ensure_view()
    text = ha._get_resolver_text()
    assert "home:" in text
    assert "  unplaced:" in text
    assert "- scene.movie_night" in text
    assert "    toggles:" in text


# ---------------------------------------------------------------------------
# Vacuum (Tier 2, first slice)
# ---------------------------------------------------------------------------


async def test_start_the_vacuum_generic_word(ha, monkeypatch):
    applied = {}

    async def fake_apply(devices, device_map):
        applied["devices"] = devices
        return []

    monkeypatch.setattr(ha, "_apply_devices", fake_apply)
    result = await ha.fast_home_control("start the vacuum", "living_room", "john")
    assert result.is_handled
    assert applied["devices"] == [{"id": "vacuum.rosie", "action": "start"}]
    assert "Started" in result.text and "Rosie" in result.text


async def test_vacuum_generic_word_from_other_room_finds_only_vacuum(ha):
    idx = await _view(ha)
    # asked from the office: not the vacuum's room, but it's the only one in the house
    assert ha._resolve_named(idx, "the vacuum", "office") == "vacuum.rosie"


async def test_vacuum_generic_word_ambiguous_with_two(ha):
    idx = await _view(ha)
    idx.rooms.setdefault("den", {})["vacuums"] = ["vacuum.upstairs"]
    idx.device_map["vacuum.upstairs"] = "vacuum.upstairs"
    assert ha._resolve_named(idx, "the vacuum", "office") is None  # clarify via LLM


async def test_stop_the_vacuum(ha, monkeypatch):
    applied = {}

    async def fake_apply(devices, device_map):
        applied["devices"] = devices
        return []

    monkeypatch.setattr(ha, "_apply_devices", fake_apply)
    result = await ha.fast_home_control("stop the vacuum", "living_room", "john")
    assert result.is_handled
    assert applied["devices"] == [{"id": "vacuum.rosie", "action": "stop"}]


async def test_send_vacuum_home_by_name(ha, monkeypatch):
    applied = {}

    async def fake_apply(devices, device_map):
        applied["devices"] = devices
        return []

    monkeypatch.setattr(ha, "_apply_devices", fake_apply)
    result = await ha.fast_home_control("send rosie back to the dock", "office", "john")
    assert result.is_handled
    assert applied["devices"] == [{"id": "vacuum.rosie", "action": "return_to_base"}]
    assert "home" in result.text


async def test_turn_on_the_vacuum_translates_to_start(ha, monkeypatch):
    applied = {}

    async def fake_apply(devices, device_map):
        applied["devices"] = devices
        return []

    monkeypatch.setattr(ha, "_apply_devices", fake_apply)
    result = await ha.fast_home_control("turn on rosie", "office", "john")
    assert result.is_handled
    assert applied["devices"] == [{"id": "vacuum.rosie", "action": "start"}]


async def test_turn_on_the_vacuum_generic_word(ha, monkeypatch):
    applied = {}

    async def fake_apply(devices, device_map):
        applied["devices"] = devices
        return []

    monkeypatch.setattr(ha, "_apply_devices", fake_apply)
    result = await ha.fast_home_control("turn off the vacuum", "office", "john")
    assert result.is_handled
    assert applied["devices"] == [{"id": "vacuum.rosie", "action": "stop"}]


async def test_send_a_light_home_defers(ha):
    result = await ha.fast_home_control("send the floor lamp home", "living_room", "john")
    assert not result.is_handled


async def test_stop_a_lock_defers(ha):
    # no _STOP_SERVICES entry for lock — must not actuate anything
    result = await ha.fast_home_control("stop the front door", "foyer", "john")
    assert not result.is_handled


# ---------------------------------------------------------------------------
# media_player transport (Tier 2, second slice)
# ---------------------------------------------------------------------------


def _media_env(ha, monkeypatch, states):
    """Stub live state reads + capture actuations for media tests."""
    applied = {}

    async def fake_apply(devices, device_map):
        applied.setdefault("devices", []).extend(devices)
        return []

    async def fake_state(entity_id):
        return {"state": states.get(entity_id, "off")}

    monkeypatch.setattr(ha, "_apply_devices", fake_apply)
    monkeypatch.setattr(ha, "_ha_state", fake_state)
    return applied


async def test_pause_in_single_player_room_needs_no_state(ha, monkeypatch):
    applied = _media_env(ha, monkeypatch, {})  # all "off": lone room player still wins
    result = await ha.fast_home_control("pause the tv", "living_room", "john")
    assert result.is_handled
    assert applied["devices"] == [{"id": "media_player.living_room_tv", "action": "media_pause"}]


async def test_pause_widens_to_the_one_playing_house_wide(ha, monkeypatch):
    applied = _media_env(ha, monkeypatch, {"media_player.den_tv": "playing"})
    # asked from the office (no players there); den TV is the only thing playing
    result = await ha.fast_home_control("pause the music", "office", "john")
    assert result.is_handled
    assert applied["devices"] == [{"id": "media_player.den_tv", "action": "media_pause"}]


async def test_two_playing_players_defer_to_llm(ha, monkeypatch):
    _media_env(
        ha,
        monkeypatch,
        {"media_player.den_tv": "playing", "media_player.living_room_tv": "playing"},
    )
    result = await ha.fast_home_control("pause the music", "office", "john")
    assert not result.is_handled


async def test_multi_player_room_picks_by_state(ha, monkeypatch):
    applied = _media_env(ha, monkeypatch, {"media_player.den_speaker": "playing"})
    result = await ha.fast_home_control("turn the music down", "den", "john")
    assert result.is_handled
    assert applied["devices"] == [{"id": "media_player.den_speaker", "action": "volume_down"}]


async def test_explicit_room_targeting(ha, monkeypatch):
    applied = _media_env(ha, monkeypatch, {})
    result = await ha.fast_home_control("pause the music in the living room", "office", "john")
    assert result.is_handled
    assert applied["devices"] == [{"id": "media_player.living_room_tv", "action": "media_pause"}]


async def test_mute_the_tv_is_media_not_node(ha, monkeypatch):
    applied = _media_env(ha, monkeypatch, {})
    result = await ha.fast_home_control("mute the tv", "living_room", "john")
    assert result.is_handled
    assert applied["devices"] == [{"id": "media_player.living_room_tv", "action": "media_mute"}]


async def test_resume_prefers_paused_player(ha, monkeypatch):
    applied = _media_env(ha, monkeypatch, {"media_player.den_speaker": "paused"})
    result = await ha.fast_home_control("resume the music", "den", "john")
    assert result.is_handled
    assert applied["devices"] == [{"id": "media_player.den_speaker", "action": "media_play"}]


async def test_skip_track(ha, monkeypatch):
    applied = _media_env(ha, monkeypatch, {"media_player.den_speaker": "playing"})
    result = await ha.fast_home_control("skip this song", "den", "john")
    assert result.is_handled
    assert applied["devices"] == [
        {"id": "media_player.den_speaker", "action": "media_next_track"}
    ]


async def test_unavailable_lone_player_widens_instead_of_lying(ha, monkeypatch):
    applied = _media_env(
        ha,
        monkeypatch,
        {"media_player.living_room_tv": "unavailable", "media_player.den_tv": "playing"},
    )
    # living room's only player is dead; the den TV is the one thing playing
    result = await ha.fast_home_control("pause the tv", "living_room", "john")
    assert result.is_handled
    assert applied["devices"] == [{"id": "media_player.den_tv", "action": "media_pause"}]


async def test_volume_command_moves_multiple_notches(ha, monkeypatch):
    calls = []

    async def fake_service(entity_id, service, extra=None):
        calls.append((entity_id, service))

    async def fake_state(entity_id):
        return {"state": "playing"}

    monkeypatch.setattr(ha, "_ha_service", fake_service)
    monkeypatch.setattr(ha, "_ha_state", fake_state)
    result = await ha.fast_home_control("turn the tv up", "living_room", "john")
    assert result.is_handled
    # one spoken command = 3 device notches (HA's volume_up is one notch/call)
    assert calls == [("media_player.living_room_tv", "volume_up")] * 3


async def test_play_by_name_is_not_swallowed(ha, monkeypatch):
    # "play some jazz" must reach the LLM (which explains play-by-name isn't
    # here yet), not be misread as a transport verb.
    _media_env(ha, monkeypatch, {})
    result = await ha.fast_home_control("play some jazz", "den", "john")
    assert not result.is_handled


async def test_turn_off_the_tv_legacy_verb(ha, monkeypatch):
    applied = _media_env(ha, monkeypatch, {})
    result = await ha.fast_home_control("turn off the den tv", "den", "john")
    assert result.is_handled
    assert applied["devices"] == [{"id": "media_player.den_tv", "action": "turn_off"}]
