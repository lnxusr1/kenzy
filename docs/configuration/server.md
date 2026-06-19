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

### Discovery and config-pull

| Key | Default | Description |
|---|---|---|
| `discovery.enabled` | `true` | Advertise the server as `_kenzy._tcp` over mDNS so nodes auto-discover it without a hardcoded `server_url` |
| `discovery.instance` | `"kenzy-server"` | mDNS instance name |
| `discovery.token` | — | Shared secret required in each node's `hello`; mismatching nodes are rejected. Sets the `auth` flag in the advertisement. |
| `node_defaults` | `{}` | Node tuning defaults (wake-word thresholds, VAD timing) pushed to every node on connect. Per-node overrides live in `configs/nodes/<node_id>.yaml` and shallow-merge over these. |

On connect, a node's `hello` carries its stable `node_id` and its room name; the server replies with the node's **effective config** = `node_defaults` merged with `configs/nodes/<node_id>.yaml`. Live-tunable keys apply immediately on the node; hardware keys (audio device, sample rates, wakeword models, sounds) take effect on restart. The per-node file is keyed by `node_id`, so a node keeps its config even if its room is renamed; pre-existing room-named files migrate automatically on first connect. This is how a room device runs with no local tuning file — see [Node Configuration](node.md).

### Dashboard

Opt-in web fleet manager served by `kenzy-server`. **Off by default**; when disabled nothing is wired up (no route, no overhead). When enabled it provides a live fleet/health view, a per-node config editor (with room rename), node controls (trigger/stop/restart), TTS announcements, a log viewer, and a settings page. See the [Dashboard guide](../dashboard.md) for the full walkthrough.

| Key | Default | Description |
|---|---|---|
| `dashboard.enabled` | `false` | Master switch. `false` ⇒ nothing below is mounted. |
| `dashboard.bind` | `"127.0.0.1"` | Listener address — keep it on localhost or the LAN; do **not** port-forward it (login is plaintext HTTP) |
| `dashboard.port` | `8770` | Dashboard HTTP port (separate from the node WS port) |
| `dashboard.auth.username` / `dashboard.auth.password_hash` | `admin` / *(hash of `password`)* | Browser login. Change it with the server-only **`kenzy-passwd`** CLI (or the dashboard's Settings page); never edit the hash by hand. |
| `dashboard.auth_token` | `null` | Optional bearer token for API/CLI clients (the browser uses the login cookie, not this) |
| `dashboard.controls` | `false` | Enable mutating actions — config edits, room rename, trigger/stop/restart, announcements. `false` ⇒ read-only. |
| `dashboard.logs` | `false` | Enable the pull-based log viewer (server, services, and per-node buffers) |
| `dashboard.tuning` | `false` | Reserved sub-flag for a later phase |

!!! warning "Keep the dashboard off the public internet"
    Login runs over plaintext HTTP on a LAN bind and defaults to `admin` / `password`. Bind it to localhost or the LAN only, change the password with `kenzy-passwd`, and do **not** port-forward the dashboard port.

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

discovery:
  enabled: true
  instance: "kenzy-server"
  # token: "change-me"      # require this in every node's hello

node_defaults:             # pushed to nodes on connect (config-pull)
  wakeword_threshold: 0.5
  silence_rms_threshold: 50
  silence_ms: 400

dashboard:
  enabled: false           # opt-in; nothing is wired up while false
  bind: "127.0.0.1"
  port: 8770

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
