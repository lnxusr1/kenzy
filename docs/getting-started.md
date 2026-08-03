# Getting Started

Kenzy sets up in **parts** — each one short, each one ending with something
working. Do Part 1 today and stop there happily; come back for the rest
whenever you're ready.

1. **[Part 1 — First Conversation](guides/part1-first-conversation.md)** —
   one computer, one command, and a spoken answer to "hey Kenzie, what time
   is it?" *(under an hour)*
2. **[Part 2 — People & Rooms](guides/part2-rooms-and-people.md)** — a device
   in each room, and Kenzy knowing who's talking (which unlocks per-person
   memory).
3. **[Part 3 — Home Assistant Basics](guides/part3-home-assistant.md)** —
   "turn off the kitchen lights": voice control of your smart home.
4. **[Part 4 — Home Assistant, the Works](guides/part4-ha-complete.md)** —
   Kenzy on your phone in her own voice, and Kenzy's rooms inside HA.

If anything doesn't behave the way a step says it should, jump to
[Troubleshooting](troubleshooting.md) — it's organized by symptom.

## What you'll need

- **A Linux computer to be the "brain."** Debian or Ubuntu with Python 3.11+, and
  some horsepower — a desktop, mini-PC, or home server is ideal. (A Raspberry Pi is
  great as a *room device* later, but it's not enough to run the speech and language
  services well.)
