# Configuration Overview

All configuration lives in the `configs/` directory. Each service accepts an optional path argument; if omitted, it looks for the file in `configs/<service>.yaml` relative to the working directory.

```bash
kenzy-server                        # reads configs/server.yaml
kenzy-server /etc/kenzy/server.yaml # reads a custom path
```

## Common keys

Every service supports these top-level keys:

| Key | Default | Description |
|---|---|---|
| `host` | `"127.0.0.1"` | Bind address for the HTTP/WebSocket listener |
| `port` | *(varies)* | Listen port |
| `log_level` | `"info"` | Log verbosity: `debug`, `info`, `warning`, `error` |

## Environment variables

API keys and secrets are never stored in YAML files. Copy `.env.example` to `.env` and populate it:

```bash
OPENAI_API_KEY="sk-..."
HA_API_KEY="..."
```

Services call `load_dotenv()` at startup. You can also export variables directly in your shell or systemd unit file.

## Pinning dependencies

If a host needs a specific version of a library (e.g. `transformers` for a particular GPU/driver or model), pin it in `~/.config/kenzy/constraints.txt` — a standard pip [constraints file](https://pip.pypa.io/en/stable/user_guide/#constraints-files), one requirement per line:

```
transformers==4.30.0
numpy<2.0
```

Kenzy honors this file on install (`kenzy-init` scaffolds it; `install.sh --constraints FILE` seeds it) **and on every future auto-upgrade**, so an upgrade can't silently move a pinned package. If a new release genuinely can't satisfy a pin, the upgrade fails loudly on that host instead of breaking it — resolve the conflict in this file, then upgrade again.

## Service-specific references

- [Node](node.md) — `configs/node.yaml`
- [Server](server.md) — `configs/server.yaml`
- [STT](stt.md) — `configs/stt.yaml`
- [TTS](tts.md) — `configs/tts.yaml`
- [LLM](llm.md) — `configs/llm.yaml`
- [Speaker ID](speaker.md) — `configs/speaker.yaml`
