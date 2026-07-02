# Built-in Skills

## Weather — `builtin_skills/weather.py`

Provides current conditions and multi-day forecasts using the [National Weather Service API](https://www.weather.gov/documentation/services-web-api) (US only, no API key required). Geocoding uses Nominatim (OpenStreetMap).

### Skills

| Function | Description |
|---|---|
| `get_current_weather(location)` | Current temperature, conditions, humidity, wind |
| `get_forecast(location, days)` | Multi-day forecast; `days` defaults to 3 |

### Configuration

The weather skill has **no per-skill keys**. When the user doesn't name a location it
uses the top-level **`location:`** block in `llm.yaml` (`city` + `state`; optional
`latitude`/`longitude` skip a geocoding step and are otherwise derived from city/state).
Output is in °F.

---

## News — `builtin_skills/news.py`

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

## Stocks — `builtin_skills/stocks.py`

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

## Web Search — `builtin_skills/web_search.py`

Lets Kenzy search the web for things it can't answer from the language model's own knowledge — recent events, prices, sports scores, opening hours, or any "look it up…" request. The skill returns the top results (title, snippet, source) to the model, which reads them and speaks a concise answer.

### Skills

| Function | Description |
|---|---|
| `web_search(query)` | Searches the web and returns a numbered list of results for the model to synthesize from |

### Configuration (`skills.web_search` in `llm.yaml`)

| Key | Default | Description |
|---|---|---|
| `provider` | `"duckduckgo"` | Search backend: `duckduckgo` or `searxng` |
| `max_results` | `5` | Results returned per search |
| `timeout` | `15` | Request timeout in seconds |
| `region` | `"wt-wt"` | DuckDuckGo region code (`wt-wt` = no region) |
| `searxng_url` | `"http://localhost:8888/search"` | Your SearXNG `/search` endpoint (searxng provider only) |

All of these are editable from the dashboard (**Services → llm**), with the provider as a dropdown.

**DuckDuckGo** (the default) is keyless and needs zero setup — it just works. If you'd rather no search queries leave your network, point the `searxng` provider at a self-hosted [SearXNG](https://docs.searxng.org/) instance; its JSON output format must be enabled (`search.formats: [html, json]` in SearXNG's `settings.yml`).

!!! note "Queries go out, either way something is searched"
    With DuckDuckGo, the search query (not the room audio) is sent to an external service. Use SearXNG if you self-host your search. Disable the skill entirely (`web_search` under `skills.disabled`, or the dashboard's Skills tab) if you don't want Kenzy searching the web at all.

---

## Home Assistant — `builtin_skills/home_assistant.py`

Controls and queries smart home devices via the [Home Assistant REST API](https://developers.home-assistant.io/docs/api/rest/). See [Home Assistant](home-assistant.md) for full setup documentation.

### Skills

| Function | Description |
|---|---|
| `handle_home_control(request, speaker)` | Natural language control of lights, fans, locks, covers, and thermostats |

### Configuration (`skills.home_assistant` in `llm.yaml`)

| Key | Default | Description |
|---|---|---|
| `url` | `"http://homeassistant.local:8123"` | Home Assistant base URL |
| `model` | `"gpt-4o"` | Model used by the fallback resolver |
| `base_url` | — | Base URL for local model providers |
| `curation_file` | `"data/home_assistant/curation.yaml"` | Aliases, notes, room defaults, and exclusions (optional) |
| `cache_ttl` | `300` | Seconds to cache the live HA topology pull |
| `domains` | `light, switch, fan, cover, lock, climate` | Entity domains exposed to voice control |
| `default_room` | `""` | Room assumed when the user does not specify one |

The device inventory is pulled live from Home Assistant — there are no device-map files to maintain. See [Home Assistant](home-assistant.md).

**Requires:** `HA_API_KEY` in `.env`

---

## Random Tools — `builtin_skills/random_tools.py`

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

## About — `builtin_skills/about.py`

| Function | Description |
|---|---|
| `get_assistant_version()` | Returns the installed Kenzy package version |

---

## Announce — `builtin_skills/announce.py`

Broadcasts a spoken message to other rooms. Say *"Hey Kenzy… tell everyone dinner's ready"* and Kenzy speaks it in every room, then confirms in the room you asked from.

| Function | Description |
|---|---|
| `announce(message, rooms="")` | Speak `message` aloud in other rooms; `rooms` is an optional comma-separated list of room names (empty = everywhere) |

This is the first user of the **server-actions** mechanism: the skill can't speak in other rooms itself (it runs in `kenzy-llm`), so it queues an action that `kenzy-server` actuates via its existing `announce()` (synthesize once, stream to the target nodes). The asking room is excluded from the broadcast so it doesn't hear the message twice. The server tells the model which room names are currently connected, so it targets real rooms.

---

## Intercom — `builtin_skills/intercom.py`

Starts a live two-way voice call to another room. Say *"call the living room"* and Kenzy rings that room; the call connects **only after someone there says "yes"** to accept it.

| Function | Description |
|---|---|
| `connect_room(room)` | Ring `room` for a live intercom call (the other room must verbally accept) |

Like `announce`, this queues a server action. The server rings the target room, plays a spoken consent prompt, and bridges audio **only on a clear spoken "yes"** (default-deny on silence/ambiguity/timeout). During an active call a wake word at either end ends it immediately. Requires a speakerphone with hardware echo cancellation.

---

## Date & Time — `builtin_skills/datetime_skill.py`

Answers "what time is it?" and "what's the date today?" **instantly** — these are handled by a deterministic fast intent with no language-model call, so the most common question of the day is also the fastest. Uses `location.timezone` from `llm.yaml`.

| Matcher | Description |
|---|---|
| `fast_datetime` (fast intent) | Time and date queries, answered locally with no LLM round-trip |

---

## Volume — `builtin_skills/volume.py`

Adjusts the volume of the room you're speaking from — also a deterministic fast intent, so it's instant.

Say things like: *"turn it up"*, *"quieter"*, *"set the volume to 40"*, *"volume at 75 percent"*, *"mute"*, *"unmute"*.

| Matcher | Description |
|---|---|
| `fast_volume` (fast intent) | up / down (±15), set to an exact level, mute, unmute — applied to the asking room's node |

The volume level **persists** (it's part of the node's server-owned config, also settable from the dashboard's slider). **Mute is temporary** — a muted node still plays its wake-word chime at a low level so you can tell it's listening, and comes back un-muted after a restart.

---

## Enroll Speaker — `builtin_skills/enroll.py`

Starts hands-free voice enrollment: *"hey Kenzie, enroll me as Alice"* — Kenzy reads a few sentences aloud and records your replies through the room's mic.

| Function | Description |
|---|---|
| `enroll_speaker(name)` | Requests a server-side enrollment session for `name` at the asking node |

**Off by default**: the server honors this skill only when `allow_voice_enroll` is enabled in the speaker service config — when it's on, *anyone within earshot can enroll*, including under an existing name. See the security discussion and the alternative enrollment paths (dashboard, CLI) in [Speaker Enrollment](../speaker-enrollment.md#enrolling-by-voice-from-a-node).