- **A microphone and speaker.** Strongly recommended: a **USB conference
  speakerphone** (e.g. an Anker PowerConf or similar). These have built-in echo
  cancellation (AEC), which matters — it's what lets Kenzy hear you while she's
  making sound. A webcam mic plus desktop speakers will limp along for a first
  test, but a speakerphone is the single best purchase for this project. Using a
  speaker **without** echo cancellation? Calibration detects this and sets
  `hardware_aec: false` for that room automatically (or set it yourself)
  and Kenzy adapts honestly — intercom and alarm ring-loops are disabled there
  (with a polite spoken explanation) instead of misbehaving; everything else
  works normally. See [rooms without echo cancellation](configuration/node.md#rooms-without-echo-cancellation-hardware_aec-false).
- **A language model — Kenzy's "brain."** Kenzy needs a large language model
  (LLM) to do the thinking. It can come from a compatible third-party service
  or a locally hosted platform — **OpenAI is just the default** (quickest to a
  working setup: one key, no downloads, runs beside everything else on a small
  box), but Ollama (fully local), Claude, OpenRouter, and others work too —
  see [Setting Up the Language Model](configuration/llm.md). A third-party
  service means an API key from that provider; for the OpenAI default that's
  [platform.openai.com](https://platform.openai.com). Running locally is kind
  of the point of Kenzy — switch whenever you're ready:
  [Running Fully Local](fully-local.md).
- **Optional:** [Home Assistant](https://www.home-assistant.io/), if you want voice
  control of your smart home. You can add it any time.

!!! note "Nothing here takes over your computer"
    The installer puts everything in your own user folder (`~/.local/share/kenzy`
    and `~/.config/kenzy`) plus a few standard system packages via `apt`. It doesn't
    need root for Kenzy itself, and `install.sh --uninstall` removes it cleanly.


All set? Great! — **[Continue](guides/part1-first-conversation.md)** to **[Part 1 — First Conversation](guides/part1-first-conversation.md)**.

---

## Reference: installer options

Everything below this line is reference material — you don't need it for a normal
setup.

The installer prompts for what it needs; pass flags after `bash -s --` (or set the
matching environment variables) to run it unattended:

```bash
# A room node, no prompts
curl -fsSL https://kenzy.ai/install.sh | bash -s -- --profile node --token <token> --yes
```

| Flag | Variable | Default | Purpose |
|---|---|---|---|
| `--profile` | `KENZY_PROFILE` | *(prompt)* | `node`, `server`, or `all` — skips the prompt |
| `--token` | `KENZY_TOKEN` | *(generated for server/all)* | Shared join/service token. A server/all install **generates one** (printed, and shown in the dashboard under Settings); on a node install, paste that value so the node can join. Pass the same value to share a token across hosts. |
| `--node-id` | `KENZY_NODE_ID` | *(generated)* | Stable `node_id` for a node install (so its server-side config can be pre-seeded by that id). A generated id is printed when omitted. The room name is set later from the dashboard, not at install. |
| `--no-apt` | `KENZY_NO_APT` | `0` | Don't install system packages (non-Debian hosts) |
| `--version` | `KENZY_VERSION` | *(latest ≥3)* | Pin a specific PyPI version |
| `--package` | `KENZY_PACKAGE` | *(PyPI)* | Install a local wheel/sdist/source dir instead of PyPI |
| `--constraints` | `KENZY_CONSTRAINTS` | *(none)* | A pip constraints file of dependency pins to honor on install **and every future auto-upgrade** (seeds the config home's `constraints.txt`) |
| `--llm` | `KENZY_LLM` | *(ask)* | Server/all installs ask where the "thinking" happens — `openai` (quick-start default), `claude`, `ollama` (fully local), or `skip` (decide in the dashboard). This flag answers without the prompt; `--yes`/no-TTY defaults to `openai` |
| `--llm-model` | `KENZY_LLM_MODEL` | *(per choice)* | Override the model string, LiteLLM format (e.g. `anthropic/claude-sonnet-5`, `ollama/qwen3:8b`) |
| `--llm-url` | `KENZY_LLM_URL` | `http://127.0.0.1:11434` | The model server's URL (Ollama choice) |
| `--local-voice` | `KENZY_LOCAL_VOICE` | *(ask when non-OpenAI brain)* | Use the local Kokoro voice — installs the `kokoro` extra + `espeak-ng` and sets the tts service's provider; nothing spoken leaves your network |
| `--no-media-keys` | `KENZY_MEDIA_KEYS=0` | on (node/all) | Skip [speakerphone volume button](configuration/node.md#speakerphone-volume-buttons) support. On by default because the recommended node is a USB speakerphone and those have volume keys: the installer adds the `mediakeys` extra (+ `gcc`; evdev builds from source) and puts you in the `input` group (**reboot or re-login before enabling**). Opt out if you don't want a compiler or `/dev/input` access on this host. A failed evdev build only warns — the rest of the install still succeeds. The feature itself stays off until it's enabled on the node — easiest from the audio setup wizard |
| `--tls` / `--no-tls` | `KENZY_TLS` | *(ask)* | Server/all installs ask whether to [enable TLS](configuration/server.md#tls-optional) — node audio (wss), the dashboard (https), and the backend services all encrypt — with a **generated self-signed cert**; these flags answer without the prompt. **TLS is on by default** (including under `--yes`); use `--no-tls` for plaintext. |
| `--tls-cert` / `--tls-key` | `KENZY_TLS_CERT` / `KENZY_TLS_KEY` | *(generated)* | Use your own certificate pair instead of generating one (both required; implies `--tls`) |
| `--no-service` | — | — | Install + config only; skip the systemd units |
| `--yes` | `KENZY_YES` | `0` | Assume defaults / no prompts (CI) |
| `--home` | `KENZY_HOME` | `~/.config/kenzy` | Config home (configs, skills, data, `.env`) |
| `--venv` | `KENZY_VENV` | `~/.local/share/kenzy/venv` | Virtualenv location |
| `--uninstall` | — | — | Remove the services, venv, and commands (`--purge` also removes the config home) |

!!! tip "The join token, in one paragraph"
    A server install generates a `discovery.token` — a shared secret each node
    presents when it connects. It's shown under **Settings → Node provisioning** in
    the dashboard (copy button). Without a matching token the server refuses the
    node; that's the secure default. Clear `discovery.token` in `server.yaml` only
    if you deliberately want open joins.

## Reference: manual installation

Prefer to do it by hand, without the installer script? Kenzy installs from PyPI into
a per-user virtualenv — no source checkout required. **Set up the server host first,
then add room nodes**: a node discovers the server and pulls its configuration on
connect, so the server needs to exist first.

On Raspberry Pi OS / Debian, install PortAudio first (the installer script normally
does this for you):

```bash
sudo apt-get install libportaudio2 portaudio19-dev
```

### Server host

```bash
python3 -m venv ~/.local/share/kenzy/venv
source ~/.local/share/kenzy/venv/bin/activate

pip install 'kenzy[server,stt,tts,llm,speaker]'   # full backend stack
kenzy-init          # scaffold the config home (~/.config/kenzy): configs, skills, .env
kenzy-setup         # download the inference models
```

You can split the backends across machines — install only the extras a given host
runs (e.g. `kenzy[server]` on one box and `kenzy[stt,tts,llm,speaker]` on a beefier
one). Add your API keys to `~/.config/kenzy/.env`, then start the services (server
first):

```bash
kenzy-server
kenzy-stt
kenzy-tts
kenzy-llm
kenzy-speaker
```

The server reads its config from your config home; the backend services
(stt/tts/llm/speaker) **pull theirs from the server** at boot — start the
server first. Pass an explicit path (e.g. `kenzy-stt configs/stt.yaml`) only
to force a local file (dev/offline). For systemd units and fleet management,
see [Deployment](deployment.md).

### Room node

```bash
python3 -m venv ~/.local/share/kenzy/venv
source ~/.local/share/kenzy/venv/bin/activate

pip install 'kenzy[node]'
kenzy-setup
kenzy-node
```

A room node needs **no** config home — it discovers the server over mDNS and pulls
its settings on connect. If your server requires a join token (the default for
installer-built servers), put it in the node's `node.yaml`, or scaffold one with
`kenzy-init --profile node --token <token>`. Set `server_url` only to pin a specific
server (e.g. across VLANs that block mDNS).

### Identify your audio device

If the node should use a device other than the system default, either pick it in the
dashboard (Configure → **Set up / calibrate audio…** — no terminal needed) or run
the scanner on the box:

```bash
kenzy-devices
```

It tests every audio device against Kenzy's required sample rates and prints
ready-to-paste settings, including resampling rates when a device doesn't natively
support 16 kHz capture / 24 kHz playback.

### Develop from source

To work on Kenzy itself, clone the repo and install it **editable** instead of from
PyPI:

```bash
git clone https://github.com/lnxusr1/kenzy.git
cd kenzy
python3 -m venv .venv
source .venv/bin/activate

pip install -e ".[node,server,stt,tts,llm,speaker,dev]"
```

!!! warning "Source installs don't self-upgrade"
    An editable (`-e`) source install is for development. The per-user PyPI install
    is the supported path for production hosts and is what the dashboard /
    `pip install -U` upgrade flow expects.
