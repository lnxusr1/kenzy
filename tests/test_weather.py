"""Weather home-location resolution: derives from the shared `location` block
(city/state the source of truth; lat/lon an optional geocode shortcut)."""

from __future__ import annotations

from kenzy.llm import skills as sk
from kenzy.llm.builtin_skills import weather


async def test_resolve_uses_latlon_when_set(monkeypatch):
    sk.set_config(
        {"location": {"city": "Burlington", "state": "NC", "latitude": 36.1, "longitude": -79.4}}
    )
    called: list[str] = []

    async def fake_geocode(loc):  # noqa: ANN001, ANN202
        called.append(loc)
        return (0.0, 0.0)

    monkeypatch.setattr(weather, "_geocode", fake_geocode)
    try:
        assert await weather._resolve(None) == (36.1, -79.4, "Burlington, NC")
        assert called == []  # coordinates present → no geocoding
    finally:
        sk.set_config({})


async def test_resolve_geocodes_city_state_without_latlon(monkeypatch):
    sk.set_config({"location": {"city": "Burlington", "state": "NC"}})
    called: list[str] = []

    async def fake_geocode(loc):  # noqa: ANN001, ANN202
        called.append(loc)
        return (35.0, -79.0)

    monkeypatch.setattr(weather, "_geocode", fake_geocode)
    try:
        assert await weather._resolve(None) == (35.0, -79.0, "Burlington, NC")
        assert called == ["Burlington, NC"]  # default derived from the location block
    finally:
        sk.set_config({})


async def test_resolve_none_when_unconfigured(monkeypatch):
    sk.set_config({})
    monkeypatch.setattr(weather, "_geocode", lambda loc: None)  # should not be reached
    try:
        assert await weather._resolve(None) is None
    finally:
        sk.set_config({})
