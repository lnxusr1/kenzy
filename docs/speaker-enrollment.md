# Speaker Enrollment

Speaker identification lets Kenzy know who is talking. This enables personalized responses and is required for sensitive operations like locking and unlocking doors.

## How it works

During enrollment, Kenzy records several short audio samples from a person and computes a speaker embedding — a compact numerical representation of their voice. At runtime, each captured utterance is compared against all enrolled embeddings using cosine similarity. The speaker with the highest similarity above the configured threshold is returned; otherwise the speaker is reported as `unknown`.

Embeddings are stored as `.npy` files in `data/speakers/<name>.npy`.

## Requirements

- The `kenzy-speaker` service must be running
- The `kenzy-tts` service must be running (used to read prompts aloud during enrollment)
- A microphone connected to the machine running `kenzy-enroll`

## Running enrollment

```bash
kenzy-enroll [configs/speaker.yaml]
```

The CLI will:

1. Ask for the speaker's name
2. Read each enrollment prompt aloud via TTS
3. Record the speaker saying the prompt
4. Repeat for all prompts
5. Compute and save the embedding to `data/speakers/<name>.npy`

The default prompts are phonetically diverse sentences chosen to capture a broad range of sounds. You can customize them in `configs/speaker.yaml` under `enroll_prompts`.

## Re-enrolling a speaker

Run `kenzy-enroll` again with the same name. The existing embedding file is overwritten.

## Removing a speaker

Delete the embedding file:

```bash
rm data/speakers/<name>.npy
```

Restart `kenzy-speaker` for the change to take effect.

## Tuning the identification threshold

The `identify_threshold` in `configs/speaker.yaml` controls how strict the match must be:

| Threshold | Behavior |
|---|---|
| `0.20` | Permissive — fewer `unknown` results, higher risk of misidentification |
| `0.25` | Default — good balance for a home environment |
| `0.30–0.35` | Strict — more `unknown` results if audio quality varies |

If enrolled speakers are frequently returned as `unknown`, lower the threshold. If strangers are being matched to enrolled speakers, raise it.

!!! tip "Enrollment quality"
    Record in the room and with the microphone you will use day-to-day. Enrollment done in a quiet studio with a headset will not generalize well to a noisy kitchen with a far-field mic.

## Security implications

Speaker identification is not a strong authentication mechanism — it can be fooled by a recording or a similar-sounding voice. It is used as a convenience gate (requiring a recognisable voice for lock/cover operations) rather than a cryptographic security boundary.
