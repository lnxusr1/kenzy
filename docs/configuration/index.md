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
WEATHER_API_KEY="..."
HA_API_KEY="..."
```

Services call `load_dotenv()` at startup. You can also export variables directly in your shell or systemd unit file.

## Service-specific references

- [Node](node.md) — `configs/node.yaml`
- [Server](server.md) — `configs/server.yaml`
- [STT](stt.md) — `configs/stt.yaml`
- [TTS](tts.md) — `configs/tts.yaml`
- [LLM](llm.md) — `configs/llm.yaml`
- [Speaker ID](speaker.md) — `configs/speaker.yaml`
