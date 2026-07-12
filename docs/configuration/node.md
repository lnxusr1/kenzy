# Node Configuration

**File:** `configs/node.yaml`  
**Command:** `kenzy-node [config_path]`

The node service runs on each room device. It captures microphone audio, detects the wake word, streams PCM to the server, and plays back TTS responses.

`node.yaml` is **bootstrap-only**: it holds just what the node needs to start up, log, and reach the server (`log_level`/`verbose`, `server_url`/`discovery`, and a stable `node_id`). On every boot the node **pulls its full operational config from the server** — audio device, sample rates, wakeword models/threshold/VAD, sounds, VAD timing, and its room name — and does **not initialize audio until that config arrives**. Configure all of it from the [dashboard](../dashboard.md), keyed by the node's `node_id`. The operational keys below may still be set locally as a *pre-connect fallback* for any key the server does not push, but the dashboard/server is authoritative.

## Full reference

| Key | Default | Description |
|---|---|---|
| `server_url` | `null` | WebSocket URL of the kenzy-server. Leave `null`/empty to **auto-discover** the server on the LAN via mDNS; set an explicit `ws://` (or `wss://` for a TLS-enabled server) URL to skip discovery (e.g. across VLANs that block multicast). |
| `discovery.enabled` | `true` | Browse for the server over mDNS when `server_url` is unset |
| `tls_verify` | `false` | When connecting over `wss://` (a [TLS-enabled server](server.md#tls-optional)): `false` = encrypted but the certificate is **not** verified — the right posture for a self-signed home-LAN cert, no CA install needed. `true` = require a certificate trusted by the system store. |
| `tls_ca` | — | Path to the server's certificate (or a CA bundle) to **pin**: connections verify against this file specifically. Implies verification. |
| `node_id` | *(generated)* | Stable primary identifier for this node. Leave unset — one is generated and written back to `node.yaml` on first run (or assigned at install with `kenzy-init --node-id ID`), then kept across restarts. The server keys the registry, per-node config, and all controls on it, so a node's identity (and its config) survives even when the room name changes or the device is reimaged. |
| `room_id` | `null` | Human **room name** (e.g. `kitchen`). **Server-owned**: set it from the [dashboard](../dashboard.md). Until the server provides one it falls back to the **hostname**. Sent to the assistant as context (used in conversation history). |
| `audio_device` | `null` | PortAudio device name substring or integer index. `null` uses the system default. Use `kenzy-devices` to find the correct value. |
| `capture_sample_rate` | `16000` | Sample rate for microphone capture. Set to the device's native rate if it does not support 16000 Hz; audio is resampled automatically. |
| `playback_sample_rate` | `24000` | Sample rate for speaker output. Set to the device's native rate if it does not support 24000 Hz; TTS audio is resampled automatically. |
| `volume` | `100` | Playback volume [0–100]. **Server-owned**, applies live (config-pull). Settable from the dashboard or by voice ("turn it up", "set the volume to 40"). Affects TTS, intercom, and announcements. |
| `muted` | `false` | **Runtime only — not persisted.** Mutes all playback *except* the wake-word ready chime, which stays audible (at a floor level) so you can tell the device is listening and knowingly unmute. Toggle from the dashboard or by voice ("mute"/"unmute"). A node always comes back un-muted after a restart. |
| `log_level` | `"info"` | What the node prints to its console (`debug`/`info`/`warning`/`error`). Live-tunable from the dashboard. |
| `log_capture_level` | `"debug"` | **Server-owned.** How deep the dashboard log viewer can see for this node (`trace`/`debug`/…), independent of `log_level`. Captured only while the dashboard's logs flag is on (otherwise zero overhead). Set `trace` to include per-frame audio logs. |
| `verbose` | `false` | Also enables debug output from websockets and asyncio internals |

!!! tip "Let the dashboard tune these for you"
    `wakeword_threshold`, `wakeword_vad_threshold`, and `silence_rms_threshold` are
    mic- and room-specific. Rather than guessing, use the **Calibration** panel in the
    dashboard (or `kenzy-node --calibrate` on a headless node) to measure your room and
    apply suggested values. See [Calibrating a node's audio](../dashboard.md#calibrating-a-nodes-audio).

### Wake word

| Key | Default | Description |
|---|---|---|
| `wakeword_models` | `[]` | List of paths to `.tflite` or `.onnx` model files. Empty uses the bundled `hey_ken_zee.tflite` |
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
| `sound_connect` | `"connect.wav"` | Chime played when an **intercom** call connects (bundled default; path, or empty/`null` to disable). |
| `sound_disconnect` | `"disconnect.wav"` | Chime played when an **intercom** call ends (bundled default; path, or empty/`null` to disable). |
| `sound_ringback` | `"ringback.wav"` | The ring the **caller** hears while an intercom target room is being rung and asked to accept — loops until the call connects, is declined, or times out. Empty/`null` = silent wait. |
| `sound_timer` | `"timer.wav"` | Tone played before a **timer** announcement. Unlike the other sounds, this is prepended by the **server** at fire time — a path is read on the server host, and changes apply **live** (no node restart). Empty/`null` = voice only. |
| `sound_alarm` | `"alarm.wav"` | Tone played before **each alarm ring** — same server-side, live-apply behavior as `sound_timer`. The tone still plays even if TTS synthesis fails. Empty/`null` = voice only. |
| `sound_error` | `"error.wav"` | Spoken cue when a request **fails** mid-pipeline ("I'm sorry, but I'm having trouble…"). Pre-recorded — not synthesized — so it still plays when TTS itself is the broken part. Same server-side, live-apply semantics. Empty/`null` = stay silent on failure. |
| `dialog_no_speech_timeout_ms` | `8000` | How long Kenzy **waits for your answer** during a multi-turn dialog before giving up (the soft end cue = "I stopped waiting"). Live-applied. |
| `dialog_onset_ms` | `300` | Speech required to **start** a dialog turn — either sustained speech of this length, or a shorter *complete* word ("yes", "Boo") that ends in silence. A cough or clink can't trip the mic; your first word is buffered and kept whole. Live-applied. |
| `dialog_onset_vad_threshold` | `0.5` | Voice-activity confidence gate for dialog onset (falls back to energy sensing if the VAD model is absent). Live-applied. |
| `hardware_aec` | `true` | Whether this room's speaker does **acoustic echo cancellation**. **Calibration detects and sets this automatically** (it plays a known sound through the node's speaker and measures the echo); set it manually only to override an ambiguous reading. Set `false` for a non-AEC speaker and the room runs **half-duplex**: see the table below for exactly what changes. Live-applied; shown as a "no AEC" badge on the room's fleet card. |

### Rooms without echo cancellation (`hardware_aec: false`)

Kenzy assumes an echo-cancelling speakerphone (see the hardware guide) — it's
what lets her hear you *while she's making sound*. On a speaker without AEC,
her own audio floods the microphone, so features that depend on
listening-during-playback are turned off **cleanly** rather than left to
misbehave:

| Feature | Without AEC |
|---|---|
| Voice interrupt while Kenzy is speaking/playing | Off — the wake word works again the instant playback ends |
| Talk-over (barge-in during a dialog) | Off — she can't hear you over herself; you answer after she finishes (3.6.0 strict turns). With AEC, you can talk over her and she yields |
| Intercom (live two-way calls) | Unavailable — Kenzy politely refuses (a two-way call without echo cancellation is a feedback loop) |
| Alarms | Refused at set time with an offer of a timer or reminder instead (an alarm's ring loop can only be silenced by voice — impossible over the ringing). An already-set alarm still fires, once, timer-style |
| Timers, reminders, announcements, all voice commands, multi-turn dialogs | **Work normally** |

!!! note "Zero-config nodes (discovery + config-pull)"
    A node needs no operational local config. With `server_url` unset it finds the server via mDNS, generates a stable `node_id` on first run (or one assigned at install via `kenzy-init --node-id`), and **blocks until the server answers** — it connects, sends `hello`, and waits for the server's `config` frame before initializing audio. That effective config is the server's `node_defaults` plus any per-node override in `configs/nodes/<node_id>.yaml`. **Hardware keys** (`audio_device`, sample rates, `wakeword_models`/VAD gate, sounds) are applied as the audio stack is built on this first pull; a *later* change to a hardware key needs a restart (one click in the dashboard). **Live-tunable keys** (wake-word threshold, silence RMS, VAD timing) apply immediately on every push. So a room device can run with an essentially empty `node.yaml`, and everything — including its room name — is configured from the [dashboard](../dashboard.md) and centralised on the server. Pre-seed a node by creating `configs/nodes/<node_id>.yaml` on the server before the device first connects. See [Server Configuration](server.md#discovery-and-config-pull).

!!! tip "Finding the right device name"
    Run `kenzy-devices` after install. It tests every PortAudio device against Kenzy's required sample rates and prints ready-to-paste `node.yaml` settings including `capture_sample_rate` and `playback_sample_rate` if resampling is needed.

!!! tip "Prefer a speakerphone with hardware AEC"
    Use a USB **speakerphone with built-in acoustic echo cancellation** as the node's mic+speaker. Kenzy does not cancel echo itself, so without hardware AEC the node hears its own TTS playback and may falsely wake or interrupt — otherwise you must handle echo cancellation outside Kenzy.

!!! tip "Custom wake word"
    Custom wake word models can be trained at [openWakeWord](https://github.com/dscripka/openWakeWord) and pointed to via `wakeword_models`. Both `.tflite` and `.onnx` formats are supported.

## Example

A typical node only needs the bootstrap keys — audio and tuning come from the server:

```yaml
log_level: "info"
server_url: null                      # null = discover the server via mDNS
discovery:
  enabled: true
# node_id is generated and written here automatically on first run — leave it unset
```

The operational keys may still be set locally as a **pre-connect fallback** for any key the server does not push (e.g. to pin a device before the node is configured in the dashboard):

```yaml
audio_device: "Anker PowerConf S330"  # substring of name shown by kenzy-devices
capture_sample_rate: 48000            # device native rate; resampled to 16000 Hz
playback_sample_rate: 48000           # device native rate; resampled to 24000 Hz
wakeword_threshold: 0.4               # lower is safe once VAD gating is on
wakeword_vad_threshold: 0.5           # reject wake-word hits on near-silence/noise
```
