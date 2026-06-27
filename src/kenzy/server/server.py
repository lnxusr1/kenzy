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
import hmac
import json
import logging
import os
import re
import sys
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websockets
import websockets.exceptions
import websockets.server
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from kenzy import kenzy_version, protocol
from kenzy.config import SERVICES
from kenzy.serviceauth import check_bearer
from kenzy.speaker import DEFAULT_ENROLL_PROMPTS

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
        "sound_connect",
        "sound_disconnect",
        "log_level",
        "log_capture_level",
        "volume",
    }
)
# Server-owned keys stored in the per-node override file and pushed via config-pull,
# but NOT edited through the generic config grid (they have dedicated UI / actions).
# The node applies them from the config frame. Preserved across editor saves.
_SERVER_MANAGED_KEYS = frozenset({"room_id"})
_SECRET_KEY_RE = re.compile(r"key|token|secret|password|passwd|credential", re.IGNORECASE)
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
_ANNOUNCE_VOICE_PROMPT = "Read this aloud as a clear, calm public announcement."
_INTERCOM_VOICE_PROMPT = "Read this aloud as a brief, friendly spoken notification."
# How long to wait for the receiver's spoken consent before declining (no answer).
_CALL_TIMEOUT_SEC = 25.0
# Voice enrollment: one sample per configured prompt (see _enroll_prompts), min bytes
# for a usable sample, extra retries allowed beyond the prompt count for unclear audio,
# and how long an idle enrollment session lives before it's abandoned.
_ENROLL_MIN_PCM_BYTES = 16000  # ~0.5 s of 16 kHz int16 — shorter captures are retried
_ENROLL_MAX_RETRIES = 4
_ENROLL_TIMEOUT_SEC = 120.0

# Resource caps (F-10): bound a single capture buffer and inbound WS frame size, and
# rate-limit new connections per source IP, so a hostile/buggy LAN peer can't exhaust
# memory or hammer the listener.
_MAX_SESSION_PCM_BYTES = 16_000 * 2 * 120  # ~2 min of 16 kHz int16 (~3.8 MB) per capture
_MAX_WS_FRAME = 65_536  # node→server frames are tiny (2.5 KB audio / small JSON)
_CONN_RATE_MAX = 30  # max new connections per source IP …
_CONN_RATE_WINDOW = 60.0  # … within this many seconds
# Peer service URLs the server injects into a dependent service's served config so they
# aren't duplicated in two places (an override in the service's own config still wins).
# Only speaker (its kenzy-enroll CLI) needs TTS today.
_SERVICE_PEERS: dict[str, tuple[str, ...]] = {"speaker": ("tts",)}
# Words that count as accepting an incoming call. Default-deny: anything else declines.
_AFFIRM_WORDS = frozenset(
    {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "accept", "accepted", "affirmative"}
)


def _is_affirmative(text: str) -> bool:
    """True only for a clear yes (default-deny on silence/ambiguity/no)."""
    norm = re.sub(r"[^\w\s]", "", text or "").strip().lower()
    if not norm:
        return False
    return bool(set(norm.split()) & _AFFIRM_WORDS) or "go ahead" in norm


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base`` (override wins)."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# Server settings the dashboard may edit (dotted path → type). Deliberately excludes
# lockout/secret-risky keys — server/dashboard bind+port, dashboard.auth.*, and
# discovery.token stay file/CLI-managed (see design/centralized-config.md M4).
_SERVER_EDITABLE: dict[str, str] = {
    "dashboard.logs": "bool",
    "dashboard.controls": "bool",
    "stt.url": "str",
    "stt.timeout": "num",
    "tts.url": "str",
    "tts.timeout": "num",
    "llm.url": "str",
    "llm.timeout": "num",
    "speaker.url": "str",
    "speaker.timeout": "num",
    "speaker.unknown_speaker": "str",
    "discovery.enabled": "bool",
    "discovery.instance": "str",
    # Home Assistant / MQTT integration (no secrets — broker creds are env-only).
    "integrations.mqtt.enabled": "bool",
    "integrations.mqtt.host": "str",
    "integrations.mqtt.port": "num",
    "integrations.mqtt.base_topic": "str",
    "integrations.mqtt.discovery_prefix": "str",
    "integrations.mqtt.commands": "bool",
}


def _server_override_path(config_path: str | Path) -> Path:
    """Dashboard-written server settings live beside server.yaml, layered over it
    (keeps the hand-edited server.yaml + its comments untouched)."""
    return Path(config_path).parent / "server.local.yaml"


def load_server_config(config_path: str | Path) -> dict[str, Any]:
    """Load server.yaml deep-merged with its ``server.local.yaml`` override (if any)."""
    import yaml  # type: ignore[import-untyped]

    with open(config_path) as fh:
        cfg = yaml.safe_load(fh) or {}
    if not isinstance(cfg, dict):
        cfg = {}
    ov = _server_override_path(config_path)
    if ov.is_file():
        try:
            data = yaml.safe_load(ov.read_text()) or {}
            if isinstance(data, dict):
                cfg = _deep_merge(cfg, data)
                log.info("Applied server override %s", ov)
        except Exception as exc:
            log.warning("failed to read server override %s: %s", ov, exc)
    return cfg


def _strip_secrets(data: dict[str, Any]) -> list[str]:
    """Recursively delete secret-like keys in place; return the dotted paths dropped."""
    dropped: list[str] = []

    def _walk(d: dict[str, Any], prefix: str) -> None:
        for key in list(d):
            path = f"{prefix}{key}"
            if _SECRET_KEY_RE.search(key):
                del d[key]
                dropped.append(path)
            elif isinstance(d[key], dict):
                _walk(d[key], f"{path}.")

    _walk(data, "")
    return dropped


# ---------------------------------------------------------------------------
# Per-node session state
# ---------------------------------------------------------------------------


