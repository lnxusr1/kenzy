# Getting Started

## Prerequisites

- Python 3.11 or later
- On Raspberry Pi OS / Debian, install PortAudio before the node package:

```bash
sudo apt-get install libportaudio2 portaudio19-dev
```

## Installation

Create a virtual environment and install the extras for the services you intend to run on this host:

```bash
python3 -m venv .venv
source .venv/bin/activate

# Room node only (Pi Zero 2 W, Pi 4, etc.)
pip install -e ".[node]"

# Full server stack (STT, TTS, LLM, speaker ID)
pip install -e ".[server,stt,tts,llm,speaker]"

# Everything including dev tools
pip install -e ".[node,server,stt,tts,llm,speaker,dev]"
```

## Download models

After installing the `node` or `speaker` package for the first time, download the required inference models:

```bash
kenzy-setup
```

This downloads the openwakeword feature-extraction models and the SpeechBrain ECAPA-TDNN speaker identification model. It is safe to run multiple times — files that already exist are skipped.

## Identify your audio device

If your node uses a USB speakerphone or any device other than the system default, run:

```bash
kenzy-devices
```

This scans every PortAudio device, tests which sample rates each one supports, and prints ready-to-paste `node.yaml` settings. Example output:

```
[6] Anker PowerConf S330: USB Audio (hw:2,0)
    in=2  out=2  default=48000 Hz
    capture  : 16000 ✗ | 44100 ✗ | 48000 ✓
    playback : 24000 ✗ | 44100 ✗ | 48000 ✓

Suggested node.yaml settings

  [6] Anker PowerConf S330: USB Audio (hw:2,0)
      audio_device: "Anker PowerConf S330"  # resampling: capture 48000→16000 Hz, playback 48000→24000 Hz
      capture_sample_rate: 48000
      playback_sample_rate: 48000
```

Kenzy captures at 16 kHz and plays TTS at 24 kHz. If a device does not natively support those rates, the suggested settings tell the node to open the stream at the device's native rate and resample automatically.

## Configure API keys

```bash
cp .env.example .env
```

Edit `.env` and fill in your credentials:

```bash
OPENAI_API_KEY="sk-..."       # Required for TTS; also for LLM if using OpenAI models
HA_API_KEY="..."               # Home Assistant long-lived access token (home control skill)
```

!!! note
    `.env` is never committed to version control. All services call `load_dotenv()` at startup and read keys from the environment.

## Configure services

Each service reads its settings from a YAML file in `configs/`. The bundled files contain sensible defaults with comments explaining every key. At minimum you will want to:

1. Set `server_url` in `configs/node.yaml` to point at your server host
2. Set the service URLs in `configs/server.yaml` to point at wherever each service is running
3. Set your LLM model and location in `configs/llm.yaml`

See the [Configuration](configuration/index.md) section for a full reference.

## Run the services

Start each service in a separate terminal (or as a systemd unit — see [Deployment](deployment.md)):

```bash
# On the server host
kenzy-server  configs/server.yaml
kenzy-stt     configs/stt.yaml
kenzy-tts     configs/tts.yaml
kenzy-llm     configs/llm.yaml
kenzy-speaker configs/speaker.yaml

# On each room node
kenzy-node    configs/node.yaml
```

Say your wake word ("Hey Kenzie") and start talking.

## Next steps

- [Enroll speakers](speaker-enrollment.md) so Kenzy knows who is talking
- [Add skills](skills/writing-skills.md) to extend what Kenzy can do
- [Deploy remotely](deployment.md) to push updates to all your devices at once
