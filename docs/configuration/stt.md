# Setting Up Speech Recognition

**File:** `configs/stt.yaml`  
**Command:** `kenzy-stt [config_path]`

This service is Kenzy's ears — audio in, transcript out. **This is the one
service that defaults to local**, and for most setups there is nothing to
change here.

## Local or cloud?

|  | **Local** ([faster-whisper](https://github.com/SYSTRAN/faster-whisper)) | **Cloud** (OpenAI) |
|---|---|---|
| **What it needs** | A model download (~75 MB for `tiny`) and some CPU. A GPU helps but isn't required. | An API key. No download, near-zero CPU. |
| **What leaves your network** | Nothing. **Your voice never leaves.** | Every captured utterance — the audio after the wake word. |
| **What it costs** | CPU time on your server. | Per-minute billing from the provider. |

**The default is local**, and it's the recommended path if you care that spoken
audio stays on your hardware. The cloud option exists for two honest reasons:
your server is too light to transcribe quickly (a lone Raspberry Pi), or you
want to *rule out* local transcription while troubleshooting accuracy — switch,
test, switch back.

Deciding this across *all* the services at once? Start at
[Running Fully Local](../fully-local.md).

## Set it up

Switching is also a dropdown in the dashboard (**Fleet → stt**).

=== "Local (default)"

    ```yaml
    provider: "whisper"

    whisper:
      model: "base"          # tiny/base/small/medium/large-v3
      device: "cpu"          # or "cuda"
      compute_type: "int8"
      language: "en"
    ```

    **Requires:** no API key. The model downloads on first use.

    Which size to pick is the [model size guide](#model-size-guide); running on
    an NVIDIA card is [GPU (CUDA)](#gpu-cuda). Both are below.

    !!! tip "Don't run this on a room node"
        Keep speech recognition off the Pi-class boards that run rooms — put it
        on a more capable server and point `stt.url` in `server.yaml` at it.
        `tiny` or `base` on a modern x86 CPU gives acceptable latency.

=== "Cloud"

    ```yaml
    provider: "openai"

    openai:
      model: "gpt-4o-mini-transcribe"
      language: "en"
    ```

    **Requires:** `OPENAI_API_KEY` in `~/.config/kenzy/.env` — the same key the
    default voice and language-model setup already uses.

    !!! warning "Your voice leaves the network"
        With this provider, **everything captured after the wake word is sent
        to OpenAI** for transcription. (Audio only, and nothing is recorded
        between wake words either way.) If keeping spoken audio on your own
        hardware matters to you, stay on the default.

!!! note "Pulled from the server"
    `kenzy-stt` pulls this config from the server at boot — it discovers the server via mDNS (or `KENZY_SERVER_URL`) and blocks until it answers, so start the server first. Edit it from the dashboard's **Services** tab (writes `configs/services/stt.yaml` on the server and restarts the service). Passing an explicit path (`kenzy-stt configs/stt.yaml`) loads locally instead — a dev/offline escape hatch. See [central config for backend services](server.md#central-config-for-backend-services).

## Advanced

Everything below is the full reference.

### Provider selection

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

### Whisper provider (local)

Runs entirely on your hardware with no API key — the default, and the recommended path if you care that spoken audio never leaves the box.

| Key | Default | Description |
|---|---|---|
| `whisper.model` | `"tiny"` | Model size: `tiny`, `base`, `small`, `medium`, `large-v2`, `large-v3`. Larger models are more accurate but slower and need more RAM. |
| `whisper.device` | `"cpu"` | Inference device: `cpu` or `cuda` |
| `whisper.compute_type` | `"int8"` | Quantisation: `int8` (fastest on CPU), `float16` (GPU), `float32` (highest quality) |
| `whisper.language` | `"en"` | Language code (e.g. `"en"`, `"fr"`), or `null` for auto-detect |

### GPU (CUDA)

Set `whisper.device: cuda` to run inference on an NVIDIA GPU. Two things to know:

- **Current builds need cuDNN 9.** The `ctranslate2` engine (pulled in by
  `faster-whisper`) switched from cuDNN 8 to cuDNN 9 at version 4.5 — so a
  Kenzy **upgrade** can move you onto a wheel that no longer matches your
  installed CUDA libraries. The failure is sneaky: startup looks fine and
  `/health` is green, but every transcription fails (the GPU is first touched
  at inference time). Kenzy logs the error and **falls back to CPU
  automatically** (one loud log line; `/health` shows `device_fallback: true`)
  so your voice pipeline keeps working while you fix it.
- **The easy fix is pip.** Install the NVIDIA runtimes straight into Kenzy's
  venv — no `LD_LIBRARY_PATH`, no unit edits; Kenzy preloads them itself:

  ```bash
  ~/.local/share/kenzy/venv/bin/pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
  ```

  Then restart the stt service (dashboard → Fleet → stt → Restart).

  If the TTS service on the same host uses **kokoro** (PyTorch), torch has
  usually already installed these wheels into the shared venv — in that case
  GPU STT needs no extra installs at all, and both services run on the same
  runtime libraries.

Alternatively, pin the old engine in your config home's `constraints.txt`
(`ctranslate2<4.5` and `faster-whisper<1.1`) — Kenzy honors it on every
install and upgrade, so the pin sticks.

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

---

### OpenAI provider (cloud)

No model download, near-zero CPU/RAM — the whole transcription happens on OpenAI's side. This is the right choice when the server host is underpowered (or you'd rather not budget cores for Whisper), and the accuracy of the `gpt-4o-transcribe` family is excellent.

| Key | Default | Description |
|---|---|---|
| `openai.model` | `"gpt-4o-mini-transcribe"` | Transcription model: `gpt-4o-mini-transcribe`, `gpt-4o-transcribe`, or `whisper-1` |
| `openai.language` | `"en"` | Language code (e.g. `"en"`), or `null` for auto-detect |
| `openai.timeout` | `30.0` | HTTP timeout in seconds |
| `openai.fallback` | `true` | On a cloud failure, silently retry with **local faster-whisper** (loaded lazily on first need, using the `whisper.*` settings). Note: works offline only if the whisper model was previously downloaded/cached; otherwise the failure surfaces as the error cue. |

!!! warning "Your voice leaves the network"
    With this provider, **everything captured after the wake word is sent to OpenAI** for transcription (audio only — nothing is recorded between wake words either way). If keeping spoken audio on your own hardware matters to you, stay on the default `whisper` provider. This is the same trade the default OpenAI TTS/LLM setup already makes for text.


Switching provider is also a two-click change in the dashboard: **Fleet → stt**, pick the provider from the dropdown, Save (the service restarts and re-pulls its config).


#### Wyoming listener (Home Assistant voice pipelines)

Expose this service as a native HA **speech-to-text** provider, so the HA
pipeline transcribes through Kenzy's STT — one whisper/cloud setup for the
whole house, fallback chain included — see
[On Your Phone](../phone.md) for the full setup.

| Key | Default | Description |
|---|---|---|
| `wyoming.enabled` | `false` | Start the [Wyoming protocol](https://github.com/rhasspy/wyoming) listener alongside the HTTP service. Uses the exact same transcription path (provider, model, fallback chain) as `/transcribe`; incoming audio is converted to 16 kHz mono as needed. Requires the `wyoming` package (included in the `stt` extra). |
| `wyoming.port` | `10300` | Listener port (the whisper convention, so HA operators guess right). |

Wyoming is plain, unauthenticated TCP — the listener follows the service
bind, so it stays loopback-only unless you've deliberately opened the
service to the LAN (`KENZY_BIND=0.0.0.0` / `--listen-all`).
