# Node Configuration

**File:** `configs/node.yaml`  
**Command:** `kenzy-node [config_path]`

The node service runs on each room device. It captures microphone audio, detects the wake word, streams PCM to the server, and plays back TTS responses.

## Full reference

| Key | Default | Description |
|---|---|---|
| `server_url` | `"ws://127.0.0.1:8765"` | WebSocket URL of the kenzy-server |
| `room_id` | `"living_room"` | Unique identifier for this node; used as the key in conversation history and pipeline routing |
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
| `sound_waiting` | `null` | WAV file played while waiting for the server response. Plays once and stops naturally or is interrupted when TTS begins. `null` uses the bundled `waiting.wav`. Set to an empty string to disable. |

!!! tip "Finding the right device name"
    Run `kenzy-devices` after install. It tests every PortAudio device against Kenzy's required sample rates and prints ready-to-paste `node.yaml` settings including `capture_sample_rate` and `playback_sample_rate` if resampling is needed.

!!! tip "Custom wake word"
    Custom wake word models can be trained at [openWakeWord](https://github.com/dscripka/openWakeWord) and pointed to via `wakeword_models`. Both `.tflite` and `.onnx` formats are supported.

## Example

```yaml
server_url: "ws://192.168.1.100:8765"
room_id: "office"
audio_device: "Anker PowerConf S330"  # substring of name shown by kenzy-devices
capture_sample_rate: 48000            # device native rate; resampled to 16000 Hz
playback_sample_rate: 48000           # device native rate; resampled to 24000 Hz

wakeword_threshold: 0.6
silence_rms_threshold: 80
silence_ms: 500

sound_ready: null
sound_waiting: null
```
