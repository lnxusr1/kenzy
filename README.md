# KENZY &middot; [![GitHub license](https://img.shields.io/github/license/lnxusr1/kenzy.svg)](https://github.com/lnxusr1/kenzy/blob/main/LICENSE) ![Python Versions](https://img.shields.io/pypi/pyversions/kenzy.svg) ![GitHub release (latest by date)](https://img.shields.io/github/v/release/lnxusr1/kenzy.svg)

**[kenzy.ai](https://kenzy.ai)** &middot; [Documentation](https://docs.kenzy.ai/) &middot; [Install](https://docs.kenzy.ai/getting-started/)

A distributed home voice assistant built as six independently deployable microservices. Kenzy runs wake-word detection locally on room nodes (Orange Pi Zero 3 / 3W or Raspberry Pi 3 / 4 / 5), streams audio to a central server for transcription, runs it through an LLM with tool-calling skills, and streams synthesized speech back to the room. It also keeps a live, per-room sense of where people are, built from your Home Assistant sensors and from who it last heard in each room.

## Architecture

```
Node (mic) ──PCM over WebSocket──► Server
                                      │
                    ┌─────────────────┘ on session end
                    ▼
            STT  ──┐  (parallel)
            Speaker ID ──┘
                    │
                    ▼
                   LLM  ◄──► Skills (weather, news, home control, …)
                    │
                    ▼
                   TTS
                    │
             PCM over WebSocket ──► Node (speaker)
```

Alongside the voice pipeline, the server holds a persistent subscription to Home
Assistant's WebSocket API, feeding the room-presence model described under
[Room presence](#room-presence). It's the one inbound stream that isn't audio.

| Service | Command | Default port | Role |
|---|---|---|---|
| **node** | `kenzy-node` | — | Wake word + audio capture, TTS playback |
| **server** | `kenzy-server` | 8765 | WebSocket hub, pipeline orchestrator |
| **stt** | `kenzy-stt` | 8767 | Speech-to-text via faster-whisper |
| **tts** | `kenzy-tts` | 8769 | Text-to-speech via OpenAI or local Kokoro |
| **llm** | `kenzy-llm` | 8766 | LLM + skill tool-calling via LiteLLM |
| **speaker** | `kenzy-speaker` | 8768 | Speaker identification via SpeechBrain |
| **s2s** | `kenzy-s2s` | 8771 | Conversation engine (experimental follow-up mode — off by default) |

## Requirements

- Python 3.11+ (a room node on 3.12+ installs its wake-word engine differently — see below)
- On Raspberry Pi OS / Debian: `sudo apt-get install libportaudio2 portaudio19-dev`
- On a Linux/x86 host with **no NVIDIA card**, the installer picks the CPU-only PyTorch
  build for the speaker service and the local voice, skipping ~2.7 GB of unusable CUDA
  runtime. Detected automatically; `--cpu-torch` / `--cuda-torch` override. An existing
  install keeps the build it already has.
- API keys: OpenAI (TTS + LLM), Home Assistant (home control skill). The weather skill uses the National Weather Service API — no key required.

### Wake-word engine: a Python ceiling on Linux, and a note for macOS

Wake-word detection uses [openwakeword](https://github.com/dscripka/openWakeWord),
which declares `tflite-runtime` as a hard dependency **on Linux** — and that package
publishes no wheel past **Python 3.11** and no sdist. Its own code imports tflite
lazily and only fails if you hand it a `.tflite` model, so the requirement is real
only in the packaging.

Because of that, the engine lives in its **own extra**: a working node is
`pip install "kenzy[node,wakeword]"`. On Linux with Python 3.12+ that extra can't
resolve, so install it without its dependencies instead — Kenzy then uses the
bundled ONNX model automatically:

```bash
pip install "kenzy[node]"
pip install --no-deps openwakeword
pip install onnxruntime tqdm scipy scikit-learn requests
```

The one-line installer picks the right path from your Python version. The server
and backend services are unaffected — only room nodes run a wake word.

**On macOS it's the opposite problem, and it's already handled.** `tflite-runtime`
is excluded there by a platform marker, so the install succeeds and there is no
tflite runtime at all. Kenzy ships the wake-word model in **both** formats and picks
by what the host can actually run — a Mac uses the bundled ONNX copy automatically,
with no conversion step and nothing to configure. ONNX measured ~13% *faster* than
tflite on x86 and ~36% slower on Pi-class ARM, which is why the choice is made per
host rather than globally.

## Setup

Kenzy installs from PyPI — the default configs, built-in skills, and `.env.example`
ship as package data, so a service runs from a bare install with no source checkout:

```bash
pipx install "kenzy[node]"           # or use the one-line installer at kenzy.ai/install.sh
kenzy-setup                          # download wake-word / speaker-ID models (run once)
kenzy-init                           # scaffold a config home (~/.config/kenzy)
```

On a room node, add `kenzy[node,mediakeys]` for a USB speakerphone's physical
**volume buttons** (Linux only) — the one-line installer and `kenzy-deploy` do
this by default, since the recommended node is a speakerphone and those have
keys. It stays a separate extra because it builds a C extension (`python3-dev`
+ `gcc`), so a failed build costs the buttons and never the install; opt out
with `--no-media-keys`. The node's user also needs to be in the `input` group.

For development from a checkout, use an editable install instead:

```bash
# Create and activate a virtualenv
python3 -m venv .venv
source .venv/bin/activate

# Install the services you need
pip install -e ".[node]"                          # room node only
pip install -e ".[server,stt,tts,llm,speaker]"   # full server stack
pip install -e ".[node,server,stt,tts,llm,speaker,dev]"  # everything

# Download wake-word and speaker-ID models (run once after install)
kenzy-setup

# Configure API keys
cp .env.example .env
# Edit .env and fill in OPENAI_API_KEY, HA_API_KEY (as needed)
```

## Running

The config-path argument is optional — each service resolves its config from the config home automatically. **Start the server first**: the backend services and nodes pull their config from it on startup and block until it answers.

```bash
# Server host first
kenzy-server  [configs/server.yaml]
kenzy-stt     [configs/stt.yaml]
kenzy-tts     [configs/tts.yaml]
kenzy-llm     [configs/llm.yaml]
kenzy-speaker [configs/speaker.yaml]

kenzy-node    [configs/node.yaml]     # then each room device (discovers + pulls from the server)
```

### Speaker enrollment

To enable speaker identification, enroll each person once:

```bash
kenzy-enroll [configs/speaker.yaml]
```

To identify the correct audio device and sample rates for a node:

```bash
kenzy-devices
```

### Remote deployment

`kenzy-deploy` manages installation and updates across a fleet of remote hosts over SSH. See `configs/deploy.yaml` for host configuration.

```bash
kenzy-deploy init       # one-time OS setup on all hosts
kenzy-deploy install    # first full deployment (source or PyPI mode)
kenzy-deploy upgrade    # install updates and restart services
kenzy-deploy status     # check service health
kenzy-deploy uninstall  # stop, remove units + venv (--purge also removes the install dir)
```

Prerequisites on each remote host: SSH key auth and passwordless sudo. Backend services are deployed in **pull mode** (they fetch config from the server) and per-node config lives in the server-owned central store (`configs/nodes/`, `configs/services/`), so a deployed fleet is managed from the dashboard. See the [deployment docs](https://docs.kenzy.ai/deployment/) for the central-config model, per-host `node_id`, and `--reseed`.

## Dashboard

`kenzy-server` serves a web fleet manager — **on by default** in the shipped config
(set `dashboard.enabled: false` in `server.yaml` to disable it entirely; nothing is
then wired up). Open `https://<server>:8770` — `http://` if you installed without TLS.
It gives you one place to:

- See live node + backend-service health and each host's installed version — including any room that has **gone missing**, with how long it has been unreachable (an orphaned node still answers its wake word, so it looks fine from inside the room)
- Configure each node, **rename its room**, and run **guided calibration** (also available by voice: "Hey Kenzy, calibrate") — it measures the room, detects echo cancellation, and applies the thresholds itself; if the speakerphone you pick has **volume buttons**, it offers to wire those up in the same step
- Manage **skills** (enable/disable live) and **speaker profiles** (rename / delete / enroll from a room)
- Watch **room presence** — which rooms have someone in them, what each belief rests on, and how fresh it is
- See everything she has said **on her own** — and, just as usefully, every time she decided *not* to, with the reason. Includes a "test an alert" button so safety announcements can be verified without setting off a real smoke detector
- Watch **pipeline activity** (transcripts, latency, fast-path hit rate) and read server / service / node **logs**
- Trigger / stop / restart nodes and send TTS **announcements** to every room
- **Upgrade** the server, backend services, and nodes in place — one click, with an "update available" check against PyPI (installed **add-ons** upgrade jointly, so a version pairing can never silently break)
- Edit a safe subset of the server's own config and change the dashboard password

**Add-ons** (5.1): optional capabilities ship as separate pip packages — install one into Kenzy's environment and it appears with its own dashboard pages under an *Add-ons* section; uninstalled, it costs nothing and shows nothing. The first is [kenzy-ld2450](https://github.com/lnxusr1/kenzy-ld2450), an in-node mmWave radar that gives each room true someone-is-here presence (including people sitting still), with a live sensor view, drag-to-draw ignore zones, and per-room Home Assistant occupancy entities over MQTT. An installed add-on that can't load says why on the Settings page instead of vanishing.

The `install.sh` installer **enables TLS by default** (generating a self-signed pair into
the config home; `--no-tls` opts out). A plain `pip install` starts plaintext — a
trusted-LAN posture — until you add `tls: {cert, key}` to `server.yaml`. Either way TLS
covers both the dashboard (https) and the node audio stream (wss); a self-signed cert
works because Kenzy's own clients connect encrypted-but-unverified by default. See the
[server configuration docs](https://docs.kenzy.ai/configuration/server/#tls-optional).

All `/api` reads and actions require login; mutating actions also need `controls`. Login
defaults to `admin` / `password` — change it with `kenzy-passwd` (server host only) or
from the Settings page. It's a LAN appliance either way, so **do not port-forward it**.
The Settings page also shows the **node join token** to copy when provisioning new nodes.
See the [Dashboard guide](https://docs.kenzy.ai/dashboard/).

## Configuration

The server is the configuration authority for the whole fleet. Nodes and the backend
services pull their config from it at boot and are edited from the dashboard; the YAML
files below are the server-side store and the seed defaults.

Key settings:

* **`configs/node.yaml`** — **bootstrap-only** (identity + how to reach the server + early logging). A node auto-generates a stable `node_id`, then blocks until the server pushes its full operational config (audio device, wake-word threshold/VAD, sounds, room name) and initializes audio from that. Per-node overrides live in `configs/nodes/<node_id>.yaml`; the room name is server-owned and set from the dashboard.
* **`configs/server.yaml`** — URLs for each downstream service (omit a URL to disable that stage), `node_defaults`, discovery, room presence (`occupancy.enabled`), and the dashboard block
* **`configs/services/<svc>.yaml`** — server-owned overrides for the backend services (stt/tts/llm/speaker), edited from the dashboard (**Fleet** → the service's chip); each service pulls its effective config (packaged default + this override, secrets stripped) from the server at boot
* **`configs/llm.yaml` / `stt.yaml` / `tts.yaml` / `speaker.yaml`** — packaged seed defaults for those services (model/voice/thresholds/etc.)

Secrets stay in each host's environment / `.env` — never in the config store.

## Skills

Skills are async Python functions in `skills/` decorated with `@skill`. They are discovered and loaded automatically at startup — no registration required. The LLM calls them as tools based on their docstrings and type signatures.

Included skills:

| Skill file | What it does |
|---|---|
| `weather.py` | Current conditions and forecast via NWS |
| `news.py` | RSS headlines and article summaries |
| `web_search.py` | General web search for current or niche questions the model can't answer alone |
| `stocks.py` | Stock quotes via yfinance |
| `home_assistant.py` | Smart home control via Home Assistant REST API (secure actions require a recognized speaker) |
| `lists.py` | Shopping / to-do lists, backed by Home Assistant's `todo` entities — add, read, check off, create (no Kenzy-side storage, so your phone already has them) |
| `schedule.py` | Timers, alarms, and reminders — including "turn on the lights in 30 seconds", replayed through the pipeline at fire time |
| `proactive_control.py` | Voice control over unprompted announcements — "stop" silences a sounding alert until its sensor cycles; "disable the alerts" turns the feature off entirely, and confirms first |
| `memory_skill.py` | Remember / recall / forget, per person, with private / personal / shared tiers (recognized voices only) |
| `presence.py` | "Is Mom home?", "where's Alice?", "is anyone in the loft?" — HA `person` entities composed with the room-presence model (where a voice was last heard, with its age). Spoken names match forgivingly ("Sara" finds Sarah; nicknames via per-person aliases on the People page), and a genuine near-tie asks instead of guessing |
| `datetime_skill.py` | Current date and time (with a deterministic fast path) |
| `announce.py` | Speak a message in every room (broadcast) |
| `intercom.py` | Start a live two-way voice call between two rooms (consent-gated at the far end) |
| `volume.py` | Set / adjust a room's playback volume or mute |
| `enroll.py` | Voice speaker enrollment ("enroll me as Alice") |
| `calibrate.py` | "Hey Kenzy, calibrate" — runs the guided audio calibration on the asking node |
| `social.py` | Instant greetings and "never mind" — fast path only, no model round-trip |
| `random_tools.py` | Coin flip, dice, random number, pick from list |
| `knock_knock.py` | Knock-knock jokes, both directions — she tells them and plays along with yours |
| `about.py` | Reports the installed Kenzy version |

### Adding a skill

```python
# skills/my_skill.py
from kenzy.llm.skills import skill

@skill
async def my_skill(query: str) -> str:
    """One-line description the LLM uses to decide when to call this."""
    return "result"
```

Per-skill config lives under `skills.<name>` in `llm.yaml`. Secrets come from environment variables in `.env`.

### Home Assistant device topology

Smart home control pulls your device **topology live from Home Assistant** — which entities exist, their friendly names, domains, and floor/area placement — so there's no device-map file to maintain. Add a device in HA and it's voice-controllable on the next refresh. Commands resolve in two tiers: a deterministic fast path (padacioso + rapidfuzz, no LLM) for everyday imperatives, and a sub-LLM fallback that reads the live `floor → area → type → entity` outline for harder requests.

Covered domains: lights, switches, fans, covers, locks, climate — plus **scenes, scripts, buttons, and toggle helpers** ("activate movie night", "run the goodnight routine", "turn on guest mode" — resolved by name house-wide), the **robot vacuum** ("start the vacuum", "send it home"), and **media-player transport** ("pause the TV", "skip this song", "turn the music down" — targeting whatever's actually playing; starting new music by name arrives with the Music Assistant integration).

The only hand-authored input is an optional `data/home_assistant/curation.yaml` — the voice layer HA can't store: spoken aliases, per-device notes, room group-defaults, voice-control exclusions, which to-do list "the list" means, and which sensors count as presence. Edit it directly or from the dashboard's **Home Assistant** tab (sub-tabs: Devices, Presence sensors, Safety sensors, Lists). Run `kenzy-ha-devices` to print the live tree with each entity ID and its included/excluded status.

### Room presence

Kenzy keeps a live picture of which rooms have someone in them. The server holds a
persistent subscription to Home Assistant's WebSocket API and folds motion, occupancy
and presence sensors — plus whose voice was just heard where — into a per-room belief
that decays with time. The dashboard's **Presence** tab shows it: each room's state,
what the claim rests on, and how old it is.

Two things it does on purpose:

- **"Unknown" is a real answer.** No sensor and no recent voice means Kenzy doesn't
  know, which is not the same as the room being empty. Rooms read unknown after a
  restart until something says otherwise.
- **It answers questions, but acts on nothing.** Ask "where is Alice?" and the answer
  composes her home/away state with where her voice was last heard — with the age
  always spoken ("…I last heard them in the office just now"). Ask "is anyone in the
  loft?" and the answer carries its evidence and its freshness, names a person only
  when a recognized voice was actually heard, and never claims a room is *empty* —
  the strongest no is "no sign of anyone for a while". But presence still changes no
  behavior on its own: nothing speaks unprompted, no delivery is re-targeted. It's a
  world model you can question, and catch being wrong, before anything depends on it.

It needs Home Assistant configured; without it nothing starts and the tab stays hidden.
Turn it off with `occupancy.enabled: false`. Which entities count is automatic (motion,
occupancy and presence sensors, plus `person` entities for home/away) and adjustable per
sensor under **Home Assistant → Presence sensors** — useful when a hallway sensor the cat
trips keeps a room "occupied".

### Speaking first

Kenzy can start a conversation, and so far only for emergencies: smoke, carbon monoxide,
gas, a water leak, or an alarm panel that has actually **triggered**. She says it in every
room including muted ones. Every one of those is a hazard a *device asserted* — she relays
it and never infers one herself, which is the boundary that makes speaking unprompted
defensible at all.

It is **off until you switch it on** (`proactive.safety.enabled`), rides the same Home
Assistant subscription as presence, and picks its sensors the same way — automatic by
device class, adjustable under **Home Assistant → Safety sensors**.

Say anything at all — "stop", or just the wake word — and a sounding alert goes quiet
until that sensor cycles. Turning the feature off needs a different sentence, *"disable
the alerts"*, and confirms first. Both work with the language model out of the picture,
because if she is talking nonsense the model is a suspect. The **Proactive** tab shows
every decision, including the times she stayed silent and why.

Announcements *you* send — from the dashboard, or an HA automation on the MQTT topic —
never pass through any of this. It governs her initiative, not your instructions.

## Development

```bash
source .venv/bin/activate
ruff check src/      # lint
ruff format src/     # format
mypy src/            # type-check
pytest               # run tests
```

## Environment variables

See `.env.example` for the full list. Required variables:

| Variable | Used by |
|---|---|
| `OPENAI_API_KEY` | TTS service, LLM service (if using OpenAI models) |
| `HA_API_KEY` | Home Assistant skill |
