# Home Assistant Integration

The Home Assistant skill lets Kenzy control and query your smart home devices using natural language. It supports lights, switches, fans, covers (blinds/garage doors), locks, and thermostats.

## How it works

Rather than querying HA's entity list at runtime, the skill uses a pre-built **device map** — two files that describe your devices in a format optimized for LLM reasoning:

1. **`device_ids.yaml`** — a human-readable hierarchy of your devices, organized by floor → room → type → alias. YAML line comments provide context the LLM reads.
2. **`device_ids.json`** — a flat mapping of alias → HA entity ID, used for the final API call.

When a home control request arrives, the skill:

1. Loads both files
2. Sends the YAML + user request to a sub-LLM call that resolves which device aliases to act on and what action to take
3. Looks up each alias in the JSON to get the real HA entity ID
4. Calls the HA REST API

This design means the LLM knows your actual device names ("of_floor_lamps", "lr_ceiling_fan") and can pick the right one when you say "turn on the lamp in the office" — without querying HA's entity list on every request.

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

## Supported actions

| Device type | Actions |
|---|---|
| `light`, `switch` | `turn_on`, `turn_off`, `toggle` |
| `fan` | `turn_on`, `turn_off`, `toggle` |
| `cover` | `open_cover`, `close_cover` |
| `lock` | `lock`, `unlock` |
| `climate` | `set_temperature` (65–85 °F), `get_status` |

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
    model:            "gpt-4o"      # sub-LLM model for device resolution
    # base_url: null                # set for Ollama / LM Studio
    device_ids_yaml:  "data/home_assistant/device_ids.yaml"
    device_ids_json:  "data/home_assistant/device_ids.json"
    default_room:     "living_room" # assumed room if user doesn't specify
```

## Example interactions

- *"Turn off the lights in the office"* → turns off all lights in the office
- *"Turn on the lamp"* → turns on the default device in the default room
- *"Set the thermostat to 72"* → sets the climate entity in the inferred room
- *"Make it a bit warmer"* → reads current setpoint via `get_status`, then sets +2°F
- *"Lock the front door"* → requires an enrolled speaker; refused for unknown
- *"What's the temperature in the living room?"* → returns current state from HA
