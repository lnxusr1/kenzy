# Built-in Skills

## Weather — `skills/weather.py`

Provides current conditions and multi-day forecasts using the [National Weather Service API](https://www.weather.gov/documentation/services-web-api) (US only, no API key required). Geocoding uses Nominatim (OpenStreetMap).

### Skills

| Function | Description |
|---|---|
| `get_current_weather(location)` | Current temperature, conditions, humidity, wind |
| `get_forecast(location, days)` | Multi-day forecast; `days` defaults to 3 |

### Configuration (`skills.weather` in `llm.yaml`)

| Key | Default | Description |
|---|---|---|
| `default_location` | *(from `location` block)* | Used when the user does not specify a location |
| `units` | `"imperial"` | `imperial` (°F) or `metric` (°C) |

---

## News — `skills/news.py`

Fetches headlines and article summaries from configurable RSS feeds. Articles are extracted with [trafilatura](https://trafilatura.readthedocs.io) and summarized by a sub-LLM call.

### Skills

| Function | Description |
|---|---|
| `get_news(category)` | Returns a numbered list of headlines for the given category |
| `get_news_article(category, article_number)` | Fetches and summarizes a specific article by its position in the list |

### Configuration (`skills.news` in `llm.yaml`)

| Key | Default | Description |
|---|---|---|
| `max_headlines` | `5` | Maximum headlines returned per request |
| `model` | `"gpt-4o"` | Model used for article summarization |
| `base_url` | — | Base URL for local model providers |
| `feeds` | *(see below)* | Map of category name → RSS feed URL |

Default feeds:

```yaml
feeds:
  latest:   "https://moxie.foxnews.com/google-publisher/latest.xml"
  world:    "https://moxie.foxnews.com/google-publisher/world.xml"
  politics: "https://moxie.foxnews.com/google-publisher/politics.xml"
  local:    "https://myfox8.com/feed/"
```

Add or replace any category by editing the `feeds` map. Any RSS 2.0 or Atom feed works.

---

## Stocks — `skills/stocks.py`

Returns stock quotes using [yfinance](https://github.com/ranaroussi/yfinance).

### Skills

| Function | Description |
|---|---|
| `get_stock_info(tickers)` | Price, day range, 52-week range, and percentage change for one or more symbols |

### Example response

```
Apple Inc. (AAPL)
  Price: 213.45 USD  +5.32 (+2.56%)  [REGULAR]
  Day range: 208.10 – 214.20
  52-week range: 164.08 – 237.23
  200-day avg: 198.45
```

---

## Home Assistant — `skills/home_assistant.py`

Controls and queries smart home devices via the [Home Assistant REST API](https://developers.home-assistant.io/docs/api/rest/). See [Home Assistant](home-assistant.md) for full setup documentation.

### Skills

| Function | Description |
|---|---|
| `handle_home_control(request, speaker)` | Natural language control of lights, fans, locks, covers, and thermostats |

### Configuration (`skills.home_assistant` in `llm.yaml`)

| Key | Default | Description |
|---|---|---|
| `url` | `"http://homeassistant.local:8123"` | Home Assistant base URL |
| `model` | `"gpt-4o"` | Model used for device resolution |
| `base_url` | — | Base URL for local model providers |
| `device_ids_yaml` | `"data/home_assistant/device_ids.yaml"` | Device hierarchy (human-readable aliases) |
| `device_ids_json` | `"data/home_assistant/device_ids.json"` | Alias → HA entity ID mapping |
| `default_room` | `""` | Room assumed when the user does not specify one |

**Requires:** `HA_API_KEY` in `.env`

---

## Random Tools — `skills/random_tools.py`

Utility skills for randomness and selection.

| Function | Description |
|---|---|
| `flip_coin()` | Returns `"heads"` or `"tails"` |
| `roll_dice(sides, count)` | Rolls one or more dice; `sides` defaults to 6 |
| `pick_number(min, max)` | Random integer in the given range |
| `pick_from_list(items)` | Picks one item from a list |
| `yes_no_maybe()` | Returns `"yes"`, `"no"`, or `"maybe"` |

!!! note
    `yes_no_maybe` and `pick_from_list` include explicit docstring clauses that instruct the LLM not to use them for factual or deterministic questions.

---

## About — `skills/about.py`

| Function | Description |
|---|---|
| `get_assistant_version()` | Returns the installed Kenzy package version |
