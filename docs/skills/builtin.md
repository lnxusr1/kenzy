# Built-in Skills

## About

**File:** `builtin_skills/about.py`

| Function | Description |
|---|---|
| `get_assistant_version()` | Returns the installed Kenzy package version |

---

## Announce

**File:** `builtin_skills/announce.py`

Broadcasts a spoken message to other rooms. Say *"Hey Kenzy… tell everyone dinner's ready"* and Kenzy speaks it in every room, then confirms in the room you asked from.

| Function | Description |
|---|---|
| `announce(message, rooms="")` | Speak `message` aloud in other rooms; `rooms` is an optional comma-separated list of room names (empty = everywhere) |

This is the first user of the **server-actions** mechanism: the skill can't speak in other rooms itself (it runs in `kenzy-llm`), so it queues an action that `kenzy-server` actuates via its existing `announce()` (synthesize once, stream to the target nodes). The asking room is excluded from the broadcast so it doesn't hear the message twice. The server tells the model which room names are currently connected, so it targets real rooms.

---

## Date & Time

**File:** `builtin_skills/datetime_skill.py`

Answers "what time is it?" and "what's the date today?" **instantly** — these are handled by a deterministic fast intent with no language-model call, so the most common question of the day is also the fastest. Uses `location.timezone` from `llm.yaml`.

| Matcher | Description |
|---|---|
| `fast_datetime` (fast intent) | Time and date queries, answered locally with no LLM round-trip |

---

## Enroll Speaker

**File:** `builtin_skills/enroll.py`

Starts hands-free voice enrollment: *"hey Kenzie, enroll me as Alice"* — Kenzy reads a few sentences aloud and records your replies through the room's mic.

| Function | Description |
|---|---|
| `enroll_speaker(name)` | Requests a server-side enrollment session for `name` at the asking node |

**Off by default**: the server honors this skill only when `allow_voice_enroll` is enabled in the speaker service config — when it's on, *anyone within earshot can enroll*, including under an existing name. See the security discussion and the alternative enrollment paths (dashboard, CLI) in [Speaker Enrollment](../speaker-enrollment.md#enrolling-by-voice-from-a-node).

---

## Home Assistant

**File:** `builtin_skills/home_assistant.py`

