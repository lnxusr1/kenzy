# KENZY

Kenzy is a distributed home voice assistant built as six independently deployable microservices. Wake-word detection runs locally on low-power room nodes (Orange Pi Zero 3 / 3W or Raspberry Pi 3 / 4 / 5). Audio streams over WebSocket to a central server that runs the full speech-to-text → LLM → text-to-speech pipeline and streams synthesized speech back to the room.

## Key features

- **Distributed** — each service runs independently; deploy only what you need on each host
- **Wake-word activation** — always-on local detection with no cloud dependency for the trigger
- **Speaker identification** — knows who is speaking; used for personalization and access control
- **Multi-room voice** — broadcast announcements to every room by voice ("tell everyone dinner's ready"), or place a live two-way **intercom** call between rooms (the receiving room must verbally accept)
- **Extensible skills** — add new capabilities by dropping a Python file in `skills/`; no registration required
- **LLM-agnostic** — works with OpenAI, Anthropic, Ollama, LM Studio, and any provider supported by LiteLLM
- **Conversation history** — per-room rolling context window so follow-up questions resolve naturally
- **Web dashboard** — an opt-in fleet manager: live health, per-node config + room rename, controls, announcements, and logs in one place

## Services at a glance

| Service | Command | Port | Role |
|---|---|---|---|
| **node** | `kenzy-node` | — | Wake word, audio capture, TTS playback |
| **server** | `kenzy-server` | 8765 | WebSocket hub, pipeline orchestrator |
| **stt** | `kenzy-stt` | 8767 | Speech-to-text via faster-whisper |
| **tts** | `kenzy-tts` | 8769 | Text-to-speech via OpenAI TTS |
| **llm** | `kenzy-llm` | 8766 | LLM + skill tool-calling via LiteLLM |
| **speaker** | `kenzy-speaker` | 8768 | Speaker identification via SpeechBrain |

## Quick links

- [Getting Started](getting-started.md) — install, configure, and run your first session
- [Architecture](architecture.md) — how the pieces fit together
- [Dashboard](dashboard.md) — the opt-in web fleet manager
- [Skills](skills/index.md) — extend Kenzy with custom capabilities
- [Deployment](deployment.md) — push to a fleet of remote hosts with `kenzy-deploy`
