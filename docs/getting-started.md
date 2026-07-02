# Getting Started

By the end of this page you'll say **"hey Kenzie"** to a device in your home and get
a spoken answer back. The path of least resistance:

1. [Set up everything on one computer](#step-1-install-everything-on-one-computer) and have your first conversation.
2. [Add a room device](#step-4-add-a-room-node-optional) (a Raspberry Pi with a speakerphone) whenever you're ready — the one-computer setup keeps working either way.

If anything doesn't behave the way a step says it should, jump to
[Troubleshooting](troubleshooting.md) — it's organized by symptom.

## What you'll need

- **A Linux computer to be the "brain."** Debian or Ubuntu with Python 3.11+, and
  some horsepower — a desktop, mini-PC, or home server is ideal. (A Raspberry Pi is
  great as a *room device* later, but it's not enough to run the speech and language
  services well.)
- **A microphone and speaker.** Strongly recommended: a **USB conference
  speakerphone** (e.g. an Anker PowerConf or similar). These have built-in echo
  cancellation, which matters — without it, Kenzy hears its own voice through the
  speaker and can interrupt itself. A webcam mic plus desktop speakers will limp
  along for a first test, but a speakerphone is the single best purchase for this
  project.
- **An OpenAI API key** (from [platform.openai.com](https://platform.openai.com)).
  The default setup uses OpenAI for the "thinking" and the voice — the easiest way
  to start. You can switch to other providers, or to fully local models, later
  ([LLM](configuration/llm.md), [TTS](configuration/tts.md)).
- **Optional:** [Home Assistant](https://www.home-assistant.io/), if you want voice
  control of your smart home. You can add it any time.

!!! note "Nothing here takes over your computer"
    The installer puts everything in your own user folder (`~/.local/share/kenzy`
    and `~/.config/kenzy`) plus a few standard system packages via `apt`. It doesn't
    need root for Kenzy itself, and `install.sh --uninstall` removes it cleanly.

## Step 1 — Install everything on one computer

Plug in your speakerphone first (so setup can find it), then run:

```bash
curl -fsSL https://kenzy.dev/install.sh | bash
```

When it asks what to install, choose **all** (everything on this machine). The
installer then:

- creates a private Python environment and installs Kenzy from PyPI,
- downloads the wake-word and voice-identification models,
- creates your **config home** at `~/.config/kenzy` (all your settings live there),
- sets the services to start automatically, and starts them now.

It prints a **join token** near the end — that's the password room devices will use
to connect later. Don't worry about copying it; the dashboard can show it to you
any time.

## Step 2 — Add your OpenAI key

Open the settings file the installer created:

```bash
nano ~/.config/kenzy/.env
```

Find the `OPENAI_API_KEY` line, paste your key between the quotes, save (in nano:
`Ctrl+O`, `Enter`, `Ctrl+X`), then restart the services so they pick it up:

```bash
systemctl --user restart 'kenzy-*'
```

## Step 3 — Open the dashboard and say hello

In a browser on the same network, open:

```
http://localhost:8770        (or http://<the-computer's-IP>:8770 from another machine)
```

Log in with **`admin` / `password`**.

!!! note "✓ Checkpoint"
    You should see the **fleet view**: a card for this computer's room node, and
    health indicators for the four backend services (STT, TTS, LLM, Speaker) — all
    green within a minute or so of starting. If a service shows unhealthy or the
    page won't load, see [Troubleshooting](troubleshooting.md#a-service-shows-unhealthy-in-the-dashboard).

Two things to do while you're here:

1. **Change the password** — Settings → change password. The dashboard is reachable
   from your whole network by default, so don't leave it on the default login.
2. **Name your room** — open the node's **Configure** page and set its room name
   (e.g. "office"). Kenzy uses room names in conversation and for announcements.

Now talk to it. Say:

> **"Hey Kenzie"** … *(you'll hear a chime)* … **"what time is it?"**

!!! note "✓ Checkpoint"
    Chime after the wake word, spoken answer a moment later. If there's no chime,
    see [Kenzy doesn't hear the wake word](troubleshooting.md#kenzy-doesnt-hear-the-wake-word);
    if it chimes but never answers, see
    [It hears me but never replies](troubleshooting.md#it-hears-me-but-never-replies).

If the wake word feels deaf or trigger-happy, run the guided tuner: the node's
Configure page → **Set up / calibrate audio…**. It measures your actual room and mic
and applies the right thresholds — much better than guessing numbers
([details](dashboard.md#calibrating-a-nodes-audio)).

**That's a working Kenzy.** Everything below is optional.

## Step 4 — Add a room node (optional)

A room node is a small device whose only job is to listen and speak — an
**Orange Pi Zero 3 / 2W** or **Raspberry Pi 3/4/5** with a USB speakerphone. The
thinking still happens on your server.

1. Set up the board with its standard OS (Raspberry Pi OS Lite is fine), connected
   to the same network.
2. In the dashboard, go to **Settings → Node provisioning** and copy the **join
   token**.
3. On the new device, run:

    ```bash
    curl -fsSL https://kenzy.dev/install.sh | bash -s -- --profile node --token PASTE_TOKEN_HERE
    ```

That's the whole install. The node finds your server on the network by itself,
connects with the token, and pulls all its settings from the server — there's
nothing to configure on the device.

!!! note "✓ Checkpoint"
    Within a minute the new node appears as a card in the dashboard's fleet view.
    Open its **Configure** page to name its room, then run **Set up / calibrate
    audio…** to pick its microphone and tune it — all from your browser. Then walk
    over and say "hey Kenzie."

Repeat for as many rooms as you like. With more than one room you can try:

> "Hey Kenzie… **tell everyone dinner's ready**" (speaks in every room)
>
> "Hey Kenzie… **call the living room**" (live intercom — the other room says "yes" to accept)

## Step 5 — Point it at your smart home (optional)

If you run Home Assistant, give Kenzy a long-lived access token (`HA_API_KEY` in
`~/.config/kenzy/.env`) and set your HA URL — then "turn on the kitchen lights" just
works, using the names and rooms you already have in HA. The full walkthrough,
including giving devices natural spoken names, is at
[Home Assistant](skills/home-assistant.md).

## Next steps

- [Enroll voices](speaker-enrollment.md) so Kenzy knows who's talking
- [Explore the dashboard](dashboard.md) — updates, logs, activity view, per-room tuning
- [See what it can do](skills/builtin.md) — weather, news, announcements, and more
- [Write a skill](skills/writing-skills.md) — add abilities with a small Python file
- [Go local](configuration/llm.md#model-strings) — run the language model on your own
  hardware with Ollama, and the voice with [Kokoro](configuration/tts.md#kokoro-provider)

---

## Reference: installer options

Everything below this line is reference material — you don't need it for a normal
setup.

The installer prompts for what it needs; pass flags after `bash -s --` (or set the
matching environment variables) to run it unattended:

```bash
# A room node, no prompts
curl -fsSL https://kenzy.dev/install.sh | bash -s -- --profile node --token <token> --yes
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

Each service finds its config in your config home automatically; pass an explicit
path (e.g. `kenzy-server configs/server.yaml`) only to override. For systemd units
and fleet management, see [Deployment](deployment.md).

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