Controls and queries smart home devices via the [Home Assistant REST API](https://developers.home-assistant.io/docs/api/rest/). See [Home Assistant](home-assistant.md) for full setup documentation.

### Skills

| Function | Description |
|---|---|
| `handle_home_control(request, speaker)` | Natural language control of lights, fans, locks, covers, and thermostats |

### Configuration

Set under `skills.home_assistant` in `llm.yaml`:

| Key | Default | Description |
|---|---|---|
| `url` | `"http://homeassistant.local:8123"` | Home Assistant base URL |
| `model` | `"gpt-4o"` | Model used by the fallback resolver |
| `base_url` | — | Base URL for local model providers |
| `curation_file` | `"data/home_assistant/curation.yaml"` | Aliases, notes, room defaults, and exclusions (optional) |
| `cache_ttl` | `300` | Seconds to cache the live HA topology pull |
| `domains` | `light, switch, fan, cover, lock, climate` | Entity domains exposed to voice control |
| `default_room` | `""` | Room assumed when the user does not specify one |
| `thermo_min` / `thermo_max` | `65` / `85` | Comfort clamp (°F) for relative "make it warmer/cooler" thermostat adjustments |

The device inventory is pulled live from Home Assistant — there are no device-map files to maintain. See [Home Assistant](home-assistant.md).

**Requires:** `HA_API_KEY` in `.env`

---

## Intercom

**File:** `builtin_skills/intercom.py`

Starts a live two-way voice call to another room. Say *"call the living room"* and Kenzy rings that room; the call connects **only after someone there says "yes"** to accept it.

| Function | Description |
|---|---|
| `connect_room(room)` | Ring `room` for a live intercom call (the other room must verbally accept) |

Like `announce`, this queues a server action. The server rings the target room, plays a spoken consent prompt, and bridges audio **only on a clear spoken "yes"** (default-deny on silence/ambiguity/timeout). During an active call a wake word at either end ends it immediately. Requires a speakerphone with hardware echo cancellation at **both** ends — a room whose node is marked [`hardware_aec: false`](../configuration/node.md#rooms-without-echo-cancellation-hardware_aec-false) can't join a call (two-way audio without AEC is a feedback loop), and Kenzy politely says so instead of connecting.

---

## Lists

**File:** `builtin_skills/lists.py`

Shopping and to-do lists by voice — *"add milk to the shopping list"*, *"what's on the list?"*, *"check off eggs"*, *"take bread off the list"*.

The lists themselves live in **Home Assistant** (`todo` entities), not in Kenzy — deliberately: HA's companion app puts the list on everyone's phone (the half of a shopping list that matters at the store), and the same interface covers HA's local lists and synced backends (Google Tasks, Todoist, CalDAV, …) alike. Kenzy is the voice layer.

**Setup:** you barely need any — with **no list yet**, saying "add eggs to the shopping list" makes Kenzy ask *"Should I create one called Shopping list?"*; on your spoken **yes** it creates a Local to-do list in HA and adds your items (nothing is ever created without the confirmation). If HA won't allow the creation, Kenzy falls back to the manual instruction: *Settings → Devices & Services → Add Integration → **Local to-do***. You can also ask directly — *"create a camping list"*. Then, optionally, in the Kenzy dashboard's **Home Assistant tab → Lists**, pick which list a bare "the list" means and add spoken aliases ("the groceries"). With exactly one list it's the default automatically; with several and no default set, Kenzy asks which one — and keeps the mic open for your answer.

The common phrasings are a deterministic fast intent (instant, no model call): adding (*"add bread, jam and coffee…"* adds three items), reading, removing (*"take X off the list"* — deletes the item), and checking off (*"check off X"*, *"mark X as done"* — stays visible as completed in HA). Item names match case-insensitively. Fuzzier requests (*"add everything I need for pancakes"*) fall through to the LLM tools:

| Function | Description |
|---|---|
| `add_to_list(items, list_name)` | Add one or more items; empty `list_name` = the default list |
| `read_list(list_name)` | Read the open items |
| `remove_from_list(items, list_name)` | Delete items from the list |
| `complete_list_items(items, list_name)` | Mark items done (kept as completed in HA) |
| `create_list(name, items)` | Create a new Local to-do list (only on explicit request/confirmation) |
| `fast_lists` (fast intent) | The instant tier for all the phrasings above |

**Requires:** `HA_API_KEY` in `.env` and the Home Assistant skill's `url` (the lists share its HA connection). Everything here is hard-gated on that connection: with no `HA_API_KEY`, or with the `home_assistant` skill disabled in the Skills tab, list commands stay out of the way entirely.

---

## News

**File:** `builtin_skills/news.py`

Fetches headlines and article summaries from configurable RSS feeds. Articles are extracted with [trafilatura](https://trafilatura.readthedocs.io) and summarized by a sub-LLM call.

### Skills

| Function | Description |
|---|---|
| `get_news(category)` | Returns a numbered list of headlines for the given category |
| `get_news_article(category, article_number)` | Fetches and summarizes a specific article by its position in the list |

### Configuration

Set under `skills.news` in `llm.yaml`:

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

## Random Tools

**File:** `builtin_skills/random_tools.py`

The common bare forms ("flip a coin", "roll a d20", "roll 3d6", "pick a number between 1 and 10") are handled by a fast intent (`fast_random`) with no model call. Anything with a tail that needs reasoning ("flip a coin to decide whether I should…") falls through to the LLM.

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

## Social

**File:** `builtin_skills/social.py`

Instant greetings and a conversational bail-out — fast intents, no model call, no network (they work offline).

| Matcher | Description |
|---|---|
| `fast_greeting` (fast intent) | "hello", "good morning/afternoon/evening", "goodnight", "howdy", "what's up" → a warm, varied reply (time-specific greetings echo the right part of day) |
| `fast_nevermind` (fast intent) | "never mind" / "forget it" → "Okay, no problem" — and ends a held multi-turn dialog cleanly |

Both match the **whole utterance** only, so a greeting with a request attached ("hello, turn on the lights") or a list edit ("forget the eggs on the list") falls through to the right skill. "Thanks/thank you" is intentionally left to the LLM — speech-to-text hallucinates those from background noise, so a fast "you're welcome" would fire on phantom transcriptions.

---

## Stocks

**File:** `builtin_skills/stocks.py`

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

## Timers, Alarms & Reminders

**File:** `builtin_skills/schedule.py`

Set timers, alarms, and spoken reminders by voice. Entries are stored **on the server** (`data/schedules.json`) so they survive a restart, and fire as spoken announcements in the room that set them — or another room you name ("wake me at 7 **in the bedroom**"). Everything is visible and cancellable in the dashboard's **Scheduled** tab.

One hardware caveat: an alarm's ring loop is silenced by the wake word — heard *over the ringing* — so **alarms need an echo-cancelling speaker**. Setting an alarm for a room marked [`hardware_aec: false`](../configuration/node.md#rooms-without-echo-cancellation-hardware_aec-false) is refused with an offer of a timer or reminder instead; an alarm that already exists for such a room still fires, once, timer-style. Timers and reminders work in every room.

Say things like:

- *"set a timer for 10 minutes"* · *"start a pizza timer for an hour and a half"*
- *"how much time is left?"* · *"cancel the timer"* · *"what timers do I have?"*
- *"wake me at 7"* · *"set an alarm for 6:30 pm every weekday"*
- *"remind me in 20 minutes to flip the bread"* · *"remind me at 6:15 to take out the trash"*
- *"turn on the porch light in 30 seconds"* · *"lock the front door at 10:30 pm"* — **any command can be deferred**: it replays at fire time exactly as if you'd just said it, in the same room

The common phrasings are handled by a deterministic fast intent — instant, no model call. Fuzzier requests ("remind me tomorrow evening…", ambiguous times) fall through to the LLM tools below. A **timer** plays a tone and announces once when done; an **alarm rings repeatedly** — tone plus announcement, every ~25 seconds — **until you acknowledge it** by saying the wake word (capped at ~4 minutes so an empty house isn't lectured); a **reminder** speaks its text ("You asked me to remind you to take the dog out."). Alarms and clock reminders can recur — every day, weekdays, weekends, or specific days.

The tones are per-room settings (`sound_timer` / `sound_alarm`, bundled defaults), editable in each node's dashboard config page like the other sounds — set one empty for voice-only. They're mixed in by the server, so changes apply immediately, and the alarm tone still sounds even if speech synthesis is down. See [Node Configuration](../configuration/node.md#sound-files).

| Function | Description |
|---|---|
| `set_timer(duration_seconds, label)` | Countdown timer, optionally named |
| `set_alarm(time, days, room)` | Clock alarm (`"HH:MM"`), optional recurrence + room |
| `set_reminder(text, in_seconds, time, days, room)` | Spoken reminder, relative or clock-based |
| `run_later(command, in_seconds, time)` | Defer any voice command — replayed through the normal pipeline at fire time |
| `list_schedules()` | The asking room's active entries (with ids) |
| `cancel_schedules(ids)` | Cancel entries by id |
| `fast_schedule` (fast intent) | The instant tier for all the common phrasings above |

With multiple timers running, a bare "cancel the timer" asks which one (and holds the mic open for your answer); "cancel my timers" cancels them all. Missed entries (server down at fire time) are spoken late if less than 5 minutes late, otherwise dropped; a missed recurring alarm just moves to its next occurrence.

Deferred commands carry the **speaker identity from when they were set**, so a voice-gated action ("unlock the door in 10 minutes") is authorized by the voice that scheduled it. They are deliberately **one-shot** — "turn on the porch light *every day* at 8" is a standing automation, which is [Home Assistant's](home-assistant.md) job, and Kenzy will point you there.

---

## Volume

**File:** `builtin_skills/volume.py`

Adjusts the volume of the room you're speaking from — also a deterministic fast intent, so it's instant.

Say things like: *"turn it up"*, *"quieter"*, *"set the volume to 40"*, *"volume at 75 percent"*, *"mute"*, *"unmute"*.

| Matcher | Description |
|---|---|
| `fast_volume` (fast intent) | up / down (±15), set to an exact level, mute, unmute — applied to the asking room's node |

The volume level **persists** (it's part of the node's server-owned config, also settable from the dashboard's slider). **Mute is temporary** — a muted node still plays its wake-word chime at a low level so you can tell it's listening, and comes back un-muted after a restart.

---

## Weather

**File:** `builtin_skills/weather.py`

Everyday phrasings ("what's the weather", "temperature outside", "what's the forecast for the week") are **fast-dispatched** by `fast_weather` — routed straight to the current/forecast function without the LLM's tool-selection round-trip (faster, though it still fetches from the weather service). A named location ("weather in Paris") or a reasoning question ("should I bring an umbrella?") falls through to the LLM.

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

## Web Search

**File:** `builtin_skills/web_search.py`

Lets Kenzy search the web for things it can't answer from the language model's own knowledge — recent events, prices, sports scores, opening hours, or any "look it up…" request. The skill returns the top results (title, snippet, source) to the model, which reads them and speaks a concise answer.

### Skills

| Function | Description |
|---|---|
| `web_search(query)` | Searches the web and returns a numbered list of results for the model to synthesize from |

### Configuration

Set under `skills.web_search` in `llm.yaml`:

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
