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
        {"entity_id": "light.lr_main", "name": "Main", "area": "Living Room", "floor": "Downstairs"},
        {"entity_id": "sensor.temp", "name": "Temp", "area": "Living Room", "floor": "Downstairs"},
        {"entity_id": "light.office_plug_led", "name": "LED", "area": "Office", "floor": "Downstairs"},
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
    assert fan.name == "bp fan"          # derived from id (no friendly_name)
    assert fan.area == "back_porch"      # slugified
    assert fan.floor == "home"           # floorless fallback


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
            "light.a": {"aliases": ["lamp", " "], "note": " hi ", "in_group": False, "hidden": False},
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
        [],                                    # not a mapping
        {"bogus": 1},                          # unknown top-level key
        {"exclude": {"patterns": "x"}},        # exclude list is a string
        {"devices": {"light.a": "x"}},         # device entry not a mapping
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
    assert loaded == {"devices": {"light.a": {"aliases": ["lamp"], "note": "x"}}}  # empty exclude dropped


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
        Entity("light.lr_floor_lamp", "light", "Floor Lamp", "living_room", "Living Room", "downstairs", "Downstairs"),
        Entity("light.lr_ceiling", "light", "Living Room Ceiling", "living_room", "Living Room", "downstairs", "Downstairs"),
        Entity("light.lr_accent", "light", "Accent Light", "living_room", "Living Room", "downstairs", "Downstairs"),
        Entity("fan.lr_fan", "fan", "Ceiling Fan", "living_room", "Living Room", "downstairs", "Downstairs"),
        Entity("climate.lr_thermo", "climate", "Thermostat", "living_room", "Living Room", "downstairs", "Downstairs"),
        Entity("lock.front_door", "lock", "Front Door", "foyer", "Foyer", "downstairs", "Downstairs"),
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
        "light.lr_floor_lamp", "light.lr_ceiling", "light.lr_accent"
    }
    assert idx.spoken["light.lr_floor_lamp"] == "Floor Lamp"
    assert idx.device_map["light.lr_floor_lamp"] == "light.lr_floor_lamp"  # identity
    assert "light.lr_accent" in idx.exclude                                # in_group: false
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
    assert ids == {"light.lr_floor_lamp", "light.lr_ceiling"}      # accent excluded
    assert all(d["action"] == "turn_off" for d in applied["devices"])
