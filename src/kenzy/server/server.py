"""
Kenzy audio server.

Accepts WebSocket connections from one or more room nodes.  Each node
registers with a HELLO message, then may send:

  audio_start  – begins a capture session
  <binary>     – raw PCM frames (16 kHz / int16 / mono)
  audio_end    – ends the capture session

The server can push commands to any connected node at any time:

  trigger      – tells an idle node to begin streaming
  stop         – tells a streaming node to stop

Phase-1 implementation: audio frames are forwarded to an overrideable
``on_audio_frame`` hook (no-op by default) so the pipeline can be wired
in without changing this file.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
import os
import re
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websockets
import websockets.exceptions
import websockets.server
from websockets.asyncio.server import ServerConnection

from kenzy import protocol

log = logging.getLogger(__name__)

# Per-room override keys a dashboard editor may set — exactly the keys the node
# honors via NodeClient._apply_pulled_config (live-tunable + restart-required).
# Anything else is rejected on write, so a per-room override can't carry secrets
# or junk. (room_id / server_url are node-local identity, never pushed.)
_ALLOWED_OVERRIDE_KEYS = frozenset(
    {
        "wakeword_threshold",
        "wakeword_vad_threshold",
        "silence_rms_threshold",
        "vad_enabled",
        "silence_ms",
        "speech_min_ms",
        "no_speech_timeout_ms",
        "hard_cap_ms",
        "audio_device",
        "capture_sample_rate",
        "playback_sample_rate",
        "wakeword_models",
        "sound_ready",
        "sound_waiting",
    }
)
_SECRET_KEY_RE = re.compile(r"key|token|secret|password|passwd|credential", re.IGNORECASE)
_SAFE_ROOM_RE = re.compile(r"[A-Za-z0-9._-]+")
_ANNOUNCE_VOICE_PROMPT = "Read this aloud as a clear, calm public announcement."


# ---------------------------------------------------------------------------
# Per-node session state
# ---------------------------------------------------------------------------


@dataclass
class NodeSession:
    ws: ServerConnection
    room_id: str
    session_id: str | None = field(default=None)
    streaming: bool = field(default=False)

    async def send_json(self, payload: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(payload))


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class AudioServer:
    """
    WebSocket server that multiplexes audio from many room nodes.

    Subclass and override ``on_audio_frame`` / ``on_session_start`` /
    ``on_session_end`` to integrate an STT pipeline.
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        self._host: str = cfg.get("host", "0.0.0.0")
        self._port: int = int(cfg.get("port", 8765))

        # Central node tuning defaults pushed to nodes on connect (config-pull).
        self._node_defaults: dict[str, Any] = cfg.get("node_defaults", {}) or {}
        # Optional shared-secret required in the node's hello (discovery.token).
        self._join_token: str | None = (cfg.get("discovery", {}) or {}).get("token") or None
        # Shared service-to-service bearer for outbound calls to stt/tts/llm/speaker.
        # KENZY_SERVICE_TOKEN (a real env var, seen by all services) is canonical;
        # discovery.token is a fallback for single-host setups.
        self._service_token: str | None = os.environ.get("KENZY_SERVICE_TOKEN") or self._join_token

        # room_id → NodeSession  (guarded by _lock)
        self._nodes: dict[str, NodeSession] = {}
        self._lock = asyncio.Lock()
        # Observers notified when the node registry/state changes (the dashboard
        # registers one for live push). Empty by default ⇒ zero overhead.
        self._state_listeners: list[Callable[[], None]] = []
        # Pull-based logs: when the dashboard's `logs` flag is on it sets this, and
        # nodes are told (config `keep_logs`) to keep a buffer. Off ⇒ no node cost.
        self._capture_node_logs: bool = False
        self._log_waiters: dict[str, asyncio.Future[list[dict[str, Any]]]] = {}

    def add_state_listener(self, fn: Callable[[], None]) -> None:
        """Register a callback fired (in-loop) when the node registry/state changes."""
        self._state_listeners.append(fn)

    def _notify_state(self) -> None:
        for fn in self._state_listeners:
            try:
                fn()
            except Exception:  # a listener must never break the pipeline
                log.debug("state listener error", exc_info=True)

    def _service_headers(self) -> dict[str, str]:
        """Bearer header for outbound backend calls (empty when no token set)."""
        return {"Authorization": f"Bearer {self._service_token}"} if self._service_token else {}

    # ------------------------------------------------------------------
    # Config-pull: effective per-node config = defaults + per-room override
    # ------------------------------------------------------------------

    def _effective_node_config(self, room_id: str) -> dict[str, Any]:
        """Merge central ``node_defaults`` with ``configs/nodes/<room>.yaml``.

        The per-room file (if present) shallow-overrides the defaults. A node
        with no override file just receives the defaults; absence is logged so
        operators can see which rooms are unconfigured.
        """
        import yaml  # type: ignore[import-untyped]

        from kenzy.config import kenzy_data_root

        effective: dict[str, Any] = dict(self._node_defaults)
        override = kenzy_data_root() / "configs" / "nodes" / f"{room_id}.yaml"
        if override.is_file():
            try:
                data = yaml.safe_load(override.read_text()) or {}
                if isinstance(data, dict):
                    effective.update(data)
                log.info("[%s] applied per-room override %s", room_id, override)
            except Exception as exc:
                log.warning("[%s] failed to read override %s: %s", room_id, override, exc)
        else:
            log.info("[%s] no per-room override (%s) — sending defaults only", room_id, override)
        # Invariant: secrets never leave the server, even if an operator put one in
        # node_defaults by mistake.
        leaked = [k for k in effective if _SECRET_KEY_RE.search(k)]
        for k in leaked:
            del effective[k]
        if leaked:
            log.warning("[%s] dropped secret-like keys from served config: %s", room_id, leaked)
        if self._capture_node_logs:
            effective["keep_logs"] = True
        return effective

    async def request_node_logs(
        self, room_id: str, level: str = "", limit: int = 200, timeout: float = 5.0
    ) -> list[dict[str, Any]] | None:
        """Ask a node for its log buffer and await the reply (None on failure)."""
        session = self._nodes.get(room_id)
        if session is None:
            return None
        req_id = str(uuid.uuid4())
        fut: asyncio.Future[list[dict[str, Any]]] = asyncio.get_running_loop().create_future()
        self._log_waiters[req_id] = fut
        try:
            await session.ws.send(protocol.request_logs(req_id, level, limit))
            return await asyncio.wait_for(fut, timeout)
        except Exception:
            return None
        finally:
            self._log_waiters.pop(req_id, None)

    # ------------------------------------------------------------------
    # Per-room override read/write (dashboard config editor) + live re-push
    # ------------------------------------------------------------------

    @staticmethod
    def allowed_override_keys() -> list[str]:
        return sorted(_ALLOWED_OVERRIDE_KEYS)

    def _override_path(self, room_id: str) -> Path:
        from kenzy.config import kenzy_data_root

        if room_id in (".", "..") or not _SAFE_ROOM_RE.fullmatch(room_id):
            raise ValueError("invalid room id")
        return kenzy_data_root() / "configs" / "nodes" / f"{room_id}.yaml"

    def read_node_override(self, room_id: str) -> dict[str, Any]:
        import yaml

        path = self._override_path(room_id)
        if not path.is_file():
            return {}
        data = yaml.safe_load(path.read_text()) or {}
        return data if isinstance(data, dict) else {}

    def write_node_override(self, room_id: str, mapping: dict[str, Any]) -> None:
        """Validate and persist configs/nodes/<room>.yaml (empty ⇒ remove file)."""
        import yaml

        if not isinstance(mapping, dict):
            raise ValueError("override must be a mapping")
        unknown = sorted(k for k in mapping if k not in _ALLOWED_OVERRIDE_KEYS)
        if unknown:
            raise ValueError("unsupported keys: " + ", ".join(unknown))
        path = self._override_path(room_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if mapping:
            path.write_text(yaml.safe_dump(dict(sorted(mapping.items())), default_flow_style=False))
            log.info("[%s] wrote per-room override (%d keys)", room_id, len(mapping))
        elif path.is_file():
            path.unlink()
            log.info("[%s] cleared per-room override", room_id)

    async def push_config(self, room_id: str) -> bool:
        """Re-push effective config to a connected node (live config_update)."""
        session = self._nodes.get(room_id)
        if session is None:
            return False
        await session.ws.send(protocol.config(self._effective_node_config(room_id)))
        self._notify_state()
        return True

    # ------------------------------------------------------------------
    # Friendly room names (dashboard label only — never sent to the node)
    # ------------------------------------------------------------------

    def _names_path(self) -> Path:
        from kenzy.config import kenzy_data_root

        return kenzy_data_root() / "configs" / "node_names.yaml"

    def read_node_names(self) -> dict[str, str]:
        import yaml

        path = self._names_path()
        if not path.is_file():
            return {}
        data = yaml.safe_load(path.read_text()) or {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def set_display_name(self, room_id: str, name: str) -> None:
        """Set/clear a room's friendly display name (empty ⇒ remove)."""
        import yaml

        if room_id in (".", "..") or not _SAFE_ROOM_RE.fullmatch(room_id):
            raise ValueError("invalid room id")
        name = name.strip()
        if len(name) > 64:
            raise ValueError("name too long (max 64)")
        names = self.read_node_names()
        if name:
            names[room_id] = name
        else:
            names.pop(room_id, None)
        path = self._names_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if names:
            path.write_text(yaml.safe_dump(dict(sorted(names.items())), default_flow_style=False))
        elif path.is_file():
            path.unlink()

    # ------------------------------------------------------------------
    # Pipeline hooks (override in subclasses or replace at runtime)
    # ------------------------------------------------------------------

    async def on_session_start(self, session: NodeSession) -> None:
        """Called when a node sends audio_start."""

    async def on_audio_frame(self, session: NodeSession, data: bytes) -> None:
        """Called for every binary audio frame received while streaming."""

    async def on_session_end(self, session: NodeSession, reason: str) -> None:
        """Called when a node sends audio_end."""

    async def on_wakeword(self, session: NodeSession, model: str, score: float) -> None:
        """Called when a node detects a wake word while already streaming."""

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    async def _handle(self, ws: ServerConnection) -> None:
        session: NodeSession | None = None
        try:
            session = await self._register(ws)
            if session is None:
                return
            await self._node_loop(session)
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as exc:
            log.error(
                "Handler error [%s]: %s",
                session.room_id if session else "unregistered",
                exc,
                exc_info=True,
            )
        finally:
            if session is not None:
                if session.streaming:
                    await self.on_session_end(session, "disconnect")
                await self._deregister(session)

    async def _register(self, ws: ServerConnection) -> NodeSession | None:
        """Wait for the initial HELLO and register the node."""
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            msg = json.loads(raw)
        except (TimeoutError, json.JSONDecodeError):
            log.warning("Registration timeout or bad JSON from %s", ws.remote_address)
            return None

        if msg.get("type") != protocol.MSG_HELLO:
            log.warning("Expected hello, got '%s' from %s", msg.get("type"), ws.remote_address)
            return None

        if self._join_token is not None and msg.get("token") != self._join_token:
            log.warning("Rejected node from %s: bad/missing join token", ws.remote_address)
            try:
                await ws.close(1008, "invalid join token")
            except Exception:
                pass
            return None

        room_id: str = str(msg.get("room_id", "unknown"))
        caps = msg.get("capabilities") or {}
        if caps:
            log.info("[%s] capabilities: %s", room_id, caps)
        session = NodeSession(ws=ws, room_id=room_id)

        async with self._lock:
            old = self._nodes.get(room_id)
            self._nodes[room_id] = session

        if old is not None:
            log.info("Node '%s' reconnected; closing old connection", room_id)
            try:
                await old.ws.close(1001, "replaced by reconnect")
            except Exception:
                pass

        log.info(
            "Node '%s' registered (%s) – %d node(s) connected",
            room_id,
            ws.remote_address,
            len(self._nodes),
        )

        # Config-pull: push the node's effective config so it needs no local file.
        effective = self._effective_node_config(room_id)
        if effective:
            try:
                await ws.send(protocol.config(effective))
            except Exception as exc:
                log.warning("[%s] failed to send config: %s", room_id, exc)

        self._notify_state()
        return session

    async def _deregister(self, session: NodeSession) -> None:
        async with self._lock:
            if self._nodes.get(session.room_id) is session:
                del self._nodes[session.room_id]
        log.info(
            "Node '%s' disconnected – %d node(s) remaining",
            session.room_id,
            len(self._nodes),
        )
        self._notify_state()

    # ------------------------------------------------------------------
    # Per-node message loop
    # ------------------------------------------------------------------

    async def _node_loop(self, session: NodeSession) -> None:
        async for raw in session.ws:
            if isinstance(raw, bytes):
                if session.streaming:
                    await self.on_audio_frame(session, raw)
            else:
                try:
                    await self._handle_control(session, json.loads(raw))
                except json.JSONDecodeError:
                    pass

    async def _handle_control(self, session: NodeSession, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")

        if mtype == protocol.MSG_AUDIO_START:
            session.session_id = msg.get("session_id")
            session.streaming = True
            log.info(
                "[%s/%s] audio_start",
                session.room_id,
                (session.session_id or "?")[:8],
            )
            await self.on_session_start(session)
            await session.send_json({"type": protocol.MSG_ACK, "session_id": session.session_id})
            self._notify_state()

        elif mtype == protocol.MSG_AUDIO_END:
            session.streaming = False
            self._notify_state()
            reason = msg.get("reason", "unknown")
            log.info(
                "[%s/%s] audio_end reason=%s",
                session.room_id,
                (session.session_id or "?")[:8],
                reason,
            )
            await self.on_session_end(session, reason)
            session.session_id = None

        elif mtype == protocol.MSG_WAKEWORD:
            model = str(msg.get("model", ""))
            score = float(msg.get("score", 0.0))
            log.info(
                "[%s/%s] wakeword model=%s score=%.4f",
                session.room_id,
                (session.session_id or "?")[:8],
                model,
                score,
            )
            await self.on_wakeword(session, model, score)

        elif mtype == protocol.MSG_LOGS:
            fut = self._log_waiters.get(str(msg.get("request_id", "")))
            if fut is not None and not fut.done():
                fut.set_result(msg.get("logs") or [])

        else:
            log.debug("[%s] unhandled control msg: %s", session.room_id, mtype)

    # ------------------------------------------------------------------
    # Outbound commands
    # ------------------------------------------------------------------

    async def trigger_node(self, room_id: str, session_id: str | None = None) -> bool:
        """Send a TRIGGER command to a named node.  Returns False if not connected."""
        async with self._lock:
            session = self._nodes.get(room_id)
        if session is None:
            log.warning("trigger_node: '%s' is not connected", room_id)
            return False
        sid = session_id or str(uuid.uuid4())
        try:
            await session.ws.send(protocol.trigger(sid))
        except websockets.exceptions.ConnectionClosed:
            log.warning("trigger_node: '%s' disconnected before send", room_id)
            return False
        log.info("Triggered '%s' session=%s", room_id, sid[:8])
        return True

    async def stop_node(self, room_id: str) -> bool:
        """Send a STOP command to a named node.  Returns False if not connected."""
        async with self._lock:
            session = self._nodes.get(room_id)
        if session is None:
            log.warning("stop_node: '%s' is not connected", room_id)
            return False
        try:
            await session.ws.send(protocol.stop())
        except websockets.exceptions.ConnectionClosed:
            log.warning("stop_node: '%s' disconnected before send", room_id)
            return False
        log.info("Stopped '%s'", room_id)
        return True

    async def broadcast_trigger(self) -> int:
        """Trigger all connected nodes.  Returns number of nodes triggered."""
        async with self._lock:
            targets = list(self._nodes.values())
        count = 0
        for session in targets:
            try:
                await session.ws.send(protocol.trigger())
                count += 1
            except Exception as exc:
                log.warning("broadcast_trigger failed for '%s': %s", session.room_id, exc)
        return count

    async def restart_node(self, room_id: str) -> bool:
        """Ask a connected node to re-exec itself."""
        session = self._nodes.get(room_id)
        if session is None:
            log.warning("restart_node: '%s' is not connected", room_id)
            return False
        try:
            await session.ws.send(protocol.restart())
            return True
        except Exception as exc:
            log.warning("restart_node: '%s' send failed: %s", room_id, exc)
            return False

    async def announce(self, text: str, rooms: list[str] | None = None) -> int:
        """Speak ``text`` aloud on target rooms (all connected if None).

        Returns the number of nodes addressed. The base server has no TTS
        pipeline; ``TranscribingServer`` provides the real implementation.
        """
        return 0

    def connected_nodes(self) -> list[str]:
        return list(self._nodes.keys())

    # ------------------------------------------------------------------
    # Serve forever
    # ------------------------------------------------------------------

    async def serve(self) -> None:
        log.info("Kenzy server listening on %s:%d", self._host, self._port)
        async with websockets.serve(self._handle, self._host, self._port):
            await asyncio.Future()  # run until cancelled


# ---------------------------------------------------------------------------
# CLI entry point  (basic server + stdin control for testing)
# ---------------------------------------------------------------------------


async def _stdin_control(server: AudioServer) -> None:
    """Read simple commands from stdin for manual testing."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

    while True:
        print("> ", end="", flush=True)
        line_bytes = await reader.readline()
        if not line_bytes:  # EOF / pipe closed
            break
        parts = line_bytes.decode().strip().split()
        if not parts:
            continue
        cmd, *args = parts
        if cmd == "list":
            nodes = server.connected_nodes()
            print("Connected nodes:", nodes if nodes else "(none)")
        elif cmd == "trigger" and args:
            ok = await server.trigger_node(args[0])
            print("trigger:", "sent" if ok else "node not found")
        elif cmd == "stop" and args:
            ok = await server.stop_node(args[0])
            print("stop:", "sent" if ok else "node not found")
        elif cmd == "broadcast":
            n = await server.broadcast_trigger()
            print(f"broadcast: triggered {n} node(s)")
        else:
            print("commands: list | trigger <room_id> | stop <room_id> | broadcast")


# ---------------------------------------------------------------------------
# STT pipeline
# ---------------------------------------------------------------------------

_STOP_PHRASES: frozenset[str] = frozenset(
    {
        "stop",
        "be quiet",
        "quiet",
        "shut up",
        "silence",
        "please stop",
        "please be quiet",
        "please shut up",
        "shut the heck up",
    }
)


class TranscribingServer(AudioServer):
    """
    AudioServer subclass that transcribes each captured utterance with
    faster-whisper and logs the result.

    Concurrency model
    -----------------
    - One audio buffer per room_id, held in ``_buffers``.
    - One asyncio Task per room_id for in-flight transcription, held in
      ``_stt_tasks``.
    - A new activation from the same node (on_session_start or on_wakeword)
      cancels any in-flight transcription for that room before proceeding.
    - Transcription runs in the default thread-pool executor so the event
      loop is never blocked.
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)

        self._buffers: dict[str, bytearray] = {}
        self._stt_tasks: dict[str, asyncio.Task[None]] = {}
        # Rooms currently playing TTS — used to decide whether to send STOP on wakeword.
        self._tts_active: set[str] = set()

        scfg: dict[str, Any] = cfg.get("stt", {})
        self._stt_url: str | None = str(scfg["url"]) if scfg.get("url") else None
        self._stt_timeout: float = float(scfg.get("timeout", 60.0))
        if self._stt_url:
            log.info("STT service: %s (timeout=%.0fs)", self._stt_url, self._stt_timeout)
        else:
            log.warning("STT service not configured — audio will not be transcribed.")

        tcfg: dict[str, Any] = cfg.get("tts", {})
        self._tts_url: str | None = str(tcfg["url"]) if tcfg.get("url") else None
        self._tts_timeout: float = float(tcfg.get("timeout", 60.0))
        self._tts_chunk_size: int = int(tcfg.get("chunk_size", 4096))
        if self._tts_url:
            log.info("TTS service: %s (timeout=%.0fs)", self._tts_url, self._tts_timeout)
        else:
            log.info("TTS service not configured — responses will not be spoken.")

        lcfg: dict[str, Any] = cfg.get("llm", {})
        self._llm_url: str | None = str(lcfg["url"]) if lcfg.get("url") else None
        self._llm_timeout: float = float(lcfg.get("timeout", 30.0))
        if self._llm_url:
            log.info("LLM service: %s (timeout=%.0fs)", self._llm_url, self._llm_timeout)
        else:
            log.info("LLM service not configured — STT results will be logged only.")

        spcfg: dict[str, Any] = cfg.get("speaker", {})
        self._speaker_url: str | None = str(spcfg["url"]) if spcfg.get("url") else None
        self._speaker_timeout: float = float(spcfg.get("timeout", 10.0))
        self._unknown_speaker: str = str(spcfg.get("unknown_speaker", "unknown"))
        if self._speaker_url:
            log.info(
                "Speaker service: %s (timeout=%.0fs)", self._speaker_url, self._speaker_timeout
            )
        else:
            log.info(
                "Speaker service not configured — speaker will be '%s'.", self._unknown_speaker
            )

    # ------------------------------------------------------------------
    # Pipeline hooks
    # ------------------------------------------------------------------

    async def on_session_start(self, session: NodeSession) -> None:
        self._cancel_stt(session.room_id)
        self._tts_active.discard(session.room_id)
        self._buffers[session.room_id] = bytearray()

    async def on_audio_frame(self, session: NodeSession, data: bytes) -> None:
        buf = self._buffers.get(session.room_id)
        if buf is not None:
            buf += data

    async def on_session_end(self, session: NodeSession, reason: str) -> None:
        pcm = bytes(self._buffers.pop(session.room_id, b""))
        if not pcm:
            return
        task = asyncio.create_task(
            self._transcribe(session.room_id, session.session_id, pcm),
            name=f"stt-{session.room_id}",
        )
        self._stt_tasks[session.room_id] = task

    async def on_wakeword(self, session: NodeSession, model: str, score: float) -> None:
        self._cancel_stt(session.room_id)
        # If the node is not currently streaming audio to us it may be playing
        # TTS or waiting idle — send STOP so it can interrupt and re-activate.
        if not session.streaming:
            await self.stop_node(session.room_id)
            self._tts_active.discard(session.room_id)

    # ------------------------------------------------------------------
    # TTS helpers  (called by the LLM/TTS pipeline phase)
    # ------------------------------------------------------------------

    async def send_tts_start(
        self, room_id: str, session_id: str, sample_rate: int = 22050, channels: int = 1
    ) -> bool:
        """Tell a node to enter TTS mode and begin accepting audio frames."""
        async with self._lock:
            session = self._nodes.get(room_id)
        if session is None:
            return False
        try:
            await session.ws.send(protocol.tts_start(session_id, sample_rate, channels))
            self._tts_active.add(room_id)
            return True
        except websockets.exceptions.ConnectionClosed:
            return False

    async def send_tts_frame(self, room_id: str, data: bytes) -> bool:
        """Stream a raw PCM frame to a node currently in TTS mode."""
        async with self._lock:
            session = self._nodes.get(room_id)
        if session is None:
            return False
        try:
            await session.ws.send(data)
            return True
        except websockets.exceptions.ConnectionClosed:
            self._tts_active.discard(room_id)
            return False

    async def send_tts_end(self, room_id: str, session_id: str) -> bool:
        """Tell a node that the TTS stream is complete."""
        self._tts_active.discard(room_id)
        async with self._lock:
            session = self._nodes.get(room_id)
        if session is None:
            return False
        try:
            await session.ws.send(protocol.tts_end(session_id))
            return True
        except websockets.exceptions.ConnectionClosed:
            return False

    # ------------------------------------------------------------------
    # STT helpers
    # ------------------------------------------------------------------

    def _cancel_stt(self, room_id: str) -> None:
        task = self._stt_tasks.pop(room_id, None)
        if task and not task.done():
            task.cancel()
            log.info("[%s] STT cancelled", room_id)

    async def _transcribe(self, room_id: str, session_id: str | None, pcm: bytes) -> None:
        try:
            if not self._stt_url:
                return

            # STT and speaker ID run in parallel on the same PCM buffer.
            stt_coro = self._call_stt(pcm, room_id, session_id)
            if self._speaker_url:
                (text, speaker) = await asyncio.gather(
                    stt_coro,
                    self._call_speaker(pcm, room_id),
                )
            else:
                text = await stt_coro
                speaker = self._unknown_speaker

            log.info("[%s] STT: %s | speaker: %s", room_id, text or "(none)", speaker)

            if not text:
                await self.stop_node(room_id)
                self._tts_active.discard(room_id)
                return

            normalized = re.sub(r"[^\w\s]", "", text).strip().lower()
            if normalized in _STOP_PHRASES:
                log.info("[%s] stop phrase detected (%r) — ending session", room_id, text)
                await self.stop_node(room_id)
                self._tts_active.discard(room_id)
                return

            if self._llm_url:
                response_text, voice_prompt = await self._call_llm(
                    text, room_id, session_id, speaker
                )
                log.info("[%s] LLM: %s", room_id, response_text)
                log.debug("[%s] voice_prompt: %s", room_id, voice_prompt)
                await self._run_tts(room_id, session_id, response_text, voice_prompt)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("[%s] pipeline error: %s", room_id, exc, exc_info=True)
        finally:
            self._stt_tasks.pop(room_id, None)

    async def _call_speaker(self, pcm: bytes, room_id: str) -> str:
        import base64

        import httpx  # type: ignore[import-untyped]

        payload = {"audio_b64": base64.b64encode(pcm).decode(), "room_id": room_id}
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self._speaker_url,  # type: ignore[arg-type]
                    json=payload,
                    timeout=self._speaker_timeout,
                    headers=self._service_headers(),
                )
                resp.raise_for_status()
            return str(resp.json()["speaker"])
        except Exception as exc:
            log.warning("[%s] speaker ID failed: %s", room_id, exc)
            return self._unknown_speaker

    async def _call_stt(self, pcm: bytes, room_id: str, session_id: str | None) -> str:
        import base64

        import httpx  # type: ignore[import-untyped]

        payload = {
            "audio_b64": base64.b64encode(pcm).decode(),
            "room_id": room_id,
            "session_id": session_id,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._stt_url,  # type: ignore[arg-type]
                json=payload,
                timeout=self._stt_timeout,
                headers=self._service_headers(),
            )
            resp.raise_for_status()
        return str(resp.json()["text"])

    async def _call_llm(
        self, text: str, room_id: str, session_id: str | None, speaker: str | None = None
    ) -> tuple[str, str]:
        import httpx  # type: ignore[import-untyped]

        payload = {
            "text": text,
            "room_id": room_id,
            "session_id": session_id,
            "speaker": speaker,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._llm_url,  # type: ignore[arg-type]
                json=payload,
                timeout=self._llm_timeout,
                headers=self._service_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        return str(data["text"]), str(data["voice_prompt"])

    async def _run_tts(
        self, room_id: str, session_id: str | None, text: str, voice_prompt: str
    ) -> None:
        if not self._tts_url:
            return

        import httpx  # type: ignore[import-untyped]

        sid = session_id or str(uuid.uuid4())
        await self.send_tts_start(room_id, sid, sample_rate=24000, channels=1)
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self._tts_url,
                    json={"text": text, "voice_prompt": voice_prompt, "room_id": room_id},
                    timeout=self._tts_timeout,
                    headers=self._service_headers(),
                )
                resp.raise_for_status()
            pcm = resp.content
            for i in range(0, len(pcm), self._tts_chunk_size):
                if not await self.send_tts_frame(room_id, pcm[i : i + self._tts_chunk_size]):
                    return  # node disconnected mid-stream
            await self.send_tts_end(room_id, sid)
            log.info("[%s] TTS complete", room_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("[%s] TTS error: %s", room_id, exc, exc_info=True)
            await self.send_tts_end(room_id, sid)
        finally:
            if room_id in self._tts_active:
                await self.stop_node(room_id)
                self._tts_active.discard(room_id)

    # ------------------------------------------------------------------
    # Announcements: synth once, play on every (or selected) room
    # ------------------------------------------------------------------

    async def _synthesize(self, text: str, voice_prompt: str) -> bytes | None:
        if not self._tts_url:
            return None
        import httpx

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self._tts_url,
                    json={"text": text, "voice_prompt": voice_prompt, "room_id": "announce"},
                    timeout=self._tts_timeout,
                    headers=self._service_headers(),
                )
                resp.raise_for_status()
            return resp.content
        except Exception as exc:
            log.error("announce TTS synth failed: %s", exc)
            return None

    async def _stream_pcm(self, room_id: str, pcm: bytes) -> None:
        sid = str(uuid.uuid4())
        if not await self.send_tts_start(room_id, sid, sample_rate=24000, channels=1):
            return
        for i in range(0, len(pcm), self._tts_chunk_size):
            if not await self.send_tts_frame(room_id, pcm[i : i + self._tts_chunk_size]):
                return
        await self.send_tts_end(room_id, sid)

    async def announce(self, text: str, rooms: list[str] | None = None) -> int:
        text = text.strip()
        if not text or not self._tts_url:
            return 0
        async with self._lock:
            targets = [r for r in (rooms or list(self._nodes)) if r in self._nodes]
        if not targets:
            return 0
        pcm = await self._synthesize(text, _ANNOUNCE_VOICE_PROMPT)
        if not pcm:
            return 0
        await asyncio.gather(*(self._stream_pcm(r, pcm) for r in targets), return_exceptions=True)
        log.info("Announced to %d node(s): %r", len(targets), text[:60])
        return len(targets)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import yaml
    from dotenv import load_dotenv  # type: ignore[import-untyped]

    load_dotenv()

    from kenzy.config import resolve_config

    config_path = resolve_config("server", sys.argv[1] if len(sys.argv) > 1 else None)
    with open(config_path) as fh:
        cfg = yaml.safe_load(fh)

    log_level: int = getattr(logging, str(cfg.get("log_level", "info")).upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=logging.WARNING, format=fmt)
    logging.getLogger("kenzy").setLevel(log_level)

    server = TranscribingServer(cfg)

    # mDNS advertisement so nodes can discover this server without a server_url.
    discovery_cfg = cfg.get("discovery", {}) or {}
    advertiser = None
    if discovery_cfg.get("enabled", True):
        from kenzy.discovery import ServerAdvertiser

        try:
            version = importlib.metadata.version("kenzy")
        except importlib.metadata.PackageNotFoundError:
            version = "0"
        auth = "required" if discovery_cfg.get("token") else "none"
        advertiser = ServerAdvertiser(
            port=server._port,
            host=server._host,
            instance=str(discovery_cfg.get("instance", "kenzy-server")),
            properties={"version": version, "auth": auth},
        )

    # Dashboard: opt-in and only wired up when enabled (zero overhead when off).
    dashboard = None
    if (cfg.get("dashboard", {}) or {}).get("enabled", False):
        from kenzy.server.dashboard import Dashboard, DashboardConfig

        dashboard = Dashboard(server, cfg, DashboardConfig.from_cfg(cfg))

    async def _main() -> None:
        coros: list[Any] = [server.serve()]
        if dashboard is not None:
            coros.append(dashboard.serve())
        if sys.stdin.isatty():
            coros.append(_stdin_control(server))
        await asyncio.gather(*coros)

    if advertiser is not None:
        advertiser.start()
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    finally:
        if advertiser is not None:
            advertiser.stop()


if __name__ == "__main__":
    main()
