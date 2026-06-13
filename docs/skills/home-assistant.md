# Home Assistant Integration

The Home Assistant skill lets Kenzy control and query your smart home devices using natural language. It supports lights, switches, fans, covers (blinds/garage doors), locks, and thermostats.

## How it works

The skill uses a pre-built **device map** — two files describing your devices:

1. **`device_ids.yaml`** — a hierarchy of your devices, organized by area → room → type → alias.
2. **`device_ids.json`** — a flat mapping of alias → HA entity ID, used for the final API call.

Requests are resolved in **two tiers** (see [Skills → Two resolution tiers](index.md#two-resolution-tiers)):

### 1. Deterministic fast path (no LLM)

A `@fast_intent` handles the common imperative commands instantly, with no remote model call:

1. [`padacioso`](https://github.com/OpenVoiceOS/padacioso) parses the utterance into an action (`turn_on`/`turn_off`/`toggle`/`lock`/`unlock`/`open`/`close`/`set_temperature`) and a target phrase.
2. The target is scoped to the room named in the utterance (or the originating node's room) and resolved to device codes via **overlay aliases → fuzzy device match ([`rapidfuzz`](https://github.com/rapidfuzz/RapidFuzz)) → group rules**.
3. The HA REST API is called directly and a short spoken confirmation is returned.

This turns "turn on the kitchen lights" into a single local parse plus one HA call — no LLM latency.

### 2. LLM fallback

Anything the fast path can't confidently resolve — status queries ("what's the temperature?"), relative changes ("make it warmer"), unrecognised device descriptions, or ambiguous phrasing — falls through to the LLM, which sends the YAML map (comments included) to a **sub-LLM** call that resolves the device aliases and action. The sub-LLM path is the safety net; the fast path handles the everyday commands.

### Resolution rules (fast path)

- **Plural type word = a group** ("the lights", "the fans"); a singular or descriptive phrase = a specific device ("the floor lamp", "the lamp by the chair").
- **On/off asymmetry.** A bare *activate* command ("turn on the lights") uses the room's curated **default** subset; a bare *deactivate* command ("turn off the lights") always acts on **all** devices of that type in the room. Saying "all"/"every" forces all even on activate.
- **Explicit room wins.** "Turn on the lamps **in the living room**" from the office node acts on the living-room lamps — it never wanders to a similarly-named device in another room. If no in-room match is found, it defers to the LLM rather than guessing.
- **Unlock/open require a named device.** A bare "unlock the doors" is never executed on a group; it defers. (See [Security](#security-lock-and-cover-operations).)

## Prerequisites

1. A running Home Assistant instance with the REST API enabled (it is on by default)
2. A long-lived access token from your HA user profile page, stored as `HA_API_KEY` in `.env`

## Device map setup

### `data/home_assistant/device_ids.yaml`

Organise your devices hierarchically. Aliases are short identifiers you invent — they just need to be unique and match the JSON file.

```yaml
downstairs:
  living_room:
    default: [lr_floor_lamp]       # used when no specific device or type is named
    lights: [lr_ceiling_light, lr_floor_lamp]
    fans:   [lr_ceiling_fan]
    climate: [lr_thermostat]
    lock:   [fy_front_door]        # front door lock — not normally used directly

  office:
    default: [of_floor_lamps]
    lights:  [of_ceiling_fan_light, of_floor_lamps]
    fans:    [of_ceiling_fan]

upstairs:
  master_bedroom:
    default: [mb_overhead_light]
    lights:  [mb_overhead_light, mb_nightstand_lamp]
```

Use YAML comments to give the sub-LLM context it could not otherwise infer:

```yaml
lock: [fy_front_door]    # front door; only accessible from inside
covers: [gy_garage_door] # main garage door
```

### `data/home_assistant/device_ids.json`

A flat alias → entity ID map:

```json
{
  "lr_floor_lamp":      "light.living_room_floor_lamp",
  "lr_ceiling_light":   "light.living_room_ceiling",
  "lr_ceiling_fan":     "switch.living_room_ceiling_fan",
  "lr_thermostat":      "climate.downstairs",
  "of_floor_lamps":     "light.office_lamps",
  "of_ceiling_fan":     "fan.office_ceiling_fan",
  "fy_front_door":      "lock.front_door",
  "gy_garage_door":     "cover.garage_door"
}
```

Entity IDs can be found in Home Assistant under **Settings → Devices & Services → Entities**.

### `data/home_assistant/device_overlay.yaml` (optional)

The overlay adds the *human* layer the fast path needs — the nicknames and policy that have no home in Home Assistant itself. It is optional; without it the fast path still works using device names auto-derived from the codes (e.g. `lr_floor_lamp` → "floor lamp").

```yaml
rooms:
  master_bedroom:
    aliases:                         # spoken name(s) → device code (or a list)
      "lamp": mb_chair_lamp          # bare "lamp" in this room = the chair lamp
      "light": mb_ceiling_fan_light
      "nightstand lamp": mb_nicki_nightstand_lamp
      "nickis lamp": mb_nicki_nightstand_lamp
    exclude:
      - mb_hallway_light             # never part of a group; only when named directly

  kitchen:
    defaults:                        # curated subset for bare "turn on the <type>"
      lights: [kt_island_pendant_lights, kt_counter_peninsula_pendant_lights]
    exclude:
      - kt_sink_light
```

| Key | Purpose |
|---|---|
| `aliases` | Spoken name → device code (or a list of codes for a named group). Room-scoped, so the same nickname can mean different things in different rooms. Resolved before fuzzy matching — use these for bare/ambiguous words. |
| `defaults` | Per-type curated subset for a bare **activate** command. A bare deactivate ignores it and means all. |
| `exclude` | Device codes barred from bare-group commands — addressable only when named directly. |

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
    url:              "http://homeassistant.local:8123"
    model:            "gpt-4o"      # sub-LLM model for the fallback resolver
    # base_url: null                # set for Ollama / LM Studio
    device_ids_yaml:  "data/home_assistant/device_ids.yaml"
    device_ids_json:  "data/home_assistant/device_ids.json"
    device_overlay:   "data/home_assistant/device_overlay.yaml"  # optional
    default_room:     "living_room" # assumed room if user doesn't specify
```

## Example interactions

- *"Turn off the office lights"* → **(fast)** all office lights
- *"Turn on the lights"* → **(fast)** the room's curated default subset
- *"Turn on the lamps in the living room"* → **(fast)** the living-room lamps, even from another room's node
- *"Set the thermostat to 72"* → **(fast)** sets the room's climate entity (clamped 65–85 °F)
- *"Lock the front door"* → **(fast)** requires an enrolled speaker; refused for unknown
- *"Make it a bit warmer"* → **(LLM)** reads current setpoint via `get_status`, then sets +2°F
- *"What's the temperature in the living room?"* → **(LLM)** returns current state from HA
