"""
Weather skill — current conditions and forecast via NOAA National Weather Service.

No API key required. US locations only.
Dynamic location queries (non-home) use the US Census Bureau geocoding API,
which is also free and requires no key.

The default (home) location comes from the top-level ``location:`` block in
llm.yaml — there is no separate weather location key. ``city``/``state`` are the
source of truth; ``latitude``/``longitude`` are optional (they skip a geocoding
step and are otherwise derived from the city/state).

Config in llm.yaml:
  location:
    city: "Burlington"
    state: "NC"
    latitude: 36.0957     # optional
    longitude: -79.4378   # optional
"""

from __future__ import annotations

import re

import httpx

from kenzy.llm.skills import FastResult, fast_intent, get_config, skill  # type: ignore[import]

_NWS_AGENT = "kenzy-home-assistant/1.0"

# Cache NWS grid URLs and nearest station per lat/lon so we don't hit
# the points endpoint on every request.
_url_cache: dict[str, dict[str, str]] = {}
# Cache geocoded coordinates per location string (incl. the home city/state) so a
# repeated query doesn't re-hit the geocoder.
_geocode_cache: dict[str, tuple[float, float]] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _geocode(location: str) -> tuple[float, float] | None:
    """Resolve a city/address string to (lat, lon) via Nominatim (OpenStreetMap)."""
    key = location.strip().lower()
    if key in _geocode_cache:
        return _geocode_cache[key]
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": _NWS_AGENT}) as client:
            resp = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": location, "format": "json", "limit": "1"},
            )
            resp.raise_for_status()
            results = resp.json()
        if not results:
            return None
        coords = (float(results[0]["lat"]), float(results[0]["lon"]))
        _geocode_cache[key] = coords
        return coords
    except Exception:
        return None


async def _get_nws_urls(lat: float, lon: float) -> dict[str, str]:
    """Return NWS forecast URLs and nearest observation station for lat/lon."""
    key = f"{lat:.4f},{lon:.4f}"
    if key in _url_cache:
        return _url_cache[key]

    headers = {"User-Agent": _NWS_AGENT}
    async with httpx.AsyncClient(timeout=15, headers=headers, follow_redirects=True) as client:
        r = await client.get(f"https://api.weather.gov/points/{lat},{lon}")
        r.raise_for_status()
        props = r.json()["properties"]

        sr = await client.get(props["observationStations"])
        sr.raise_for_status()
        station_id = sr.json()["features"][0]["properties"]["stationIdentifier"]

    result: dict[str, str] = {
        "forecast": props["forecast"],
        "station_id": station_id,
    }
    _url_cache[key] = result
    return result


async def _resolve(location: str | None) -> tuple[float, float, str] | None:
    """Return (lat, lon, display_label) for a location string or home default."""
    if not location:
        city = get_config("location", "city", "")
        state = get_config("location", "state", "")
        label = ", ".join(filter(None, [city, state])) or "home"
        lat = get_config("location", "latitude")
        lon = get_config("location", "longitude")
        if lat is not None and lon is not None:
            return float(lat), float(lon), label
        # No explicit coordinates — derive them from the home city/state, which is the
        # single source of truth (lat/lon are just an optional precision/speed override).
        home = ", ".join(filter(None, [city, state]))
        coords = await _geocode(home) if home else None
        if coords is None:
            return None
        return coords[0], coords[1], label

    coords = await _geocode(location)
    if coords is None:
        return None
    return coords[0], coords[1], location


def _spoken_conditions(text: str) -> str:
    """Make NWS's terse Title-Case ``shortForecast`` read naturally aloud.

    NWS returns strings like "Chance Showers And Thunderstorms then Partly
    Cloudy" — no "of", run-on "And". Insert the implied "of" after a leading
    probability word and lowercase the connector so TTS speaks a sentence, not
    a headline.
    """
    text = re.sub(r"\b(Slight Chance|Chance)\b(?! of\b)", r"\1 of", text)
    return text.replace(" And ", " and ")


def _c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


