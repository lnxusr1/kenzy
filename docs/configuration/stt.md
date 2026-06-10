# STT Configuration

**File:** `configs/stt.yaml`  
**Command:** `kenzy-stt [config_path]`

The STT service accepts POST requests with base64-encoded PCM audio and returns a transcript. It is built on [faster-whisper](https://github.com/SYSTRAN/faster-whisper), a CTranslate2-optimized implementation of OpenAI Whisper.

## Full reference

| Key | Default | Description |
|---|---|---|
| `host` | `"127.0.0.1"` | Bind address |
| `port` | `8767` | HTTP port |
| `log_level` | `"info"` | Log verbosity |

### Whisper model

| Key | Default | Description |
|---|---|---|
| `whisper.model` | `"tiny"` | Model size: `tiny`, `base`, `small`, `medium`, `large-v2`, `large-v3`. Larger models are more accurate but slower and need more RAM. |
| `whisper.device` | `"cpu"` | Inference device: `cpu` or `cuda` |
| `whisper.compute_type` | `"int8"` | Quantisation: `int8` (fastest on CPU), `float16` (GPU), `float32` (highest quality) |
| `whisper.language` | `"en"` | Language code (e.g. `"en"`, `"fr"`), or `null` for auto-detect |

## Model size guide

| Model | Size | Relative speed | Notes |
|---|---|---|---|
| `tiny` | ~75 MB | Fastest | Good for fast hardware or simple commands |
| `base` | ~145 MB | Fast | Better accuracy, still CPU-friendly |
| `small` | ~460 MB | Moderate | Good balance for a dedicated CPU server |
| `medium` | ~1.5 GB | Slow on CPU | Recommended with a GPU |
| `large-v3` | ~3 GB | Slow | Best accuracy; GPU strongly recommended |

!!! tip "Raspberry Pi"
    On a Pi Zero 2 W, run the STT service on a more powerful server and point `stt.url` in `server.yaml` at it. The `tiny` or `base` model on a modern x86 CPU gives acceptable latency.

## Example

```yaml
host: "127.0.0.1"
port: 8767

whisper:
  model: "base"
  device: "cpu"
  compute_type: "int8"
  language: "en"
```
