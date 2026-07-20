# Speaker Enrollment

Speaker identification lets Kenzy know **who** is talking. Enroll each family member once and Kenzy can address people by name, keep track of who asked what, and require a recognized voice for sensitive actions like unlocking doors.

There are three ways to enroll someone — pick whichever fits:

- **From the dashboard (easiest):** on the **People** tab, add the person (or find their card) and click **Enroll voice** — choose the room, and Kenzy walks them through a few sentences right at that room's microphone. No terminal needed, and the voice is linked to the person automatically. Enrolling again later adds more samples, which makes recognition more reliable.
- **From the terminal:** the `kenzy-enroll` CLI, [below](#running-enrollment).
- **By voice** ("hey Kenzie, enroll me as Alice") — off by default for a good security reason, [see below](#enrolling-by-voice-from-a-node).

Whichever you use, enroll **in the room, on the microphone people will actually use** — that's the biggest factor in how well recognition works.

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

## Enrolling by voice (from a node)

You can also enroll without the CLI by speaking to a room node — say something like **"Hey Kenzy, enroll me as Alice"**. Kenzy then reads out the `enroll_prompts` sentences (the same configurable list the CLI uses) and records your reply to each through that node's microphone, so the samples come from the device and room you actually use.

This is **off by default**. Enable it from the **dashboard** (Services → **speaker** →
toggle `allow_voice_enroll` on, Save) or by setting it in the speaker config:

```yaml
allow_voice_enroll: true
```

The speaker service owns it: the enrollment skill checks it at the start of every spoken enrollment (via the service's `/enroll/info`), and a dashboard Services save restarts the service to apply a change.

How it flows once enabled:

1. You ask Kenzy to enroll a name; it replies "Okay, enrolling Alice…".
2. After each prompt tone, read back the sentence Kenzy asks for (one sample per `enroll_prompts` entry) — it POSTs each to `kenzy-speaker`.
3. It confirms "All done — I've enrolled Alice." (or cancels after several unclear captures, or times out if abandoned).

!!! warning "Why it's off by default"
    When voice enrollment is on, **anyone within earshot of a node can enroll** — including under an *existing* name. Because speaker identity gates sensitive actions (e.g. unlocking doors), that could let someone register their voice as a trusted person and bypass the gate. Leave it off unless you trust everyone with microphone access, and prefer the `kenzy-enroll` CLI for the people who can unlock things. Speaker ID is a convenience gate, not strong authentication (see [Security implications](#security-implications)).

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
