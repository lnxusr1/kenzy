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
| `hello` | node → server | Registration on connect; carries the stable `node_id` (primary key) + `room_id` (room name), optional audio `capabilities` and a join `token` |
| `config` | server → node | Effective node config pushed right after `hello` (config-pull) |
| `set_room` | server → node | Dashboard renames the node's (server-owned) room; the node persists it to `node.yaml` and applies it live |
| `audio_start` | node → server | Begins a capture session |
| `audio_end` | node → server | Ends a capture session, includes `reason` |
| `wakeword` | node → server | Wake word fired mid-stream |
| `trigger` | server → node | Server-initiated activation of an idle node |
| `stop` | server → node | Tells node to stop streaming or TTS playback |
| `ack` | server → node | Confirms `audio_start` was received |
| `tts_start` | server → node | Begins TTS playback; includes `sample_rate` and `channels` |
| `tts_end` | server → node | Ends TTS playback |
| `call_request` | server → node | Ring the node for an incoming intercom call (no audio bridged yet) |
| `call_cancel` | server → node | Caller cancelled before the receiver accepted |
| `intercom_start` | server → node | Consent accepted — begin a live two-way call with the peer room |
| `intercom_end` | server ↔ node | End the call (sent to both ends; a node sends it when its wake word fires mid-call) |

Binary frames sent server → node between `tts_start` and `tts_end` are raw int16 PCM at 24 kHz mono. During an intercom call, raw mic PCM is relayed node ↔ server ↔ peer node.

## Discovery & config-pull

The server advertises itself as `_kenzy._tcp` over **mDNS**, so a node with no `server_url` finds it automatically; an explicit `server_url` skips discovery. On connect, the node's `hello` carries its **identity** — a stable `node_id` (generated and persisted in `node.yaml` on first run, or assigned at install with `kenzy-init --node-id`) plus its `room_id` (room name) — its audio `capabilities`, and an optional join `token`. The server validates the token and replies with a `config` frame holding the node's **effective config** — the server's `node_defaults` merged with the per-node override `configs/nodes/<node_id>.yaml`.

`node.yaml` is **bootstrap-only**: a node does **not** initialize audio until this first `config` frame arrives (it blocks in the reconnect loop until the server answers — no boot-from-cache). Hardware values (audio device, sample rates, wakeword models, sounds) are applied as the audio stack is built on that first pull; a *later* change to a hardware value takes effect on restart, while live-tunable values (thresholds, VAD timing, log levels) and the room name apply immediately on every push. The server keys its registry, per-node config, and all controls on `node_id`, so a node keeps its identity and config even when its room is renamed; the **room name is server-owned** — stored in the per-node override, pushed on connect, and editable from the dashboard. The result: room devices carry only their identity and how to reach the server; everything operational is centralised on the server.

### Central config for backend services

The server is also the config authority for the backend HTTP services. It serves an **always-on**, token-gated `GET /config/<service>` on the node WebSocket port (independent of the dashboard), returning that service's effective config (packaged default deep-merged with the server-owned `configs/services/<service>.yaml`, secrets stripped). At boot, `kenzy-stt`/`kenzy-tts`/`kenzy-llm`/`kenzy-speaker` discover the server like a node does (mDNS, or `KENZY_SERVER_URL`), pull their config, and **block with retry/backoff until the server answers** — so the server must come up first (`After=kenzy-server`). Each also exposes a token-gated `POST /restart` (re-exec) so a dashboard config edit can apply by restarting the service. The dashboard's **Services** tab edits this central store.

## Dashboard

`kenzy-server` serves a web fleet manager (`dashboard.enabled`, on by default in the shipped config, localhost-bound) on its own bind/port. Set it false and it is wired up nowhere and adds zero overhead. When enabled it serves a no-build SPA over the `websockets` HTTP hook (no new dependency): username/password login, a live fleet/health view, a per-node config editor with room rename, a **Services** editor for the backend services' central config (with restart), node controls (trigger/stop/restart), TTS announcements, a pull-based log viewer (with on-demand TRACE capture for a node), and a settings page (system info, feature flags, password change). It reuses the server's existing registry and connections — no new transport. See the [Dashboard guide](dashboard.md).

## Node state machine

The node runs three concurrent asyncio tasks:

**`_audio_loop`** — pulls frames from a thread-safe queue fed by the sounddevice callback. Runs openwakeword on every frame regardless of state.

- **IDLE**: wake word detected → begin streaming
- **STREAMING**: frames sent to server; VAD/timeout/hard-cap ends session
- **TTS**: wake word detected → interrupt playback, begin new session

**`_recv_loop`** — reads inbound messages from the server WebSocket and routes them to `_cmd_q` (JSON control) or `_tts_q` (binary PCM).

**`_cmd_loop`** — processes `_cmd_q`: handles `config` (apply pulled config), `trigger`, `stop`, `tts_start`, `tts_end`.

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

One `.tflite` model ships with the package and is loaded by default: `hey_ken_zee.tflite` (wake phrase "hey Kenzie"). Custom models (`.tflite` or `.onnx`) can be specified via `wakeword_models` in `configs/node.yaml`; the inference framework is inferred from the file extension.
