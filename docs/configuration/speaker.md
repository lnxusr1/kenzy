# Speaker ID Configuration

**File:** `configs/speaker.yaml`  
**Commands:** `kenzy-speaker`, `kenzy-enroll`, `kenzy-setup`

The speaker identification service uses a SpeechBrain ECAPA-TDNN model to compare incoming audio against enrolled speaker profiles and return the closest match.

## Full reference

### Service

| Key | Default | Description |
|---|---|---|
| `host` | `"127.0.0.1"` | Bind address |
| `port` | `8768` | HTTP port |
| `log_level` | `"info"` | Log verbosity |

### Model

| Key | Default | Description |
|---|---|---|
| `model_source` | `"speechbrain/spkrec-ecapa-voxceleb"` | HuggingFace model ID. Downloaded once by `kenzy-setup`. |
| `model_save_dir` | `"models/speaker"` | Local cache directory for the downloaded model |

### Speaker profiles

| Key | Default | Description |
|---|---|---|
| `embeddings_dir` | `"data/speakers"` | Directory containing per-speaker `.npy` embedding files. Each file is named `<speaker_name>.npy`. |
| `identify_threshold` | `0.25` | Cosine similarity threshold [0.0–1.0]. Utterances below this score are attributed to `unknown_speaker`. |
| `unknown_speaker` | `"unknown"` | Name returned when no enrolled speaker exceeds the threshold. |

### Enrollment (`kenzy-enroll`)

| Key | Default | Description |
|---|---|---|
| `enroll_sample_rate` | `16000` | Microphone sample rate during enrollment |
| `enroll_silence_rms` | `300` | RMS threshold above which a frame is considered speech |
| `enroll_silence_ms` | `800` | Consecutive silence (ms) that ends a recording |
| `enroll_min_speech_ms` | `1500` | Minimum speech (ms) required for a valid sample |
| `enroll_prompts` | *(built-in list)* | Sentences read aloud by the user during enrollment. Phonetically diverse sentences produce better embeddings. |
| `tts.url` | — | TTS service used to read enrollment prompts aloud |
| `tts.timeout` | `30.0` | TTS HTTP timeout |

## Threshold tuning

The default threshold of `0.25` is permissive. In a quiet environment with good microphone placement, raising it to `0.30–0.35` reduces false matches. Lower it if enrolled speakers are being returned as `unknown`.

!!! warning "Security"
    Speaker identification is used as an access gate for sensitive operations (locking/unlocking doors, opening covers). A misidentified speaker could bypass this gate. Keep the threshold at a value you are comfortable with for your environment.

## Example

```yaml
host: "127.0.0.1"
port: 8768

model_source: "speechbrain/spkrec-ecapa-voxceleb"
model_save_dir: "models/speaker"

embeddings_dir: "data/speakers"
identify_threshold: 0.28
unknown_speaker: "unknown"
```