@dataclass
class NodeSession:
    ws: ServerConnection
    node_id: str
    room_id: str
    session_id: str | None = field(default=None)
    streaming: bool = field(default=False)
    # node_id of the peer this node is in a live intercom call with (None = not in a call).
    intercom_peer: str | None = field(default=None)
    # Node health: False once a node reports audio init failed (it stays connected so
    # it can be fixed + restarted from the dashboard). Defaults True (healthy).
    audio_ok: bool = field(default=True)
    audio_error: str | None = field(default=None)
    # Capabilities announced in `hello` (audio device + the device probe used by the
    # dashboard's device picker). Not persisted; refreshed on each connect.
    capabilities: dict[str, Any] = field(default_factory=dict)
    # Installed kenzy package version the node reported in `hello` (None = legacy node
    # that didn't send one). For the dashboard's per-host version view.
    kenzy_version: str | None = field(default=None)

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
        # Backend service URLs the server is configured with, injected into dependent
        # services' served config (see _SERVICE_PEERS) so they aren't duplicated.
        self._peer_service_urls: dict[str, str] = {
            s: str((cfg.get(s) or {}).get("url"))
            for s in ("stt", "tts", "llm", "speaker")
            if isinstance(cfg.get(s), dict) and (cfg.get(s) or {}).get("url")
        }
        # Optional shared-secret required in the node's hello (discovery.token).
        self._join_token: str | None = (cfg.get("discovery", {}) or {}).get("token") or None
        # Shared service-to-service bearer for outbound calls to stt/tts/llm/speaker.
        # KENZY_SERVICE_TOKEN (a real env var, seen by all services) is canonical;
        # discovery.token is a fallback for single-host setups.
        self._service_token: str | None = os.environ.get("KENZY_SERVICE_TOKEN") or self._join_token

        # node_id → NodeSession  (guarded by _lock)
        self._nodes: dict[str, NodeSession] = {}
        self._lock = asyncio.Lock()
        # Per-source-IP connection timestamps for the registration rate limit (F-10).
        self._conn_log: dict[str, deque[float]] = {}
        # Observers notified when the node registry/state changes (the dashboard
        # registers one for live push). Empty by default ⇒ zero overhead.
        self._state_listeners: list[Callable[[], None]] = []
        # Pull-based logs: when the dashboard's `logs` flag is on it sets this, and
        # nodes are told (config `keep_logs`) to keep a buffer. Off ⇒ no node cost.
        self._capture_node_logs: bool = False
        self._log_waiters: dict[str, asyncio.Future[list[dict[str, Any]]]] = {}
        # Transient (non-persisted) per-node config overlays + their revert timers,
        # used by the dashboard's temporary TRACE log boost.
        self._transient_node_cfg: dict[str, dict[str, Any]] = {}
        self._boost_tasks: dict[str, asyncio.Task[None]] = {}
        # Calibration telemetry relay: the dashboard registers a listener and the
        # node's per-frame tune samples are forwarded to it. Empty ⇒ zero overhead.
        self._tune_listeners: list[Callable[[str, dict[str, Any]], None]] = []
        # Pipeline observability: the dashboard registers a listener fed a record per
        # completed command pipeline. Empty ⇒ no record is built (no transcript kept).
        self._session_listeners: list[Callable[[dict[str, Any]], None]] = []

    def add_state_listener(self, fn: Callable[[], None]) -> None:
        """Register a callback fired (in-loop) when the node registry/state changes."""
        self._state_listeners.append(fn)

    def _notify_state(self) -> None:
        for fn in self._state_listeners:
            try:
                fn()
            except Exception:  # a listener must never break the pipeline
                log.debug("state listener error", exc_info=True)

    def add_tune_listener(self, fn: Callable[[str, dict[str, Any]], None]) -> None:
        """Register a callback fired with ``(node_id, sample)`` for each tune sample."""
        self._tune_listeners.append(fn)

    def add_session_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback fired with a completed-pipeline record (observability)."""
        self._session_listeners.append(fn)

    def _notify_session(self, record: dict[str, Any]) -> None:
        for fn in self._session_listeners:
            try:
                fn(record)
            except Exception:
                log.debug("session listener error", exc_info=True)

    def _notify_tune(self, node_id: str, sample: dict[str, Any]) -> None:
        for fn in self._tune_listeners:
            try:
                fn(node_id, sample)
            except Exception:
                log.debug("tune listener error", exc_info=True)

    async def start_node_tuning(self, node_id: str, seconds: float = 20.0) -> bool:
        """Ask a connected node to begin a bounded calibration window."""
        session = self._nodes.get(node_id)
        if session is None:
            return False
        try:
            await session.ws.send(protocol.tune_start(seconds))
            return True
        except Exception as exc:
            log.warning("start_node_tuning: %s send failed: %s", node_id, exc)
            return False

    async def stop_node_tuning(self, node_id: str) -> bool:
        """Ask a connected node to end calibration early."""
        session = self._nodes.get(node_id)
        if session is None:
            return False
        try:
            await session.ws.send(protocol.tune_stop())
            return True
        except Exception:
            return False

    def _service_headers(self) -> dict[str, str]:
        """Bearer header for outbound backend calls (empty when no token set)."""
        return {"Authorization": f"Bearer {self._service_token}"} if self._service_token else {}

    # ------------------------------------------------------------------
    # Config-pull: effective per-node config = defaults + per-room override
    # ------------------------------------------------------------------

    def _effective_node_config(self, node_id: str) -> dict[str, Any]:
        """Merge central ``node_defaults`` with ``configs/nodes/<node_id>.yaml``.

        The per-node file (if present) shallow-overrides the defaults. A node
        with no override file just receives the defaults; absence is logged so
        operators can see which nodes are unconfigured.
        """
        import yaml  # type: ignore[import-untyped]

        from kenzy.config import kenzy_data_root

        effective: dict[str, Any] = dict(self._node_defaults)
        override = kenzy_data_root() / "configs" / "nodes" / f"{node_id}.yaml"
        if override.is_file():
            try:
                data = yaml.safe_load(override.read_text()) or {}
                if isinstance(data, dict):
                    effective.update(data)
                log.info("[%s] applied per-node override %s", node_id, override)
            except Exception as exc:
                log.warning("[%s] failed to read override %s: %s", node_id, override, exc)
        else:
            log.info("[%s] no per-node override (%s) — sending defaults only", node_id, override)
        # Transient overlay (e.g. a temporary TRACE log boost): wins over stored
        # config but is never persisted.
        transient = self._transient_node_cfg.get(node_id)
        if transient:
            effective.update(transient)
        # Invariant: secrets never leave the server, even if an operator put one in
        # node_defaults by mistake.
        leaked = [k for k in effective if _SECRET_KEY_RE.search(k)]
        for k in leaked:
            del effective[k]
        if leaked:
            log.warning("[%s] dropped secret-like keys from served config: %s", node_id, leaked)
        if self._capture_node_logs:
            effective["keep_logs"] = True
        return effective

    async def request_node_logs(
        self, node_id: str, level: str = "", limit: int = 200, timeout: float = 5.0
    ) -> list[dict[str, Any]] | None:
        """Ask a node for its log buffer and await the reply (None on failure)."""
        session = self._nodes.get(node_id)
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

    def _override_path(self, node_id: str) -> Path:
        from kenzy.config import kenzy_data_root

        if node_id in (".", "..") or not _SAFE_ID_RE.fullmatch(node_id):
            raise ValueError("invalid node id")
        return kenzy_data_root() / "configs" / "nodes" / f"{node_id}.yaml"

    def read_node_override(self, node_id: str) -> dict[str, Any]:
        import yaml

        path = self._override_path(node_id)
        if not path.is_file():
            return {}
        data = yaml.safe_load(path.read_text()) or {}
        return data if isinstance(data, dict) else {}

    def _write_override_file(self, node_id: str, mapping: dict[str, Any]) -> None:
        """Write configs/nodes/<node_id>.yaml verbatim (empty ⇒ remove file)."""
        import yaml

        path = self._override_path(node_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if mapping:
            path.write_text(yaml.safe_dump(dict(sorted(mapping.items())), default_flow_style=False))
        elif path.is_file():
            path.unlink()

    def write_node_override(self, node_id: str, mapping: dict[str, Any]) -> None:
        """Validate and persist configs/nodes/<node_id>.yaml (empty ⇒ remove file).

        Server-managed keys (e.g. ``room_id``, set via :meth:`set_room`) live in
        the same file but aren't part of the editable grid, so they're preserved
        across an editor save rather than wiped.
        """
        if not isinstance(mapping, dict):
            raise ValueError("override must be a mapping")
        unknown = sorted(k for k in mapping if k not in _ALLOWED_OVERRIDE_KEYS)
        if unknown:
            raise ValueError("unsupported keys: " + ", ".join(unknown))
        existing = self.read_node_override(node_id)
        preserved = {k: existing[k] for k in _SERVER_MANAGED_KEYS if k in existing}
        merged = {**preserved, **mapping}
        self._write_override_file(node_id, merged)
        if merged:
            log.info("[%s] wrote per-node override (%d keys)", node_id, len(merged))
        else:
            log.info("[%s] cleared per-node override", node_id)

    async def push_config(self, node_id: str) -> bool:
        """Re-push effective config to a connected node (live config_update)."""
        session = self._nodes.get(node_id)
        if session is None:
            return False
        await session.ws.send(protocol.config(self._effective_node_config(node_id)))
        self._notify_state()
        return True

    # ------------------------------------------------------------------
    # Central service config store (stt/tts/llm/speaker pull this at boot)
    # ------------------------------------------------------------------

    def _effective_service_config(self, service: str) -> dict[str, Any]:
        """Effective config for a backend service = packaged default ← stored override.

        The stored override lives at ``configs/services/<service>.yaml`` (server-
        owned) and is deep-merged over the packaged default. Secret-like keys are
        stripped, so secrets are never stored or served — they stay in each host's
        environment/``.env``.
        """
        import yaml

        from kenzy.config import kenzy_data_root, packaged_config

        base: dict[str, Any] = {}
        pkg = packaged_config(service)
        if pkg.is_file():
            loaded = yaml.safe_load(pkg.read_text()) or {}
            if isinstance(loaded, dict):
                base = loaded
        override = kenzy_data_root() / "configs" / "services" / f"{service}.yaml"
        if override.is_file():
            try:
                data = yaml.safe_load(override.read_text()) or {}
                if isinstance(data, dict):
                    base = _deep_merge(base, data)
                log.info("[%s] applied service override %s", service, override)
            except Exception as exc:
                log.warning("[%s] failed to read override %s: %s", service, override, exc)
        dropped = _strip_secrets(base)
        if dropped:
            log.warning("[%s] dropped secret-like keys from served config: %s", service, dropped)
        # Auto-wire peer endpoints from the server's own config so dependent services
        # don't duplicate them. setdefault ⇒ an explicit value in the service's config
        # (its override) wins, preserving the multi-host escape hatch.
        for peer in _SERVICE_PEERS.get(service, ()):
            url = self._peer_service_urls.get(peer)
            if not url:
                continue
            section = base.get(peer)
            if not isinstance(section, dict):
                section = {}
                base[peer] = section
            section.setdefault("url", url)
        return base

    def _service_override_path(self, service: str) -> Path:
        from kenzy.config import kenzy_data_root

        if service not in SERVICES or service == "node":
            raise ValueError(f"unknown service: {service}")
        return kenzy_data_root() / "configs" / "services" / f"{service}.yaml"

    def read_service_override(self, service: str) -> dict[str, Any]:
        import yaml

        path = self._service_override_path(service)
        if not path.is_file():
            return {}
        data = yaml.safe_load(path.read_text()) or {}
        return data if isinstance(data, dict) else {}

    def write_service_override(self, service: str, mapping: dict[str, Any]) -> None:
        """Persist configs/services/<service>.yaml (empty ⇒ remove file).

        Rejects secret-like keys outright — secrets live in each host's
        environment/``.env``, never in the central store.
        """
        import copy

        import yaml

        if not isinstance(mapping, dict):
            raise ValueError("config must be a mapping")
        dropped = _strip_secrets(copy.deepcopy(mapping))
        if dropped:
            raise ValueError("secret-like keys are not allowed: " + ", ".join(dropped))
        path = self._service_override_path(service)
        path.parent.mkdir(parents=True, exist_ok=True)
        if mapping:
            path.write_text(yaml.safe_dump(mapping, default_flow_style=False, sort_keys=True))
            log.info("[%s] wrote service override (%d keys)", service, len(mapping))
        elif path.is_file():
            path.unlink()
            log.info("[%s] cleared service override", service)

    @staticmethod
    def _http_json(status: int, payload: Any) -> Response:
        headers = Headers()
        headers["Content-Type"] = "application/json"
        return Response(
            status, "OK" if status == 200 else "ERR", headers, json.dumps(payload).encode()
        )

    def _check_service_token(self, request: Request) -> bool:
        """True if the service-to-service bearer is satisfied (or none is configured)."""
        if not self._service_token:
            return True
        return check_bearer(request.headers.get("authorization"), self._service_token)

    async def _process_config_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        """websockets ``process_request`` hook: always-on HTTP on the node WS port.

        Runs whenever the server runs (independent of the dashboard), token-gated by
        the service-to-service bearer:
          - ``GET /config/<service>`` → that service's effective config.
          - ``GET|POST /announce?text=…&rooms=…`` → speak a message in rooms (for
            Home Assistant / scripts). Params are in the query string because the
            ``websockets`` request hook exposes no HTTP body.
        Returns ``None`` for any other path so the WebSocket handshake proceeds.
        """
        path = request.path.split("?", 1)[0]
        if path == "/announce":
            return await self._http_announce(request)
        if not path.startswith("/config/"):
            return None
        if not self._check_service_token(request):
            return self._http_json(401, {"error": "invalid service token"})
        service = path[len("/config/") :]
        if service not in SERVICES or service == "node":
            return self._http_json(404, {"error": "unknown service"})
        return self._http_json(200, self._effective_service_config(service))

    async def _http_announce(self, request: Request) -> Response:
        """Handle ``/announce?text=…&rooms=…`` — speak a message aloud in rooms.

        ``rooms`` is a comma-separated list of room names (empty = every room).
        Token-gated like the other always-on endpoints.
        """
        from urllib.parse import parse_qs, urlsplit

        if not self._check_service_token(request):
            return self._http_json(401, {"error": "invalid service token"})
        qs = parse_qs(urlsplit(request.path).query)
        text = (qs.get("text") or [""])[0].strip()
        if not text:
            return self._http_json(400, {"error": "missing 'text' query parameter"})
        rooms_raw = (qs.get("rooms") or [""])[0].strip()
        if rooms_raw:
            wanted = {r.strip().lower() for r in rooms_raw.split(",") if r.strip()}
            targets = [nid for nid, s in self._nodes.items() if s.room_id.lower() in wanted]
        else:
            targets = list(self._nodes)
        count = await self.announce(text, targets or None)
        return self._http_json(200, {"announced": count, "text": text})

    async def boost_node_trace(self, node_id: str, seconds: int = 30) -> bool:
        """Temporarily capture TRACE-level logs on a node, auto-reverting later.

        Pushes a transient ``log_capture_level: trace`` to the connected node
        (live, no restart), then after ``seconds`` re-pushes its normal config so
        the deep capture doesn't run indefinitely. Returns False if not connected.
        """
        if node_id not in self._nodes:
            return False
        seconds = max(1, min(int(seconds), 300))
        old = self._boost_tasks.pop(node_id, None)
        if old:
            old.cancel()
        self._transient_node_cfg.setdefault(node_id, {})["log_capture_level"] = "trace"
        await self.push_config(node_id)
        self._boost_tasks[node_id] = asyncio.create_task(
            self._revert_trace(node_id, seconds), name=f"trace-revert-{node_id}"
        )
        log.info("[%s] TRACE log capture boosted for %ds", node_id, seconds)
        return True

    async def _revert_trace(self, node_id: str, seconds: int) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return  # superseded by a newer boost, which owns the revert
        transient = self._transient_node_cfg.get(node_id)
        if transient:
            transient.pop("log_capture_level", None)
            if not transient:
                self._transient_node_cfg.pop(node_id, None)
        self._boost_tasks.pop(node_id, None)
        await self.push_config(node_id)
        log.info("[%s] TRACE log capture reverted", node_id)

    def _migrate_room_keyed_files(self, node_id: str, room_id: str) -> None:
        """One-time migration: re-key a room-named override file to ``node_id``.

        Before identity was split from the room name, per-node override files were
        keyed by the room name. On a node's first connect under its ``node_id`` we
        adopt any matching room-named override so existing config isn't orphaned.
        """
        if node_id == room_id or not _SAFE_ID_RE.fullmatch(room_id):
            return
        try:
            new_override = self._override_path(node_id)
            old_override = new_override.with_name(f"{room_id}.yaml")
            if old_override.is_file() and not new_override.is_file():
                old_override.rename(new_override)
                log.info("migrated override %s → %s", old_override.name, new_override.name)
        except (ValueError, OSError) as exc:
            log.warning("override migration failed for %s: %s", room_id, exc)

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

    def _allow_connection(self, ip: str) -> bool:
        """Sliding-window per-IP connection rate limit (F-10)."""
        now = time.monotonic()
        dq = self._conn_log.setdefault(ip, deque())
        while dq and now - dq[0] > _CONN_RATE_WINDOW:
            dq.popleft()
        if len(dq) >= _CONN_RATE_MAX:
            return False
        dq.append(now)
        return True

    async def _handle(self, ws: ServerConnection) -> None:
        ip = ws.remote_address[0] if ws.remote_address else "?"
        if not self._allow_connection(ip):
            log.warning("Connection rate limit hit for %s — rejecting", ip)
            try:
                await ws.close(1013, "rate limited")  # 1013 = try again later
            except Exception:
                pass
            return
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
                session.node_id if session else "unregistered",
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

        if self._join_token is not None and not hmac.compare_digest(
            str(msg.get("token") or ""), self._join_token
        ):
            log.warning("Rejected node from %s: bad/missing join token", ws.remote_address)
            try:
                await ws.close(1008, "invalid join token")
            except Exception:
                pass
            return None

        # node_id is the stable primary key; legacy nodes that send only a room
        # name fall back to keying by that name. Validate an explicitly-provided
        # node_id so a crafted value can't become an unsafe registry key / override
        # filename (F-3); the room-name fallback path already guards override writes.
        explicit_node_id = msg.get("node_id")
        if explicit_node_id is not None and (
            str(explicit_node_id) in (".", "..") or not _SAFE_ID_RE.fullmatch(str(explicit_node_id))
        ):
            log.warning("Rejected node from %s: invalid node_id", ws.remote_address)
            try:
                await ws.close(1008, "invalid node id")
            except Exception:
                pass
            return None

        room_id: str = str(msg.get("room_id", "unknown"))
        node_id: str = str(explicit_node_id or room_id)
        caps = msg.get("capabilities") or {}
        if caps:
            log.info("[%s] capabilities: %s", node_id, caps)

        # One-time adoption of any pre-split, room-named config for this node.
        self._migrate_room_keyed_files(node_id, room_id)

        # Room name is server-owned: a stored room (override file) wins over the
        # name the node announced, so a pre-seeded/renamed room takes effect on
        # connect. The node adopts it from the config frame pushed below.
        effective = self._effective_node_config(node_id)
        if effective.get("room_id"):
            room_id = str(effective["room_id"])

        session = NodeSession(
            ws=ws,
            node_id=node_id,
            room_id=room_id,
            capabilities=caps,
            kenzy_version=(str(msg["kenzy_version"]) if msg.get("kenzy_version") else None),
        )

        async with self._lock:
            old = self._nodes.get(node_id)
            self._nodes[node_id] = session

        if old is not None:
            log.info("Node %s reconnected; closing old connection", node_id)
            try:
                await old.ws.close(1001, "replaced by reconnect")
            except Exception:
                pass

        log.info(
            "Node %s registered as room '%s' (%s) – %d node(s) connected",
            node_id,
            room_id,
            ws.remote_address,
            len(self._nodes),
        )

        # Config-pull: push the node's effective config so it needs no local file.
        # Always send a frame (even when empty) — zero-config nodes block on this
        # before initializing audio, so the frame must arrive on every connect.
        try:
            await ws.send(protocol.config(effective))
        except Exception as exc:
            log.warning("[%s] failed to send config: %s", node_id, exc)

        self._notify_state()
        return session

    async def _deregister(self, session: NodeSession) -> None:
        peer_id = session.intercom_peer
        async with self._lock:
            if self._nodes.get(session.node_id) is session:
                del self._nodes[session.node_id]
        # If it was in a call, tear the call down on the peer.
        if peer_id:
            await self.end_intercom(peer_id, reason="peer_disconnected")
        self._cleanup_on_disconnect(session.node_id)
        log.info(
            "Node %s (room '%s') disconnected – %d node(s) remaining",
            session.node_id,
            session.room_id,
            len(self._nodes),
        )
        self._notify_state()

    def _cleanup_on_disconnect(self, node_id: str) -> None:
        """Hook for subclasses to release per-node session state on disconnect."""

    # ------------------------------------------------------------------
    # Per-node message loop
    # ------------------------------------------------------------------

    async def _node_loop(self, session: NodeSession) -> None:
        async for raw in session.ws:
            if isinstance(raw, bytes):
                if session.intercom_peer is not None:
                    await self._relay_intercom(session, raw)
                elif session.streaming:
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
                session.node_id,
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
                session.node_id,
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
                session.node_id,
                (session.session_id or "?")[:8],
                model,
                score,
            )
            await self.on_wakeword(session, model, score)

        elif mtype == protocol.MSG_INTERCOM_END:
            # Node-initiated end (e.g. its wake word fired during the call).
            await self.end_intercom(session.node_id, reason=str(msg.get("reason", "peer")))

        elif mtype == protocol.MSG_STATUS:
            session.audio_ok = bool(msg.get("audio_ok", True))
            session.audio_error = msg.get("audio_error") or None
            if msg.get("devices") is not None:
                session.capabilities = {**session.capabilities, "devices": msg["devices"]}
            if not session.audio_ok:
                log.warning(
                    "[%s] reports audio init FAILED: %s — fix device + restart",
                    session.node_id,
                    session.audio_error,
                )
            self._notify_state()

        elif mtype == protocol.MSG_TUNE_SAMPLE:
            self._notify_tune(
                session.node_id,
                {
                    "rms": msg.get("rms", 0.0),
                    "wake": msg.get("wake", 0.0),
                    "vad": msg.get("vad", 0.0),
                    "seq": msg.get("seq", 0),
                    "stopped": bool(msg.get("stopped", False)),
                },
            )

        elif mtype == protocol.MSG_LOGS:
            fut = self._log_waiters.get(str(msg.get("request_id", "")))
            if fut is not None and not fut.done():
                fut.set_result(msg.get("logs") or [])

        else:
            log.debug("[%s] unhandled control msg: %s", session.node_id, mtype)

    # ------------------------------------------------------------------
    # Intercom relay + teardown (call setup lives in TranscribingServer)
    # ------------------------------------------------------------------

    async def _relay_intercom(self, session: NodeSession, data: bytes) -> None:
        """Forward a live audio frame from one paired node to its peer."""
        peer = self._nodes.get(session.intercom_peer or "")
        if peer is None:
            return
        try:
            await peer.ws.send(data)
        except Exception:
            await self.end_intercom(session.node_id, reason="peer_lost")

    async def end_intercom(self, node_id: str, reason: str = "ended") -> bool:
        """Tear down a call on both ends. Safe to call with either party's node_id."""
        session = self._nodes.get(node_id)
        peer_id = session.intercom_peer if session else None
        if session is None and peer_id is None:
            return False
        ids = {node_id}
        if peer_id:
            ids.add(peer_id)
        ended = False
        for nid in ids:
            s = self._nodes.get(nid)
            if s is None:
                continue
            was_in_call = s.intercom_peer is not None
            s.intercom_peer = None
            if was_in_call:
                ended = True
                try:
                    await s.ws.send(protocol.intercom_end(reason))
                except Exception:
                    pass
        if ended:
            log.info("Intercom ended (%s): %s", reason, ", ".join(sorted(ids)))
            self._notify_state()
        return ended

    # ------------------------------------------------------------------
    # Outbound commands
    # ------------------------------------------------------------------

    async def trigger_node(self, node_id: str, session_id: str | None = None) -> bool:
        """Send a TRIGGER command to a node.  Returns False if not connected."""
        async with self._lock:
            session = self._nodes.get(node_id)
        if session is None:
            log.warning("trigger_node: %s is not connected", node_id)
            return False
        sid = session_id or str(uuid.uuid4())
        try:
            await session.ws.send(protocol.trigger(sid))
        except websockets.exceptions.ConnectionClosed:
            log.warning("trigger_node: %s disconnected before send", node_id)
            return False
        log.info("Triggered %s session=%s", node_id, sid[:8])
        return True

    async def stop_node(self, node_id: str) -> bool:
        """Send a STOP command to a node.  Returns False if not connected."""
        async with self._lock:
            session = self._nodes.get(node_id)
        if session is None:
            log.warning("stop_node: %s is not connected", node_id)
            return False
        try:
            await session.ws.send(protocol.stop())
        except websockets.exceptions.ConnectionClosed:
            log.warning("stop_node: %s disconnected before send", node_id)
            return False
        log.info("Stopped %s", node_id)
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

    def restart_server(self) -> None:
        """Re-exec the server process (re-reads server.yaml + override). Used by the
        dashboard to apply server-config changes that wire up at startup."""
        log.warning("Restarting server (re-exec)")
        os.execv(sys.executable, [sys.executable, *sys.argv])

    async def run_self_upgrade(
        self, extra: str = "server", version: str | None = None
    ) -> tuple[bool, str]:
        """Upgrade this process's venv (``kenzy[extra]``) and return ``(ok, output_tail)``.
        Does **not** re-exec — the caller re-execs on success so the new code loads."""
        from kenzy.upgrade import run_pip_upgrade

        return await run_pip_upgrade(extra, version)

    async def restart_node(self, node_id: str) -> bool:
        """Ask a connected node to re-exec itself."""
        session = self._nodes.get(node_id)
        if session is None:
            log.warning("restart_node: %s is not connected", node_id)
            return False
        try:
            await session.ws.send(protocol.restart())
            return True
        except Exception as exc:
            log.warning("restart_node: %s send failed: %s", node_id, exc)
            return False

    async def upgrade_node(self, node_id: str, version: str | None = None) -> bool:
        """Ask a connected node to pip-upgrade kenzy[node] and re-exec. Fire-and-watch:
        the node reconnects with its new version on success (visible in the fleet view)."""
        session = self._nodes.get(node_id)
        if session is None:
            log.warning("upgrade_node: %s is not connected", node_id)
            return False
        try:
            await session.ws.send(protocol.upgrade(version))
            return True
        except Exception as exc:
            log.warning("upgrade_node: %s send failed: %s", node_id, exc)
            return False

    async def set_node_volume(
        self, node_id: str, level: int | None = None, delta: int | None = None
    ) -> int | None:
        """Set a node's playback volume (0–100). Persisted to the override + live push.

        Pass an absolute ``level`` or a relative ``delta``. Returns the new level
        (clamped 0–100), or None if neither was given. Works for an offline node
        (persisted now, pulled on connect).
        """
        override = self.read_node_override(node_id)
        current = int(override.get("volume", self._node_defaults.get("volume", 100)))
        if level is not None:
            new = int(level)
        elif delta is not None:
            new = current + int(delta)
        else:
            return None
        new = max(0, min(100, new))
        override["volume"] = new
        self._write_override_file(node_id, override)
        await self.push_config(node_id)  # live re-push if connected
        log.info("[%s] volume → %d", node_id, new)
        return new

    async def set_node_muted(self, node_id: str, muted: bool) -> bool:
        """Mute/unmute a connected node (transient — not persisted across restart).

        Mute rides the transient overlay (like the TRACE boost) so a node comes
        back un-muted after a restart; the ready chime stays audible while muted.
        Returns False if the node isn't connected.
        """
        if node_id not in self._nodes:
            return False
        self._transient_node_cfg.setdefault(node_id, {})["muted"] = bool(muted)
        await self.push_config(node_id)
        log.info("[%s] %s", node_id, "muted" if muted else "unmuted")
        self._notify_state()  # surface the change to observers (dashboard, integrations)
        return True

    async def set_room(self, node_id: str, room_name: str) -> bool:
        """Set a node's room name. Server-owned: persisted + pulled on connect.

        The name is stored in the node's override file so it survives reconnects
        and can be pre-seeded for a not-yet-connected node (it's pushed in the
        config frame on connect). If the node is connected it's also applied live.
        """
        room_name = room_name.strip()
        if not room_name or len(room_name) > 64:
            raise ValueError("room name must be 1–64 characters")
        # Persist server-side (merge into any existing override).
        override = self.read_node_override(node_id)
        override["room_id"] = room_name
        self._write_override_file(node_id, override)
        log.info("Set room name for node %s → '%s'", node_id, room_name)

        session = self._nodes.get(node_id)
        if session is not None:
            try:
                await session.ws.send(protocol.set_room(room_name))
            except Exception as exc:
                log.warning("set_room: %s live push failed: %s", node_id, exc)
            session.room_id = room_name
        self._notify_state()
        return True

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
        async with websockets.serve(
            self._handle,
            self._host,
            self._port,
            process_request=self._process_config_request,
            max_size=_MAX_WS_FRAME,
        ):
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
            print("commands: list | trigger <node_id> | stop <node_id> | broadcast")


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
    - One audio buffer per node_id, held in ``_buffers``.
    - One asyncio Task per node_id for in-flight transcription, held in
      ``_stt_tasks``.
    - A new activation from the same node (on_session_start or on_wakeword)
      cancels any in-flight transcription for that node before proceeding.
    - Transcription runs in the default thread-pool executor so the event
      loop is never blocked.
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        super().__init__(cfg)

        # All keyed by node_id (per connection), not the room name.
        self._buffers: dict[str, bytearray] = {}
        self._stt_tasks: dict[str, asyncio.Task[None]] = {}
        # Nodes currently playing TTS — used to decide whether to send STOP on wakeword.
        self._tts_active: set[str] = set()
        # Pending intercom calls awaiting the receiver's spoken consent, keyed by the
        # *receiver* node_id → (caller_node_id, caller_room, timeout_task). No audio is
        # bridged while a call is pending.
        self._pending_calls: dict[str, tuple[str, str, asyncio.Task[None]]] = {}

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
        # Voice enrollment ("enroll me as Alice") is gated by `allow_voice_enroll` in the
        # speaker *service* config (read live via _voice_enroll_allowed, so it's editable
        # from the dashboard's Services tab). Active sessions keyed by node_id
        # (prompt → capture → POST /enroll loop).
        self._enroll_sessions: dict[str, dict[str, Any]] = {}
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
        self._cancel_stt(session.node_id)
        self._tts_active.discard(session.node_id)
        self._buffers[session.node_id] = bytearray()

    async def on_audio_frame(self, session: NodeSession, data: bytes) -> None:
        buf = self._buffers.get(session.node_id)
        if buf is None:
            return
        # Backstop cap (F-10): the node's hard_cap_ms normally bounds a session, but a
        # buggy/hostile node could stream without VAD — don't grow a buffer unbounded.
        if len(buf) >= _MAX_SESSION_PCM_BYTES:
            return
        buf += data

    async def on_session_end(self, session: NodeSession, reason: str) -> None:
        pcm = bytes(self._buffers.pop(session.node_id, b""))
        if not pcm:
            return
        task = asyncio.create_task(
            self._transcribe(session.node_id, session.room_id, session.session_id, pcm),
            name=f"stt-{session.node_id}",
        )
        self._stt_tasks[session.node_id] = task

    async def on_wakeword(self, session: NodeSession, model: str, score: float) -> None:
        self._cancel_stt(session.node_id)
        # If the node is not currently streaming audio to us it may be playing
        # TTS or waiting idle — send STOP so it can interrupt and re-activate.
        if not session.streaming:
            await self.stop_node(session.node_id)
            self._tts_active.discard(session.node_id)

    # ------------------------------------------------------------------
    # TTS helpers  (called by the LLM/TTS pipeline phase)
    # ------------------------------------------------------------------

    async def send_tts_start(
        self, node_id: str, session_id: str, sample_rate: int = 22050, channels: int = 1
    ) -> bool:
        """Tell a node to enter TTS mode and begin accepting audio frames."""
        async with self._lock:
            session = self._nodes.get(node_id)
        if session is None:
            return False
        try:
            await session.ws.send(protocol.tts_start(session_id, sample_rate, channels))
            self._tts_active.add(node_id)
            return True
        except websockets.exceptions.ConnectionClosed:
            return False

    async def send_tts_frame(self, node_id: str, data: bytes) -> bool:
        """Stream a raw PCM frame to a node currently in TTS mode."""
        async with self._lock:
            session = self._nodes.get(node_id)
        if session is None:
            return False
        try:
            await session.ws.send(data)
            return True
        except websockets.exceptions.ConnectionClosed:
            self._tts_active.discard(node_id)
            return False

    async def send_tts_end(self, node_id: str, session_id: str) -> bool:
        """Tell a node that the TTS stream is complete."""
        self._tts_active.discard(node_id)
        async with self._lock:
            session = self._nodes.get(node_id)
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

    def _cancel_stt(self, node_id: str) -> None:
        task = self._stt_tasks.pop(node_id, None)
        if task and not task.done():
            task.cancel()
            log.info("[%s] STT cancelled", node_id)

    async def _transcribe(
        self, node_id: str, room_name: str, session_id: str | None, pcm: bytes
    ) -> None:
        # node_id addresses the node (state + control); room_name is the semantic
        # label the backends see (STT/speaker/LLM/TTS `room_id`).
        try:
            # Voice enrollment: this capture is an enrollment sample, not a command —
            # route it to /enroll instead of the STT→LLM pipeline.
            if node_id in self._enroll_sessions:
                await self._handle_enroll_capture(node_id, room_name, pcm)
                return

            if not self._stt_url:
                return

            # Per-stage timings for the dashboard's pipeline observability.
            async def _timed(coro: Any) -> tuple[Any, float]:
                t = time.monotonic()
                result = await coro
                return result, (time.monotonic() - t) * 1000.0

            t0 = time.monotonic()

            # STT and speaker ID run in parallel on the same PCM buffer.
            if self._speaker_url:
                (text, stt_ms), (speaker, spk_ms) = await asyncio.gather(
                    _timed(self._call_stt(pcm, room_name, session_id)),
                    _timed(self._call_speaker(pcm, room_name)),
                )
            else:
                text, stt_ms = await _timed(self._call_stt(pcm, room_name, session_id))
                speaker, spk_ms = self._unknown_speaker, 0.0

            log.info("[%s] STT: %s | speaker: %s", node_id, text or "(none)", speaker)

            # Intercom consent: if this node has a pending incoming call, the captured
            # utterance is the accept/decline answer — not a command for the LLM.
            if node_id in self._pending_calls:
                await self._resolve_call(node_id, _is_affirmative(text))
                return

            if not text:
                await self.stop_node(node_id)
                self._tts_active.discard(node_id)
                return

            normalized = re.sub(r"[^\w\s]", "", text).strip().lower()
            if normalized in _STOP_PHRASES:
                log.info("[%s] stop phrase detected (%r) — ending session", node_id, text)
                await self.stop_node(node_id)
                self._tts_active.discard(node_id)
                return

            if self._llm_url:
                _t = time.monotonic()
                response_text, voice_prompt, actions, fast = await self._call_llm(
                    text, room_name, session_id, speaker
                )
                llm_ms = (time.monotonic() - _t) * 1000.0
                log.info("[%s] LLM%s: %s", node_id, " (fast)" if fast else "", response_text)
                log.debug("[%s] voice_prompt: %s", node_id, voice_prompt)
                _t = time.monotonic()
                await self._run_tts(node_id, room_name, session_id, response_text, voice_prompt)
                tts_ms = (time.monotonic() - _t) * 1000.0

                # Record the completed pipeline for the dashboard (only when something
                # is listening, so no transcript is kept when observability is off).
                if self._session_listeners:
                    self._notify_session(
                        {
                            "ts": time.time(),
                            "node_id": node_id,
                            "room": room_name,
                            "speaker": speaker,
                            "transcript": text,
                            "response": response_text,
                            "fast": fast,
                            "stt_ms": round(stt_ms),
                            "speaker_ms": round(spk_ms),
                            "llm_ms": round(llm_ms),
                            "tts_ms": round(tts_ms),
                            "total_ms": round((time.monotonic() - t0) * 1000.0),
                        }
                    )
                if actions:
                    await self._dispatch_actions(actions, node_id, room_name)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("[%s] pipeline error: %s", node_id, exc, exc_info=True)
        finally:
            self._stt_tasks.pop(node_id, None)

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
    ) -> tuple[str, str, list[dict[str, Any]], bool]:
        import httpx  # type: ignore[import-untyped]

        payload = {
            "text": text,
            "room_id": room_id,
            "session_id": session_id,
            "speaker": speaker,
            # Connected room names so the model can target real rooms (announce/intercom).
            "rooms": sorted({s.room_id for s in self._nodes.values()}),
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
        actions = data.get("actions") or []
        return str(data["text"]), str(data["voice_prompt"]), actions, bool(data.get("fast", False))

    async def _dispatch_actions(
        self, actions: list[dict[str, Any]], source_node_id: str, source_room: str
    ) -> None:
        """Actuate server-side actions returned by the LLM (e.g. announce).

        ``announce()`` keys on ``node_id``, but the LLM targets human room names, so
        names are resolved here. The asking node is excluded so it doesn't hear the
        broadcast on top of its own spoken reply.
        """
        for action in actions:
            atype = action.get("type")
            if atype == "announce":
                msg = str(action.get("text", "")).strip()
                if not msg:
                    continue
                names = action.get("rooms")
                if names:
                    wanted = {str(n).strip().lower() for n in names}
                    targets = [
                        nid
                        for nid, s in self._nodes.items()
                        if s.room_id.lower() in wanted and nid != source_node_id
                    ]
                else:
                    targets = [nid for nid in self._nodes if nid != source_node_id]
                if not targets:
                    continue
                count = await self.announce(msg, targets)
                log.info("[%s] announced to %d node(s): %s", source_node_id, count, msg)
            elif atype == "start_intercom":
                await self.start_intercom(source_node_id, source_room, str(action.get("room", "")))
            elif atype == "start_enrollment":
                await self.start_enrollment(
                    source_node_id, source_room, str(action.get("name", ""))
                )
            elif atype == "set_volume":
                # Volume/mute change targeting the asking node (room context the
                # server already holds — no room resolution needed).
                if "muted" in action:
                    await self.set_node_muted(source_node_id, bool(action["muted"]))
                else:
                    await self.set_node_volume(
                        source_node_id,
                        level=action.get("level"),
                        delta=action.get("delta"),
                    )
            else:
                log.warning("[%s] unknown LLM action type: %r", source_node_id, atype)

    # ------------------------------------------------------------------
    # Intercom call setup + consent gate
    # ------------------------------------------------------------------

    def _resolve_room_node(self, name: str, exclude: str | None = None) -> str | None:
        """Return the node_id of a connected room by name (case-insensitive)."""
        wanted = name.strip().lower()
        for nid, s in self._nodes.items():
            if nid != exclude and s.room_id.lower() == wanted:
                return nid
        return None

    async def _say(self, node_id: str, room: str, text: str) -> None:
        """Speak a short line to one node (call status feedback)."""
        await self._run_tts(node_id, room, str(uuid.uuid4()), text, _INTERCOM_VOICE_PROMPT)

    # ------------------------------------------------------------------
    # Voice speaker enrollment (prompt → capture one utterance → POST /enroll)
    # ------------------------------------------------------------------

    def _voice_enroll_allowed(self) -> bool:
        """Whether voice enrollment is enabled — read live from the speaker service
        config so a dashboard toggle takes effect without a server restart."""
        try:
            return bool(self._effective_service_config("speaker").get("allow_voice_enroll", False))
        except Exception:
            return False

    async def start_enrollment(
        self, node_id: str, room: str, name: str, *, operator: bool = False
    ) -> None:
        """Begin a voice-enrollment session for ``name`` on a node.

        ``operator=True`` is for dashboard-initiated enrollment: it bypasses the
        ``allow_voice_enroll`` earshot gate (the request is already authenticated and
        ``controls``-gated, a stronger check than "anyone in the room").
        """
        if not operator and not self._voice_enroll_allowed():
            await self._say(node_id, room, "Voice enrollment is turned off.")
            return
        name = name.strip()
        if not name:
            await self._say(node_id, room, "I didn't catch the name to enroll.")
            return
        if not self._speaker_url:
            await self._say(
                node_id, room, "Speaker identification isn't set up, so I can't enroll."
            )
            return
        if node_id in self._enroll_sessions:
            return  # already enrolling on this node
        prompts = self._enroll_prompts()
        timeout = asyncio.create_task(
            self._enroll_timeout(node_id), name=f"enroll-timeout-{node_id}"
        )
        self._enroll_sessions[node_id] = {
            "name": name,
            "room": room,
            "collected": 0,
            "attempts": 0,
            "prompts": prompts,
            "timeout": timeout,
        }
        log.info(
            "[%s] voice enrollment started for '%s' (%d prompt(s))", node_id, name, len(prompts)
        )
        await self._enroll_prompt(
            node_id, room, f"Okay, enrolling {name}. After the tone, please say: {prompts[0]}"
        )

    def _enroll_prompts(self) -> list[str]:
        """The enrollment sentences to read — the dashboard-editable speaker-service
        ``enroll_prompts`` (single source of truth, shared with ``kenzy-enroll``),
        falling back to the bundled defaults when unset."""
        try:
            configured = self._effective_service_config("speaker").get("enroll_prompts")
        except Exception:
            configured = None
        prompts = (
            [str(p).strip() for p in configured if str(p).strip()]
            if isinstance(configured, list)
            else []
        )
        return prompts or list(DEFAULT_ENROLL_PROMPTS)

    async def _enroll_prompt(self, node_id: str, room: str, text: str) -> None:
        """Arm one-shot capture on the node, then speak the prompt."""
        node = self._nodes.get(node_id)
        if node is None:
            self._end_enroll_session(node_id)
            return
        try:
            await node.ws.send(protocol.expect_utterance())
        except Exception:
            self._end_enroll_session(node_id)
            return
        await self._run_tts(node_id, room, str(uuid.uuid4()), text, _INTERCOM_VOICE_PROMPT)

    async def _handle_enroll_capture(self, node_id: str, room: str, pcm: bytes) -> None:
        """Route one captured utterance to /enroll, then prompt for the next or finish."""
        session = self._enroll_sessions.get(node_id)
        if session is None:
            return
        session["attempts"] += 1
        ok = len(pcm) >= _ENROLL_MIN_PCM_BYTES and await self._call_enroll(pcm, session["name"])
        if ok:
            session["collected"] += 1

        # One sample per configured prompt; `collected` doubles as the index of the
        # next sentence to read, so a failed capture re-reads the same prompt.
        prompts = session.get("prompts") or list(DEFAULT_ENROLL_PROMPTS)
        if session["collected"] >= len(prompts):
            name = session["name"]
            self._end_enroll_session(node_id)
            await self._say(node_id, room, f"All done — I've enrolled {name}.")
            return
        if session["attempts"] >= len(prompts) + _ENROLL_MAX_RETRIES:
            self._end_enroll_session(node_id)
            await self._say(
                node_id, room, "I couldn't get enough clear audio. Enrollment cancelled."
            )
            return
        sentence = prompts[session["collected"]]
        prompt = (
            f"I didn't catch that. Please say: {sentence}"
            if not ok
            else f"Got it. Next, please say: {sentence}"
        )
        await self._enroll_prompt(node_id, room, prompt)

    def _end_enroll_session(self, node_id: str) -> None:
        session = self._enroll_sessions.pop(node_id, None)
        if session is not None:
            t = session.get("timeout")
            if t is not None:
                t.cancel()

    def _cleanup_on_disconnect(self, node_id: str) -> None:
        self._end_enroll_session(node_id)

    async def _enroll_timeout(self, node_id: str) -> None:
        try:
            await asyncio.sleep(_ENROLL_TIMEOUT_SEC)
        except asyncio.CancelledError:
            return
        session = self._enroll_sessions.pop(node_id, None)
        if session is not None:
            log.info("[%s] enrollment timed out", node_id)
            await self._say(node_id, session["room"], "Enrollment timed out.")

    async def _call_enroll(self, pcm: bytes, name: str) -> bool:
        """POST one PCM sample to the speaker service's /enroll. Returns success."""
        if not self._speaker_url:
            return False
        import base64

        import httpx  # type: ignore[import-untyped]

        enroll_url = self._speaker_url.rsplit("/", 1)[0] + "/enroll"
        payload = {"audio_b64": base64.b64encode(pcm).decode(), "name": name}
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    enroll_url,
                    json=payload,
                    timeout=self._speaker_timeout,
                    headers=self._service_headers(),
                )
                resp.raise_for_status()
            return True
        except Exception as exc:
            log.warning("enroll call failed: %s", exc)
            return False

    async def start_intercom(self, caller_id: str, caller_room: str, target_room: str) -> None:
        """Ring a target room for an intercom call (consent required before bridging)."""
        target_room = (target_room or "").strip()
        if not target_room:
            return
        receiver_id = self._resolve_room_node(target_room, exclude=caller_id)
        if receiver_id is None:
            await self._say(caller_id, caller_room, f"I couldn't reach the {target_room}.")
            return
        receiver = self._nodes[receiver_id]
        caller = self._nodes.get(caller_id)
        busy = (
            receiver_id in self._pending_calls
            or receiver.intercom_peer is not None
            or (caller is not None and caller.intercom_peer is not None)
        )
        if busy:
            await self._say(caller_id, caller_room, f"The {receiver.room_id} is busy.")
            return
        try:
            await receiver.ws.send(protocol.call_request(caller_room))
        except Exception:
            await self._say(caller_id, caller_room, f"I couldn't reach the {receiver.room_id}.")
            return
        timeout = asyncio.create_task(
            self._call_timeout(receiver_id), name=f"call-timeout-{receiver_id}"
        )
        self._pending_calls[receiver_id] = (caller_id, caller_room, timeout)
        log.info("Intercom ring: %s → %s", caller_room, receiver.room_id)
        # Spoken consent prompt; the receiver auto-captures the answer once it finishes.
        prompt = (
            f"The {caller_room} would like to start a voice chat. "
            "Say yes to accept, or no to decline."
        )
        await self._run_tts(
            receiver_id, receiver.room_id, str(uuid.uuid4()), prompt, _INTERCOM_VOICE_PROMPT
        )

    async def _call_timeout(self, receiver_id: str) -> None:
        try:
            await asyncio.sleep(_CALL_TIMEOUT_SEC)
        except asyncio.CancelledError:
            return
        pending = self._pending_calls.pop(receiver_id, None)
        if pending is None:
            return
        caller_id, caller_room, _ = pending
        receiver = self._nodes.get(receiver_id)
        rname = receiver.room_id if receiver else "the other room"
        if receiver is not None:
            try:
                await receiver.ws.send(protocol.call_cancel())
            except Exception:
                pass
        await self._say(caller_id, caller_room, f"No answer from the {rname}.")

    async def _resolve_call(self, receiver_id: str, accepted: bool) -> None:
        """Apply the receiver's consent decision: connect on yes, notify caller on no."""
        pending = self._pending_calls.pop(receiver_id, None)
        if pending is None:
            return
        caller_id, caller_room, timeout = pending
        timeout.cancel()
        receiver = self._nodes.get(receiver_id)
        rname = receiver.room_id if receiver else "the other room"
        if not accepted:
            await self._say(caller_id, caller_room, f"The {rname} declined.")
            return
        await self._connect_call(caller_id, receiver_id)

    async def _connect_call(self, caller_id: str, receiver_id: str) -> None:
        caller = self._nodes.get(caller_id)
        receiver = self._nodes.get(receiver_id)
        if caller is None or receiver is None:
            if caller is not None:
                await self._say(caller_id, caller.room_id, "The call couldn't connect.")
            return
        caller.intercom_peer = receiver_id
        receiver.intercom_peer = caller_id
        # NOTE: do not cancel STT here — this runs *inside* the receiver's own
        # consent-capture _transcribe task, so cancelling it would abort this very
        # method partway and leave one intercom_start unsent (a one-way call). Both
        # nodes' pipelines are already finished by the time we connect.
        try:
            await caller.ws.send(protocol.intercom_start(receiver.room_id))
            await receiver.ws.send(protocol.intercom_start(caller.room_id))
        except Exception:
            await self.end_intercom(caller_id, reason="connect_failed")
            return
        log.info("Intercom connected: %s ↔ %s", caller.room_id, receiver.room_id)
        self._notify_state()

    async def _run_tts(
        self, node_id: str, room_name: str, session_id: str | None, text: str, voice_prompt: str
    ) -> None:
        if not self._tts_url:
            return

        import httpx  # type: ignore[import-untyped]

        sid = session_id or str(uuid.uuid4())
        await self.send_tts_start(node_id, sid, sample_rate=24000, channels=1)
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self._tts_url,
                    json={"text": text, "voice_prompt": voice_prompt, "room_id": room_name},
                    timeout=self._tts_timeout,
                    headers=self._service_headers(),
                )
                resp.raise_for_status()
            pcm = resp.content
            for i in range(0, len(pcm), self._tts_chunk_size):
                if not await self.send_tts_frame(node_id, pcm[i : i + self._tts_chunk_size]):
                    return  # node disconnected mid-stream
            await self.send_tts_end(node_id, sid)
            log.info("[%s] TTS complete", node_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("[%s] TTS error: %s", node_id, exc, exc_info=True)
            await self.send_tts_end(node_id, sid)
        finally:
            if node_id in self._tts_active:
                await self.stop_node(node_id)
                self._tts_active.discard(node_id)

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

    async def _stream_pcm(self, node_id: str, pcm: bytes) -> None:
        sid = str(uuid.uuid4())
        if not await self.send_tts_start(node_id, sid, sample_rate=24000, channels=1):
            return
        for i in range(0, len(pcm), self._tts_chunk_size):
            if not await self.send_tts_frame(node_id, pcm[i : i + self._tts_chunk_size]):
                return
        await self.send_tts_end(node_id, sid)

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
    from dotenv import load_dotenv  # type: ignore[import-untyped]

    load_dotenv()

    from kenzy.config import resolve_config

    config_path = resolve_config("server", sys.argv[1] if len(sys.argv) > 1 else None)
    # server.yaml deep-merged with any dashboard-written server.local.yaml override.
    cfg = load_server_config(config_path)

    from kenzy.logutil import configure_logging, level_value

    configure_logging(
        level_value(cfg.get("log_level"), logging.INFO), bool(cfg.get("verbose", False))
    )

    server = TranscribingServer(cfg)

    # mDNS advertisement so nodes can discover this server without a server_url.
    discovery_cfg = cfg.get("discovery", {}) or {}
    advertiser = None
    if discovery_cfg.get("enabled", True):
        from kenzy.discovery import ServerAdvertiser

        version = kenzy_version()
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

        dashboard = Dashboard(server, cfg, DashboardConfig.from_cfg(cfg), config_path=config_path)

    # Home Assistant / MQTT integration: opt-in; publishes node state/events via HA
    # MQTT Discovery. When off, nothing is wired (zero node-/pipeline-side overhead).
    mqtt_transport = None
    mqtt_cfg = (cfg.get("integrations", {}) or {}).get("mqtt", {}) or {}
    if mqtt_cfg.get("enabled", False):
        from kenzy.integrations import IntegrationHub, attach_to_server
        from kenzy.integrations.mqtt import Command, MqttConfig, MqttTransport

        _mcfg = MqttConfig.from_cfg(mqtt_cfg)

        async def _dispatch_command(cmd: Command) -> None:
            """Map an inbound HA command onto the matching server action."""
            if cmd.action == "trigger" and cmd.node_id:
                await server.trigger_node(cmd.node_id)
            elif cmd.action == "stop" and cmd.node_id:
                await server.stop_node(cmd.node_id)
            elif cmd.action == "volume" and cmd.node_id is not None:
                await server.set_node_volume(cmd.node_id, level=int(cmd.value))
            elif cmd.action == "mute" and cmd.node_id is not None:
                await server.set_node_muted(cmd.node_id, bool(cmd.value))
            elif cmd.action == "announce" and cmd.value:
                await server.announce(str(cmd.value))

        _hub = IntegrationHub()
        _dispatch = _dispatch_command if _mcfg.commands else None
        mqtt_transport = MqttTransport(_mcfg, dispatch=_dispatch)
        _hub.subscribe(mqtt_transport.submit)
        attach_to_server(_hub, server)

    async def _main() -> None:
        coros: list[Any] = [server.serve()]
        if dashboard is not None:
            coros.append(dashboard.serve())
        if mqtt_transport is not None:
            coros.append(mqtt_transport.run())
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
