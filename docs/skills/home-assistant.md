# Home Assistant Integration

Kenzy integrates with Home Assistant in **both directions**:

- **Kenzy → HA** — the Home Assistant skill lets Kenzy control and query your smart home devices using natural language (lights, switches, fans, covers, locks, thermostats). This is the bulk of this page.
- **HA → Kenzy** — Home Assistant can see and control Kenzy itself: each node appears in HA as a device (state/presence sensors, Trigger/Stop buttons, a Mute switch) and automations can trigger, announce, set volume, or mute. See the [Home Assistant integration](../integrations/home-assistant.md) guide. (Automations can also make Kenzy speak via the server's announce webhook — see [Calling Kenzy from Home Assistant](#calling-kenzy-from-home-assistant) at the end.)

The Home Assistant skill lets Kenzy control and query your smart home devices using natural language. It supports lights, switches, fans, covers (blinds/garage doors), locks, and thermostats.

## How it works

The skill pulls your device **topology live from Home Assistant** — which entities exist, their friendly names, their domains, and their floor/area placement. You don't hand-maintain a device list: add a device in HA and it's voice-controllable on the next refresh, named the way HA names it.

The only thing you author is a small **curation file** (`curation.yaml`) holding the handful of things HA can't store — spoken aliases, per-device context notes, room group-defaults, and voice-control exclusions. Everything else is derived.

> **How the topology is fetched.** The skill renders one Jinja template through HA's REST `POST /api/template` endpoint, returning the whole area→entity tree (this is how it learns area/floor placement, which the plain `/api/states` endpoint doesn't expose). The result is cached (`cache_ttl`, default 300s); device **state** (on/off, current temperature) is never cached and is read live only when a request needs it. If HA is briefly unreachable the last good snapshot is reused.

