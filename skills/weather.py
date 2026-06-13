"""
Weather skill — current conditions and forecast via NOAA National Weather Service.

No API key required. US locations only.
Dynamic location queries (non-home) use the US Census Bureau geocoding API,
which is also free and requires no key.

Config in llm.yaml:
  location:
    latitude: 36.0957
    longitude: -79.4378
    city: "Burlington"
    state: "NC"
"""

from __future__ import annotations

import httpx

from kenzy.llm.skills import get_config, skill  # type: ignore[import]

_NWS_AGENT = "kenzy-home-assistant/1.0"

# Cache NWS grid URLs and nearest station per lat/lon so we don't hit
# the points endpoint on every request.
_url_cache: dict[str, dict[str, str]] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _geocode(location: str) -> tuple[float, float] | None:
    """Resolve a city/address string to (lat, lon) via Nominatim (OpenStreetMap)."""
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
        return float(results[0]["lat"]), float(results[0]["lon"])
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
        "forecast":     props["forecast"],
        "station_id":   station_id,
    }
    _url_cache[key] = result
    return result


async def _resolve(location: str | None) -> tuple[float, float, str] | None:
    """Return (lat, lon, display_label) for a location string or home default."""
    if not location:
        lat = get_config("location", "latitude")
        lon = get_config("location", "longitude")
        if lat is None or lon is None:
            return None
        city  = get_config("location", "city", "")
        state = get_config("location", "state", "")
        label = ", ".join(filter(None, [city, state])) or "home"
        return float(lat), float(lon), label

    coords = await _geocode(location)
    if coords is None:
        return None
    return coords[0], coords[1], location


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

        desc     = obs.get("textDescription") or "conditions unknown"
        temp_c   = (obs.get("temperature") or {}).get("value")
        humid    = (obs.get("relativeHumidity") or {}).get("value")
        wind_ms  = (obs.get("windSpeed") or {}).get("value")

        temp_str = f"{_c_to_f(temp_c):.0f}°F" if temp_c is not None else "temp unknown"
        parts    = [f"{label}: {desc}, {temp_str}"]
        if humid is not None:
            parts.append(f"humidity {humid:.0f}%")
        if wind_ms is not None:
            parts.append(f"wind {wind_ms * 2.237:.0f} mph")
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
        for p in all_periods[:min(periods, len(all_periods))]:
            lines.append(
                f"  {p['name']}: {p['shortForecast']}, "
                f"{p['temperature']}°{p['temperatureUnit']}"
            )
        return "\n".join(lines)

    except Exception as exc:
        return f"Could not get forecast for {label}: {exc}"
