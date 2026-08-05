# Installation Reference

Detail you don't need for a normal setup — [Getting Started](getting-started.md)
covers that. This page is for unattended installs, non-Debian hosts, splitting
the backend across machines, and working from a source checkout.

## Installer options

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
| `--tls` / `--no-tls` | `KENZY_TLS` | on (server/all) | Server/all installs ask whether to [enable TLS](configuration/server.md#tls-configuration) — node audio (wss), the dashboard (https), and the backend services all encrypt — with a **generated self-signed cert**. The interactive prompt defaults to yes; `--yes`/no TTY also enables it. Use `--no-tls` for plaintext. |
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

## Manual installation

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
