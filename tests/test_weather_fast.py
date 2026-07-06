"""Fast DISPATCH for weather: everyday phrasings route straight to the weather
skill (skipping the LLM's tool-selection); named locations and reasoning
questions fall through to the LLM. Network is mocked — this tests ROUTING."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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

    async def _cur(location=None):
        return "CURRENT"

    async def _fc(location=None, periods=4):
        return "FORECAST"

    mod.get_current_weather = _cur
    mod.get_weather_forecast = _fc
    return mod


@pytest.mark.parametrize(
    "utterance",
    [
        "what's the weather",
        "what is the weather",
        "what's the weather outside",
        "what's the temperature outside",
        "how's the weather",
        "how hot is it outside",
        "is it cold outside",
    ],
)
async def test_current_routes_to_current(wx, utterance):
    r = await wx.fast_weather(utterance, "office", None)
    assert r.is_handled and r.text == "CURRENT"


@pytest.mark.parametrize(
    "utterance",
    [
        "what's the forecast",
        "what's the weather forecast for the week",
        "how's the week looking",
        "what's the weather for tomorrow",
        "what's the weather for the next few days",
    ],
)
async def test_forecast_routes_to_forecast(wx, utterance):
    r = await wx.fast_weather(utterance, "office", None)
    assert r.is_handled and r.text == "FORECAST"


@pytest.mark.parametrize(
    "utterance",
    [
        "what's the weather in Paris",  # named location → LLM (can geocode)
        "should I bring an umbrella today",  # reasoning → LLM
        "will it rain on my wedding day",
        "what's the weather on Mars and why",
        "set the weather station to metric",
    ],
)
async def test_defers_named_locations_and_reasoning(wx, utterance):
    assert (await wx.fast_weather(utterance, "office", None)).status == "miss"