@skill
async def get_current_weather(location: str | None = None) -> str:
    """Get the current weather conditions at a location.

    Use this when the user asks about current weather, temperature, how warm or
    cold it is, or conditions right now.  Leave location empty to use the home
    location.  For non-home US locations pass a city and state.
    """
    resolved = await _resolve(location)
    if resolved is None:
        return "lat/lon not configured and location could not be geocoded."
    lat, lon, label = resolved

    try:
        urls = await _get_nws_urls(lat, lon)
        headers = {"User-Agent": _NWS_AGENT}
        async with httpx.AsyncClient(timeout=10, headers=headers, follow_redirects=True) as client:
            r = await client.get(
                f"https://api.weather.gov/stations/{urls['station_id']}/observations/latest"
            )
            r.raise_for_status()
            obs = r.json()["properties"]

        desc = obs.get("textDescription") or "conditions unknown"
        temp_c = (obs.get("temperature") or {}).get("value")
        humid = (obs.get("relativeHumidity") or {}).get("value")
        wind_ms = (obs.get("windSpeed") or {}).get("value")

        temp_str = f"{_c_to_f(temp_c):.0f} degrees" if temp_c is not None else "temperature unknown"
        parts = [f"{label}: {desc}, {temp_str}"]
        if humid is not None:
            parts.append(f"humidity {humid:.0f}%")
        if wind_ms is not None:
            parts.append(f"wind {wind_ms * 2.237:.0f} miles per hour")
        return ", ".join(parts) + "."

    except Exception as exc:
        return f"Could not get current weather for {label}: {exc}"


@skill
async def get_weather_forecast(location: str | None = None, periods: int = 4) -> str:
    """Get the upcoming weather forecast for a location.

    Use this when the user asks about future weather — tomorrow, this weekend,
    the week ahead, or whether to expect rain or sun.  Each period covers roughly
    half a day (daytime or overnight).  Leave location empty for home.
    """
    resolved = await _resolve(location)
    if resolved is None:
        return "lat/lon not configured and location could not be geocoded."
    lat, lon, label = resolved

    try:
        urls = await _get_nws_urls(lat, lon)
        headers = {"User-Agent": _NWS_AGENT}
        async with httpx.AsyncClient(timeout=10, headers=headers, follow_redirects=True) as client:
            r = await client.get(urls["forecast"])
            r.raise_for_status()
            all_periods = r.json()["properties"]["periods"]

        lines = [f"Forecast for {label}:"]
        for p in all_periods[: min(periods, len(all_periods))]:
            lines.append(
                f"{p['name']}: {_spoken_conditions(p['shortForecast'])}, {p['temperature']} degrees."
            )
        return "\n".join(lines)

    except Exception as exc:
        return f"Could not get forecast for {label}: {exc}"


# ---------------------------------------------------------------------------
# Fast DISPATCH — skips the LLM's tool-selection round-trip for the everyday
# phrasings and calls the right weather function directly. It still fetches
# (National Weather Service), so it's noticeably faster, not instant. Anchored
# whole-utterance: a NAMED location ("weather in Paris") or a reasoning question
# ("should I bring an umbrella") has a tail and falls through to the LLM, which
# can geocode / reason.
# ---------------------------------------------------------------------------

_FORECAST_RE = re.compile(
    r"^(?:"
    r"whats the (?:weather )?forecast"
    r"(?: for (?:today|the day|the week|tomorrow|the next few days|next week))?"
    r"|whats the weather (?:for )?(?:tomorrow|this week|next week|the next few days|the rest of the week)"
    r"|hows the (?:week|weather) looking"
    r"|whats the forecast look like"
    r")$"
)
_CURRENT_RE = re.compile(
    r"^(?:"
    r"whats the weather(?: like| outside| right now| now)?"
    r"|hows the weather(?: outside)?"
    r"|whats the temperature(?: outside| right now| now)?"
    r"|how (?:hot|cold|warm) is it(?: outside)?"
    r"|is it (?:hot|cold|warm)(?: outside)?"
    r")$"
)
_WEATHER_VOICE = "Speak naturally at a conversational pace."


@fast_intent(priority=48)
async def fast_weather(utterance: str, room_id: str | None, speaker: str | None) -> FastResult:
    """Everyday weather/forecast queries → the weather skill directly (no LLM
    tool-selection). Home location only; named places defer to the LLM."""
    text = re.sub(r"[^\w\s]", "", utterance).strip().lower()
    text = re.sub(r"\s+", " ", text)
    # STT renders the contraction either way ("what's" → whats, "what is") —
    # fold to the contracted form the patterns expect.
    text = re.sub(r"\bwhat is\b", "whats", text)
    text = re.sub(r"\bhow is\b", "hows", text)
    if _FORECAST_RE.match(text):
        return FastResult.handled(await get_weather_forecast(), _WEATHER_VOICE)
    if _CURRENT_RE.match(text):
        return FastResult.handled(await get_current_weather(), _WEATHER_VOICE)
    return FastResult.miss()
