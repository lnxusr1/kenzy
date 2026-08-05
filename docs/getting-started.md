# Getting Started

Kenzy sets up in **parts** — each one short, each one ending with something
working. Do Part 1 today and stop there happily; come back for the rest whenever
you're ready.

## The path

| | What you'll do | |
|---|---|---|
| **[Part 1 — First Conversation](guides/part1-first-conversation.md)** | Basic install · open the dashboard · connect the language model · **calibrate the audio** · say hello | *under an hour* |
| **[Part 2 — People & Rooms](guides/part2-rooms-and-people.md)** | Add people and enroll their voices — this is what unlocks per-person memory · add a device in another room | |
| **[Part 3 — Home Assistant Basics](guides/part3-home-assistant.md)** | Create a Home Assistant token, give it to Kenzy, and turn off the kitchen lights by voice | |
| **[Part 4 — Home Assistant, the Works](guides/part4-ha-complete.md)** | Kenzy on your phone in her own voice, and Kenzy's rooms inside HA | |

**Calibration in Part 1 isn't optional.** The default listening thresholds are
almost never right for a real room, and calibration is also how Kenzy learns
whether your speaker does echo cancellation.

If anything misbehaves, [Troubleshooting](troubleshooting.md) is organized by
symptom.

## What you'll need

- **A Linux computer to be the "brain."** Debian or Ubuntu with Python 3.11+.
  Use a mini-PC, desktop, home server, or cloud host for this role. A Raspberry
  Pi is not a practical host for the brain or for local AI and speech services.
  It is, however, an excellent separate **room node**: run the brain on a more
  capable PC or in the cloud, then connect the Pi and its speakerphone in the
  room.
- **A microphone and speaker.** Strongly recommended: a **USB conference
  speakerphone** (an Anker PowerConf or similar). These have built-in echo
  cancellation, which is what lets Kenzy hear you while she's speaking. A webcam
  mic plus desktop speakers will limp along for a first test, but a speakerphone
  is the single best purchase for this project. Without echo cancellation,
  calibration detects it and Kenzy adapts honestly — intercom and alarm
  ring-loops turn off in that room, and everything else works normally.
- **An API key, if you use cloud services.** The default [brain
  (language model)](configuration/llm.md) and default voice both use OpenAI,
  so the standard setup needs an OpenAI API key. You can instead choose another
  cloud provider for the brain (or run it locally); use the matching provider's
  key.
- **Optional:** [Home Assistant](https://www.home-assistant.io/), if you want
  voice control of your smart home. You can add it any time.

## Cloud or local?

Three services can run either way, and each one is a dropdown in the dashboard —
nothing you choose now is permanent.

| Service | Default | The other option |
|---|---|---|
| [The brain](configuration/llm.md) | Cloud | Ollama or LM Studio, on a machine with a GPU |
| [Her voice](configuration/tts.md) | Cloud | Kokoro, happy on a CPU |
| [Her ears](configuration/stt.md) | **Local** — your voice never leaves | OpenAI, if your server is light |

Speaker identification is [always local](configuration/speaker.md) and has no
cloud option.

So the defaults are a **mixed** setup: your voice audio stays home, while
transcribed text and spoken replies go to a cloud provider. That's the fastest
route to something that works, not a recommendation about where your data
belongs — [Running Fully Local](fully-local.md) walks the other path end to end.

!!! note "Nothing here takes over your computer"
    The installer puts everything in your own user folder (`~/.local/share/kenzy`
    and `~/.config/kenzy`) plus a few standard system packages via `apt`. It doesn't
    need root for Kenzy itself, and `install.sh --uninstall` removes it cleanly.

All set? **[Continue to Part 1 — First Conversation](guides/part1-first-conversation.md).**

---

Unattended installs, non-Debian hosts, splitting services across machines, or
installing by hand: [Installation Reference](install-reference.md).
