# Server Configuration

**File:** `configs/server.yaml`  
**Command:** `kenzy-server [config_path]`

The server is the central WebSocket hub. It accepts connections from room nodes, runs the STT → LLM → TTS pipeline, and streams audio responses back. Each downstream service is optional — omit its `url` to disable that stage.

## Full reference

| Key | Default | Description |
|---|---|---|
| `host` | `"0.0.0.0"` | Bind address. `0.0.0.0` listens on all interfaces. |
| `port` | `8765` | WebSocket port |
| `log_level` | `"info"` | Log verbosity |

### STT service

| Key | Default | Description |
|---|---|---|
| `stt.url` | — | URL of the kenzy-stt `/transcribe` endpoint. Omit or set to `null` to skip transcription. |
| `stt.timeout` | `60.0` | HTTP timeout in seconds |

### Speaker identification service

| Key | Default | Description |
|---|---|---|
| `speaker.url` | — | URL of the kenzy-speaker `/identify` endpoint. Omit to disable speaker ID. |
| `speaker.timeout` | `10.0` | HTTP timeout in seconds |
| `speaker.unknown_speaker` | `"unknown"` | Name used when no enrolled speaker is identified |

### LLM service

| Key | Default | Description |
|---|---|---|
| `llm.url` | — | URL of the kenzy-llm `/process` endpoint. Omit to disable LLM processing. |
| `llm.timeout` | `30.0` | HTTP timeout in seconds |

### TTS service

| Key | Default | Description |
|---|---|---|
| `tts.url` | — | URL of the kenzy-tts `/speak` endpoint. Omit to disable TTS. |
| `tts.timeout` | `60.0` | HTTP timeout in seconds |
| `tts.chunk_size` | `4096` | Bytes per PCM chunk streamed to the node. At 24 kHz int16 mono, 4096 bytes ≈ 85 ms of audio. |

## Example

```yaml
host: "0.0.0.0"
port: 8765

stt:
  url: "http://127.0.0.1:8767/transcribe"
  timeout: 60.0

speaker:
  url: "http://127.0.0.1:8768/identify"
  timeout: 10.0
  unknown_speaker: "unknown"

llm:
  url: "http://127.0.0.1:8766/process"
  timeout: 30.0

tts:
  url: "http://127.0.0.1:8769/speak"
  timeout: 60.0
  chunk_size: 4096
```

!!! note "Disabling stages"
    You can run a partial pipeline for development. For example, omit `llm.url` and `tts.url` to transcribe audio and log the results without generating responses.
