# Speaker ID Configuration

**File:** `configs/speaker.yaml`  
**Commands:** `kenzy-speaker`, `kenzy-enroll`, `kenzy-setup`

The speaker identification service uses a SpeechBrain ECAPA-TDNN model to compare incoming audio against enrolled speaker profiles and return the closest match.

!!! note "Pulled from the server"
    `kenzy-speaker` pulls this config from the server at boot — it discovers the server via mDNS (or `KENZY_SERVER_URL`) and blocks until it answers, so start the server first. Edit it from the dashboard's **Services** tab (writes `configs/services/speaker.yaml` on the server and restarts the service). Passing an explicit path loads locally instead (dev/offline). The `kenzy-enroll` CLI still reads a local config. See [central config for backend services](server.md#central-config-for-backend-services).

## Full reference

### Service

| Key | Default | Description |
|---|---|---|
| `host` | `"127.0.0.1"` | Bind address |
| `port` | `8768` | HTTP port |
| `log_level` | `"info"` | What the service prints to its console |
| `log_capture_level` | `"debug"` | How deep the dashboard log viewer can see, independent of `log_level` |

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
| `allow_voice_enroll` | `false` | Allow [voice enrollment](../speaker-enrollment.md#enrolling-by-voice-from-a-node) ("enroll me as Alice") from a node. The speaker service owns it (editable from the dashboard's Fleet → **speaker** page; the save restarts the service to apply), and the enrollment skill checks it before every spoken enrollment. Off by default; when on, anyone in earshot can enroll — see the security warning in the enrollment guide. |

### Enrollment (`kenzy-enroll`)

| Key | Default | Description |
|---|---|---|
| `enroll_sample_rate` | `16000` | Microphone sample rate during enrollment |
| `enroll_silence_rms` | `300` | RMS threshold above which a frame is considered speech |
| `enroll_silence_ms` | `800` | Consecutive silence (ms) that ends a recording |
| `enroll_min_speech_ms` | `1500` | Minimum speech (ms) required for a valid sample |
| `enroll_prompts` | *(built-in list)* | Sentences the user reads during enrollment — the single source for BOTH the `kenzy-enroll` CLI and spoken/dashboard enrollment (served to the enrollment skill via `GET /enroll/info`). Phonetically diverse sentences produce better embeddings. |
| `tts.url` | *(from server)* | TTS service used to read enrollment prompts aloud. **Auto-wired from the server by default** (it injects its own `tts.url`), so you normally leave this unset; set it only to override per host (e.g. a multi-host setup where this machine reaches TTS at a different address). |
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

## People — mapping voices to a person (`data/people.yaml`)

The speaker service returns a **voiceprint name** (whatever a voice was enrolled
as). An optional `data/people.yaml` in the server's config home elevates those
names into **person records** — one household member joining one or more
voiceprints, plus optional links for later channels — and the pipeline then
resolves every request to `(person, confidence, tier)` instead of a bare name.

```yaml
# data/people.yaml — server-owned; rides your backup
people:
  alex:                        # a stable id
    name: Alex                 # spoken/display name
    voiceprints: [alex, alex-kitchen]   # speaker-service names that are this person
    aliases: [Al, Big Al]      # optional — other things this person is called (People page → "Also called")
    ha_user: person.alex       # optional — links their HA account (Assist identity + presence answers)
    phone: null                # optional
    memory_opt_out: false      # optional — "don't remember me" (People page toggle)
    memory_capture: explicit   # optional — explicit (default) | suggest | auto
  alice:
    name: Alice
    voiceprints: [alice]
```

- **No file? No change.** Without `people.yaml` the raw voiceprint name passes
  straight through, exactly as before — this layer is purely additive.
- **Confidence tier.** A match is `recognized`; a below-threshold voice is
  `unknown`. Per-person [memory](../memory.md) (and everything privacy-gated)
  gates on the tier — an unknown voice gets no memory at all.
- **Standalone.** `ha_user`/`phone` are optional — a voiceprint-only household
  is fully supported. Linking `ha_user` is what lets the same person be
  recognized when they type through the [HA Assist channel](../phone.md) and
  what answers ["is Alice home?"](../skills/builtin.md#presence).
- **Spoken names are matched forgivingly.** The transcriber spells names its own
  way — a household's *Bobbie* is heard as "Bobby", *Vicki* as "Vikki" — so
  questions like "where is Bobby?" resolve by closeness, not exact spelling,
  and the answer confirms the real name ("Bobbie is home."). When two household
  names are genuinely close (*Jon* and *John*), Kenzy asks which one you meant
  rather than guessing. `aliases` covers what no spelling-match could: nicknames
  ("Bud" for Robert). Aliases only ever pick the *subject* of a question — who is
  **speaking** is always decided by voice, never by a name.

### Editing people from the dashboard

The dashboard's **People** tab is the easy way to manage these records — you
rarely need to touch the YAML. Enrollment is **person-first**: a person can
exist without a voice, but every enrolled voice belongs to a person, so the flow
is simply *add the person, then enroll their voice*.

- **Add a person**, then click **Enroll voice** on their card and pick the room
  to record from. The voice profile is stored under the person's stable id and
  linked automatically — and re-enrolling later adds more samples to the same
  profile, which makes recognition more reliable. (The hands-free "Hey Kenzy,
  enroll me as Alice" command is person-first too: it finds or creates the
  person record for the name it hears.)
- **Voices without a person** — profiles from a pre-people setup or the
  `kenzy-enroll` CLI — are listed with a one-click *Assign to a person*, so
  you're guided from "a voice exists" to "Kenzy knows who that is."
- Editing is inline: click a person to expand their card, change the name, and
  save. Renaming a person never touches the voice profile (it's keyed by their
  id). Deleting a person removes only the record — the enrolled voice itself
  stays; deleting a voice profile follows through to its person record
  automatically, so the links in `people.yaml` never silently break.

The expanded card also edits the **HA person** link (`ha_user`); `phone` is
reserved for a later channel and preserved across saves. Edits
need `dashboard.controls: true` (otherwise the tab is read-only) and take effect
immediately — the running pipeline resolves the new mapping on the next request,
no restart.
