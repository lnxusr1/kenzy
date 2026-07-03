# STT Configuration

**File:** `configs/stt.yaml`  
**Command:** `kenzy-stt [config_path]`

The STT service accepts POST requests with base64-encoded PCM audio and returns a transcript. Two providers are supported, selected via the `provider` key:

- **`whisper`** (default) — local [faster-whisper](https://github.com/SYSTRAN/faster-whisper); your voice never leaves your network.
- **`openai`** — OpenAI's cloud transcription API; no local model to download or run, which makes it a good fit for underpowered server hardware — at the cost of sending each captured utterance to OpenAI.

!!! note "Pulled from the server"
    `kenzy-stt` pulls this config from the server at boot — it discovers the server via mDNS (or `KENZY_SERVER_URL`) and blocks until it answers, so start the server first. Edit it from the dashboard's **Services** tab (writes `configs/services/stt.yaml` on the server and restarts the service). Passing an explicit path (`kenzy-stt configs/stt.yaml`) loads locally instead — a dev/offline escape hatch. See [central config for backend services](server.md#central-config-for-backend-services).

## Provider selection

| Key | Default | Description |
|---|---|---|
| `provider` | `"whisper"` | Transcription backend: `whisper` (local) or `openai` (cloud) |

Common service keys:

| Key | Default | Description |
|---|---|---|
| `host` | `"127.0.0.1"` | Bind address |
| `port` | `8767` | HTTP port |
| `log_level` | `"info"` | What the service prints to its console |
| `log_capture_level` | `"debug"` | How deep the dashboard log viewer can see (`trace`/`debug`/…), independent of `log_level` |

---

## Whisper provider (local)

Runs entirely on your hardware with no API key — the default, and the recommended path if you care that spoken audio never leaves the box.

| Key | Default | Description |
|---|---|---|
| `whisper.model` | `"tiny"` | Model size: `tiny`, `base`, `small`, `medium`, `large-v2`, `large-v3`. Larger models are more accurate but slower and need more RAM. |
| `whisper.device` | `"cpu"` | Inference device: `cpu` or `cuda` |
| `whisper.compute_type` | `"int8"` | Quantisation: `int8` (fastest on CPU), `float16` (GPU), `float32` (highest quality) |
| `whisper.language` | `"en"` | Language code (e.g. `"en"`, `"fr"`), or `null` for auto-detect |

### Model size guide

| Model | Size | Relative speed | Notes |
|---|---|---|---|
| `tiny` | ~75 MB | Fastest | Good for fast hardware or simple commands |
| `base` | ~145 MB | Fast | Better accuracy, still CPU-friendly |
| `small` | ~460 MB | Moderate | Good balance for a dedicated CPU server |
| `medium` | ~1.5 GB | Slow on CPU | Recommended with a GPU |
| `large-v3` | ~3 GB | Slow | Best accuracy; GPU strongly recommended |

!!! tip "Run STT off the node"
    Don't run STT on a room-node board (Orange Pi Zero 3 / Raspberry Pi 3–5) — run it on a more powerful server and point `stt.url` in `server.yaml` at it. The `tiny` or `base` model on a modern x86 CPU gives acceptable latency.

### Example

```yaml
provider: "whisper"

whisper:
  model: "base"
  device: "cpu"
  compute_type: "int8"
  language: "en"
```

---

## OpenAI provider (cloud)

**Requires:** `OPENAI_API_KEY` in `.env` (the same key the default TTS/LLM setup already uses)

No model download, near-zero CPU/RAM — the whole transcription happens on OpenAI's side. This is the right choice when the server host is underpowered (or you'd rather not budget cores for Whisper), and the accuracy of the `gpt-4o-transcribe` family is excellent.

| Key | Default | Description |
|---|---|---|
| `openai.model` | `"gpt-4o-mini-transcribe"` | Transcription model: `gpt-4o-mini-transcribe`, `gpt-4o-transcribe`, or `whisper-1` |
| `openai.language` | `"en"` | Language code (e.g. `"en"`), or `null` for auto-detect |
| `openai.timeout` | `30.0` | HTTP timeout in seconds |
| `openai.fallback` | `true` | On a cloud failure, silently retry with **local faster-whisper** (loaded lazily on first need, using the `whisper.*` settings). Note: works offline only if the whisper model was previously downloaded/cached; otherwise the failure surfaces as the error cue. |

!!! warning "Your voice leaves the network"
    With this provider, **everything captured after the wake word is sent to OpenAI** for transcription (audio only — nothing is recorded between wake words either way). If keeping spoken audio on your own hardware matters to you, stay on the default `whisper` provider. This is the same trade the default OpenAI TTS/LLM setup already makes for text.

### Example

```yaml
provider: "openai"

openai:
  model: "gpt-4o-mini-transcribe"
  language: "en"
```

Switching provider is also a two-click change in the dashboard: **Services → stt**, pick the provider from the dropdown, Save (the service restarts and re-pulls its config).
