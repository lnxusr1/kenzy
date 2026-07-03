# Configuration Overview

**The short version: configure Kenzy from the [dashboard](../dashboard.md), not by
editing files.** Room devices pull their settings from the server, the backend
services pull theirs from the server, and the dashboard edits all of it in one
place — with changes applied live or by a one-click restart. The YAML reference
pages in this section exist so you know what every setting means, not because you're
expected to hand-edit them day to day.

## Where settings actually live

Everything is under your **config home** — `~/.config/kenzy` for a normal install
(`$KENZY_HOME` if you set it; the repo's `configs/` in a source checkout):

| Path | What it holds | Edited by |
|---|---|---|
| `configs/server.yaml` | The server: ports, discovery, dashboard, pipeline | you (file) + a scoped Settings-page editor |
| `configs/nodes/<node_id>.yaml` | Per-room-node settings (mic, thresholds, room name, volume) | the dashboard (node Configure page) |
| `configs/services/<svc>.yaml` | Backend service overrides (models, voices, skills) | the dashboard (Services tab) |
| `configs/node.yaml` | A node's local bootstrap **only** — how to reach the server | you, rarely (the installer writes it) |
| `.env` | API keys and secrets | you (file, on each machine) |
| `skills/` | Your custom skills | you |
| `data/` | Speaker voice profiles, Home Assistant curation | the dashboard / `kenzy-enroll` |

Each service finds its config automatically — run `kenzy-server` with no arguments
and it uses the config home. Passing an explicit path (`kenzy-server
/path/to/server.yaml`) overrides that, which is mainly useful for development.

!!! tip "Why nodes have almost no local config"
    A room device stores only its identity and how to reach the server. Everything
    operational — which microphone, wake-word sensitivity, its room name — is
    stored **on the server** and delivered when the node connects. That's what lets
    you fix a mis-configured device entirely from your browser, and why a reimaged
    device gets its old settings back just by reconnecting.

## API keys and secrets

Secrets never go in YAML files. They live in `.env` in the config home of **the
machine that needs them**:

```bash
OPENAI_API_KEY="sk-..."    # TTS + LLM (with the default OpenAI setup)
HA_API_KEY="..."           # Home Assistant long-lived token (smart-home skill)
```

Services read `.env` at startup — after editing it, restart the affected services
(`systemctl --user restart 'kenzy-*'`). Secrets are never sent to room nodes and
never shown in the dashboard.

## Common keys

Every service supports these:

| Key | Default | Description |
|---|---|---|
| `host` | `"127.0.0.1"` | Bind address for the HTTP/WebSocket listener |
| `port` | *(varies)* | Listen port |
| `log_level` | `"info"` | Console log verbosity: `debug`, `info`, `warning`, `error` |
| `log_capture_level` | `"debug"` | How deep the dashboard's log viewer can see, independent of the console |

## Pinning dependencies (advanced)

If a host needs a specific version of a Python library (e.g. `transformers` for a
particular GPU/driver), pin it in `~/.config/kenzy/constraints.txt` — a standard pip
[constraints file](https://pip.pypa.io/en/stable/user_guide/#constraints-files), one
requirement per line:

```
transformers==4.30.0
numpy<2.0
```

Kenzy honors this file on install **and on every future auto-upgrade**, so an
upgrade can't silently move a pinned package. If a new release genuinely can't
satisfy a pin, the upgrade fails loudly on that host instead of breaking it —
resolve the conflict in this file, then upgrade again.

## Service-by-service reference

- [Node](node.md) — the room device (`node.yaml` + server-pushed settings)
- [Server](server.md) — `configs/server.yaml`
- [STT](stt.md) — speech-to-text (`configs/services/stt.yaml`)
- [TTS](tts.md) — text-to-speech (`configs/services/tts.yaml`)
- [LLM](llm.md) — the language model + skills (`configs/services/llm.yaml`)
- [Speaker ID](speaker.md) — voice identification (`configs/services/speaker.yaml`)
