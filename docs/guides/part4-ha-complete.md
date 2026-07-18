# Part 4 — Home Assistant, the Works

**Goal:** Enable HA automations with Kenzy and replace HA Assist with the Kenzy backend.

Make sure you've got a working integration with Home Assistant first from [Part 3](part3-home-assistant.md).

*Note:* These three pieces are independent — do any of them, in any order. This is
the "advanced" tier only in the sense that each one crosses between systems;
none of them is long.

## 1. Use Kenzy's nodes in HA Automations

The MQTT bridge surfaces every Kenzy room node as an **HA device** — state
sensors (idle/listening/speaking), who spoke last, mute switches, trigger
buttons — so your automations can react to Kenzy and command her:

- *"When the doorbell rings, chime in every room"* (`kenzy/chime`)
- *"When the movie starts, mute the living-room node"*
- A dashboard card showing which rooms are listening right now

Setup (an MQTT broker, two config keys) is in
[Integrations → Home Assistant](../integrations/home-assistant.md). Entities
appear via MQTT discovery — no HA-side configuration at all.

## 2. Kenzy on your phone

Install the **kenzy-hass** integration — via HACS, the one piece of Part 4
that needs it — and pick Kenzy as your voice assistant's conversation agent — then map each household member to
their HA login on the **People** tab so phone requests arrive *as them*.

The complete walkthrough is **[On Your Phone](../phone.md)** — install,
mapping, and what to expect ("set a timer" asks which room; "remind me in 10
minutes **in the office**" is one shot).

!!! note "✓ Checkpoint"
    Ask Assist on your phone: *"what's your name?"* → "I'm Kenzy." Then:
    *"what do you know about my locker?"* → your fact from
    [Part 2](part2-rooms-and-people.md), because the phone knows it's you.

## 3. Her voice and ears in the HA pipeline

Two switches make a *spoken* phone conversation Kenzy end to end — her
speech recognition transcribing, her voice answering:

1. Kenzy dashboard → **Services** → `tts` and `stt` → set
   `wyoming.enabled: true` on each, save (the services restart themselves).
2. HA → **Add Integration → Wyoming Protocol** (built into HA — no HACS
   for this one) — once for port `10200` (voice) and once for `10300`
   (ears). Both appear as **kenzy**.
3. In your voice assistant's pipeline, select kenzy for **Text-to-speech**
   and **Speech-to-text**.

If HA runs on a different machine than the Kenzy services, the services must
be reachable from it — the [On Your Phone](../phone.md#kenzys-voice-and-ears-optional-recommended)
page covers the bind setting and the (deliberate) security posture.

-----

**You made it!** Great Job.

Now dig deeper into what you can do with Kenzy:

* [Talking to Kenzy](../talking-to-kenzy.md) for the household
* [Memory](../memory.md) for what she remembers
* [Dashboard](../dashboard.md) 
