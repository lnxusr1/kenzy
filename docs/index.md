# KENZY

Kenzy is a voice assistant for your whole home that runs on **your own hardware**.
You talk to it naturally — *"hey Kenzie, turn off the lights downstairs, I'm ready
for bed"* — and it understands you the way a person would, because it's powered by a
real language model (one you choose: a cloud provider or a model running locally).
Your rooms share one assistant, it can control your smart home through Home
Assistant, and it can recognize **who** is talking.

## What you can do with it

- **Ask it things, naturally** — no memorized phrases. "What time is it?", "Is it
  going to rain tomorrow?", "What's in the news?"
- **Control your smart home by voice** — Kenzy connects to Home Assistant and
  understands your devices by the names you actually use ("the lamp by the chair").
- **Play music by name** — "play some jazz", "put on The Beatles in the loft" —
  through [Music Assistant](skills/home-assistant.md), if you run it; MA finds the
  music, Kenzy picks the right room.
- **Talk to the whole house** — "tell everyone dinner's ready" speaks it in every
  room; "call the living room" starts a live two-way intercom (the other room has to
  say "yes" first).
- **Get answers that know who's asking** — enroll your voice and Kenzy can tell
  family members apart, and can require a recognized voice for sensitive actions
  like unlocking a door.
- **Ask her to remember things** — "remember that the spare key is under the
  blue pot" stores a fact *for you*; private facts are only ever spoken back to
  their owner, "everyone should know…" shares one with the house, and codes and
  passwords are auto-vaulted into an encrypted lockbox.
- **Take her with you on your phone** — through the Home Assistant companion app,
  the same Kenzy (your memory, your identity) answers from anywhere — and can
  speak in her own voice.
- **Manage it all from a web page** — the built-in dashboard shows every room and
  service, and is where you name rooms, tune microphones, and update everything with
  a click.

## What it looks like in your home

One computer runs the "brain" (the server and the speech/language services). Each
room gets a small, cheap device — a Raspberry Pi with a USB speakerphone — that
listens for the wake word and plays the replies. You can also start with
**everything on a single computer** to try it out.

Kenzy is a self-hosted project for people comfortable running Linux on their own
machine. You don't need to be a programmer — the installer is one command and
day-to-day management happens in the dashboard — but you should be comfortable
opening a terminal and editing a text file. If that's you, you'll be talking to
Kenzy in under an hour.

**→ Start here: [Getting Started](getting-started.md)**

## Under the hood (you don't need this to get started)

Kenzy is built as six small services that you can run together on one machine or
spread across several:

| Service | Command | Port | Role |
|---|---|---|---|
| **node** | `kenzy-node` | — | Room device: wake word, audio capture, playback |
| **server** | `kenzy-server` | 8765 | The hub: connects rooms and runs the pipeline |
| **stt** | `kenzy-stt` | 8767 | Speech-to-text (local faster-whisper, or OpenAI cloud) |
| **tts** | `kenzy-tts` | 8769 | Text-to-speech (OpenAI, or local Kokoro) |
| **llm** | `kenzy-llm` | 8766 | The language model + skills (any LiteLLM provider) |
| **speaker** | `kenzy-speaker` | 8768 | Voice identification (runs locally) |

Voice identification always runs on your hardware, and speech recognition does by
default. Every stage is your choice — run the speech, language, and voice services
fully local, use a cloud provider (the easiest start, and the lightest on your
hardware), or mix and match per service. See [Architecture](architecture.md) for
how it all fits together.

## Quick links

- **[Setup Guides](getting-started.md)** — Part 1: first conversation → Part 4: everything connected, each part short and ending with something working
- [Talking to Kenzy](talking-to-kenzy.md) — the five-minute guide to using her: waking, interrupting, canceling, muting, conversations
- [Troubleshooting](troubleshooting.md) — when something doesn't work
- [Dashboard](dashboard.md) — the web page where you manage everything
- [Home Assistant](skills/home-assistant.md) — voice-control your smart home
- [Speaker Enrollment](speaker-enrollment.md) — teach Kenzy who's who
- [Memory](memory.md) — what she remembers, for whom, and how to manage it
- [On Your Phone](phone.md) — Kenzy through the Home Assistant companion app
- [Skills](skills/index.md) — add new abilities with a small Python file
