# Home Assistant integration

Kenzy can surface itself **into** Home Assistant over MQTT, the same way Frigate
does — Kenzy stays a standalone product, and HA reaches in to see and control it.
Each room node shows up in HA as a **device** with sensors and controls, with no
custom component and no HA-side configuration: it uses **HA MQTT Discovery**.

This is separate from the [Home Assistant *skill*](../skills/home-assistant.md),
which lets Kenzy **control** your HA devices by voice. This page is the other
direction: putting **Kenzy** into HA.

!!! note "Two-way, but distinct"
    - **Skill (Kenzy → HA):** "turn on the kitchen lights" — Kenzy calls HA.
    - **Integration (HA → Kenzy):** Kenzy's nodes appear in HA; automations can
      trigger a node, announce, set volume, or mute.

## Prerequisites

- An MQTT broker. If you run Home Assistant, the **Mosquitto broker** add-on is the
  easiest — install it and add a login (Add-on → Configuration → `logins:`), or use
  a Home Assistant user.
- HA's **MQTT integration** configured against that broker.
- The `mqtt` extra on the Kenzy **server** host:

    ```bash
    pip install "kenzy[server,mqtt]"
    ```

## Enable it

In your server config (`server.yaml`):

```yaml
integrations:
  mqtt:
    enabled: true
    host: "homeassistant.lan"     # your broker
    port: 1883
    base_topic: "kenzy"
    discovery_prefix: "homeassistant"
    commands: true                # false ⇒ read-only (no buttons/switch/commands)
```

Put the broker credentials in the environment (never in the config file) — the
server reads them from `.env`:

```bash
KENZY_MQTT_USERNAME=kenzy
KENZY_MQTT_PASSWORD=...
```

Restart the server. Within a few seconds each connected node appears under
**Settings → Devices & Services → MQTT** as a `Kenzy <room>` device.

It is **off by default** and adds zero overhead until enabled.

!!! tip "Edit from the dashboard"
    These settings (`enabled`, `host`, `port`, `base_topic`, `discovery_prefix`,
    `commands`) are also editable from the Kenzy dashboard's **Settings** tab — it
    writes a `server.local.yaml` override and restarts the server. The broker
    username/password stay in the environment and are never shown or stored there.
    (The server needs an on-disk `server.yaml` for the override to have a writable
    home — see note below.)

## What appears in Home Assistant

One device per node, with these entities:

| Entity | Type | Meaning |
|---|---|---|
| State | sensor | `idle` / `streaming` / `offline` |
| Last speaker | sensor | the last identified speaker in that room |
| Last heard | sensor (timestamp) | when that room last completed an interaction |
| Trigger | button | start a listening session on the node |
| Stop | button | stop streaming / playback |
| Mute | switch | mute/unmute the node (reflects mutes made by voice or the dashboard too) |

The buttons and the Mute switch appear only when `commands: true` (the default).
Entities show **Unavailable** unless *both* the bridge and that node are online.

!!! info "Privacy — no spoken text"
    The integration **never publishes transcripts or responses.** Only state,
    presence (who was heard, where, and when), and timing leave the box. To review
    what was said, use the dashboard's **Activity** tab instead.

## Driving Kenzy from automations

When `commands: true`, automations can publish to these topics (e.g. with the
`mqtt.publish` service). `<node_id>` is the node's id (its slug — for friendly ids
it's the id itself):

| Topic | Payload | Effect |
|---|---|---|
| `kenzy/<node_id>/trigger` | any | activate the node |
| `kenzy/<node_id>/stop` | any | stop the node |
| `kenzy/<node_id>/volume` | `0`–`100` | set volume |
| `kenzy/<node_id>/mute` | `ON` / `OFF` | mute / unmute |
| `kenzy/announce` | text | speak the text in **every** room |
| `kenzy/chime` | see below | play a chime — instant, no TTS involved |

`kenzy/chime` payloads: empty = the bundled doorbell, once. A bare string names the
sound (`doorbell.wav`, `chime.wav`, or a name from `integrations.mqtt.chimes` — a
map of name → WAV path on the server host). JSON unlocks the rest:

```json
{"sound": "doorbell.wav", "seconds": 8, "rooms": ["kitchen", "office"]}
```

`seconds` loops the cue (whole repeats, capped at 30 s); `rooms` limits which nodes
play it (omit for house-wide). Chimes are **alert audio**: a muted node still plays
them at a low, audible floor — same as the wake-word chime — because "someone is at
your door" is exactly the class of thing mute shouldn't hide. (Nodes on older
releases simply honor mute instead.)

Example — announce when the back door opens after 11pm:

```yaml
automation:
  - alias: Late-night door announce
    trigger:
      - platform: state
        entity_id: binary_sensor.back_door
        to: "on"
    condition:
      - condition: time
        after: "23:00:00"
    action:
      - service: mqtt.publish
        data:
          topic: kenzy/announce
          payload: "The back door just opened."
```

### Recipe: a house-wide doorbell

If your smart doorbell's own chime is too quiet, let every Kenzy node announce a
press. In HA: **Settings → Automations & Scenes → Create Automation**, then
three-dots → **Edit in YAML**:

```yaml
alias: Doorbell → Kenzy chime
mode: single
trigger:
  - platform: state
    entity_id: binary_sensor.front_doorbell   # your doorbell's PRESS entity
    to: "on"
condition:
  # Debounce: an impatient triple-press shouldn't announce three times.
  - condition: template
    value_template: >
      {{ this.attributes.last_triggered is none or
         now() - this.attributes.last_triggered > timedelta(seconds=15) }}
action:
  - service: mqtt.publish
    data:
      topic: kenzy/chime
      payload: '{"sound": "doorbell.wav", "seconds": 4}'
```

Prefer a spoken announcement instead (or as well)? Publish to `kenzy/announce` with
payload `Someone's at the front door.` — the chime is instant and TTS-free; the
announcement says who/what but costs a couple seconds of synthesis.

Tips:

- **Find the right entity** in Developer Tools → States. Trigger on the *button
  press* entity, not the doorbell's motion/person sensor — or the porch cat
  announces itself. Newer integrations expose an `event.…` entity instead of a
  binary sensor; for those, drop the `to: "on"` line (any state change on an
  event entity means it fired).
- **Test the Kenzy half first**: in Developer Tools → Actions, call
  `mqtt.publish` with the topic/payload above — every connected node should
  ring. Then the automation is just attaching your trigger to that action.
- `mode: single` (the default) is right here; the action completes in
  milliseconds, so modes never really compete — the debounce condition is what
  prevents repeat announcements.

## How it works

The server publishes to MQTT using HA MQTT Discovery (retained config messages on
`<discovery_prefix>/<component>/…/config`), so HA builds the entities itself. A
bridge **availability** topic with a last-will marks everything unavailable if the
server crashes or stops. Inbound command topics map to the same actions the
dashboard uses (trigger/stop/volume/mute/announce/chime). No spoken text is ever sent.

## Roadmap

A **HACS custom integration** (config-flow setup) and a **Home Assistant add-on**
(one-click install) are planned as separate projects. The integration described
here works today over MQTT without either of them.
