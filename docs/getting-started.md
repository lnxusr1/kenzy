# Getting Started

## Prerequisites

- Python 3.11 or later
- On Raspberry Pi OS / Debian, install PortAudio before the node package:

```bash
sudo apt-get install libportaudio2 portaudio19-dev
```

!!! warning "Room-node hardware"
    Wake-word, voice activity detection, and reliable audio streaming need a board with enough CPU/RAM headroom. Tested/confirmed room-node boards:

    - **Orange Pi Zero 3 / Zero 3W** (recommended low-cost option)
    - **Raspberry Pi 3, 4, or 5**

    The backend services (server/STT/LLM/TTS/speaker) require a more powerful machine for local inference — run those on a different machine or configure to them to use cloud services for optimum experience.

!!! tip "Use a speakerphone with hardware AEC"
    Strongly prefer a USB **speakerphone with built-in acoustic echo cancellation (AEC)** as the node's combined mic+speaker (a conference speakerphone works well). Kenzy does **not** perform AEC itself, so without it the node hears its own TTS playback — causing false wakes / self-interruption — and you'd have to add echo cancellation outside Kenzy. Set the chosen device with `audio_device` (find the name via `kenzy-devices`).

## Quick install

The fastest way to get a host running is the bootstrapper. It creates a per-user virtualenv under `~/.local/share/kenzy`, installs the services you choose from PyPI, downloads the inference models, scaffolds your config home at `~/.config/kenzy`, and links the `kenzy-*` commands into `~/.local/bin`:

```bash
curl -fsSL https://kenzy.dev/install.sh | bash
```

It prompts for what to install (room node, server stack, or everything). Pass flags after `bash -s --`, or use the matching environment variables, to drive it non-interactively:

```bash
# A room node, unattended (paste the join token from the server's dashboard → Settings)
curl -fsSL https://kenzy.dev/install.sh | bash -s -- --profile node --token <token> --yes
```

!!! tip "The join token"
    A server/all install generates a `discovery.token` and prints it (it's also under
    **Settings → Node provisioning** in the dashboard, with a copy button). Pass that value
    to each room node via `--token` so it can register. Without a matching token the server
    refuses the node — that's the secure default; clear `discovery.token` in `server.yaml`
    only if you deliberately want open joins.

