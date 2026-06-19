# KENZY &middot; [![GitHub license](https://img.shields.io/github/license/lnxusr1/kenzy.svg)](https://github.com/lnxusr1/kenzy/blob/main/LICENSE) ![Python Versions](https://img.shields.io/pypi/pyversions/yt2mp3.svg) ![Read the Docs](https://img.shields.io/readthedocs/kenzy) ![GitHub release (latest by date)](https://img.shields.io/github/v/release/lnxusr1/kenzy.svg)


A distributed home voice assistant built as six independently deployable microservices. Kenzy runs wake-word detection locally on room nodes (Orange Pi Zero 3 / 3W or Raspberry Pi 3 / 4 / 5), streams audio to a central server for transcription, runs it through an LLM with tool-calling skills, and streams synthesized speech back to the room.

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

| Service | Command | Default port | Role |
|---|---|---|---|
| **node** | `kenzy-node` | — | Wake word + audio capture, TTS playback |
| **server** | `kenzy-server` | 8765 | WebSocket hub, pipeline orchestrator |
| **stt** | `kenzy-stt` | 8767 | Speech-to-text via faster-whisper |
| **tts** | `kenzy-tts` | 8769 | Text-to-speech via OpenAI TTS |
| **llm** | `kenzy-llm` | 8766 | LLM + skill tool-calling via LiteLLM |
| **speaker** | `kenzy-speaker` | 8768 | Speaker identification via SpeechBrain |

## Requirements

- Python 3.11+
- On Raspberry Pi OS / Debian: `sudo apt-get install libportaudio2 portaudio19-dev`
- API keys: OpenAI (TTS + LLM), Home Assistant (home control skill). The weather skill uses the National Weather Service API — no key required.

## Setup

Kenzy installs from PyPI — the default configs, built-in skills, and `.env.example`
ship as package data, so a service runs from a bare install with no source checkout:

```bash
pipx install "kenzy[node]"           # or use the one-line installer at kenzy.dev/install.sh
kenzy-setup                          # download wake-word / speaker-ID models (run once)
kenzy-init                           # scaffold a config home (~/.config/kenzy)
```

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
# Edit .env and fill in OPENAI_API_KEY, WEATHER_API_KEY, HA_API_KEY
```

## Running

Each service reads its config from `configs/<service>.yaml`. Start them in any order:

```bash
kenzy-server  [configs/server.yaml]
kenzy-stt     [configs/stt.yaml]
kenzy-tts     [configs/tts.yaml]
kenzy-llm     [configs/llm.yaml]
kenzy-speaker [configs/speaker.yaml]
kenzy-node    [configs/node.yaml]     # on each room device
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
kenzy-deploy install    # first full deployment
kenzy-deploy upgrade    # push source + skills + .env updates
kenzy-deploy status     # check service health
```

Prerequisites on each remote host: SSH key auth and passwordless sudo.

## Dashboard

`kenzy-server` can serve an **opt-in** web fleet manager (off by default). Enable it in
`server.yaml` (`dashboard.enabled: true`, `controls: true`, `logs: true`) and open
`http://127.0.0.1:8770/dashboard`. It gives you one place to:

- See live node + backend-service health
- Configure each node and **rename its room** (pushed to the node and saved)
- Trigger / stop / restart nodes and send TTS **announcements** to every room
- Read server, service, and per-node **logs**

Login defaults to `admin` / `password` — change it with `kenzy-passwd` (server host
only). It is plaintext HTTP on a LAN bind, so **do not port-forward it**. See the
[Dashboard guide](https://docs.kenzy.dev/dashboard/).

## Configuration

The server is the configuration authority for the whole fleet. Nodes and the backend
services pull their config from it at boot and are edited from the dashboard; the YAML
files below are the server-side store and the seed defaults.

Key settings:

* **`configs/node.yaml`** — **bootstrap-only** (identity + how to reach the server + early logging). A node auto-generates a stable `node_id`, then blocks until the server pushes its full operational config (audio device, wake-word threshold/VAD, sounds, room name) and initializes audio from that. Per-node overrides live in `configs/nodes/<node_id>.yaml`; the room name is server-owned and set from the dashboard.
* **`configs/server.yaml`** — URLs for each downstream service (omit a URL to disable that stage), `node_defaults`, discovery, and the dashboard block
* **`configs/services/<svc>.yaml`** — server-owned overrides for the backend services (stt/tts/llm/speaker), edited from the dashboard's **Services** tab; each service pulls its effective config (packaged default + this override, secrets stripped) from the server at boot
* **`configs/llm.yaml` / `stt.yaml` / `tts.yaml` / `speaker.yaml`** — packaged seed defaults for those services (model/voice/thresholds/etc.)

Secrets stay in each host's environment / `.env` — never in the config store.

## Skills

Skills are async Python functions in `skills/` decorated with `@skill`. They are discovered and loaded automatically at startup — no registration required. The LLM calls them as tools based on their docstrings and type signatures.

Included skills:

| Skill file | What it does |
|---|---|
| `weather.py` | Current conditions and forecast via NWS |
| `news.py` | RSS headlines and article summaries |
| `stocks.py` | Stock quotes via yfinance |
| `home_assistant.py` | Smart home control via Home Assistant REST API |
| `random_tools.py` | Coin flip, dice, random number, pick from list |
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

### Home Assistant device map

Smart home control uses two files in `data/home_assistant/`:

- `device_ids.yaml` — human-readable device hierarchy (floors → rooms → types → aliases)
- `device_ids.json` — flat alias → HA entity ID mapping

The LLM resolves natural language commands against the YAML to find the right devices, then looks up their entity IDs in the JSON before calling the HA API.

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
| `WEATHER_API_KEY` | Reserved for alternative weather providers (not used by the default NWS skill) |
| `HA_API_KEY` | Home Assistant skill |