Requests are resolved in **two tiers** (see [Skills → Two resolution tiers](index.md#two-resolution-tiers)):

### 1. Deterministic fast path (no LLM)

A `@fast_intent` handles the common imperative commands instantly, with no remote model call:

1. [`padacioso`](https://github.com/OpenVoiceOS/padacioso) parses the utterance into an action (`turn_on`/`turn_off`/`toggle`/`lock`/`unlock`/`open`/`close`/`set_temperature`) and a target phrase.
2. The target is scoped to the room named in the utterance (or the originating node's room) and resolved to entity IDs via **curated aliases → fuzzy device match ([`rapidfuzz`](https://github.com/rapidfuzz/RapidFuzz)) → group rules**.
3. The HA REST API is called directly and a short spoken confirmation is returned.

This turns "turn on the kitchen lights" into a single local parse plus one HA call — no LLM latency.

### 2. LLM fallback

Anything the fast path can't confidently resolve — status queries ("what's the temperature?"), relative changes ("make it warmer"), unrecognised device descriptions, or ambiguous phrasing — falls through to the LLM. The skill renders the live topology as a `floor → area → type → entity` outline (each line an entity ID followed by its friendly name and any curation note as context) and sends it to a **sub-LLM** call that picks the entity IDs and action. The sub-LLM path is the safety net; the fast path handles the everyday commands.

### Resolution rules (fast path)

- **Plural type word = a group** ("the lights", "the fans"); a singular or descriptive phrase = a specific device ("the floor lamp", "the lamp by the chair").
- **On/off asymmetry and how `default` / `in groups` apply.** A bare *activate* ("turn on the lights") uses the room's curated **default** subset; a bare *deactivate* ("turn off the lights") acts on every device of that type. The per-device toggles narrow this:
    - `in groups` off (`in_group: false`) keeps a device addressable by name but out of **all** group commands — except an explicit **"turn off all the …"**, which means literally all (only hard-excluded devices, removed from the model entirely, stay out).
    - `default` only narrows the bare *activate* case. Saying **"all"/"every"** on activate bypasses `default` (every in-groups device comes on), but still honors `in groups`.

    | Command | Devices acted on |
    |---|---|
    | "turn on the lights" | `default` ∩ `in groups`, minus excluded |
    | "turn on **all** the lights" | `in groups`, minus excluded |
    | "turn off the lights" | `in groups`, minus excluded |
    | "turn off **all** the lights" | every device, minus excluded (ignores `in groups`) |
    | "turn on/off the **\<name\>**" | that device (ignores `in groups`; hard-excluded devices are unreachable) |
- **Explicit room wins.** "Turn on the lamps **in the living room**" from the office node acts on the living-room lamps — it never wanders to a similarly-named device in another room. If no in-room match is found, it defers to the LLM rather than guessing.
- **Unlock/open require a named device.** A bare "unlock the doors" is never executed on a group; it defers. (See [Security](#security-lock-and-cover-operations).)

## Prerequisites

1. A running Home Assistant instance with the REST API enabled (it is on by default)
2. A long-lived access token from your HA user profile page, stored as `HA_API_KEY` in `.env`

## Setup

There's nothing to set up for the device inventory itself — it comes from HA. The first time the skill runs, every controllable entity that's assigned to an **area** is voice-controllable, named by its HA friendly name. Floors group areas into the two-level `floor → area` tree the resolver uses; if you don't use floors, everything falls under a single `home` level.

To see exactly what HA exposes and which entity IDs to reference, run:

```bash
kenzy-ha-devices
```

It prints the live `floor → area → type → entity` tree with each entity ID and whether it's included or excluded.

### The curation file: `data/home_assistant/curation.yaml`

This is the only file you author, and it's entirely optional. It holds the three things HA can't store, plus voice-control exclusions. Keys are **HA entity IDs** (stable across renames). A starter file ships in `data/home_assistant/curation.yaml`.

```yaml
# Remove entities from voice control entirely (smart-plug status LEDs that show
# up as `light` entities are the classic case):
exclude:
  entities: [light.basement_plug_led]   # specific entity IDs
  patterns: ["light.*_plug_led"]        # fnmatch globs on the entity ID
  domains: []                           # a whole domain, e.g. [switch]
  areas: []                             # a whole area by name, e.g. ["Garage"]

# Per-device curation:
devices:
  light.living_room_floor_lamp:
    aliases: [the lamp, reading lamp]                  # extra spoken names
    note: "black light on the table beside the chair"  # context for the resolver
  light.master_bedroom_hallway:
    in_group: false        # addressable by name, but skipped by "turn on the lights"

# What a bare "turn on the lights" means per room (room keys are HA area names):
rooms:
  living_room:
    defaults:
      lights: [light.living_room_floor_lamp, light.living_room_table_lamp]
```

| Key | Purpose |
|---|---|
| `exclude` | Entities removed from voice control entirely — never matched, grouped, or shown to the resolver. Target by `entities` (exact IDs), `patterns` (fnmatch globs on the entity ID), `domains`, or `areas` (by name). |
| `devices.<id>.aliases` | Extra spoken name(s) for an entity. Resolved before fuzzy matching — use these for bare/ambiguous words like "the lamp". |
| `devices.<id>.note` | Free-form context handed to the sub-LLM resolver (e.g. "the light by the chair"). |
| `devices.<id>.in_group` | `false` keeps the entity addressable by name but excludes it from bare-group commands. |
| `devices.<id>.hidden` | `true` is a per-entity shorthand for the `exclude` block. |
| `rooms.<area>.defaults.<type>` | Curated subset for a bare **activate** command ("turn on the lights"). A bare deactivate ignores it and means all (minus `in_group:false`). |

### Editing from the dashboard

You don't have to hand-edit the file. The fleet dashboard has a **Home Assistant** tab (a tree of your live devices) with per-entity alias / note / *in groups* / *exclude* controls, a *default* toggle per device, and bulk exclude fields. Saving writes `curation.yaml` for you and refreshes the topology immediately. It needs `kenzy-llm` reachable and `dashboard.controls: true` for edits (read-only otherwise). See [Dashboard](../dashboard.md).

### Prerequisites recap

The skill needs HA's REST API (on by default) and a long-lived token in `HA_API_KEY`. The same template-render permission used everywhere in HA is all that's required; no extra configuration.

### Offline / legacy fallback

If HA is unreachable **and** the old static `device_ids.yaml` / `device_ids.json` files are present, the skill falls back to them so an offline dev box still resolves. A successful live pull always supersedes the static files. New installs don't need them.

## Supported actions

| Device type | Actions |
|---|---|
| `light`, `switch` | `turn_on`, `turn_off`, `toggle` |
| `fan` | `turn_on`, `turn_off`, `toggle` |
| `cover` | `open_cover`, `close_cover` |
| `lock` | `lock`, `unlock` |
| `climate` | `set_temperature` (65–85 °F), `get_status` |

`set_temperature` and the control actions are handled by the fast path. `get_status` and relative changes ("make it warmer") are resolved by the LLM fallback.

## Temperature limits

The skill enforces a thermostat range of **65–85 °F**. Any value outside this range is clamped before the API call. This prevents accidental commands like "set it to 20 degrees" from making the house uncomfortably cold.

## Security: lock and cover operations

Locking/unlocking doors and opening/closing covers require a **recognized speaker**. If the speaker is unidentified (`unknown`), the skill refuses and responds:

> *"I'm sorry, I don't recognize who is speaking and can't perform lock or cover operations for security reasons."*

This relies on the speaker identification service being configured and the speaker being enrolled. See [Speaker Enrollment](../speaker-enrollment.md).

## Configuration

In `configs/llm.yaml`:

```yaml
skills:
  home_assistant:
    url:           "http://homeassistant.local:8123"
    model:         "gpt-4o"        # sub-LLM model for the fallback resolver
    # base_url: null               # set for Ollama / LM Studio
    curation_file: "data/home_assistant/curation.yaml"   # optional
    cache_ttl:     300             # seconds to cache the HA topology
    # domains: [light, switch, fan, cover, lock, climate] # voice-controllable domains
    default_room:  "living_room"   # assumed room if user doesn't specify
```

| Key | Default | Description |
|---|---|---|
| `url` | `http://homeassistant.local:8123` | Home Assistant base URL |
| `model` | `gpt-4o` | Sub-LLM model used by the fallback resolver |
| `base_url` | — | Provider base URL for the sub-LLM (Ollama / LM Studio) |
| `curation_file` | `data/home_assistant/curation.yaml` | Path to the curation file (relative to the config root) |
| `cache_ttl` | `300` | Seconds to cache the HA topology pull before refreshing |
| `domains` | `light, switch, fan, cover, lock, climate` | Entity domains exposed to voice control |
| `default_room` | — | Room assumed when the user names none |

## Example interactions

- *"Turn off the office lights"* → **(fast)** all office lights
- *"Turn on the lights"* → **(fast)** the room's curated default subset
- *"Turn on the lamps in the living room"* → **(fast)** the living-room lamps, even from another room's node
- *"Set the thermostat to 72"* → **(fast)** sets the room's climate entity (clamped 65–85 °F)
- *"Lock the front door"* → **(fast)** requires an enrolled speaker; refused for unknown
- *"Make it a bit warmer"* → **(LLM)** reads current setpoint via `get_status`, then sets +2°F
- *"What's the temperature in the living room?"* → **(LLM)** returns current state from HA

## Calling Kenzy from Home Assistant

Integration runs the other way too: Home Assistant can make **Kenzy speak** in your rooms by calling the server's always-on `GET /announce` endpoint (see [Server → Announce endpoint](../configuration/server.md#announce-endpoint)). This is the same intercom/broadcast path the dashboard's announce composer and the voice "tell everyone…" command use.

Add a `rest_command` to your HA configuration:

```yaml
rest_command:
  kenzy_announce:
    url: "http://kenzy-server.local:8765/announce?text={{ message | urlencode }}&rooms={{ rooms | default('') | urlencode }}"
    method: GET
    headers:
      Authorization: "Bearer !secret kenzy_service_token"
```

Then call it from an automation or script:

```yaml
- service: rest_command.kenzy_announce
  data:
    message: "The laundry is done"
    rooms: "kitchen"        # comma-separated room names; omit for every room
```

The bearer token must match the server's `discovery.token` / `KENZY_SERVICE_TOKEN`; drop the `headers` block if no service token is configured. `rooms` is optional — leave it empty to announce in the whole house. The endpoint must be a **GET** with query parameters (the `websockets` HTTP hook accepts GET only and exposes no request body).
