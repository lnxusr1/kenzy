# Node Configuration

**File:** `configs/node.yaml`  
**Command:** `kenzy-node [config_path]`

The node service runs on each room device. It captures microphone audio, detects the wake word, streams PCM to the server, and plays back TTS responses.

## Full reference

| Key | Default | Description |
|---|---|---|
| `server_url` | `null` | WebSocket URL of the kenzy-server. Leave `null`/empty to **auto-discover** the server on the LAN via mDNS; set an explicit `ws://` URL to skip discovery (e.g. across VLANs that block multicast). |
| `discovery.enabled` | `true` | Browse for the server over mDNS when `server_url` is unset |
| `room_id` | `null` | Unique identifier for this node; `null` defaults to the **hostname**. Used as the key in conversation history and pipeline routing |
| `audio_device` | `null` | PortAudio device name substring or integer index. `null` uses the system default. Use `kenzy-devices` to find the correct value. |
| `capture_sample_rate` | `16000` | Sample rate for microphone capture. Set to the device's native rate if it does not support 16000 Hz; audio is resampled automatically. |
| `playback_sample_rate` | `24000` | Sample rate for speaker output. Set to the device's native rate if it does not support 24000 Hz; TTS audio is resampled automatically. |
| `log_level` | `"info"` | Log verbosity |
| `verbose` | `false` | Also enables debug output from websockets and asyncio internals |

### Wake word

| Key | Default | Description |
|---|---|---|
| `wakeword_models` | `[]` | List of paths to `.tflite` or `.onnx` model files. Empty uses the bundled `hey_kenzie.tflite` |
| `wakeword_threshold` | `0.5` | Confidence threshold [0.0–1.0] above which a detection fires |
| `wakeword_vad_threshold` | `0.0` | openwakeword Silero VAD gate [0.0–1.0]. Wake-word predictions are discarded unless the voice-activity score exceeds this. `0` disables it. Set to ~`0.5` to suppress false detections on near-silence/noise. With it enabled you can safely *lower* `wakeword_threshold` (e.g. `0.4`) for better real-speech sensitivity without reintroducing silence false-positives. The Silero VAD model is downloaded automatically by `kenzy-setup`. |

### Voice activity detection (VAD)

| Key | Default | Description |
|---|---|---|
| `vad_enabled` | `true` | When `false`, the node streams until the server sends `STOP`. Hard cap does not apply. |
| `silence_rms_threshold` | `50` | RMS amplitude [0–32767] below which a frame is considered silent |
| `silence_ms` | `400` | Consecutive silence (ms) that ends an active session, once `speech_min_ms` has been heard |
| `speech_min_ms` | `400` | Minimum speech (ms) that must be detected before silence detection activates. Prevents the session ending on the pause after the wake word. |
| `no_speech_timeout_ms` | `15000` | Timeout (ms) if no speech is heard after activation. Prevents indefinite streaming when the wake word fires accidentally. |
| `hard_cap_ms` | `30000` | Unconditional session ceiling (ms). The session ends regardless of VAD state. |

### Sound files

| Key | Default | Description |
|---|---|---|
| `sound_ready` | `null` | WAV file played on activation (the "chime"). `null` uses the bundled `ready.wav`. Accepts an absolute path or a bare filename loaded from the bundled sounds directory. |
| `sound_waiting` | `null` | WAV file played while waiting for the server response. Plays once and stops naturally or is interrupted when TTS begins. `null` (or an empty string) disables it — pure silence while waiting. Provide a filename or path to enable it. |

!!! note "Zero-config nodes (discovery + config-pull)"
    A node needs almost no local config. With `server_url` unset it finds the server via mDNS, `room_id` defaults to the hostname, and on connect the server **pushes** the node's effective tuning (wake-word thresholds, VAD timing) from its `node_defaults` plus any per-room override in `configs/nodes/<room>.yaml`. Live-tunable keys apply immediately; hardware keys (`audio_device`, sample rates, `wakeword_models`, sounds) stay local and take effect on restart. So in practice a room device only needs its audio hardware settings — everything else is centralised on the server. See [Server Configuration](server.md#discovery-and-config-pull).

!!! tip "Finding the right device name"
    Run `kenzy-devices` after install. It tests every PortAudio device against Kenzy's required sample rates and prints ready-to-paste `node.yaml` settings including `capture_sample_rate` and `playback_sample_rate` if resampling is needed.

!!! tip "Custom wake word"
    Custom wake word models can be trained at [openWakeWord](https://github.com/dscripka/openWakeWord) and pointed to via `wakeword_models`. Both `.tflite` and `.onnx` formats are supported.

## Example

```yaml
server_url: null                      # null = discover the server via mDNS
room_id: "office"                     # null = use the hostname
audio_device: "Anker PowerConf S330"  # substring of name shown by kenzy-devices
capture_sample_rate: 48000            # device native rate; resampled to 16000 Hz
playback_sample_rate: 48000           # device native rate; resampled to 24000 Hz

wakeword_threshold: 0.4         # lower is safe once VAD gating is on
wakeword_vad_threshold: 0.5     # reject wake-word hits on near-silence/noise
silence_rms_threshold: 80
silence_ms: 500

sound_ready: null
sound_waiting: null
```
