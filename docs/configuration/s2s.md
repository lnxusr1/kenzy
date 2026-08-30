# The Conversation Engine (S2S)

**File:** `configs/s2s.yaml`  
**Command:** `kenzy-s2s [config_path]`

!!! warning "Experimental — off by default"
    This service powers Kenzy's next-generation conversation mode (the
    *follow-up* feature): a realtime speech engine that keeps listening after
    she answers — and while she thinks — so you can talk back and forth
    without repeating the wake word. The server routes conversations through
    it **only when `s2s.enabled` is switched on** (see the
    [server's follow-up section](server.md#follow-up-mode-experimental)) and
    only on nodes with echo-cancelling speakers; everywhere else, and with
    the toggle off, the classic pipeline runs exactly as before.

## What it is

`kenzy-s2s` is an orchestration layer, not another model host. It composes
the services you already run — speech-to-text from the STT service, replies
from a language-model provider, speech from the TTS service — behind one
realtime, streaming session protocol. No model weights load here, and your
voice identity stays whatever the [TTS service](tts.md) is configured to
speak.

## Service basics

| Key | Default | What it does |
|---|---|---|
| `host` | `127.0.0.1` | Bind address. Loopback by default, like every service. |
| `port` | `8771` | The engine's WebSocket port. |
| `log_level` | `info` | Service log verbosity. |

## The model provider

The engine talks **directly to an OpenAI-compatible chat provider** — cloud,
or a model server on your own hardware (vLLM, llama.cpp, a LiteLLM proxy).
This is deliberately a *separate* choice from the
[LLM service's](llm.md) model: the conversation engine wants a model tuned
for realtime latency, and you may well run a different one there.

| Key | Default | What it does |
|---|---|---|
| `provider.base_url` | *(empty)* | OpenAI-compatible endpoint. Empty means OpenAI's API; point it at your own model server to stay local (e.g. `http://127.0.0.1:8000/v1`). |
| `provider.model` | `gpt-5.1` | The model name the provider serves. |
| `provider.auth_env` | `OPENAI_API_KEY` | Which environment variable holds the key. Use `CUSTOM_LLM_API_KEY` with a custom `base_url` — your OpenAI key is never sent to a non-OpenAI endpoint. |
| `provider.temperature` | *(null)* | Sampling temperature. Null = the provider's default (OpenAI's newer models reject any explicit value); set `0.0` for deterministic local serving. |
| `provider.max_output` | `512` | Reply-length ceiling per turn, in tokens. |
| `provider.timeout` | `60.0` | Per-request timeout, seconds. |

```yaml
# Fully local example — a vLLM server on the same box
provider:
  base_url: "http://127.0.0.1:8000/v1"
  model: "qwen-moe"
  auth_env: "CUSTOM_LLM_API_KEY"
```

## Stage services

The speech stages are the STT and TTS services you already run. When they
register with the server, their addresses are **wired in automatically** —
you normally configure nothing here. An explicit `url` wins over the
automatic one (the multi-host escape hatch).

| Key | Default | What it does |
|---|---|---|
| `stt.url` | *(auto-wired)* | The STT service's `/transcribe` endpoint. |
| `stt.timeout` | `30.0` | Transcription timeout, seconds. |
| `tts.url` | *(auto-wired)* | The TTS service's `/speak` endpoint. |
| `tts.timeout` | `30.0` | Synthesis timeout, seconds. |

## Using a cloud realtime engine instead

`kenzy-s2s` is the **default** engine, and the default stays local. The
server can instead point the whole conversation path at OpenAI's Realtime
API — set on the server (not in this file):

```yaml
# server.yaml (or Settings → Backend services)
s2s:
  enabled: true
  profile: openai-realtime   # default: kenzy (this local service)
  # model: gpt-realtime      # optional; this is the profile's default
```

The connection authenticates with `OPENAI_API_KEY` from the server's
environment (a custom endpoint set via `s2s.url` uses `CUSTOM_LLM_API_KEY`
instead — the OpenAI key is never sent to a non-OpenAI host). With the cloud
profile active, this local service isn't used and doesn't need to run.

!!! danger "What the cloud engine hears"
    With `profile: openai-realtime`, **all room audio captured during a
    conversation streams to OpenAI** — the model hears everything said,
    including anything sensitive spoken aloud. There is no pre-screening:
    audio leaves before any transcript exists to check. If someone says a
    password mid-conversation, it reaches the provider as audio. That is
    the trade of a cloud realtime engine; make it knowingly.

    Two things do **not** change: secrets Kenzy *stores* for you (the
    [lockbox](../memory.md)) never enter any model or any cloud
    reply — spoken readback still routes through the local lockbox flow —
    and who-may-do-what stays decided locally: every tool call is checked
    against the speaker's locally-resolved voice identity before it runs.