| Flag | Variable | Default | Purpose |
|---|---|---|---|
| `--profile` | `KENZY_PROFILE` | *(prompt)* | `node`, `server`, or `all` — skips the prompt |
| `--no-apt` | `KENZY_NO_APT` | `0` | Don't install system packages (non-Debian hosts) |
| `--package` | `KENZY_PACKAGE` | *(PyPI)* | Install a local wheel/sdist/source dir instead of PyPI |
| `--version` | `KENZY_VERSION` | *(latest ≥3)* | Pin a specific PyPI version |
| `--node-id` | `KENZY_NODE_ID` | *(generated)* | Stable `node_id` for a node install (so its server-side config can be pre-seeded by that id). A generated id is printed when omitted. The room name is set later from the dashboard, not at install. |
| `--token` | `KENZY_TOKEN` | *(generated for server/all)* | Shared join/service token. A server/all install **generates one** (printed, and shown in the dashboard under Settings); on a node install, paste that value so the node can join. Pass the same value to share a token across hosts. |
| `--constraints` | `KENZY_CONSTRAINTS` | *(none)* | A pip constraints file of dependency pins to honor on install **and every future auto-upgrade** (seeds the config home's `constraints.txt`). |
| `--yes` | `KENZY_YES` | `0` | Assume defaults / no prompts (CI) |
| `--home` | `KENZY_HOME` | `~/.config/kenzy` | Config home (configs, skills, data, `.env`) |
| `--venv` | `KENZY_VENV` | `~/.local/share/kenzy/venv` | Virtualenv location |

!!! note "Requires Kenzy ≥ 3.0 on PyPI"
    The installer floors the version at `3.0.0` so it never resolves the legacy 2.x monolith. Default configs, skills, and `.env.example` ship **inside** the package and are scaffolded to your config home by `kenzy-init`; edit the copies under `~/.config/kenzy`, not the package. To trial an unreleased build before it's on PyPI, point `--package` at a locally built wheel.

## Manual installation

Prefer to do it by hand? Kenzy installs from PyPI into a per-user virtualenv — no source checkout required. **Set up the server host first, then add room nodes**: a node discovers the server over mDNS and pulls its configuration on connect, so the server it talks to needs to exist first.

### 1. Server host

Create a virtualenv and install the extras for the backend services this host runs:

```bash
python3 -m venv ~/.local/share/kenzy/venv
source ~/.local/share/kenzy/venv/bin/activate

pip install 'kenzy[server,stt,tts,llm,speaker]'   # full backend stack
```

You can split the backends across machines — install only the extras a given host runs (e.g. `kenzy[server]` on one box and `kenzy[stt,tts,llm,speaker]` on a beefier one). Then scaffold a config home:

```bash
kenzy-init        # writes configs/, skills/, data/, and .env to ~/.config/kenzy
```

### 2. Room node

On each room device, install just the node extra:

```bash
python3 -m venv ~/.local/share/kenzy/venv
source ~/.local/share/kenzy/venv/bin/activate

pip install 'kenzy[node]'
```

A room node needs **no** config home — it discovers the server and pulls its tuning on connect. Set `server_url` (to pin a specific server, e.g. across VLANs) or `audio_device` (non-default hardware) in `node.yaml` only if needed.

!!! note "Requires Kenzy ≥ 3.0 on PyPI"
    `pip install kenzy` resolves the package once the 3.x release is published. To trial an unreleased build, install from a locally built wheel (`pip install ./dist/kenzy-*.whl`) or from source (below).

### Develop from source

To work on Kenzy itself, clone the repo and install it **editable** instead of from PyPI:

```bash
git clone https://github.com/lnxusr1/kenzy.git
cd kenzy
python3 -m venv .venv
source .venv/bin/activate

pip install -e ".[node,server,stt,tts,llm,speaker,dev]"
```

!!! warning "Source installs don't self-upgrade"
    An editable (`-e`) source install is for development. The per-user PyPI install is the supported path for production hosts and is what the dashboard / `pip install -U` upgrade flow expects.

## Download models

After installing the `node` or `speaker` package for the first time, download the required inference models:

```bash
kenzy-setup
```

This downloads the openwakeword feature-extraction models and the SpeechBrain ECAPA-TDNN speaker identification model. It is safe to run multiple times — files that already exist are skipped.

## Identify your audio device

If your node uses a USB speakerphone or any device other than the system default, run:

```bash
kenzy-devices
```

This scans every PortAudio device, tests which sample rates each one supports, and prints ready-to-paste `node.yaml` settings. Example output:

```
[6] Anker PowerConf S330: USB Audio (hw:2,0)
    in=2  out=2  default=48000 Hz
    capture  : 16000 ✗ | 44100 ✗ | 48000 ✓
    playback : 24000 ✗ | 44100 ✗ | 48000 ✓

Suggested node.yaml settings

  [6] Anker PowerConf S330: USB Audio (hw:2,0)
      audio_device: "Anker PowerConf S330"  # resampling: capture 48000→16000 Hz, playback 48000→24000 Hz
      capture_sample_rate: 48000
      playback_sample_rate: 48000
```

Kenzy captures at 16 kHz and plays TTS at 24 kHz. If a device does not natively support those rates, the suggested settings tell the node to open the stream at the device's native rate and resample automatically.

## Configure API keys

`kenzy-init` creates a `.env` in your config home (`~/.config/kenzy/.env`) from the bundled example. (In a source checkout, copy it yourself with `cp .env.example .env`.) Edit it and fill in your credentials:

```bash
OPENAI_API_KEY="sk-..."       # Required for TTS; also for LLM if using OpenAI models
HA_API_KEY="..."               # Home Assistant long-lived access token (home control skill)
```

!!! note
    `.env` is never committed to version control. All services call `load_dotenv()` at startup and read keys from the environment.

## Configure services

Each service reads its settings from a YAML file. For a `pip`/`install.sh` install these live in your **config home** (`~/.config/kenzy/configs/`, scaffolded by `kenzy-init`); in a source checkout they're in the repo's `configs/`. The bundled files contain sensible defaults with comments explaining every key. At minimum you will want to:

1. In `server.yaml`, set the service URLs and your `node_defaults` (node tuning is pushed to nodes on connect)
2. In `llm.yaml`, set your LLM model and location
3. Nodes usually need **no** config — they discover the server via mDNS and pull their tuning. Set `server_url` in `node.yaml` only to pin a specific server (e.g. across VLANs), and `audio_device` for non-default hardware.

See the [Configuration](configuration/index.md) section for a full reference.

## Run the services

Start the server host first, then each room node (or run them as systemd units — see [Deployment](deployment.md)). The config-path argument is optional: each service finds its config in your config home automatically; pass an explicit path (e.g. `kenzy-server configs/server.yaml`) only to override.

```bash
# On the server host (start these first)
kenzy-server
kenzy-stt
kenzy-tts
kenzy-llm
kenzy-speaker

# On each room node (discovers the server and pulls its config)
kenzy-node
```

Say your wake word ("Hey Kenzie") and start talking.

## Next steps

- [Enable the dashboard](dashboard.md) to manage your nodes and services from one web UI
- [Enroll speakers](speaker-enrollment.md) so Kenzy knows who is talking
- [Add skills](skills/writing-skills.md) to extend what Kenzy can do
- [Deploy remotely](deployment.md) to push updates to all your devices at once
