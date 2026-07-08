"""Weather output is speech-friendly: no "°F" (TTS reads it "degrees F"), no
bare "mph", and NWS's terse shortForecast gets the implied "of". Regression for
the reported "71 degrees F" / "Chance Showers And Thunderstorms" quirks."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from kenzy.llm import skills as reg

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def wx():
    reg.set_config({})
    path = ROOT / "src" / "kenzy" / "llm" / "builtin_skills" / "weather.py"
    spec = importlib.util.spec_from_file_location("weather", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["weather"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            "Chance Showers And Thunderstorms then Partly Cloudy",
            "Chance of Showers and Thunderstorms then Partly Cloudy",
        ),
        ("Slight Chance Rain Showers", "Slight Chance of Rain Showers"),
        ("Partly Cloudy", "Partly Cloudy"),  # unchanged
        ("Mostly Sunny", "Mostly Sunny"),
        ("Showers And Thunderstorms Likely", "Showers and Thunderstorms Likely"),
    ],
)
def test_spoken_conditions(wx, raw, expected):
    assert wx._spoken_conditions(raw) == expected


class _Resp:
    def __init__(self, data: Any):
        self._data = data

    def raise_for_status(self) -> None:
        pass

    def json(self) -> Any:
        return self._data


class _Client:
    def __init__(self, payload: Any):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, url: str, **k: Any) -> _Resp:
        return _Resp(self._payload)


async def test_forecast_output_is_speakable(wx, monkeypatch):
    async def _urls(lat, lon):
        return {"forecast": "http://x/forecast", "station_id": "KX"}

    async def _resolve(loc):
        return 36.09, -79.43, "Burlington, NC"

    monkeypatch.setattr(wx, "_get_nws_urls", _urls)
    monkeypatch.setattr(wx, "_resolve", _resolve)
    payload = {
        "properties": {
            "periods": [
                {
                    "name": "Tonight",
                    "shortForecast": "Chance Showers And Thunderstorms then Partly Cloudy",
                    "temperature": 71,
                    "temperatureUnit": "F",
                }
            ]
        }
    }
    monkeypatch.setattr(wx.httpx, "AsyncClient", lambda **k: _Client(payload))

    out = await wx.get_weather_forecast(periods=1)
    assert "Chance of Showers and Thunderstorms then Partly Cloudy" in out
    assert "71 degrees" in out
    assert "°" not in out  # would be spoken "degrees F"
    assert "71F" not in out and "°F" not in out


async def test_current_output_has_no_degree_symbol(wx, monkeypatch):
    async def _urls(lat, lon):
        return {"forecast": "http://x/forecast", "station_id": "KX"}

    async def _resolve(loc):
        return 36.09, -79.43, "Burlington, NC"

    monkeypatch.setattr(wx, "_get_nws_urls", _urls)
    monkeypatch.setattr(wx, "_resolve", _resolve)
    payload = {
        "properties": {
            "textDescription": "Partly Cloudy",
            "temperature": {"value": 21.7},  # °C from NWS
            "relativeHumidity": {"value": 55.0},
            "windSpeed": {"value": 3.6},
        }
    }
    monkeypatch.setattr(wx.httpx, "AsyncClient", lambda **k: _Client(payload))

    out = await wx.get_current_weather()
    assert "degrees" in out and "°" not in out
    assert "miles per hour" in out and " mph" not in out
