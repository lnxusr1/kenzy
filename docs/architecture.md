# Architecture

## End-to-end pipeline

```
Node (mic) ──PCM frames over WebSocket──► Server
                                              │
                          ┌───────────────────┘ on_session_end
                          ▼
                  STT service ──┐  (parallel)
                  Speaker ID ───┘
                          │
                          ▼
                   LLM service  ◄──► Skills (tool-calling loop)
                          │
                          ▼
                   TTS service
                          │
               PCM frames over WebSocket ──► Node (speaker)
```

When a wake word fires, the node streams raw 16 kHz PCM to the server. When the user stops speaking (VAD silence detection or hard cap), the server runs STT and speaker identification **in parallel**, passes the transcript and speaker name to the LLM, and streams the synthesized speech response back to the node.

## Protocol

Control messages are JSON text frames. Audio is raw int16 PCM binary frames at 16 kHz, 80 ms / 1 280 samples / 2 560 bytes each.

| Message | Direction | Purpose |
|---|---|---|
| `hello` | node → server | Registration with `room_id` on connect |
| `audio_start` | node → server | Begins a capture session |
| `audio_end` | node → server | Ends a capture session, includes `reason` |
| `wakeword` | node → server | Wake word fired mid-stream |
| `trigger` | server → node | Server-initiated activation of an idle node |
| `stop` | server → node | Tells node to stop streaming or TTS playback |
| `ack` | server → node | Confirms `audio_start` was received |
| `tts_start` | server → node | Begins TTS playback; includes `sample_rate` and `channels` |
| `tts_end` | server → node | Ends TTS playback |

Binary frames sent server → node between `tts_start` and `tts_end` are raw int16 PCM at 24 kHz mono.

## Node state machine

The node runs three concurrent asyncio tasks:

**`_audio_loop`** — pulls frames from a thread-safe queue fed by the sounddevice callback. Runs openwakeword on every frame regardless of state.

- **IDLE**: wake word detected → begin streaming
- **STREAMING**: frames sent to server; VAD/timeout/hard-cap ends session
- **TTS**: wake word detected → interrupt playback, begin new session

**`_recv_loop`** — reads inbound messages from the server WebSocket and routes them to `_cmd_q` (JSON control) or `_tts_q` (binary PCM).

**`_cmd_loop`** — processes `_cmd_q`: handles `trigger`, `stop`, `tts_start`, `tts_end`.

### State transitions

```
IDLE ──── wake word ────────────────────────────► STREAMING
IDLE ──── server TRIGGER ───────────────────────► STREAMING
IDLE ──── server TTS_START ─────────────────────► TTS

STREAMING ──── silence / no-speech / hard cap ──► IDLE
STREAMING ──── server STOP ─────────────────────► IDLE

TTS ──── server TTS_END ────────────────────────► IDLE (after playback finishes)
TTS ──── server STOP ───────────────────────────► IDLE
TTS ──── wake word ─────────────────────────────► STREAMING (interrupts playback)
```

### Sound playback

`_SoundPlayer` holds a single persistent `sd.OutputStream` that runs for the lifetime of the process, outputting silence when idle. This avoids the hardware DAC activation pop that occurs when opening a stream from cold. A waiting sound plays between `audio_end` and the TTS response to give the user feedback during server processing.

## Server pipeline

`TranscribingServer` (subclass of `AudioServer`) implements the full pipeline:

- Buffers raw PCM per room in `_buffers`
- On `on_session_end`: snapshots the buffer, runs `_call_stt` and `_call_speaker` in parallel via `asyncio.gather`, then calls `_call_llm`, then streams TTS back
- A new wake word from the same node cancels any in-flight pipeline task for that room
- Mid-stream disconnect triggers `on_session_end("disconnect")`

### Extending the pipeline

Subclass `TranscribingServer` (or `AudioServer`) and override any hooks:

```python
async def on_session_start(self, session: NodeSession) -> None: ...
async def on_audio_frame(self, session: NodeSession, data: bytes) -> None: ...
async def on_session_end(self, session: NodeSession, reason: str) -> None: ...
async def on_wakeword(self, session: NodeSession, model: str, score: float) -> None: ...
```

## LLM service

### Deterministic fast path

Before the model is consulted, each `/process` request is run through the registered **fast intents** (`@fast_intent` matchers). If one confidently matches — a time/date query, a Home Assistant control command — it answers locally with **no remote model call**, and the pipeline skips straight to TTS. Only requests that every matcher *misses* fall through to the tool-calling LLM below. This keeps high-frequency commands like "turn on the lights" at local-parse-plus-one-HTTP-call latency instead of a full model round-trip. See [Skills → Two resolution tiers](skills/index.md#two-resolution-tiers).

The `/process` response carries an `expect_response` flag (set by the fast path) reserved for re-opening the mic for a follow-up turn without the wake word.

### Conversation history

The LLM service maintains a **per-room conversation history** — a rolling window of the last 10 turns, each expiring 3 minutes after it was recorded. History is injected as real `role: user` / `role: assistant` message pairs between the system prompt and the current request, so the model can resolve follow-up references ("tell me more about the second one") naturally. Fast-path responses are recorded too, so context carries across both tiers.

The tool-calling loop executes skills sequentially until the model returns a plain text response or the iteration limit is reached. Internal tool calls are never stored in conversation history — only the final spoken response and the user's utterance are recorded.

## Wake-word models

Two `.tflite` models ship with the package: `hey_kenzie.tflite` (loaded by default) and `ken_zee.tflite`. Custom models (`.tflite` or `.onnx`) can be specified via `wakeword_models` in `configs/node.yaml`; the inference framework is inferred from the file extension.
