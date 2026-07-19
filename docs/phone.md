# On Your Phone

Kenzy answers away from the room speakers too — through **Home Assistant's
companion app** (phone, tablet, watch). Ask from the Assist screen and it's
the same Kenzy: your memory, your identity, the same skills. With two extra
steps she also *sounds* like herself and *hears* with her own speech
recognition, so a voice conversation in the app is Kenzy end to end.

This is the flip side of the [Home Assistant skill](skills/home-assistant.md)
(Kenzy controlling HA): here, HA is the front door and Kenzy is the brain
behind it. You need a running Home Assistant with the companion app already
set up.

## What installs where

Three pieces make up the full phone experience, and they install through
**different channels** — this trips people up, so here's the map:

| Piece | What it gives you | How it installs |
|---|---|---|
| **Kenzy conversation agent** (`kenzy-hass`) | Ask Kenzy from Assist — text or voice, as *you* | **HACS custom repository** (it's a custom integration; HA's Apps/add-on store will reject it) |
| **Kenzy's voice & ears** (Wyoming) | Assist *speaks* with Kenzy's voice and *hears* with her STT | **Built into HA — no HACS.** Just *Add Integration → "Wyoming Protocol"*, which ships with Home Assistant |
| **[MQTT bridge](integrations/home-assistant.md#the-mqtt-bridge)** (optional, separate feature) | Kenzy's rooms appear *in* HA as devices for automations | HA's core MQTT integration + a broker — no HACS, no custom code |

Only the first one involves HACS. If you're being asked to add a custom
repository for Wyoming, you're in the wrong dialog — it's a core integration.

## Install the integration (HACS)

The `kenzy-hass` integration makes Kenzy an HA **conversation agent**.

1. In HACS: **⋮ → Custom repositories** → add
   `https://github.com/lnxusr1/kenzy-hass` (category **Integration**), then
   find **Kenzy**, download it, and restart Home Assistant. (Use HACS, not
   HA's *Apps* screen — that's the add-on store and rejects integration
   repos.)
2. **Settings → Devices & Services → Add Integration → Kenzy**:

   | Field | Value |
   |---|---|
   | Host / Port | Your kenzy-server (port `8765` by default) |
   | Fleet token | From the Kenzy dashboard's **Settings** page (leave empty if you run without a token) |
   | Use TLS | On for a TLS-enabled server (the install default since 3.11) |
   | Verify TLS | Off for self-signed certificates (the LAN default) |

3. **Settings → Voice assistants** — create (or edit) an assistant and pick
   **Kenzy** as its conversation agent.

Type into Assist and she answers. The token is proven by a signature — it
never travels the wire — and identity, memory tiers, and skill gating are
all enforced on *your* server; the integration only ever sees reply text.

## Tell Kenzy who's asking

An Assist request arrives as an **HA user**. Map each household member to
their HA person on Kenzy's dashboard: **People → open the person → Home
Assistant person**. When Kenzy can reach your HA it's a dropdown of your
actual HA people — pick and save.

A mapped person is **recognized**: their memory, their personalization, the
same as their voice at home (an HA login is at least as strong an identity
signal as a voiceprint). An unmapped HA user is served like an unrecognized
voice — questions and device control work, but no memory and no gated
skills. That's fail-closed by design: handing a guest your tablet doesn't
hand them your facts.

## What works from the phone — and what asks for a room

Almost everything: questions, smart-home control, memory, announcements
("tell the kitchen dinner's ready"), lists, weather, news. (One deliberate
exception: [lockbox secrets](memory.md#secrets--the-lockbox) always deflect
to the dashboard on this channel — the phone's reply path isn't Kenzy's own
speech, so it never carries a secret value.)

The difference from a room speaker is that the phone isn't *in* a room, so:

- **Timers, alarms, and reminders ask where to ring.** "Set a timer for 10
  minutes" → *"Which room should I set the timer in?"* → "the office" —
  done. Name the room up front ("remind me to stretch in 5 minutes **in the
  office**") and it's one shot. Status and cancel work house-wide: "what
  timers do I have?" lists every room's.
- **Speaker-bound things decline politely** — volume, audio calibration,
  voice enrollment, and intercom calls all need a room device, and Kenzy
  says so instead of guessing.
- **"Turn on the light" has no "this room" to lean on** — device commands
  that name their target ("the porch light", "the goodnight scene") work
  exactly as at home; bare room-relative phrasings are resolved by the
  language model from context, so be specific when it matters.

## Kenzy's voice and ears (optional, recommended)

By default the HA pipeline uses its own speech-to-text and text-to-speech
around Kenzy's brain. Two [Wyoming protocol](https://github.com/rhasspy/wyoming)
listeners make the whole voice loop hers:

**No HACS for this part** — the "Wyoming Protocol" integration is built into
Home Assistant. The work is on the Kenzy side (turning the listeners on),
then two Add-Integration dialogs in HA.

1. **On the Kenzy side**, enable them (dashboard → **Services** → tts / stt,
   or the service configs), then restart the services:

   ```yaml
   # tts.yaml                      # stt.yaml
   wyoming:                        wyoming:
     enabled: true                   enabled: true
     port: 10200                     port: 10300
   ```

   If HA runs on a different host, the tts/stt services must be reachable
   off-box: bind them to the LAN (`KENZY_BIND=0.0.0.0`, or `--listen-all`
   with `kenzy-deploy`). Wyoming is plain, unauthenticated TCP — the
   listeners deliberately follow the service bind, so they're loopback-only
   until you make that call for your network.

2. **Verify the listeners are up** before touching HA — on the service host:

   ```bash
   ss -tln | grep -E '10200|10300'
   ```

   You want both ports on `0.0.0.0` (or your LAN address). `127.0.0.1` means
   the bind didn't take; nothing listed means the listener didn't start —
   the service log says why (dashboard → **Logs**).

3. **In HA**: **Settings → Devices & Services → Add Integration** → search
   **"Wyoming Protocol"** (core — no HACS). Add it **twice**, once per
   listener:

   - host = the machine running kenzy-tts, port **10200**
   - host = the machine running kenzy-stt, port **10300**

   Each creates an entry named **kenzy** (they look identical in the list —
   rename them "Kenzy voice" / "Kenzy ears" if you want labels).

4. **Pick them in the pipeline**: **Settings → Voice assistants** → your
   assistant → set **Speech-to-text** and **Text-to-speech** to **kenzy**
   (they only appear in these dropdowns *after* step 3 — the Wyoming
   entries are what put them there).

!!! tip "HA says \"failed to connect\"?"
    Check the service's log (dashboard → Logs) — if it says the `wyoming`
    package is not installed, your install was upgraded before the feature
    existed: use the service's **Upgrade** button (not just Restart) to pull
    the new dependency in. Details:
    [a new feature won't turn on after an upgrade](troubleshooting.md#a-new-feature-wont-turn-on-after-an-upgrade).

Now a spoken question in the companion app is transcribed by your configured
STT (one whisper/cloud setup for the whole house, fallback chain included),
answered by Kenzy, and spoken back in her actual voice.

## Good to know

- **Conversations carry across turns.** Assist keeps the thread, so "which
  room?" follow-ups and "what about tomorrow?" work naturally.
- **Room speakers still do the ringing.** A reminder set from the phone
  rings on the room speaker you chose — the phone is a front door, not a
  speaker. (Pushing notifications *to* phones is on the roadmap.)
- **Your HA tab and the People field appear only when relevant.** A Kenzy
  install with no Home Assistant shows no HA surfaces at all; installing
  this integration reveals them automatically after the first Assist
  request.
