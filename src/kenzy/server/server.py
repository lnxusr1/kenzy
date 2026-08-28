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

import array
import asyncio
import datetime
import json
import logging
import math
import os
import random
import re
import sys
import time
import uuid
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websockets
import websockets.exceptions
import websockets.server
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from kenzy import calibration, kenzy_version, protocol, redact, serviceauth, tlsutil
from kenzy.config import SERVICES
from kenzy.server import occupancy as occupancy_mod
from kenzy.server.people import (
    UNSET,
    Identity,
    PeopleStore,
    resolve_assist_identity,
    resolve_voice_identity,
)
from kenzy.server.roster import NodeRoster

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
        "sound_ringback",
        "sound_dialog_end",
        "sound_offline",
        "sound_timer",
        "sound_alarm",
        "sound_error",
        "sound_thinking",
        "sound_working",
        "log_level",
        "log_capture_level",
        "volume",
        # Co-audible arbitration group (5.1.x). Nodes sharing a non-empty value
        # arbitrate their simultaneous wakes; unset (the default) = this node
        # never waits on, and never loses to, anyone.
        "audio_group",
        # Managed ALSA capture gain — flat, grid-editable, live. Unset =
        # the device's own gain is never touched.
        "mic_volume",
        # 5.0.4 speakerphone volume buttons — flat, grid-editable, live.
        "volume_buttons",
        "volume_button_device",
        "volume_button_step",
        "hardware_aec",
        "dialog_no_speech_timeout_ms",
        "dialog_onset_ms",
        "dialog_onset_vad_threshold",
    }
)
# Server-owned keys stored in the per-node override file and pushed via config-pull,
# but NOT edited through the generic config grid (they have dedicated UI / actions).
# The node applies them from the config frame. Preserved across editor saves.
_SERVER_MANAGED_KEYS = frozenset({"room_id"})
# Valid per-node override keys the EDITOR doesn't edit (nested dicts, yaml-only).
# An editor save must neither reject nor wipe them — the file is their source of
# truth, and the grid simply doesn't know they exist (the save_curation lesson:
# a second writer that rewrites wholesale silently loses what it doesn't know).
_YAML_ONLY_OVERRIDE_KEYS = frozenset({"watchdog"})
_SECRET_KEY_RE = re.compile(r"key|token|secret|password|passwd|credential", re.IGNORECASE)
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
# Env-var names the write-only secret editor will accept (dashboard → API keys).
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
# One-breath commands: the node's wake gate deliberately captures the WHOLE wake
# phrase in its pre-roll (finding the phrase boundary in audio is a losing game —
# a short pre-roll cost the rig the command's first word), so the transcript
# arrives as "Hey Kenzy, turn on the lights". Strip a LEADING rendering of the
# phrase — Whisper's usual spellings, a bare "Kenzy" when the pre-roll started
# mid-"Hey", or a lone "zee"/"z" tail fragment — only when more text follows, so
# a bare "Hey Kenzy" still reaches the model (it answers "yes?", as before).
# The bare-name form is a judged tradeoff: a transcript-leading "Kenzy" followed
# by more words is address, not content, in essentially all real traffic.
# k[ei]n[sz]: Whisper's renderings of the name vary by SPEAKER — the rig's
# Kokoro voice yields "Kenzy"/"Kenzie", but a real voice produced "Kinsey"
# (found live 2026-08-01: every fast intent missed, and "Hey Kinsey, stop."
# reached the model — a stop must never be model-mediated).
_WAKE_PREFIX_RE = re.compile(
    r"^\s*(?:(?:(?:hey|hay|a|eh)[,!.\s]+)?k[ei]n[sz]\w{0,3}|zee|z)[,.!?:\s]+(?=\S)",
    re.IGNORECASE,
)


def _strip_wake_prefix(text: str) -> str:
    return _WAKE_PREFIX_RE.sub("", text, count=1)
_ANNOUNCE_VOICE_PROMPT = "Read this aloud as a clear, calm public announcement."
_INTERCOM_VOICE_PROMPT = "Read this aloud as a brief, friendly spoken notification."
_CALIB_VOICE_PROMPT = "Calm, clear, unhurried — you are guiding someone through a setup step."
_CALIB_SAY_MARGIN = 0.9  # playback outlasts streaming by roughly this much
_CALIB_PEAK_REFRACTORY_S = 1.5  # min gap between counted wake attempts
# Processing-cue ladder (4.4 presence audio): escalating spoken acknowledgements
# while NO reply audio has started — the waiting bed is the instant layer, the
# first rung says "this one's taking a beat", the second reassures the long
# tail, then restraint: silence until the reply or the real error cue. Each
# rung is a (delay_ms, config_key, bundled_default) triple. TIMING MODEL
# (founder-tuned on the rig 2026-07-23): the FIRST rung's delay is absolute
# from ladder start; every LATER rung's delay counts from when the previous
# cue FINISHES PLAYING (the ladder sleeps out the cue's audio duration after
# streaming it) — absolute deadlines made rung 2 land ~2s after rung 1
# finished, which read as chatter, not reassurance. PHRASE ⇄ TIMING COUPLING
# (founder decision 2026-07-23): the first rung fires at 5s in STATUS register
# ("Working on it." — a report that the wait is underway), NOT a quick-ack
# ("On it.", which only sounds right ≤3s — but 3s fires BEFORE a typical
# prod-streamed reply lands at ~4s TTFA, taxing ordinary replies; 5s speaks
# only on the genuinely slow tail; the bed is the instant layer). Keys accept
# a string or a POOL (list) picked with a per-node no-immediate-repeat. Cue
# sessions ride tts_start(cue=True) so nodes duck-mix them OVER the bed
# instead of cutting it. Internal constants: promote to config keys only on
# field evidence (deployment latency profiles differing enough to need it).
_CUE_LADDER: tuple[tuple[int, str, str], ...] = (
    (5000, "sound_thinking", "thinking.wav"),  # at 5s into the wait
    (8000, "sound_working", "working.wav"),  # 8s after the thinking cue ends
)
# Spoken-cue regeneration (dashboard "Regenerate spoken cues"): the cue PHRASES
# are the source of truth (``cues:`` in server.yaml overrides these defaults);
# WAVs are a cache rendered through the CONFIGURED TTS voice, so the whole cue
# set follows a voice change with one click — and a local (Kokoro) install
# stops hearing the bundled OpenAI-voice cues against its own voice.
_CUE_KINDS: tuple[tuple[str, str], ...] = (
    ("error", "sound_error"),
    ("thinking", "sound_thinking"),
    ("working", "sound_working"),
)
_DEFAULT_CUE_TEXTS: dict[str, list[str]] = {
    "error": ["I'm sorry, but I'm having trouble processing your request at the moment."],
    "thinking": ["Working on it."],
    "working": ["Still working on it."],
}
_CUE_VOICE_PROMPTS: dict[str, str] = {
    "error": (
        "Speak calmly and apologetically, at a measured pace — a brief, sincere "
        "apology from a home voice assistant that cannot complete a request."
    ),
    "thinking": "A calm, brief status note that work is underway — light, unhurried.",
    "working": "A calm, friendly reassurance that work is still underway — brief, unhurried.",
}
_CALIB_WAKE_WINDOW_S = 20.0  # wake phase cap (ends early at WAKE_TARGET attempts)
_CALIB_WAKE_EXTEND_S = 12.0  # one extension when the phase gate fails
_CALIB_PROBE_LEAD_S = 0.4  # skip the probe's first moments (playback lags streaming)
_CALIB_PROBE_TAIL_S = 0.3  # ...and its tail, so only mid-playback frames are tagged
_CALIB_PROBE_MIN_S = 1.0  # a shorter probe signal than this can't be tagged reliably
_CALIB_PROBE_BEEP_S = 3.0  # probe length; the verdict discards ~0.6 s of AEC
# convergence warm-up (calibration.ECHO_WARMUP_FRAMES), so the tagged window
# must leave MIN_ECHO_FRAMES after that — 2.0 s left exactly zero margin
_CALIB_VOLUME_FLOOR = 20  # below this volume a silent speaker fakes perfect AEC
_CALIB_VERIFY_S = 12.0  # how long Verify waits for a real wake before nudging
_CALIB_MAX_NUDGES = 2  # bounded: then be honest instead of oscillating
_CALIB_RECONNECT_S = 35.0  # post-restart reconnect wait
_CALIB_CLOSE_MARGIN_S = 2.5  # her verify-exchange reply plays out after streaming ends
_CHIME_MAX_S = 30.0  # loop cap: a buggy automation must not ring the house forever
_CHIME_DEFAULT = "doorbell.wav"
# Voice enrollment: one sample per configured prompt (see _enroll_prompts), min bytes
# for a usable sample, extra retries allowed beyond the prompt count for unclear audio,
# and how long the session lives with NO capture arriving before it's abandoned. The
# timeout is per-stage (re-armed on every capture), not a total budget — five prompts
# through a slow local TTS legitimately take longer than any reasonable fixed cap
# (field finding: a real 5-prompt run blew a 120s total cap and died mid-enrollment).
# Per stage the window covers one prompt's TTS synth+playback plus the reply, so
# 60s is generous headroom for a slow local TTS — while a walked-away session dies fast.

# Resource caps (F-10): bound a single capture buffer and inbound WS frame size, and
# rate-limit new connections per source IP, so a hostile/buggy LAN peer can't exhaust
# memory or hammer the listener.
_MAX_SESSION_PCM_BYTES = 16_000 * 2 * 120  # ~2 min of 16 kHz int16 (~3.8 MB) per capture
# Parallel-hearing groundwork: every capture logs the RMS of its opening audio,
# so co-audible nodes' takes on the SAME utterance can be paired up from the
# journal and compared — the empirical base for louder-wins arbitration. Two
# windows: the fast-decision budget an arbiter could afford, and one long
# enough to cover the whole wake phrase in the pre-roll regardless of when each
# node's wake fired (the nodes' buffers don't start at the same instant).
_ONSET_SHORT_BYTES = 2560 * 4  # ~320 ms of 16 kHz int16 frames
_ONSET_LONG_BYTES = 2560 * 12  # ~960 ms
# Co-audible arbitration timing — DEFAULTS; both are operator-tunable via
# `arbitration.window_ms` / `arbitration.deadzone_ms` in server.yaml (read at
# boot, clamped in __init__). The window must close, and the stop reach the
# loser, before the wake gate expires and plays the chime (wake_onset_ms,
# default 400 ms). Measured spread between co-audible nodes' wake_pending
# arrivals: 23–146 ms — 250 ms holds them all with LAN round-trip to spare.
_ARB_WINDOW_S = 0.25
# How long a stopped session_id stays on the loser list: long enough to catch
# its audio_start racing the stop, and an already-open capture's audio_end.
_ARB_LOSER_TTL_S = 10.0
# A wake_pending from a node stopped this recently is the score TAIL of the
# utterance it just lost, not a new wake — openwakeword stays above threshold
# for a few frames after a phrase, and the stop drops the loser back to IDLE
# while that tail is live. Seen 2026-08-15: a re-wake 6 ms after losing, with a
# silent (-90 dBFS) pre-roll, that then answered as a solo candidate. New nodes
# also suppress this locally (_ARB_REFRACTORY_S); this is the server-side
# backstop that additionally covers older nodes.
_ARB_REWAKE_S = 1.5
# One utterance gets ONE second of arbitration budget, measured from the FIRST
# wake_pending: the opening 250 ms is the window where candidates compete; the
# remaining 750 ms is a dead zone where a straggler — a node whose wake fired
# late on the same phrase — is ignored (actively stopped), because the winner
# has already proceeded and can't be un-answered. A genuinely NEW utterance
# can't start inside the dead zone: saying the wake phrase takes ~1 s itself.
_ARB_DEADZONE_S = 1.0
# The release that introduced wake_pending. A grouped node older than this
# cannot arbitrate — it never announces its wakes — and the failure is silent,
# so registration warns (see _register). Deliberately NOT worked around:
# synthesizing candidates from old nodes' evidence-less audio_starts could let
# the worst-placed protocol-speaking node stand down the best-placed old one.
_ARB_MIN_NODE_VERSION = (5, 1, 1)


@dataclass
class GroupEngagement:
    """One audio_group's CURRENT conversation (Layer 1 of stateful groups).

    The group is one virtual node: it holds at most one conversation, owned by
    the node that won the opening turn's arbitration. Phases follow the
    exchange — ``capturing`` → ``thinking`` → ``speaking`` → ``reply-window`` —
    and the record exists for three reasons: a NEW wake anywhere in the group
    cancels the engagement (the single-node "wake always cancels" contract at
    group scope), a wake landing during ``speaking`` is suspect-by-construction
    (TTS bleed — an accepted risk, but a visible one), and a collision reads as
    one record instead of journal archaeology."""

    group: str
    owner: str  # node_id
    session_id: str | None
    phase: str  # capturing | thinking | speaking | reply-window
    since: float  # monotonic, when the engagement began


# An engagement this old is presumed stale (a completion path we failed to see);
# a group cancel still clears it but no longer sends a stop for it — stopping a
# long-idle node could abort unrelated audio (an alert) for nothing.
_ENGAGEMENT_STALE_S = 600.0


def _version_tuple(version: str | None) -> tuple[int, ...] | None:
    """Tolerant parse: leading digits per dotted piece; None when unparseable."""
    if not version:
        return None
    parts: list[int] = []
    for piece in str(version).split(".")[:3]:
        digits = ""
        for ch in piece:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else None


def _supports_arbitration(version: str | None) -> bool:
    """Tolerant version gate: unparseable/absent reads as too old (fail loud)."""
    vt = _version_tuple(version)
    return vt is not None and vt >= _ARB_MIN_NODE_VERSION


# The release that added tts_done (playback-complete). Engagements of older
# nodes clear at reply DISPATCH (the pre-tts_done behavior) instead of waiting
# for a frame that will never come and sticking in `speaking` forever.
_TTS_DONE_MIN_NODE_VERSION = (5, 1, 3)


def _supports_tts_done(version: str | None) -> bool:
    vt = _version_tuple(version)
    return vt is not None and vt >= _TTS_DONE_MIN_NODE_VERSION


def _pcm_rms(pcm: bytes | bytearray) -> float:
    """RMS of int16 mono PCM — stdlib only (the server carries no numpy)."""
    usable = len(pcm) - (len(pcm) % 2)
    if not usable:
        return 0.0
    samples = array.array("h")
    samples.frombytes(bytes(pcm[:usable]))
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def _as_float(value: Any) -> float | None:
    """Tolerant wire-field read: a number or None, never an exception."""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
_MAX_WS_FRAME = 65_536  # node→server frames are tiny (2.5 KB audio / small JSON)
_CONN_RATE_MAX = 30  # max new connections per source IP …
_CONN_RATE_WINDOW = 60.0  # … within this many seconds
# Peer service URLs the server injects into a dependent service's served config so they
# aren't duplicated in two places (an override in the service's own config still wins).
# Only speaker (its kenzy-enroll CLI) needs TTS today.
_SERVICE_PEERS: dict[str, tuple[str, ...]] = {
    "speaker": ("tts",),
    "llm": ("speaker",),
    "s2s": ("stt", "tts"),
}

#: The environment secrets each backend service needs to do its job. The server
#: serves the ones it holds to that service over the authenticated TLS config
#: channel (3.12+, server-authority stage b), so API keys live on ONE host
#: instead of every host. Served under ``_secrets`` (never as config keys); the
#: service injects them into its own environment at boot, server value winning.
_SERVICE_SECRETS: dict[str, tuple[str, ...]] = {
    "stt": ("OPENAI_API_KEY",),
    "tts": ("OPENAI_API_KEY",),
    "llm": ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "HA_API_KEY", "CUSTOM_LLM_API_KEY"),
    "speaker": ("HF_TOKEN",),
    # The conversation engine calls the model provider directly (s2s-design
    # decision 8 as clarified 2026-08-26) — it needs the same key family the
    # llm service uses for that provider, and nothing else.
    "s2s": ("OPENAI_API_KEY", "CUSTOM_LLM_API_KEY"),
}
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
    # Opt-in to not-yet-official features (also swaps the dashboard favicon colors
    # + badge so an experimental instance's tab stands out).
    "experimental": "bool",
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
    "dialog.max_turns": "num",
    "alarm.ring_repeats": "num",
    "alarm.ring_interval": "num",
    # 4.4 streaming pipeline: sentence-overlapped replies (off ⇒ the buffered
    # serial pipeline, byte-for-byte pre-4.4 behavior).
    "streaming.enabled": "bool",
    # v6 follow-up mode: captures on hardware_aec-capable nodes route through
    # the conversation engine (kenzy-s2s); off ⇒ the classic pipeline
    # everywhere. Restart to apply, like its siblings here.
    "s2s.enabled": "bool",
    "s2s.url": "str",
    "s2s.hard_cap_s": "num",
    "discovery.enabled": "bool",
    "discovery.instance": "str",
    # Home Assistant / MQTT integration (no secrets — broker creds are env-only).
    "integrations.mqtt.enabled": "bool",
    "integrations.mqtt.host": "str",
    "integrations.mqtt.port": "num",
    "integrations.mqtt.base_topic": "str",
    "integrations.mqtt.discovery_prefix": "str",
    "integrations.mqtt.commands": "bool",
    # Proactive speech (5.0.6). `dnd_rooms` is deliberately absent: it's a list,
    # and the editable grid only coerces bool/num/str — it stays file-managed
    # rather than being half-supported here.
    "proactive.enabled": "bool",
    "proactive.quiet_hours": "str",
    "proactive.rate_limit": "num",
    "proactive.rate_window": "num",
    "proactive.safety.enabled": "bool",
    "proactive.safety.repeat_after": "num",
    # v5 occupancy spine. Default ON: 5.0 is watch-only (nothing speaks, no
    # delivery changes), so the risk of leaving it on is nil and the soak only
    # gathers data if it is actually running — the 4.4 streaming lesson, where
    # shipping dark just delayed the learning. This key is the kill switch, not
    # the enabler. HA absent ⇒ nothing starts regardless.
    "occupancy.enabled": "bool",
    # Fleet health: how long a room may be missing before it is a fault, and the
    # expected-downtime window granted when we ourselves take a node away.
    "fleet.offline_alert_minutes": "num",
    "fleet.restart_grace_minutes": "num",
    # Co-audible wake arbitration timing (nodes opt in via `audio_group`).
    # Window: how long the group collects contenders after its first wake;
    # dead zone: the utterance's total budget — late wakes inside it are stood
    # down. Restart to apply (read at boot, like the fleet keys).
    "arbitration.window_ms": "num",
    "arbitration.deadzone_ms": "num",
}

#: Endpoint path each backend service serves, appended to an announced base URL so
#: the pipeline can reach an auto-registered service without a static ``<svc>.url``.
_SERVICE_ENDPOINTS: dict[str, str] = {
    "stt": "/transcribe",
    "tts": "/speak",
    "llm": "/process",
    "speaker": "/identify",
    # The conversation engine (v6 follow-up). Registration writes an http(s)
    # base into _s2s_url; _s2s_engine_url normalizes the scheme to ws(s).
    "s2s": "/v1/realtime",
}
#: Drop an auto-registered service if it hasn't re-announced within this many seconds.
_REGISTER_TTL = 90.0


def _server_override_path(config_path: str | Path) -> Path:
    """Dashboard-written server settings live beside server.yaml, layered over it
    (keeps the hand-edited server.yaml + its comments untouched). When the server
    runs off the packaged default config, the override lives in the config home
    instead — the package data dir is never written (a dashboard edit must not
    land inside site-packages) nor read (an override accidentally baked into a
    wheel must not flip settings on)."""
    from kenzy.config import writable_config_path

    return writable_config_path("server.local", Path(config_path).parent / "server.local.yaml")


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


_LOOPBACK_HTTP_RE = re.compile(r"^http://(127\.0\.0\.1|localhost)[:/]")


def _mesh_url(cfg: dict[str, Any], url: str | None) -> str | None:
    """Auto-upgrade a loopback service URL to https when mesh TLS is on.

    With ``tls:`` set, co-located services follow the server into TLS (their
    pair is injected via config-pull) — so a stale ``http://127.0.0.1:…`` URL
    from a pre-TLS config would speak plaintext at a TLS listener and break
    the pipeline. Remote URLs are left alone: another host's scheme is the
    operator's call.
    """
    tls = cfg.get("tls") or {}
    if url and tls.get("cert") and tls.get("key") and _LOOPBACK_HTTP_RE.match(url):
        upgraded = "https://" + url[len("http://") :]
        log.info("Mesh TLS: upgraded loopback service URL %s → %s", url, upgraded)
        return upgraded
    return url


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
    # Operator switch (runtime-only, test/ops): the server DISREGARDS this
    # node's audio — wake announcements and sessions are refused — so live
    # audio tests can force "only node B hears" situations without config
    # writes or restarts. Deliberately NOT persisted and cleared by any
    # reconnect: a muted-for-testing node must fail open to normal behavior,
    # never be quietly forgotten.
    ignore_audio: bool = field(default=False)
    # Node-measured wake evidence from the CURRENT session's audio_start (None on
    # triggered/legacy sessions): phrase level (dBFS), phrase-over-floor margin
    # (dB — the gain-invariant one), peak wake score. The comparable inputs for
    # co-audible (louder-wins) arbitration.
    wake_db: float | None = field(default=None)
    wake_margin_db: float | None = field(default=None)
    wake_score: float | None = field(default=None)
    # Capabilities announced in `hello` (audio device + the device probe used by the
    # dashboard's device picker). Not persisted; refreshed on each connect.
    capabilities: dict[str, Any] = field(default_factory=dict)
    # Installed kenzy package version the node reported in `hello` (None = legacy node
    # that didn't send one). For the dashboard's per-host version view.
    kenzy_version: str | None = field(default=None)
    # Latest periodic system metrics (cpu/ram/disk/temp; None = never reported).
    metrics: dict[str, Any] | None = field(default=None)

    async def send_json(self, payload: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(payload))


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------




def _llm_reply_from(data: dict[str, Any]) -> LlmReply:
    return LlmReply(
        text=str(data["text"]),
        voice_prompt=str(data["voice_prompt"]),
        actions=list(data.get("actions") or []),
        fast=bool(data.get("fast", False)),
        expect_response=bool(data.get("expect_response", False)),
        secret=bool(data.get("secret", False)),
        spans=list(data.get("spans") or []),
        continuation=data.get("continuation") or None,
        ask_timeout_s=data.get("ask_timeout_s"),
        ask_capture=str(data.get("ask_capture") or "text"),
        ask_cue=bool(data.get("ask_cue", False)),
        ask_room=data.get("ask_room") or None,
        ask_prompt=str(data.get("ask_prompt") or ""),
        ask_busy_cues=bool(data.get("ask_busy_cues", True)),
    )


@dataclass
class LlmReply:
    """What the LLM service said back for one exchange — one object instead of
    an ever-growing tuple. ``continuation`` set means the reply IS a question
    from a parked skill (the 4.2 ask() primitive): speak it, hold the floor,
    route the answer to /process/continue (wake/timeout → /process/cancel)."""

    text: str
    voice_prompt: str
    actions: list[dict[str, Any]] = field(default_factory=list)
    fast: bool = False
    expect_response: bool = False
    secret: bool = False
    spans: list[dict[str, Any]] = field(default_factory=list)
    continuation: str | None = None
    ask_timeout_s: float | None = None
    ask_capture: str = "text"
    ask_cue: bool = False
    ask_room: str | None = None
    ask_prompt: str = ""
    # Processing-cue ladder over the answer turn: on by default; a skill's
    # ask(busy_cues=False) keeps its conversational turnarounds cue-free.
    ask_busy_cues: bool = True


# Sentence boundary for the 4.4 streaming aggregator: terminator (+ closing
# quotes/brackets) followed by whitespace. Decimals ("93.5") never match — no
# whitespace after the dot.
_SENT_END_RE = re.compile(r"[.!?…]+[\"'”’)\]]*\s+")


def _split_sentences(buf: str) -> tuple[list[str], str]:
    """Split complete raw sentence slices off the front of ``buf``.

    Slices keep their trailing whitespace so ``"".join(slices) + remainder ==
    buf`` EXACTLY — the spoken-prefix bookkeeping relies on byte equality with
    the authoritative end-event text."""
    out: list[str] = []
    start = 0
    for m in _SENT_END_RE.finditer(buf):
        out.append(buf[start : m.end()])
        start = m.end()
    return out, buf[start:]


class _StreamSpeech:
    """Node-side speech state for ONE streamed reply (4.4).

    Lazily opens the tts session on the first synthesized sentence, tracks the
    exact raw prefix that went to TTS (byte-equal bookkeeping against the end
    event's authoritative text), trails off honestly on a mid-reply synthesis
    failure, and closes the session only after the caller has done its floor
    arming — mirroring the buffered order (expect_utterance before tts_end).
    """

    def __init__(
        self,
        server: TranscribingServer,
        node_id: str,
        session_id: str | None,
        on_first_audio: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self.server = server
        self.node_id = node_id
        self.sid = session_id or str(uuid.uuid4())
        self.voice_prompt = ""  # from the head event when the provider sends it
        self.spoken = ""  # the exact raw prefix handed to TTS, in order
        self.started = False  # tts_start sent
        self.failed = False  # a synth/send failure — stop speaking early
        self.closed = False
        self._on_first_audio = on_first_audio

    async def speak(
        self, raw: str, *, voice_prompt: str | None = None, sensitive: bool = False
    ) -> None:
        """Synthesize + stream one raw sentence slice. Failure trails off: the
        reply record stays authoritative, we just stop adding audio."""
        if self.failed or self.closed or not raw.strip():
            if raw and not self.failed and not self.closed:
                self.spoken += raw  # pure whitespace still counts toward the prefix
            return
        vp = self.voice_prompt if voice_prompt is None else voice_prompt
        pcm = await self.server._synthesize(raw.strip(), vp, sensitive=sensitive)
        if not pcm:
            self.failed = True
            log.warning(
                "[%s] sentence TTS failed — trailing off (%d chars spoken)",
                self.node_id,
                len(self.spoken),
            )
            return
        if not self.started:
            if self._on_first_audio is not None:
                await self._on_first_audio()
            self.started = True
            await self.server.send_tts_start(
                self.node_id, self.sid, sample_rate=24000, channels=1, stream=True
            )
        size = self.server._tts_chunk_size
        for i in range(0, len(pcm), size):
            if not await self.server.send_tts_frame(self.node_id, pcm[i : i + size]):
                self.failed = True
                return
        self.spoken += raw

    async def close(self, reply: LlmReply) -> bool:
        """Speak the reply's unspoken remainder, end the session. Returns
        spoke_ok — False only when nothing at all could be spoken."""
        if self.closed:
            return bool(self.spoken) and not self.failed
        remainder = ""
        if reply.text.startswith(self.spoken):
            remainder = reply.text[len(self.spoken) :]
        elif not self.spoken:
            remainder = reply.text
        else:
            # The end text diverged from the streamed preview (shouldn't happen;
            # deltas are a prefix). What was spoken stands — add nothing.
            log.warning("[%s] streamed preview diverged from final text", self.node_id)
        if remainder.strip() and not self.failed:
            await self.speak(
                remainder, voice_prompt=reply.voice_prompt, sensitive=reply.secret
            )
        self.closed = True
        if self.started:
            await self.server.send_tts_end(self.node_id, self.sid)
            if self.node_id in self.server._tts_active:
                await self.server.stop_node(self.node_id)
                self.server._tts_active.discard(self.node_id)
            log.info("[%s] TTS complete (streamed)", self.node_id)
            return not self.failed or bool(self.spoken)
        return not reply.text.strip()  # empty reply = silence by choice, not failure

    async def abort(self) -> None:
        """Best-effort session close on an error/cancel path."""
        if self.started and not self.closed:
            self.closed = True
            try:
                await self.server.send_tts_end(self.node_id, self.sid)
            except Exception:  # noqa: BLE001
                pass


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
            s: str(_mesh_url(cfg, str((cfg.get(s) or {}).get("url"))))
            for s in ("stt", "tts", "llm", "speaker")
            if isinstance(cfg.get(s), dict) and (cfg.get(s) or {}).get("url")
        }
        # Optional shared-secret required in the node's hello (discovery.token).
        self._join_token: str | None = (cfg.get("discovery", {}) or {}).get("token") or None
        # Shared service-to-service bearer for outbound calls to stt/tts/llm/speaker.
        # KENZY_SERVICE_TOKEN (a real env var, seen by all services) is canonical;
        # discovery.token is a fallback for single-host setups.
        self._service_token: str | None = serviceauth.service_token_from_env() or self._join_token

        # node_id → NodeSession  (guarded by _lock)
        self._nodes: dict[str, NodeSession] = {}
        self._lock = asyncio.Lock()
        # Co-audible wake arbitration (opt-in per node via `audio_group`).
        # A wake_pending opens a short window for its group; when it closes the
        # best-placed node proceeds and every other candidate is stopped —
        # inside the one-breath gate's held-chime silence, so a loser never
        # chimes, never captures, never runs a pipeline.
        self._arb_window: dict[str, dict[str, dict[str, Any]]] = {}  # group → node_id → cand
        self._arb_tasks: dict[str, asyncio.Task[None]] = {}  # group → decision task
        self._arb_losers: dict[str, float] = {}  # stopped session_id → expiry (monotonic)
        self._arb_recent: dict[str, float] = {}  # recently-stopped node_id → expiry
        self._arb_first: dict[str, float] = {}  # group → its open window's first-wake time
        self._arb_deadzone: dict[str, tuple[float, str]] = {}  # group → (expiry, winner)
        # Layer 1 stateful groups: each group's current conversation (at most
        # one — the group is one virtual node). See GroupEngagement.
        self._engagements: dict[str, GroupEngagement] = {}
        # Per-source-IP connection timestamps for the registration rate limit (F-10).
        self._conn_log: dict[str, deque[float]] = {}
        # Observers notified when the node registry/state changes (the dashboard
        # registers one for live push). Empty by default ⇒ zero overhead.
        # Optional TLS termination (F-13 slice): `tls: {cert, key}` in server.yaml
        # enables wss on this port (and https on the dashboard, which reads the
        # same block). Clients default to encrypted-but-unverified (self-signed).
        self._ssl: Any = None
        self._tls_paths: tuple[str, str] | None = None
        self._channel_binding: bytes = b""  # SHA-256 of our TLS leaf cert (b"" = plaintext)
        from kenzy.config import kenzy_data_root

        self._data_root = kenzy_data_root()  # our config home, fixed for this process
        # Identity core (F1): person records (voiceprint→person). Absent file ⇒
        # empty store ⇒ the resolver is a passthrough (no behavior change).
        self._people = PeopleStore(self._data_root / "data" / "people.yaml")
        # v5 occupancy spine (Slice B). None unless main() wires it — so an
        # install with occupancy off, or no HA, carries zero overhead and the
        # every-utterance hook in _transcribe is a single `is not None` check.
        self._occupancy: Any = None
        self._ha_events: Any = None
        #: Tier A (5.0.6). Both stay None unless the occupancy spine started —
        #: safety rides its socket, so no HA means no hazard announcements.
        self._proactive: Any = None
        self._safety: Any = None
        # 5.1 plugin seam: server-role plugins from installed kenzy-* dists.
        # One scan per process (install/uninstall is restart-to-apply); the scan
        # is fail-closed per plugin and never raises — belt-and-braces anyway,
        # because a plugin must never take the server down.
        try:
            from kenzy.plugins import scan_plugins

            self._plugins = scan_plugins()
        except Exception as exc:  # pragma: no cover - scan_plugins is designed not to raise
            from kenzy.plugins import PluginScan

            log.error("Plugin scan failed entirely: %s", exc, exc_info=True)
            self._plugins = PluginScan()
        #: Per-plugin context, built at serve() (after main() wires occupancy /
        #: integrations) and reused for every on_plugin_frame dispatch.
        self._plugin_ctx: dict[str, Any] = {}
        self._plugin_tasks: list[asyncio.Task[None]] = []
        #: The integrations hub, when main() wired one (plugins publish to HA
        #: through it). None ⇒ integrations off.
        self._integrations: Any = None
        #: Every proactive decision, refusals included. Deliberately on the
        #: SERVER and not in the dashboard's Activity ring: Activity is gated on
        #: `dashboard.logs` because its records carry household transcripts,
        #: but this is Kenzy's own conduct, and an audit trail that disappears
        #: when a privacy flag flips is not an audit trail. In-memory like the
        #: job history — persistence lands with the panel that displays it.
        self._proactive_log: deque[dict[str, Any]] = deque(maxlen=200)
        #: Where server.yaml came from, so the VOICE off-switch can persist
        #: without the dashboard. A feature you disabled because it misbehaved
        #: must not return on the next restart, and the panic button must not
        #: require `dashboard.enabled`.
        self._config_path: str | None = None
        # F3: has this server EVER received an /assist request? Persistent
        # marker — the dashboard uses it to reveal HA surfaces for app-only
        # households (no HA_API_KEY, but the companion-app front door in use).
        self._assist_seen_path = self._data_root / "data" / ".assist-seen"
        self._assist_seen = self._assist_seen_path.exists()
        tls_cfg = cfg.get("tls") or {}
        if isinstance(tls_cfg, dict) and tls_cfg.get("cert") and tls_cfg.get("key"):
            from kenzy import tlsutil

            try:
                self._ssl = tlsutil.server_context(str(tls_cfg["cert"]), str(tls_cfg["key"]))
                self._tls_paths = (str(tls_cfg["cert"]), str(tls_cfg["key"]))
                self._channel_binding = tlsutil.own_cert_binding(str(tls_cfg["cert"]))
                log.info("TLS enabled on the node WebSocket port (wss)")
            except Exception as exc:
                log.error("TLS config invalid (%s) — continuing WITHOUT TLS", exc)
        self._state_listeners: list[Callable[[], None]] = []
        self._memory_listeners: list[Callable[[], None]] = []
        # Node-override-changed pokes (5.0.4 follow-up): an OPEN node config
        # page is stale the moment any other surface — speakerphone buttons,
        # voice, MQTT, calibration — writes the override. Empty ⇒ zero overhead.
        self._node_config_listeners: list[Callable[[str], None]] = []
        self._calib_listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self._metrics_listeners: list[Callable[[], None]] = []
        # node_id → (override mtime, parsed override) — see _effective_node_config.
        self._node_cfg_cache: dict[str, tuple[float | None, dict[str, Any]]] = {}
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
        # Backend services that auto-register via GET /register (name → {base, version,
        # last_seen}); merged with static <svc>.url config for the dashboard + pipeline.
        self._announced_services: dict[str, dict[str, Any]] = {}
        # Services whose URL came from static config (never overwritten by an announce).
        self._static_services: set[str] = set()
        # Durable roster of nodes that *exist*, so a disconnected one becomes absent
        # rather than nonexistent. Kept next to the other operational state.
        # Co-audible arbitration timing — configurable because the right values
        # depend on hardware the operator owns: a fleet with slow-waking mics
        # (visible as dead-zone suppressions in the log) widens the window; a
        # short custom wake phrase shortens the dead zone. Clamped to sane
        # bounds, and the dead zone can never be shorter than the window it
        # contains. The guard TTLs (loser list, re-wake, node refractory) stay
        # code constants deliberately: they are race mechanics, not tuning.
        arb_cfg: dict[str, Any] = cfg.get("arbitration") or {}
        self._arb_window_s: float = min(
            2.0, max(0.05, float(arb_cfg.get("window_ms", _ARB_WINDOW_S * 1000)) / 1000.0)
        )
        self._arb_deadzone_s: float = max(
            self._arb_window_s,
            min(5.0, float(arb_cfg.get("deadzone_ms", _ARB_DEADZONE_S * 1000)) / 1000.0),
        )
        fleet_cfg: dict[str, Any] = cfg.get("fleet") or {}
        #: How long a node may be missing before it is a fault rather than a note.
        #: Comfortably longer than a restart, so ordinary churn stays quiet.
        self._offline_alert_s: float = float(fleet_cfg.get("offline_alert_minutes", 5)) * 60.0
        #: Expected-downtime window granted when we ourselves tell a node to go.
        self._restart_grace_s: float = float(fleet_cfg.get("restart_grace_minutes", 10)) * 60.0
        self._roster = NodeRoster(self._roster_path())

    @staticmethod
    def _roster_path() -> Path | None:
        from kenzy.config import kenzy_data_root

        try:
            return kenzy_data_root() / "data" / "nodes.json"
        except Exception:  # pragma: no cover - unresolvable data root
            return None

    def forget_node(self, node_id: str) -> bool:
        """Drop a node from the roster. Only meaningful for one that is absent —
        a connected node re-adds itself on its next state change."""
        return self._roster.forget(node_id)

    def absent_nodes(self) -> list[dict[str, Any]]:
        """Known-but-not-connected nodes, as dashboard/state dicts."""
        now = time.time()
        out: list[dict[str, Any]] = []
        for entry in self._roster.absent(self._nodes.keys()):
            rec = entry.as_dict()
            rec["offline_seconds"] = max(0.0, now - entry.last_seen)
            rec["alerting"] = self._roster.is_alerting(entry, self._offline_alert_s, now=now)
            out.append(rec)
        return out

    def add_state_listener(self, fn: Callable[[], None]) -> None:
        """Register a callback fired (in-loop) when the node registry/state changes."""
        self._state_listeners.append(fn)

    def add_metrics_listener(self, fn: Callable[[], None]) -> None:
        """Observe node metrics updates (dashboard live refresh). Kept separate
        from state listeners so 30-second metrics ticks never wake the MQTT/HA
        bridge; zero overhead when nothing subscribes."""
        self._metrics_listeners.append(fn)

    def _notify_metrics(self) -> None:
        for fn in self._metrics_listeners:
            try:
                fn()
            except Exception:
                log.exception("metrics listener failed")

    def _notify_state(self) -> None:
        for fn in self._state_listeners:
            try:
                fn()
            except Exception:  # a listener must never break the pipeline
                log.debug("state listener error", exc_info=True)

    def add_tune_listener(self, fn: Callable[[str, dict[str, Any]], None]) -> None:
        """Register a callback fired with ``(node_id, sample)`` for each tune sample."""
        self._tune_listeners.append(fn)

    def add_calib_listener(self, fn: Callable[[str, dict[str, Any]], None]) -> None:
        """Register a callback fired with ``(node_id, event)`` for each guided-
        calibration progress event (the dashboard's live view)."""
        self._calib_listeners.append(fn)

    def _notify_calib(self, node_id: str, event: dict[str, Any]) -> None:
        for fn in self._calib_listeners:
            try:
                fn(node_id, event)
            except Exception:
                log.debug("calibration listener error", exc_info=True)

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

    async def start_calibration(
        self, node_id: str, room: str, *, mode: str = "spoken"
    ) -> str | None:
        """Guided calibration (implemented by TranscribingServer)."""
        return "calibration not supported by this server"

    def _end_calib_session(self, node_id: str, *, force: bool = False) -> None:
        """Base no-op (TranscribingServer overrides)."""

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

    def _service_headers(self, method: str, url: str | None) -> dict[str, str]:
        """Auth headers for an outbound call to a backend service.

        Sends only the token-proof signature (``X-Kenzy-Auth``) — the raw token
        never rides the wire.
        """
        token = self._service_token
        if not token:
            return {}
        from urllib.parse import urlparse

        path = urlparse(url or "").path or "/"
        return {serviceauth.SIG_HEADER: serviceauth.sign_service_request(token, method, path)}

    # ------------------------------------------------------------------
    # Config-pull: effective per-node config = defaults + per-room override
    # ------------------------------------------------------------------

    def _node_aec(self, node_id: str) -> bool:
        """Whether a node's room has an echo-cancelling speaker (hardware_aec,
        default true). Declared per node in config — it can't be detected."""
        try:
            return bool(self._effective_node_config(node_id).get("hardware_aec", True))
        except Exception:
            return True

    def _occupancy_payload(self) -> dict[str, Any]:
        """The occupancy snapshot for request injection (empty when disabled).

        Refreshes the tracker's staleness from the socket first: a dropped
        connection means held levels stop being trustworthy — reported as stale
        rather than silently rewritten to "empty", which would be inventing a
        fact about someone's house.
        """
        if self._occupancy is None:
            return {}
        if self._ha_events is not None:
            self._occupancy.set_stale(self._ha_events.stats.is_stale())
        rooms = [
            occupancy_mod.room_slug(s.room_id) for s in self._nodes.values() if s.room_id
        ]
        snap = self._occupancy.snapshot(rooms)
        return snap if isinstance(snap, dict) else {}

    def _no_aec_rooms(self) -> list[str]:
        """Connected room names lacking AEC — injected into /process so skills
        refuse alarm/intercom requests conversationally, in the reply itself,
        instead of confirming and then failing after the fact."""
        return sorted({s.room_id for nid, s in self._nodes.items() if not self._node_aec(nid)})

    def _effective_node_config(self, node_id: str) -> dict[str, Any]:
        """Merge central ``node_defaults`` with ``configs/nodes/<node_id>.yaml``.

        The per-node file (if present) shallow-overrides the defaults. A node
        with no override file just receives the defaults; absence is logged so
        operators can see which nodes are unconfigured.
        """
        import yaml  # type: ignore[import-untyped]

        from kenzy.config import kenzy_data_root

        override = kenzy_data_root() / "configs" / "nodes" / f"{node_id}.yaml"
        # mtime-cached: this runs on every dashboard state broadcast (and metrics
        # ticks broadcast), so it must not re-parse YAML per node per snapshot.
        try:
            mtime: float | None = override.stat().st_mtime
        except OSError:
            mtime = None
        cached = self._node_cfg_cache.get(node_id)
        if cached is not None and cached[0] == mtime:
            data = cached[1]  # cache ONLY the file read — everything below always runs
        else:
            data = {}
            if mtime is not None:
                try:
                    loaded = yaml.safe_load(override.read_text()) or {}
                    if isinstance(loaded, dict):
                        data = loaded
                    log.info("[%s] applied per-node override %s", node_id, override)
                except Exception as exc:
                    log.warning("[%s] failed to read override %s: %s", node_id, override, exc)
            else:
                log.info(
                    "[%s] no per-node override (%s) — sending defaults only", node_id, override
                )
            self._node_cfg_cache[node_id] = (mtime, data)
        effective: dict[str, Any] = {**self._node_defaults, **data}
        # 5.1: the `addons` namespace merges PER-ADDON, never shallowly — a
        # per-node override touching one addon key must not drop the defaults'
        # other addons or that addon's sibling keys (the watchdog-dict trap,
        # prevented rather than re-learned). Fresh dicts throughout: the cached
        # override and node_defaults must never be mutated downstream.
        addon_srcs = [
            s
            for s in (self._node_defaults.get("addons"), data.get("addons"))
            if isinstance(s, dict)
        ]
        if addon_srcs:
            merged_addons: dict[str, Any] = {}
            for src in addon_srcs:
                for aid, acfg in src.items():
                    if isinstance(acfg, dict):
                        merged_addons[aid] = {**merged_addons.get(aid, {}), **acfg}
            # The secret-name invariant holds one level down too: an addon key
            # named like a secret is stripped, not served.
            for aid, acfg in merged_addons.items():
                bad = [k for k in acfg if _SECRET_KEY_RE.search(k)]
                for k in bad:
                    del acfg[k]
                if bad:
                    log.warning(
                        "[%s] dropped secret-like keys from served addon '%s' config: %s",
                        node_id,
                        aid,
                        bad,
                    )
            effective["addons"] = merged_addons
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

    # ------------------------------------------------------------------
    # Plugins (5.1): server-role halves — config, startup, frame routing
    # ------------------------------------------------------------------

    def _addon_config(self, plugin: Any) -> dict[str, Any]:
        """This plugin's server-half config: manifest defaults deep-merged with
        ``configs/addons/<id>.yaml`` (its OWN file — a plugin config can never
        wholesale-replace another surface's, the curation.yaml lesson)."""
        import yaml

        path = self._data_root / "configs" / "addons" / f"{plugin.manifest.id}.yaml"
        data: dict[str, Any] = {}
        try:
            if path.exists():
                loaded = yaml.safe_load(path.read_text()) or {}
                if isinstance(loaded, dict):
                    data = loaded
        except Exception as exc:
            log.warning("Addon config %s unreadable (%s) — using defaults", path, exc)
        return _deep_merge(dict(plugin.manifest.config_defaults), data)

    def _plugin_context(self, plugin: Any) -> Any:
        """The (cached) context a server-half plugin runs against."""
        pid = plugin.manifest.id
        ctx = self._plugin_ctx.get(pid)
        if ctx is None:
            from kenzy.plugins import ServerPluginContext

            async def _send(node_id: str, payload: dict[str, Any], _pid: str = pid) -> bool:
                return await self._plugin_send_to_node(_pid, node_id, payload)

            ctx = ServerPluginContext(
                config=self._addon_config(plugin),
                occupancy=self._occupancy,
                integrations=self._integrations,
                log=logging.getLogger(f"kenzy.addon.{pid}"),
                room_of=self._room_of_node,
                send_to_node=_send,
            )
            self._plugin_ctx[pid] = ctx
        return ctx

    async def _plugin_send_to_node(
        self, plugin_id: str, node_id: str, payload: dict[str, Any]
    ) -> bool:
        """Server half → node half. False (never an exception) when the node is
        disconnected, doesn't carry this plugin, or carries an API-skewed half
        — the same refusal as the inbound gate, in the other direction."""
        session = self._nodes.get(node_id)
        if session is None:
            return False
        plugin = self._plugins.get(plugin_id)
        if plugin is None:
            return False
        advert = next(
            (
                p
                for p in (session.capabilities.get("plugins") or [])
                if isinstance(p, dict) and p.get("id") == plugin_id
            ),
            None,
        )
        if advert is None or advert.get("api") != plugin.manifest.api:
            return False
        try:
            await session.ws.send(protocol.plugin_event(plugin_id, payload))
            return True
        except Exception as exc:
            log.debug("[%s] plugin '%s' send failed: %s", node_id, plugin_id, exc)
            return False

    def _room_of_node(self, node_id: str) -> str:
        """The room name a CONNECTED node is in ("" otherwise) — the plugin
        context's ``room_of``. Absent nodes answer "": they send no frames, and
        an offline node's stored room is the roster's business, not evidence's."""
        session = self._nodes.get(node_id)
        return str(session.room_id or "") if session is not None else ""

    def _start_plugins(self) -> None:
        """Launch each server-half plugin's ``server_start`` as a task. Called
        from serve(), after main() has wired occupancy/integrations, so the
        context carries the real subsystems rather than always-None."""
        for plugin in self._plugins.for_role("server"):
            start = plugin.hook("server_start")
            ctx = self._plugin_context(plugin)  # built even hookless: frames need it
            if start is None:
                continue
            self._plugin_tasks.append(
                asyncio.create_task(
                    self._run_plugin_start(plugin.manifest.id, start, ctx),
                    name=f"addon-{plugin.manifest.id}",
                )
            )

    async def _run_plugin_start(self, pid: str, start: Any, ctx: Any) -> None:
        """Non-fatal wrapper: a plugin dying costs that plugin's capability,
        never the server. The error names the plugin so the journal says which."""
        try:
            await start(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "Plugin '%s' server task died: %s — server continues", pid, exc, exc_info=True
            )

    async def _on_plugin_event(self, session: NodeSession, msg: dict[str, Any]) -> None:
        """Route a node's ``plugin_event`` frame to that plugin's server half.
        Every refusal states its reason at the right level: an absent half is
        debug (a node-only install is legitimate), API skew is a warning (the
        operator must upgrade one side), a handler error is the plugin's alone."""
        pid = str(msg.get("plugin") or "")
        plugin = self._plugins.get(pid)
        if plugin is None or "server" not in plugin.manifest.roles:
            log.debug("[%s] plugin_event for absent server half: %r", session.node_id, pid)
            return
        # Skew gate: the node adverted its half's API version in hello. A
        # mismatched half is treated like an incompatible install — the event
        # is dropped, not half-understood.
        advert = next(
            (
                p
                for p in (session.capabilities.get("plugins") or [])
                if isinstance(p, dict) and p.get("id") == pid
            ),
            None,
        )
        if advert is not None and advert.get("api") != plugin.manifest.api:
            log.warning(
                "[%s] plugin '%s' API skew (node v%s, server v%s) — event dropped; "
                "upgrade the older half",
                session.node_id,
                pid,
                advert.get("api"),
                plugin.manifest.api,
            )
            return
        hook = plugin.hook("on_plugin_frame")
        if hook is None:
            return
        payload = msg.get("payload")
        try:
            await hook(
                self._plugin_context(plugin),
                session.node_id,
                payload if isinstance(payload, dict) else {},
            )
        except Exception as exc:
            log.error("Plugin '%s' frame handler failed: %s", pid, exc, exc_info=True)

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
        """Write configs/nodes/<node_id>.yaml verbatim (empty ⇒ remove file).

        The one choke point every override writer passes through — which is why
        the node-config poke fires here: a volume button, a spoken "turn it up",
        MQTT, calibration, and the editor's own save all land in this file, and
        an open config page deserves to hear about all of them.
        """
        import yaml

        path = self._override_path(node_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if mapping:
            path.write_text(yaml.safe_dump(dict(sorted(mapping.items())), default_flow_style=False))
        elif path.is_file():
            path.unlink()
        for fn in self._node_config_listeners:
            try:
                fn(node_id)
            except Exception:  # a listener must never break a config write
                log.debug("node-config listener error", exc_info=True)

    def add_node_config_listener(self, cb: Callable[[str], None]) -> None:
        """Observe per-node override writes (any surface). Fired with node_id."""
        self._node_config_listeners.append(cb)

    def write_node_override(self, node_id: str, mapping: dict[str, Any]) -> None:
        """Validate and persist configs/nodes/<node_id>.yaml (empty ⇒ remove file).

        Server-managed keys (e.g. ``room_id``, set via :meth:`set_room`) live in
        the same file but aren't part of the editable grid, so they're preserved
        across an editor save rather than wiped.
        """
        self._node_cfg_cache.pop(node_id, None)  # override changing — drop the cache
        if not isinstance(mapping, dict):
            raise ValueError("override must be a mapping")
        # Yaml-only keys are dropped from the INPUT (an older cached editor may
        # echo them back) and re-instated from the FILE below — the file is
        # their source of truth, never a client's copy.
        mapping = {k: v for k, v in mapping.items() if k not in _YAML_ONLY_OVERRIDE_KEYS}
        unknown = sorted(k for k in mapping if k not in _ALLOWED_OVERRIDE_KEYS)
        if unknown:
            raise ValueError("unsupported keys: " + ", ".join(unknown))
        existing = self.read_node_override(node_id)
        keep = _SERVER_MANAGED_KEYS | _YAML_ONLY_OVERRIDE_KEYS
        preserved = {k: existing[k] for k in keep if k in existing}
        merged = {**preserved, **mapping}
        self._write_override_file(node_id, merged)
        if merged:
            log.info("[%s] wrote per-node override (%d keys)", node_id, len(merged))
        else:
            log.info("[%s] cleared per-node override", node_id)

    async def write_addon_node_config(
        self, node_id: str, addon_id: str, config: dict[str, Any]
    ) -> None:
        """Persist ONE addon's slice of a node's override (``addons.<addon_id>``)
        and live-push. Everything else in the file — other addons included — is
        preserved: read-merge-write per addon, never wholesale (the
        save_curation lesson). The panel's save path.
        """
        if not isinstance(config, dict):
            raise ValueError("addon config must be a mapping")
        if self._plugins.get(addon_id) is None:
            raise ValueError(f"no such addon: {addon_id}")
        secretish = sorted(k for k in config if _SECRET_KEY_RE.search(k))
        if secretish:
            # Refused loudly at write time — served configs silently strip
            # these names, so accepting one would create a key that can never
            # reach the node (the volume_buttons trap, blocked at the door).
            raise ValueError(
                "key(s) would be stripped from served config (secret-like name): "
                + ", ".join(secretish)
            )
        self._node_cfg_cache.pop(node_id, None)
        existing = self.read_node_override(node_id)
        addons = dict(existing.get("addons") or {})
        if config:
            addons[addon_id] = config
        else:
            addons.pop(addon_id, None)  # empty ⇒ clear this addon's slice
        merged = {**existing, "addons": addons}
        if not addons:
            merged.pop("addons", None)
        self._write_override_file(node_id, merged)
        log.info("[%s] wrote addon '%s' node config (%d keys)", node_id, addon_id, len(config))
        await self.push_config(node_id)  # live: the node re-syncs its plugin task

    def apply_node_defaults(self, patch: dict[str, Any]) -> None:
        """Live-update fleet-wide ``node_defaults`` (cue regeneration): the
        caller persists the same patch to the server.local.yaml layer for the
        next boot; this makes it effective NOW (the cue keys are read live at
        fire time, so no config push is needed)."""
        self._node_defaults.update(patch)
        self._node_cfg_cache.clear()

    async def push_config(self, node_id: str) -> bool:
        """Re-push effective config to a connected node (live config_update)."""
        session = self._nodes.get(node_id)
        if session is None:
            return False
        await session.ws.send(protocol.config(self._effective_node_config(node_id)))
        self._notify_state()
        return True

    # ------------------------------------------------------------------
    # People / identity records (dashboard People panel)
    # ------------------------------------------------------------------

    def _person_memory_opt_out(self, identity: Identity | None) -> bool:
        if identity is None or not identity.person_id:
            return False
        person = self._people.get(identity.person_id)
        return bool(person and person.memory_opt_out)

    def _person_memory_capture(self, identity: Identity | None) -> str:
        if identity is None or not identity.person_id:
            return "explicit"
        person = self._people.get(identity.person_id)
        return person.memory_capture if person else "explicit"

    def assist_seen(self) -> bool:
        """True once any /assist request has ever reached this server (F3)."""
        return self._assist_seen

    def mark_assist_seen(self) -> None:
        if self._assist_seen:
            return
        self._assist_seen = True
        try:
            self._assist_seen_path.parent.mkdir(parents=True, exist_ok=True)
            self._assist_seen_path.write_text("")
        except OSError as exc:  # marker is best-effort; the in-memory flag stands
            log.debug("could not persist assist-seen marker: %s", exc)

    def list_people(self) -> list[dict[str, Any]]:
        """Serialize the person records for the dashboard (name, voiceprints,
        the ha_user presence link — editable in the drill-down — and the
        reserved phone link)."""
        return [
            {
                "id": p.id,
                "name": p.name,
                "voiceprints": list(p.voiceprints),
                "aliases": list(p.aliases),
                "ha_user": p.ha_user,
                "phone": p.phone,
                "memory_opt_out": p.memory_opt_out,
                "memory_capture": p.memory_capture,
            }
            for p in self._people.all()
        ]

    def save_person(
        self,
        person_id: str,
        name: str,
        voiceprints: list[str],
        aliases: list[str] | None = None,
        ha_user: str | None = UNSET,
        memory_opt_out: bool | None = None,
        memory_capture: str | None = None,
    ) -> str:
        """Create/update a person and persist ``data/people.yaml``. Returns the id
        (generated from the name for a new record). ``ha_user`` (the HA person
        entity linking their phone identity, F3) is three-state: omitted ⇒
        preserved, a string ⇒ set, ""/None ⇒ cleared. The pipeline's resolver
        sees the change immediately (same store object)."""
        person = self._people.save_person(
            id=person_id,
            name=name,
            voiceprints=voiceprints,
            aliases=aliases,
            ha_user=ha_user,
            memory_opt_out=memory_opt_out,
            memory_capture=memory_capture,
        )
        log.info("Saved person %r (%d voiceprint(s))", person.id, len(person.voiceprints))
        return person.id

    def delete_person(self, person_id: str) -> bool:
        ok = self._people.delete_person(person_id)
        if ok:
            log.info("Deleted person %r", person_id)
        return ok

    def rename_person_voiceprint(self, old: str, new: str) -> None:
        """Follow a speaker-service voiceprint rename in the person records."""
        if self._people.rename_voiceprint(old, new):
            log.info("Followed voiceprint rename %r → %r in person records", old, new)

    def remove_person_voiceprint(self, name: str) -> None:
        """Drop a deleted speaker-service voiceprint from its owning person."""
        if self._people.remove_voiceprint(name):
            log.info("Removed deleted voiceprint %r from its person record", name)

    def adopt_enrolled_voice(
        self, voiceprint: str, display: str, person_id: str | None = None
    ) -> None:
        """Person-first enrollment invariant: every enrolled voice belongs to a
        person. Called as soon as an enrollment stores its first sample — link
        the voiceprint to the intended person (``person_id``), else to whoever
        already owns it / matches the spoken name, else create the person. This
        covers every path: the dashboard (pre-picked person), the "enroll me
        as Alice" voice command, and legacy ``kenzy-enroll`` names."""
        if person_id:
            person = self._people.get(person_id)
            if person is not None:
                if not any(v.lower() == voiceprint.lower() for v in person.voiceprints):
                    self._people.save_person(
                        id=person.id,
                        name=person.name,
                        voiceprints=[*person.voiceprints, voiceprint],
                    )
                    log.info("Linked voiceprint %r to person %r", voiceprint, person.id)
                return
        if self._people.by_voiceprint(voiceprint) is not None:
            return  # already owned
        person = self._people.by_name(display)
        if person is not None:
            self._people.save_person(
                id=person.id, name=person.name, voiceprints=[*person.voiceprints, voiceprint]
            )
            log.info("Linked voiceprint %r to existing person %r", voiceprint, person.id)
        else:
            created = self._people.save_person(id=None, name=display, voiceprints=[voiceprint])
            log.info("Created person %r for enrolled voiceprint %r", created.id, voiceprint)

    # ------------------------------------------------------------------
    # Central service config store (stt/tts/llm/speaker pull this at boot)
    # ------------------------------------------------------------------

    def _effective_service_config(
        self, service: str, *, include_override: bool = True, include_secrets: bool = False
    ) -> dict[str, Any]:
        """Effective config for a backend service = packaged default ← stored override.

        The stored override lives at ``configs/services/<service>.yaml`` (server-
        owned) and is deep-merged over the packaged default. Secret-like keys are
        stripped from the config body, so secrets are never *stored*. When
        ``include_secrets`` is set (only on the authenticated TLS ``/config``
        pull, never the dashboard read), the server adds a ``_secrets`` map of
        the API keys this service needs that the server holds in its environment
        — so keys can live on one host (stage b). ``include_override=False``
        returns just the inherited layer for the dashboard editor's placeholders.
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
        if include_override and override.is_file():
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
        # Mesh TLS: when this server terminates TLS, co-located services reuse
        # the same pair — injected here (post-strip: `tls.key` is a path, but
        # the secret-stripper would eat any key named "key" from an override,
        # so remote hosts use KENZY_TLS_CERT/KENZY_TLS_KEY env instead). A
        # service whose files don't exist logs a warning and stays plaintext.
        if self._tls_paths and not (base.get("tls") or {}).get("key"):
            base["tls"] = {"cert": self._tls_paths[0], "key": self._tls_paths[1]}
        # Central secrets (stage b): serve the API keys this service needs that
        # we hold — only on the authenticated TLS pull (caller sets the flag).
        if include_secrets:
            secrets = {
                name: os.environ[name]
                for name in _SERVICE_SECRETS.get(service, ())
                if os.environ.get(name)
            }
            if secrets:
                base["_secrets"] = secrets
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

    def _join_check(self, msg: dict[str, Any]) -> tuple[str, str] | None:
        """``None`` when the hello is authorized, else ``(tag, detail)``.

        The raw ``token`` field (pre-3.12) is no longer accepted. No configured
        join token ⇒ open (unauthenticated joins allowed)."""
        if not self._join_token:
            return None
        token = str(self._join_token)
        node_id = str(msg.get("node_id") or msg.get("room_id") or "")
        return serviceauth.check_node_hello(msg.get("auth"), token, node_id)

    def _authorize_service(
        self, request: Request, method: str, path: str
    ) -> tuple[bool, int | None]:
        """Authorize an inbound service-to-service request.

        Returns ``(authorized, ts)`` where ``ts`` is the request timestamp from the
        token-proof (``X-Kenzy-Auth``) signature — the caller signs the response
        with it — and ``None`` when auth is disabled.
        """
        token = self._service_token
        if not token:
            return True, None
        ts = serviceauth.verify_service_request(
            request.headers.get(serviceauth.SIG_HEADER), token, method, path
        )
        if ts is not None:
            return True, ts
        return False, None

    def _check_service_token(self, request: Request) -> bool:
        """Back-compat boolean wrapper (announce/register call sites)."""
        ok, _ts = self._authorize_service(request, "GET", request.path.split("?", 1)[0])
        return ok

    def _sign_response(self, resp: Response, ts: int | None) -> Response:
        """Attach ``X-Kenzy-Sig`` when the request was token-proof (``ts`` set) —
        binds the reply body to this server's TLS cert so a relay presenting a
        different cert is caught client-side. Works for JSON or binary bodies."""
        if ts is not None and self._service_token:
            resp.headers["X-Kenzy-Sig"] = serviceauth.sign_service_response(
                self._service_token, ts, bytes(resp.body), binding=self._channel_binding
            )
        return resp

    def _signed_json(self, status: int, payload: Any, ts: int | None) -> Response:
        return self._sign_response(self._http_json(status, payload), ts)

    def _http_data_slice(self, service: str, ts: int | None) -> Response:
        """``GET /data/<service>`` — the service's data slice (embeddings, or
        skills+curation) as a signed tar.gz from this server's config home. A
        freshly installed service self-populates from this; local data wins, so
        an already-populated host never calls it. Closes the backup/restore
        asymmetry: restore the server, hosts repopulate themselves."""
        from kenzy import backup

        body = backup.create_data_slice(self._data_root, service)
        headers = Headers()
        headers["Content-Type"] = "application/gzip"
        return self._sign_response(Response(200, "OK", headers, body), ts)

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
        if path == "/chime":
            return await self._http_chime(request)
        if path == "/assist":
            return await self._http_assist(request)
        if path == "/register":
            return self._http_register(request, connection)
        if path.startswith("/notify"):
            ok, _ts = self._authorize_service(request, "GET", "/notify")
            if not ok:
                return self._http_json(401, {"error": "invalid service token"})
            from urllib.parse import parse_qs, urlsplit

            what = (parse_qs(urlsplit(request.path).query).get("what") or [""])[0]
            if what == "memory":
                # kenzy-llm pokes on memory/lockbox changes (debounced its side):
                # the dashboard pushes a data-less {"type":"memory"} to browsers.
                self._notify_memory()
            return self._http_json(200, {"ok": True})
        if path.startswith("/data/"):
            ok, ts = self._authorize_service(request, "GET", path)
            if not ok:
                return self._http_json(401, {"error": "invalid service token"})
            service = path[len("/data/") :]
            from kenzy import backup

            if service not in backup.DATA_SLICES:
                return self._http_json(404, {"error": "no data slice for service"})
            return self._http_data_slice(service, ts)
        if not path.startswith("/config/"):
            return None
        ok, ts = self._authorize_service(request, "GET", path)
        if not ok:
            return self._http_json(401, {"error": "invalid service token"})
        service = path[len("/config/") :]
        if service not in SERVICES or service == "node":
            return self._http_json(404, {"error": "unknown service"})
        # Secrets ride only an authenticated (ts set = token-proof) TLS channel
        # (channel_binding present) — never plaintext. So a relay that can't
        # produce a signature gets no keys.
        with_secrets = ts is not None and bool(self._channel_binding)
        cfg = self._effective_service_config(service, include_secrets=with_secrets)
        return self._signed_json(200, cfg, ts)

    async def _http_assist(self, request: Request) -> Response:
        """``GET /assist`` — the HA Assist channel (F3). Base server has no
        pipeline; ``TranscribingServer`` overrides."""
        return self._http_json(501, {"error": "assist is not available on this server"})

    async def _http_announce(self, request: Request) -> Response:
        """Handle ``/announce?text=…&rooms=…`` — speak a message aloud in rooms.

        ``rooms`` is a comma-separated list of room names (empty = every room).
        Token-gated like the other always-on endpoints.
        """
        from urllib.parse import parse_qs, urlsplit

        if not self._check_service_token(request):
            return self._http_json(401, {"error": "invalid service token"})
        self.mark_assist_seen()
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

    async def _http_chime(self, request: Request) -> Response:
        """Handle ``/chime?sound=…&seconds=…&repeats=…&rooms=…`` — the sound-alert
        twin of ``/announce`` for broker-less HA (``rest_command``). Token-gated;
        sound names resolve only within the configured library roots/aliases/
        bundled sounds."""
        from urllib.parse import parse_qs, urlsplit

        if not self._check_service_token(request):
            return self._http_json(401, {"error": "invalid service token"})
        qs = parse_qs(urlsplit(request.path).query)

        def _one(key: str) -> str:
            return (qs.get(key) or [""])[0].strip()

        sound = _one("sound") or None
        try:
            seconds = float(_one("seconds") or 0)
            repeats = int(_one("repeats") or 0)
        except ValueError:
            return self._http_json(400, {"error": "seconds/repeats must be numbers"})
        rooms_raw = _one("rooms")
        rooms = [r.strip() for r in rooms_raw.split(",") if r.strip()] if rooms_raw else None
        # Resolve + load first so a typo'd sound is a 404, distinct from
        # "played nowhere" (no matching rooms), which is an honest 200/0.
        from kenzy.server import tones

        spec = self._chime_spec(sound or _CHIME_DEFAULT)
        if spec is None or tones.load_tone(spec) is None:
            return self._http_json(404, {"error": f"unknown sound {sound or _CHIME_DEFAULT!r}"})
        count = await self.play_chime(sound, seconds, rooms, repeats=repeats)
        return self._http_json(200, {"played": count, "sound": sound or _CHIME_DEFAULT})

    def _http_register(self, request: Request, connection: ServerConnection) -> Response:
        """Handle ``/register?service=&host=&port=&version=`` — a backend service
        announcing itself so it appears in the dashboard and the pipeline can reach it
        without a static ``<svc>.url``. Token-gated; params ride the query string (the
        request hook exposes no body). The reachable host is the reported ``host``, or
        the request's source IP when the service binds ``0.0.0.0``.
        """
        from urllib.parse import parse_qs, urlsplit

        if not self._check_service_token(request):
            return self._http_json(401, {"error": "invalid service token"})
        qs = parse_qs(urlsplit(request.path).query)
        service = (qs.get("service") or [""])[0]
        if service not in _SERVICE_ENDPOINTS:
            return self._http_json(404, {"error": "unknown service"})
        host = (qs.get("host") or [""])[0].strip()
        if host in ("", "0.0.0.0", "::"):
            host = connection.remote_address[0] if connection.remote_address else "127.0.0.1"
        try:
            port = int((qs.get("port") or ["0"])[0])
        except ValueError:
            port = 0
        if not port:
            return self._http_json(400, {"error": "missing or invalid port"})
        scheme = "https" if (qs.get("tls") or ["0"])[0] == "1" else "http"
        base = f"{scheme}://{host}:{port}"
        first = service not in self._announced_services
        self._announced_services[service] = {
            "base": base,
            "version": (qs.get("version") or [""])[0] or None,
            "last_seen": time.time(),
        }
        # Fill the pipeline URL when the operator didn't configure one statically.
        if service not in self._static_services:
            setattr(self, f"_{service}_url", base + _SERVICE_ENDPOINTS[service])
        if first:
            log.info("Backend service '%s' registered at %s", service, base)
            self._notify_state()
        return self._http_json(200, {"ok": True})

    def announced_health_urls(self) -> dict[str, str]:
        """Fresh auto-registered services → ``{name: <base>/health}``, pruning any that
        haven't re-announced within the TTL (and clearing their pipeline URL)."""
        out: dict[str, str] = {}
        now = time.time()
        for name in list(self._announced_services):
            rec = self._announced_services[name]
            if now - float(rec["last_seen"]) > _REGISTER_TTL:
                del self._announced_services[name]
                if name not in self._static_services:
                    setattr(self, f"_{name}_url", None)
                log.info("Backend service '%s' deregistered (no heartbeat)", name)
                continue
            out[name] = f"{rec['base']}/health"
        return out

    def announced_service_version(self, name: str) -> str | None:
        rec = self._announced_services.get(name)
        return str(rec["version"]) if rec and rec.get("version") else None

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

        if self._join_token is not None and (failure := self._join_check(msg)) is not None:
            tag, detail = failure
            # Name the actual fault. Collapsing every join failure into "bad/missing
            # join token" once cost two days of hunting a token problem that was in
            # fact a node whose clock had drifted 30 minutes at boot: the server had
            # already computed the skew and then threw it away.
            log.warning(
                "Rejected node '%s' (room '%s', v%s) from %s: %s",
                msg.get("node_id") or "?",
                msg.get("room_id") or "?",
                msg.get("kenzy_version") or "?",
                ws.remote_address,
                detail,
            )
            try:
                # The tag (not the detail) rides the wire: enough for the node's own
                # log to say "stale timestamp" and point at NTP, without handing an
                # unauthenticated peer the server's precise clock.
                await ws.close(1008, f"invalid join token ({tag})")
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
        # A node can only be arbitrated if it SPEAKS the arbitration protocol
        # (wake_pending, 5.1.1+). An older node in a group doesn't degrade
        # loudly — it simply never announces its wakes, keeps answering on its
        # own, and to the operator that looks exactly like arbitration failing
        # (field report, 2026-08-17: a 3-node collision where two nodes ran
        # pre-5.1.1 code — one answered correctly, one Whisper-hallucinated an
        # answer from the first one's TTS bleed-through). Say so at every join,
        # naming the node, the group, and the fix.
        arb_group = self._node_audio_group(node_id)
        if arb_group is not None and not _supports_arbitration(session.kenzy_version):
            log.warning(
                "[%s] room '%s' is in audio_group '%s' but the node runs kenzy %s — "
                "wake arbitration needs the NODE on 5.1.1+, so this node will keep "
                "answering on its own (and cannot be stood down) until it is upgraded.",
                node_id,
                room_id,
                arb_group,
                session.kenzy_version or "unknown (pre-5.0)",
            )
        self._roster.touch(
            node_id,
            room=room_id,
            version=session.kenzy_version,
            ip=ws.remote_address[0] if ws.remote_address else None,
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
        # A node vanishing mid-conversation must not leave its group's
        # engagement claimed — the next wake would "cancel" a ghost.
        self._engagement_clear(session.node_id, "node disconnected")
        # Stamp the sighting before announcing the drop: the roster keeps the node
        # visible (as absent, with a last-seen) instead of letting it vanish. Any
        # expected-downtime grace survives — it exists precisely to cover this.
        self._roster.touch(session.node_id, room=session.room_id, clear_grace=False)
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

    def _node_audio_group(self, node_id: str) -> str | None:
        """The node's co-audible group, or None (the default: no arbitration)."""
        raw = self._effective_node_config(node_id).get("audio_group")
        group = str(raw).strip() if raw is not None else ""
        return group or None

    async def _on_wake_pending(self, session: NodeSession, msg: dict[str, Any]) -> None:
        """A node's wake fired and its gate is holding the chime. Logged for
        every node (per-wake evidence is diagnostic gold); arbitrated only when
        the node opted into an ``audio_group``."""
        sid = str(msg.get("session_id") or "")
        if session.ignore_audio:
            # Operator input-mute (test/ops): this wake never happened as far
            # as arbitration and the group are concerned — no candidacy, no
            # conversation cancel. The node's own gate resolves on its own and
            # its audio_start is refused below.
            log.info(
                "[%s/%s] wake_pending disregarded (audio ignored by operator)",
                session.node_id,
                sid[:8] or "?",
            )
            return
        wake_db = _as_float(msg.get("wake_db"))
        margin = _as_float(msg.get("wake_margin_db"))
        score = _as_float(msg.get("score"))
        # Log BEFORE anything that could raise (the group lookup reads config):
        # a received-but-unlogged wake_pending is indistinguishable from one
        # that was never sent, and a field investigation turned on exactly that
        # distinction (2026-08-17).
        log.info(
            "[%s/%s] wake_pending db=%s margin=%s score=%s",
            session.node_id,
            sid[:8] or "?",
            wake_db,
            margin,
            score,
        )
        group = self._node_audio_group(session.node_id)
        if group is None or not sid:
            return
        eng = self._engagements.get(group)
        if eng is not None and eng.phase == "speaking" and eng.owner != session.node_id:
            # The accepted risk (founder, 2026-08-18), kept visible: a wake
            # landing while the group's own reply is playing may be Kenzy's
            # TTS waking a sibling. It proceeds — and cancels the conversation
            # — by design; this line is what makes the trade auditable.
            log.info(
                "[%s/%s] wake during group '%s' speaking phase (owner %s) — "
                "possible TTS bleed; proceeding (accepted risk)",
                session.node_id,
                sid[:8],
                group,
                eng.owner,
            )
        now = time.monotonic()
        recent = self._arb_recent.get(session.node_id)
        if recent is not None and recent > now:
            # The score tail of the utterance this node just lost — stop it
            # again before it becomes a solo candidate in a fresh window.
            log.info(
                "[%s/%s] wake_pending suppressed (lost arbitration %.1f s ago)",
                session.node_id,
                sid[:8],
                _ARB_REWAKE_S - (recent - now),
            )
            self._arb_losers[sid] = now + _ARB_LOSER_TTL_S
            try:
                await session.ws.send(protocol.stop())
            except Exception:
                pass
            return
        task = self._arb_tasks.get(group)
        window_open = task is not None and not task.done()
        if not window_open:
            dz = self._arb_deadzone.get(group)
            if dz is not None and dz[0] > now:
                # Straggler: this group's window already closed for the current
                # utterance and its winner has proceeded — a wake landing in
                # the rest of the one-second budget is the SAME phrase heard
                # late, not a new contender. Stop it before it becomes a solo
                # candidate in a fresh window.
                log.info(
                    "[%s/%s] wake_pending ignored (dead zone, %d ms late; winner was %s)",
                    session.node_id,
                    sid[:8],
                    int((now - (dz[0] - self._arb_deadzone_s)) * 1000),
                    dz[1],
                )
                self._arb_losers[sid] = now + _ARB_LOSER_TTL_S
                try:
                    await session.ws.send(protocol.stop())
                except Exception:
                    pass
                return
            self._arb_deadzone.pop(group, None)  # expired
        window = self._arb_window.setdefault(group, {})
        window[session.node_id] = {  # a re-wake from the same node replaces its entry
            "node_id": session.node_id,
            "session_id": sid,
            "db": wake_db,
            "margin": margin,
            "score": score,
        }
        if not window_open:
            self._arb_first[group] = now  # the utterance's 1 s budget starts here
            self._arb_tasks[group] = asyncio.create_task(
                self._arb_decide(group), name=f"arb-{group}"
            )

    async def _arb_decide(self, group: str) -> None:
        """Close the group's window and stop every candidate but the winner —
        while the losers' gates are still holding their chimes."""
        await asyncio.sleep(self._arb_window_s)
        cands = list(self._arb_window.pop(group, {}).values())
        self._arb_tasks.pop(group, None)
        first = self._arb_first.pop(group, time.monotonic() - self._arb_window_s)
        if not cands:
            return
        # The dead zone opens for EVERY closed window — solo included: a solo
        # winner proceeded just as surely, and the straggler it must be
        # protected from is exactly the node that made the window solo.
        # (The winner is cands[0] either way; the contested sort below only
        # reorders when there is competition.)
        # The one-virtual-node rule fires on EVERY closed window, solo included:
        # a wake heard by any member ends the group's current conversation, and
        # a solo wake is the canonical case (user woke her at node B while node
        # A was mid-answer across the room).
        await self._group_cancel(group, spare={c["node_id"] for c in cands})
        if len(cands) < 2:
            self._arb_deadzone[group] = (first + self._arb_deadzone_s, cands[0]["node_id"])
            return  # one node heard it — nothing to arbitrate
        # Best-placed node: loudest phrase (dBFS), margin-over-floor as the
        # tiebreak, wake score last. Provisional metric — lab data (2026-08-14)
        # says raw dBFS separates cleanly in steady state but mismatched
        # hardware carries a per-device offset; per-node baselines are the
        # known follow-up, and every decision is logged to keep the evidence
        # flowing.
        cands.sort(
            key=lambda c: (
                c["db"] if c["db"] is not None else -999.0,
                c["margin"] if c["margin"] is not None else -999.0,
                c["score"] or 0.0,
            ),
            reverse=True,
        )
        winner, losers = cands[0], cands[1:]
        now = time.monotonic()
        self._arb_deadzone[group] = (first + self._arb_deadzone_s, winner["node_id"])
        self._arb_losers = {s: t for s, t in self._arb_losers.items() if t > now}
        log.info(
            "wake arbitration [%s]: %s wins (db=%s margin=%s score=%s) over %s",
            group,
            winner["node_id"],
            winner["db"],
            winner["margin"],
            winner["score"],
            ", ".join(
                f"{c['node_id']} (db={c['db']} margin={c['margin']} score={c['score']})"
                for c in losers
            ),
        )
        self._arb_recent = {n: t for n, t in self._arb_recent.items() if t > now}
        for cand in losers:
            self._arb_losers[cand["session_id"]] = now + _ARB_LOSER_TTL_S
            self._arb_recent[cand["node_id"]] = now + _ARB_REWAKE_S
            loser = self._nodes.get(cand["node_id"])
            if loser is None:
                continue
            try:
                await loser.ws.send(protocol.stop())
            except Exception as exc:
                log.warning("arbitration stop to %s failed: %s", cand["node_id"], exc)

    def _arb_is_loser(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        expiry = self._arb_losers.get(session_id)
        return expiry is not None and expiry > time.monotonic()

    # -- Layer 1 stateful groups: the engagement record + the group cancel ----

    async def _engagement_claim(self, node_id: str, session_id: str | None) -> None:
        """A new capture claims the group's engagement — and if that means
        taking it over another node's live conversation, the CLAIM performs
        the cancel. This cannot be left to the arbitration window's close:
        a one-breath confirm sends audio_start in ~200 ms, BEFORE the 250 ms
        window closes, and the claim used to silently destroy the old
        engagement record — leaving the old owner's still-playing answer with
        no handle to stop it (found live 2026-08-18, by forcing exactly this
        ordering with the test tools). Whoever claims, cancels; the window
        close remains the backstop for candidates that never open sessions."""
        group = self._node_audio_group(node_id)
        if group is None:
            return
        eng = self._engagements.get(group)
        if (
            eng is not None
            and eng.owner != node_id
            and time.monotonic() - eng.since <= _ENGAGEMENT_STALE_S
        ):
            self._cancel_pipeline(eng.owner)
            # v6 follow-up: the claim ends the old owner's CONVERSATION too —
            # engine session closed, floor cleared. s2s turn tasks are
            # deliberately not in _stt_tasks (a barge must not hard-kill a
            # turn), so _cancel_pipeline can't reach them; without this, the
            # old conversation's in-flight turn would finish, speak into the
            # stopped room, and re-arm its mic — the two-butlers problem
            # reborn one layer up (found by the founder asking exactly this).
            bridge = getattr(self, "_s2s_bridge", None)  # TranscribingServer state
            if bridge is not None and bridge.active(eng.owner):
                asyncio.create_task(
                    bridge.close(eng.owner, "group claimed by sibling"),
                    name=f"s2s-groupclose-{eng.owner}",
                )
            old = self._nodes.get(eng.owner)
            if old is not None:
                try:
                    await old.ws.send(protocol.stop())
                except Exception:
                    pass
            log.info(
                "group '%s': conversation on %s cancelled (%s) — %s claimed the group",
                group,
                eng.owner,
                f"phase={eng.phase}",
                node_id,
            )
        self._engagement_update(node_id, session_id, "capturing")

    def _engagement_update(self, node_id: str, session_id: str | None, phase: str) -> None:
        """Advance the node's group engagement. ``capturing`` CLAIMS the
        engagement for this node (a new turn); other phases only advance an
        engagement this node already owns."""
        group = self._node_audio_group(node_id)
        if group is None:
            return
        eng = self._engagements.get(group)
        if phase == "capturing":
            self._engagements[group] = GroupEngagement(
                group, node_id, session_id, phase, time.monotonic()
            )
            log.info("group '%s': engagement -> %s (capturing)", group, node_id)
        elif eng is not None and eng.owner == node_id:
            eng.phase = phase
            if session_id:
                eng.session_id = session_id
            log.info("group '%s': engagement %s -> %s", group, node_id, phase)

    def _engagement_clear(self, node_id: str, reason: str) -> None:
        """The exchange ended (reply done and no floor, window expired, empty
        capture). Only the owner may clear its group's engagement."""
        group = self._node_audio_group(node_id)
        if group is None:
            return
        eng = self._engagements.get(group)
        if eng is not None and eng.owner == node_id:
            del self._engagements[group]
            log.info("group '%s': engagement ended (%s)", group, reason)

    def _has_pipeline(self, node_id: str) -> bool:
        """Subclass hook: is a pipeline (STT→LLM→TTS) in flight for this node?"""
        return False

    def _cancel_pipeline(self, node_id: str) -> None:
        """Subclass hook: cancel that in-flight pipeline."""

    async def _group_cancel(self, group: str, spare: set[str]) -> None:
        """The one-virtual-node rule: a wake heard by ANY member ends the
        group's current conversation, so the collective only ever holds one.
        Called at window close; ``spare`` is this utterance's candidates —
        their NEW gates must survive (a stop would cancel their candidacy),
        though their old pipelines are cancelled like everyone else's (a
        candidate's previous reply is part of the conversation being ended).
        Ships with an accepted risk, named when it was accepted (founder,
        2026-08-18): Kenzy's own TTS waking a sibling can end a conversation
        unintentionally. Also on purpose: an in-progress calibration or
        enrollment session in the group yields to a wake — "the wake word
        always cancels" is the standing contract this extends."""
        eng = self._engagements.pop(group, None)
        now = time.monotonic()
        targets: dict[str, str] = {}
        if eng is not None and eng.owner not in spare:
            if now - eng.since <= _ENGAGEMENT_STALE_S:
                targets[eng.owner] = f"phase={eng.phase}"
            else:
                log.info(
                    "group '%s': stale engagement on %s discarded (no stop sent)",
                    group,
                    eng.owner,
                )
        for nid, sess in list(self._nodes.items()):
            if nid in spare or nid in targets:
                continue
            if not (sess.streaming or self._has_pipeline(nid)):
                continue
            if self._node_audio_group(nid) != group:
                continue
            targets[nid] = "streaming" if sess.streaming else "pipeline"
        # Old pipelines die for EVERYONE, spared candidates included — their
        # new session's on_session_start would cancel it anyway; earlier is
        # cleaner (a cancelled reply must not keep streaming at a node whose
        # gate is deciding).
        for nid in list(self._nodes):
            if self._node_audio_group(nid) == group and self._has_pipeline(nid):
                self._cancel_pipeline(nid)
        for nid, why in targets.items():
            self._cancel_pipeline(nid)
            sess2 = self._nodes.get(nid)
            if sess2 is not None:
                try:
                    await sess2.ws.send(protocol.stop())
                except Exception:
                    pass
            log.info(
                "group '%s': conversation on %s cancelled (%s) — a new wake owns the group",
                group,
                nid,
                why,
            )

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
            sid = msg.get("session_id")
            if session.ignore_audio:
                # Operator input-mute (test/ops): refuse the session exactly
                # like an arbitration loser — nothing opens, frames drop, the
                # node is told to stand down. See set_node_ignore_audio.
                log.info(
                    "[%s/%s] audio_start disregarded (audio ignored by operator)",
                    session.node_id,
                    (str(sid) or "?")[:8],
                )
                try:
                    await session.ws.send(protocol.stop())
                except Exception:
                    pass
                return
            if self._arb_is_loser(sid):
                # This session lost co-audible arbitration but its audio_start
                # crossed our stop in flight (e.g. the gate confirmed early).
                # Refuse it: no on_session_start, streaming stays False so the
                # frames are dropped, and the stop is re-sent.
                log.info(
                    "[%s/%s] audio_start from arbitration loser — stopped",
                    session.node_id,
                    (str(sid) or "?")[:8],
                )
                try:
                    await session.ws.send(protocol.stop())
                except Exception:
                    pass
                return
            session.session_id = sid
            session.streaming = True
            # Node-measured wake evidence (absent on triggered/legacy sessions).
            # Kept on the session for the co-audible arbitration work; logged so
            # paired wakes can be compared straight from the journal.
            session.wake_db = _as_float(msg.get("wake_db"))
            session.wake_margin_db = _as_float(msg.get("wake_margin_db"))
            session.wake_score = _as_float(msg.get("wake_score"))
            if session.wake_db is None and session.wake_score is None:
                # An evidence-less session from a GROUPED node while its group
                # is mid-arbitration is the mixed-fleet signature (field
                # report 2026-08-17): the node never announced, so it cannot
                # be stood down and will answer alongside the winner. Name it
                # loudly with the likely causes — this is the line that turns
                # a "3-way collision, one node skipped arbitration" mystery
                # into a one-look diagnosis.
                arb_group = self._node_audio_group(session.node_id)
                if arb_group is not None:
                    task = self._arb_tasks.get(arb_group)
                    dz = self._arb_deadzone.get(arb_group)
                    mono = time.monotonic()
                    arbitrating = (task is not None and not task.done()) or (
                        dz is not None and dz[0] > mono
                    )
                    if arbitrating:
                        log.warning(
                            "[%s/%s] UNANNOUNCED session while group '%s' is "
                            "arbitrating — this node sent no wake_pending, so it "
                            "cannot be stood down and will answer on its own. "
                            "Likely causes: the node runs pre-5.1.1 code, its "
                            "process hasn't restarted since upgrading (version %s "
                            "reported at join), or a non-wake session collided "
                            "with the group's wake.",
                            session.node_id,
                            (str(sid) or "?")[:8],
                            arb_group,
                            session.kenzy_version or "unknown",
                        )
            if session.wake_db is not None or session.wake_score is not None:
                log.info(
                    "[%s/%s] audio_start (wake %s dBFS, +%s dB over floor, score %s)",
                    session.node_id,
                    (session.session_id or "?")[:8],
                    session.wake_db,
                    session.wake_margin_db,
                    session.wake_score,
                )
            else:
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

        elif mtype == protocol.MSG_WAKE_PENDING:
            await self._on_wake_pending(session, msg)

        elif mtype == protocol.MSG_INTERCOM_END:
            # Node-initiated end (e.g. its wake word fired during the call).
            await self.end_intercom(session.node_id, reason=str(msg.get("reason", "peer")))

        elif mtype == protocol.MSG_STATUS:
            session.audio_ok = bool(msg.get("audio_ok", True))
            session.audio_error = msg.get("audio_error") or None
            if msg.get("devices") is not None:
                session.capabilities = {**session.capabilities, "devices": msg["devices"]}
            if msg.get("media_keys") is not None:
                # 5.0.4: the media-keys endpoint status (present/absent/why) —
                # capability data for the node page's status line.
                session.capabilities = {**session.capabilities, "media_keys": msg["media_keys"]}
            if msg.get("mic_volume") is not None:
                # Managed capture gain: applied/why-not, for the same status line.
                session.capabilities = {**session.capabilities, "mic_volume": msg["mic_volume"]}
            if not session.audio_ok:
                log.warning(
                    "[%s] reports audio init FAILED: %s — fix device + restart",
                    session.node_id,
                    session.audio_error,
                )
            self._notify_state()

        elif mtype == protocol.MSG_GOODBYE:
            # The node is leaving on purpose (a restart, an upgrade, a deliberate
            # stop). Without this the server cannot tell that from a power cut, and
            # every `systemctl restart` — including a whole-fleet kenzy-deploy —
            # would raise an offline fault for a node that is on its way back.
            reason = str(msg.get("reason") or "shutdown")
            log.info("[%s] going away on purpose (%s)", session.node_id, reason)
            self._roster.grant_grace(session.node_id, self._restart_grace_s)

        elif mtype == protocol.MSG_VOLUME_DELTA:
            # A physical volume button on the node's speakerphone (5.0.4). The
            # connection is the identity — this can only ever move the sending
            # node's own volume — and it reuses set_node_volume wholesale, so
            # clamping, persistence, the live push, and the dashboard broadcast
            # are the same no matter which surface asked. The delta itself is
            # sanity-clamped: whatever a misconfigured step or a hostile frame
            # says, one press moves at most 20 points.
            try:
                delta = int(msg.get("delta") or 0)
            except (TypeError, ValueError):
                delta = 0
            if delta:
                await self.set_node_volume(session.node_id, delta=max(-20, min(20, delta)))
                self._notify_state()

        elif mtype == protocol.MSG_PLUGIN_EVENT:
            await self._on_plugin_event(session, msg)

        elif mtype == protocol.MSG_FOLLOWUP_TIMEOUT:
            # A held-floor reply window expired silently on the node (which plays
            # its own end cue) — clear the dialog state server-side.
            self._followup_timed_out(session.node_id)

        elif mtype == protocol.MSG_TTS_DONE:
            # The reply finished PLAYING (not just arriving). If the exchange
            # was already over and the engagement was only being held open for
            # the delivery, it ends now; a held floor (reply-window) is NOT
            # cleared — the conversation is still live. The sid guard keeps a
            # CUE's completion from ending the engagement while the reply is
            # still queued/playing: replies ride the capture's session id, cues
            # get fresh ones.
            group = self._node_audio_group(session.node_id)
            if group is not None:
                eng = self._engagements.get(group)
                done_sid = str(msg.get("session_id") or "")
                if (
                    eng is not None
                    and eng.owner == session.node_id
                    and eng.phase == "speaking"
                    and (not eng.session_id or not done_sid or done_sid == eng.session_id)
                ):
                    self._engagement_clear(session.node_id, "playback complete")

        elif mtype == protocol.MSG_METRICS:
            session.metrics = {
                k: msg.get(k) for k in ("cpu", "ram", "disk", "temp") if msg.get(k) is not None
            }
            self._notify_metrics()

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

    def restore_from_archive(self, data: bytes) -> list[str]:
        """Restore a dashboard-uploaded backup tarball into this server's config
        home (force-overwrite — a dashboard restore is a deliberate replace), then
        regenerate the TLS cert if the restored config expects a now-absent one.
        The caller restarts the server afterward so services re-pull and
        self-populate (stage c) — a full fleet restore from one upload."""
        import tempfile

        from kenzy import backup

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=True) as tmp:
            tmp.write(data)
            tmp.flush()
            restored = backup.restore_backup(Path(tmp.name), self._data_root, force=True)
        msg = backup.regenerate_missing_certs(self._data_root)
        if msg:
            log.info("Restore: %s", msg)
        log.warning("Restored %d file(s) from a dashboard upload", len(restored))
        return restored

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
            # We asked for this absence, so don't raise a fault over it.
            self._roster.grant_grace(node_id, self._restart_grace_s)
            return True
        except Exception as exc:
            log.warning("restart_node: %s send failed: %s", node_id, exc)
            return False

    async def disable_node(self, node_id: str) -> bool:
        """Tell a connected node to self-disable its systemd unit (stop and
        stay stopped). Re-enable is the operator's one-liner on the node host —
        a disabled node has no connection to command."""
        async with self._lock:
            session = self._nodes.get(node_id)
        if session is None:
            log.warning("disable_node: %s is not connected", node_id)
            return False
        try:
            await session.ws.send(protocol.disable())
            # Deliberately out of service: drop it from the roster rather than
            # alerting forever about an absence the operator asked for. It re-adds
            # itself if it is ever re-enabled and reconnects.
            self._roster.forget(node_id)
            log.warning("[%s] disable sent — node will stop until re-enabled on its host", node_id)
            return True
        except websockets.exceptions.ConnectionClosed as exc:
            log.warning("disable_node: %s send failed: %s", node_id, exc)
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
            # A pip install on a Pi can take minutes; expected absence, not a fault.
            self._roster.grant_grace(node_id, self._restart_grace_s)
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

    async def force_wake_node(self, node_id: str) -> bool:
        """Test/ops: make a node run its REAL wake path right now — pre-roll
        evidence, arbitration announcement, one-breath gate — as if the wake
        word had fired there. With `set_node_ignore_audio` this gives scripted
        live tests full who-woke-where control (force-hear + force-deaf)
        without staging acoustics. The node ignores it unless idle."""
        async with self._lock:
            session = self._nodes.get(node_id)
        if session is None:
            log.warning("force_wake_node: %s is not connected", node_id)
            return False
        try:
            await session.ws.send(protocol.force_wake())
        except websockets.exceptions.ConnectionClosed:
            return False
        log.info("[%s] force-wake sent (test)", node_id)
        return True

    def set_node_ignore_audio(self, node_id: str, ignore: bool) -> bool:
        """Server-side input mute (test/ops): while set, this node's wake
        announcements and sessions are disregarded — as if the room were
        silent — so live tests can force who-hears-what without touching
        config or restarting anything. Runtime-only by design: any reconnect
        or server restart clears it (fail open, never quietly forgotten), and
        the state is badged on the Fleet card while active. The node itself is
        untouched — it still wakes and streams; the server declines to act."""
        session = self._nodes.get(node_id)
        if session is None:
            return False
        session.ignore_audio = bool(ignore)
        log.info(
            "[%s] audio %s by operator (server-side, runtime-only)",
            node_id,
            "DISREGARDED" if ignore else "heeded again",
        )
        self._notify_state()
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

    async def play_chime(
        self,
        sound: str | None = None,
        seconds: float = 0.0,
        rooms: list[str] | None = None,
        repeats: int = 0,
    ) -> int:
        """Play a named sound on target rooms. Base server stub (no pipeline);
        ``TranscribingServer`` provides the real implementation."""
        return 0

    def _chime_spec(self, name: str) -> str | None:
        """Sound-name resolution — base stub (see TranscribingServer)."""
        return None

    async def announce(self, text: str, rooms: list[str] | None = None) -> int:
        """Speak ``text`` aloud on target rooms (all connected if None).

        Returns the number of nodes addressed. The base server has no TTS
        pipeline; ``TranscribingServer`` provides the real implementation.
        """
        return 0

    def cue_texts_state(self) -> dict[str, Any]:
        """Spoken-cue phrases + sound-key values (dashboard Settings card). The
        base server has no TTS pipeline; ``TranscribingServer`` has the real one."""
        return {"texts": {}, "keys": {}, "tts": False}

    async def regenerate_cues(self) -> dict[str, Any]:
        """Re-render the spoken cues in the configured voice. Base server: no TTS."""
        raise RuntimeError("TTS service not configured")

    def list_schedules(self) -> list[dict[str, Any]]:
        """Active timers/alarms/reminders (dashboard surface). The base server
        has no scheduler; ``TranscribingServer`` provides the real one."""
        return []

    def proactive_log(self) -> list[dict[str, Any]]:
        """Newest-first record of every proactive decision, refusals included.

        Lives here rather than in the dashboard's Activity ring because
        Activity is gated on ``dashboard.logs`` — it carries household
        transcripts — while this is Kenzy's own conduct. An audit trail that
        disappears when a privacy flag flips is not an audit trail.
        """
        return list(reversed(self._proactive_log))

    async def test_proactive_alert(self) -> dict[str, Any]:
        """Fire a synthetic hazard for verification. Base server: nothing to fire."""
        return {"ok": False, "reason": "proactive speech is not available"}

    def set_proactive_enabled(self, enabled: bool) -> bool:
        """The off-switch. Base server has no gate, so nothing to switch."""
        return False

    def proactive_state(self) -> dict[str, Any]:
        """Current posture: on/off, which categories, what's silenced.

        Reports ``enabled: false`` on purpose. The voice off-switch persists,
        so the failure mode worth guarding against is the feature sitting off
        for months with nobody aware — off-and-visible is fine, off-and-silent
        is not.
        """
        gate = self._proactive
        if gate is None:
            return {"available": False}
        return {
            "available": True,
            "enabled": bool(gate.enabled),
            "categories": sorted(gate.categories_enabled),
            "silenced": gate.silenced(),
            "watching": self._safety.known() if self._safety is not None else 0,
        }

    def cancel_schedule_ids(self, ids: list[str]) -> int:
        """Cancel schedule entries by id; returns how many were removed."""
        return 0

    def _followup_timed_out(self, node_id: str) -> None:
        """A node's follow-up window expired. Base server holds no dialog state."""

    def add_schedule_listener(self, cb: Callable[[], None]) -> None:
        """Observe schedule-set changes (add/cancel/fire). Base server: no-op."""

    def add_memory_listener(self, cb: Callable[[], None]) -> None:
        """Observe memory-change pokes from kenzy-llm (the People page's live
        refresh). Empty list ⇒ zero overhead."""
        self._memory_listeners.append(cb)

    def _notify_memory(self) -> None:
        for cb in self._memory_listeners:
            try:
                cb()
            except Exception as exc:
                log.warning("memory listener failed: %s", exc)

    async def create_backup_archive(
        self,
        *,
        include_secrets: bool = False,
        include_models: bool = False,
        include_lockbox_key: bool = True,
    ) -> bytes:
        """Config-home backup archive (the dashboard download). The base server
        packs the local tree; ``TranscribingServer`` also merges the backend
        services' state slices so a multi-host deployment's archive is complete."""
        from kenzy.backup import create_backup
        from kenzy.config import kenzy_data_root

        return create_backup(
            kenzy_data_root(),
            include_secrets=include_secrets,
            include_models=include_models,
            include_lockbox_key=include_lockbox_key,
        )

    def set_env_secret(self, name: str, value: str) -> None:
        """Write-only secret entry (dashboard → Settings → API keys).

        Upserts ``NAME="value"`` in the config home's ``.env`` and this process's
        environment. Deliberately write-only: values are never read back, served,
        or logged. Co-located services share this ``.env`` (restart them to
        apply); remote service hosts keep their own (``kenzy-deploy`` syncs it).
        """
        from kenzy.config import kenzy_data_root

        name = name.strip()
        if not _ENV_NAME_RE.match(name):
            raise ValueError("name must be UPPER_SNAKE_CASE (A–Z, digits, underscores)")
        value = value.strip()
        if not value or len(value) > 4096 or any(c in value for c in '\r\n"'):
            raise ValueError("value must be a non-empty single line without quotes")
        env_path = kenzy_data_root() / ".env"
        lines = env_path.read_text().splitlines() if env_path.is_file() else []
        prefix_plain, prefix_export = f"{name}=", f"export {name}="
        replaced = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(prefix_plain) or stripped.startswith(prefix_export):
                lines[i] = f'{name}="{value}"'
                replaced = True
        if not replaced:
            lines.append(f'{name}="{value}"')
        env_path.write_text("\n".join(lines) + "\n")
        os.environ[name] = value
        log.info("Secret %s %s in %s", name, "updated" if replaced else "added", env_path)

    def connected_nodes(self) -> list[str]:
        return list(self._nodes.keys())

    # ------------------------------------------------------------------
    # Serve forever
    # ------------------------------------------------------------------

    async def serve(self) -> None:
        log.info("Kenzy server listening on %s:%d", self._host, self._port)
        # 5.1: server-half plugin tasks start here — after main() wired
        # occupancy/integrations, inside the running loop.
        self._start_plugins()
        async with websockets.serve(
            self._handle,
            self._host,
            self._port,
            process_request=self._process_config_request,
            max_size=_MAX_WS_FRAME,
            ssl=self._ssl,
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

# Whole-utterance phrases that end the session SILENTLY, before the LLM and
# before any skill — someone demanding quiet doesn't want a spoken "Okay!" back.
# (Conversational bail-outs like "never mind" live in the social fast intent
# instead, where a brief spoken ack is the polite response.)
_STOP_PHRASES: frozenset[str] = frozenset(
    {
        "stop",
        "stop it",
        "stop that",
        "stop talking",
        "be quiet",
        "quiet",
        "hush",
        "shush",
        "shut up",
        "silence",
        "enough",
        "that's enough",
        "thats enough",
        "please stop",
        "please be quiet",
        "please shut up",
        "shut the heck up",
    }
)

# Safety cap on consecutive assistant-held follow-up turns (multi-turn dialog), so a
# model that keeps asking for a response can't hold the mic open indefinitely.
_MAX_FOLLOWUP_TURNS = 6
# ask() chains (a parked skill re-asking) get a higher ceiling than plain
# dialog holds: enrollment alone is 5 prompts + a 4-retry budget, and the
# skill's own conversation shape is the real bound — this cap only stops a
# runaway loop. Plain LLM expect_response holds stay at dialog.max_turns.
_MAX_ASK_TURNS = 16

# Alarm ring loop: re-announce every interval until acknowledged (wake word),
# capped so an empty house doesn't get lectured all morning (~10 × 25 s ≈ 4 min).
_ALARM_RING_REPEATS = 10
_ALARM_RING_INTERVAL_S = 25.0
# Lead-in tone per schedule kind: (node config key, bundled default). The key is a
# per-node setting (dashboard grid / node_defaults) read live at fire time; empty
# disables the tone. Reminders are deliberately voice-only.
_SCHEDULE_TONE_KEYS = {"timer": ("sound_timer", "timer.wav"), "alarm": ("sound_alarm", "alarm.wav")}


class _CueLadder:
    """Escalating processing acknowledgements while a reply is pending.

    Walks ``_CUE_LADDER``, playing each rung's cue (pool-picked, cue-flagged
    tts session) only if the reply hasn't landed yet. The first rung's delay is
    measured from ``started_at`` — the wait-session start (≈ when the node's
    waiting bed begins), passed in so the cue lands N seconds into the WAIT as
    the user hears it, NOT N seconds after the LLM dispatch: STT+speaker run
    first (dev CPU whisper ~3s), and counting from creation would stack that
    latency on top of the delay (an ~8s cue for a configured 5s). Each later
    rung's delay is a GAP measured from when the previous cue finished PLAYING
    — ``_play_cue`` returns the streamed audio's duration and the walker sleeps
    it out (frames arrive near-instantly over the LAN, then the node plays for
    ~that long), so the felt silence between cues is a constant, not whatever an
    absolute deadline left over. ``finish()`` — called the moment reply audio is
    ready — cancels the timer outright, but lets a cue already MID-PLAY
    complete: its tts session must close before the reply's opens (the
    back-to-back-session ordering rule). The in-flight play is awaited through
    ``asyncio.shield`` — cancelling the walker otherwise propagates into
    whatever it awaits, which would cut the cue's tts session mid-frame."""

    def __init__(
        self, server: TranscribingServer, node_id: str, started_at: float | None = None
    ) -> None:
        self._server = server
        self._node_id = node_id
        # Wait-session start (time.monotonic); the first rung subtracts elapsed
        # STT time from its delay. None ⇒ measure from creation (legacy).
        self._started_at = started_at
        self._play: asyncio.Task[float] | None = None
        #: Rungs actually started. The streaming path hands this to the buffered
        #: fallback so a stream that hung past a rung and then failed doesn't get
        #: the whole ladder spoken a second time ("Working on it." twice).
        self.played: int = 0
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        for i, (delay_ms, key, default) in enumerate(_CUE_LADDER):
            delay = delay_ms / 1000.0
            if i == 0 and self._started_at is not None:
                # Anchor the first rung to the wait start, not to creation — so
                # STT latency already burned doesn't push the cue later. If STT
                # outran the delay, fire as soon as we get here (max(0, …)).
                delay = max(0.0, delay - (time.monotonic() - self._started_at))
            await asyncio.sleep(delay)
            self._play = asyncio.create_task(
                self._server._play_cue(self._node_id, key, default)
            )
            self.played += 1  # a streaming fallback must not replay this rung
            try:
                duration = await asyncio.shield(self._play)
            except Exception:  # noqa: BLE001 — cues are best-effort
                duration = 0.0
            if duration:
                # Let the clip finish sounding before the next rung's gap starts.
                await asyncio.sleep(duration)

    async def finish(self) -> None:
        """Reply audio is ready: stop future rungs, let a mid-play cue close."""
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        play = self._play
        if play is not None and not play.done():
            try:
                await play
            except Exception:  # noqa: BLE001
                pass


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
        # Count of consecutive assistant-held follow-up turns per node (multi-turn
        # dialog). Bumped each time we re-arm the mic without a wake word; cleared when
        # the exchange ends (no re-arm, silence, stop phrase, disconnect).
        self._followup_turns: dict[str, int] = {}
        # ask() (4.2): asked-node_id → {"id", "capture", "origin_node",
        # "origin_room", "busy_cues"} of the parked continuation awaiting the
        # answer. "audio" capture routes the raw PCM (no STT); origin != node is
        # a CROSS-ROOM ask (intercom consent) — the final reply speaks at the
        # origin, and the asked room's wake/timeout resolves as an empty
        # answer instead of a discard (the asker deserves the outcome).
        # busy_cues (bool) gates the processing-cue ladder over the answer turn.
        self._pending_ask: dict[str, dict[str, Any]] = {}

        # Timers / alarms / reminders: persisted store + firing loop (started in
        # serve(), which needs the running event loop). Ring tasks per node for
        # alarms, which repeat until acknowledged (wake word) or the repeat cap.
        from kenzy.config import kenzy_data_root

        from .scheduler import Scheduler

        self._scheduler = Scheduler(
            kenzy_data_root() / "data" / "schedules.json", self._fire_schedule
        )
        self._ring_tasks: dict[str, asyncio.Task[None]] = {}

        scfg: dict[str, Any] = cfg.get("stt", {})
        self._stt_url: str | None = _mesh_url(cfg, str(scfg["url"])) if scfg.get("url") else None
        self._stt_timeout: float = float(scfg.get("timeout", 60.0))
        if self._stt_url:
            log.info("STT service: %s (timeout=%.0fs)", self._stt_url, self._stt_timeout)
        else:
            log.warning("STT service not configured — audio will not be transcribed.")

        tcfg: dict[str, Any] = cfg.get("tts", {})
        self._tts_url: str | None = _mesh_url(cfg, str(tcfg["url"])) if tcfg.get("url") else None
        self._tts_timeout: float = float(tcfg.get("timeout", 60.0))
        self._tts_chunk_size: int = int(tcfg.get("chunk_size", 4096))
        if self._tts_url:
            log.info("TTS service: %s (timeout=%.0fs)", self._tts_url, self._tts_timeout)
        else:
            log.info("TTS service not configured — responses will not be spoken.")

        lcfg: dict[str, Any] = cfg.get("llm", {})
        self._llm_url: str | None = _mesh_url(cfg, str(lcfg["url"])) if lcfg.get("url") else None
        self._llm_timeout: float = float(lcfg.get("timeout", 30.0))
        if self._llm_url:
            log.info("LLM service: %s (timeout=%.0fs)", self._llm_url, self._llm_timeout)
        else:
            log.info("LLM service not configured — STT results will be logged only.")

        spcfg: dict[str, Any] = cfg.get("speaker", {})
        self._speaker_url: str | None = (
            _mesh_url(cfg, str(spcfg["url"])) if spcfg.get("url") else None
        )
        self._speaker_timeout: float = float(spcfg.get("timeout", 10.0))
        # The unidentified-speaker name is owned by the speaker SERVICE (it's what
        # /identify returns). Read it from that service's effective config — the
        # same value the service pulls — so the server's fallback AND its
        # "is this unknown?" comparison use one source of truth.
        self._unknown_speaker: str = str(
            self._effective_service_config("speaker").get("unknown_speaker", "unknown")
        )

        # Dialog depth + alarm-ring behavior (operator-tunable; defaults are the
        # module constants). Restart to apply (edited via the Settings tab).
        dcfg: dict[str, Any] = cfg.get("dialog", {}) or {}
        self._max_followup_turns: int = int(dcfg.get("max_turns", _MAX_FOLLOWUP_TURNS))
        acfg: dict[str, Any] = cfg.get("alarm", {}) or {}
        self._alarm_ring_repeats: int = int(acfg.get("ring_repeats", _ALARM_RING_REPEATS))
        self._alarm_ring_interval: float = float(acfg.get("ring_interval", _ALARM_RING_INTERVAL_S))
        # 4.4 streaming pipeline (sentence-overlapped replies). Default ON since
        # 4.4.2 — shipped off in 4.4 pending rig soak, which it passed. The
        # buffered path stays the fallback forever (lockbox replies,
        # non-streaming providers, and this flag set false).
        scfg_stream: dict[str, Any] = cfg.get("streaming", {}) or {}
        self._streaming_enabled: bool = bool(scfg_stream.get("enabled", True))

        # v6 follow-up mode (s2s): captures on hardware_aec-capable nodes route
        # through the conversation engine; everything else — and every failure —
        # stays on the classic pipeline (a complete mode, and the fallback).
        # OFF by default; dashboard-editable, restart to apply (like its
        # sibling keys here).
        s2s_cfg: dict[str, Any] = cfg.get("s2s", {}) or {}
        self._s2s_enabled: bool = bool(s2s_cfg.get("enabled", False))
        self._s2s_url: str = str(s2s_cfg.get("url", "") or "")
        self._s2s_hard_cap_s: float = float(s2s_cfg.get("hard_cap_s", 900.0))
        self._s2s_bridge: Any | None = None  # built lazily on first eligible capture
        self._s2s_sid: dict[str, str] = {}  # node -> current capture session id
        self._s2s_person: dict[str, str] = {}  # display name -> person id (tool context)
        if self._s2s_enabled:
            log.info(
                "Follow-up mode ENABLED (s2s.enabled) — engine: %s",
                self._s2s_url or "auto (service registry)",
            )

        # Remember which service URLs came from static config; auto-registration
        # (GET /register) fills the rest and must never overwrite a configured one.
        self._static_services = {
            svc
            for svc, url in (
                ("stt", self._stt_url),
                ("tts", self._tts_url),
                ("llm", self._llm_url),
                ("speaker", self._speaker_url),
            )
            if url
        }
        # Voice enrollment ("enroll me as Alice") is gated by `allow_voice_enroll` in the
        # speaker *service* config (the enrollment SKILL reads it via /enroll/info, editable
        # from the dashboard's Fleet tab). Active sessions keyed by node_id
        # (prompt → capture → POST /enroll loop).
        # Voice-guided calibration ("Hey Kenzy, calibrate") — active sessions keyed
        # by node_id; tune samples are routed to them via the always-registered
        # listener below (a dict miss when idle — negligible overhead).
        self._calib_sessions: dict[str, dict[str, Any]] = {}
        self.add_tune_listener(self._on_calib_sample)
        # Named chimes an MQTT automation may play (kenzy/chime): bundled sound
        # names work out of the box; integrations.mqtt.chimes maps extra names to
        # server-host WAV paths. Only these names — never caller-supplied paths.
        mqtt_cfg = (cfg.get("integrations", {}) or {}).get("mqtt", {}) or {}
        chimes = mqtt_cfg.get("chimes") or {}
        self._chimes: dict[str, str] = (
            {str(k): str(v) for k, v in chimes.items()} if isinstance(chimes, dict) else {}
        )
        # Sound library roots (4.2): payloads name files; names resolve ONLY
        # within these roots (kenzy/tones resolve_sound — traversal and
        # absolute paths rejected). data/sounds rides backups; extra roots are
        # deliberately file-managed (server.yaml, not dashboard-editable) —
        # the roots list IS the security boundary.
        from kenzy.config import kenzy_data_root

        sounds_cfg = cfg.get("sounds") if isinstance(cfg.get("sounds"), dict) else {}
        raw_dirs = sounds_cfg.get("dirs") if isinstance(sounds_cfg, dict) else None
        data_root = kenzy_data_root()
        self._sound_roots: list[Path] = [data_root / "data" / "sounds"]
        for d in raw_dirs if isinstance(raw_dirs, list) else []:
            p = Path(str(d)).expanduser()
            self._sound_roots.append(p if p.is_absolute() else data_root / p)
        # Processing-cue pools: last pick per (node_id, key) for the
        # no-immediate-repeat guard (in-memory; variety, not state).
        self._last_cue: dict[tuple[str, str], str] = {}
        # Cue PHRASES (source of truth for "Regenerate spoken cues"): the
        # ``cues:`` block in server.yaml overrides the shipped defaults.
        cues_cfg = cfg.get("cues") if isinstance(cfg.get("cues"), dict) else {}
        self._cue_texts: dict[str, list[str]] = {}
        for kind, _skey in _CUE_KINDS:
            raw = (cues_cfg or {}).get(kind, _DEFAULT_CUE_TEXTS[kind])
            if isinstance(raw, str):
                raw = [raw]
            texts = [str(s).strip() for s in raw if str(s).strip()] if isinstance(raw, list) else []
            self._cue_texts[kind] = texts or list(_DEFAULT_CUE_TEXTS[kind])
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
        # v6 follow-up: a new capture during a conversation is a barge-in — the
        # bridge cancels the in-flight engine response (~2 ms) and the new turn
        # queues behind it; the engine session (and its history) survives.
        if self._s2s_bridge is not None:
            await self._s2s_bridge.on_capture_start(session.node_id)
        self._tts_active.discard(session.node_id)
        self._buffers[session.node_id] = bytearray()
        # Layer 1: a new capture claims the group's engagement for this node —
        # and cancels the previous owner's conversation if one is live (the
        # claim can arrive before the arbitration window closes; see
        # _engagement_claim).
        await self._engagement_claim(session.node_id, session.session_id)
        self._calib_saw_wake(session.node_id)  # an idle wake opens a session
        # Live safety alerts count as heard too — same reasoning, same hook.
        self._acknowledge_proactive("a new session")
        # A ringing alarm is acknowledged HERE, not only in on_wakeword. A wake
        # word spoken while audio is playing never sends a `wakeword` frame at
        # all: the node stops its own playback and opens a fresh session (see
        # _audio_loop's TTS branch), so `audio_start` is the only thing that
        # reaches us. Same for a wake between ring repeats, when the node is
        # idle. on_wakeword only fires for a wake DURING capture — which is the
        # one moment an alarm can't be ringing — so acknowledging there alone
        # meant the ring loop outlived every real acknowledgment.
        self._stop_ringing(session.node_id)

    async def on_audio_frame(self, session: NodeSession, data: bytes) -> None:
        buf = self._buffers.get(session.node_id)
        if buf is None:
            return
        # Backstop cap (F-10): the node's hard_cap_ms normally bounds a session, but a
        # buggy/hostile node could stream without VAD — don't grow a buffer unbounded.
        if len(buf) >= _MAX_SESSION_PCM_BYTES:
            return
        crossed_onset = len(buf) < _ONSET_LONG_BYTES <= len(buf) + len(data)
        buf += data
        if crossed_onset:
            log.info(
                "[%s] capture onset rms: 320ms=%d 960ms=%d (room '%s')",
                session.node_id,
                round(_pcm_rms(buf[:_ONSET_SHORT_BYTES])),
                round(_pcm_rms(buf[:_ONSET_LONG_BYTES])),
                session.room_id,
            )

    async def on_session_end(self, session: NodeSession, reason: str) -> None:
        pcm = bytes(self._buffers.pop(session.node_id, b""))
        if not pcm:
            self._engagement_clear(session.node_id, "empty capture")
            return
        if self._arb_is_loser(session.session_id):
            # An arbitration loser whose capture had already opened before the
            # stop landed (one-breath confirm beats the window sometimes). The
            # winner's pipeline is answering this utterance — running a second
            # one is the duplicate-answer bug with extra steps.
            log.info(
                "[%s/%s] dropping capture from arbitration loser (%d bytes)",
                session.node_id,
                (session.session_id or "?")[:8],
                len(pcm),
            )
            # No-op when the winner already re-claimed the engagement (the
            # usual order); real when this loser's early capture had claimed it.
            self._engagement_clear(session.node_id, "stood down")
            return
        # Reply-level instrument (parallel-hearing research): the phrase level
        # of the WHOLE capture, not just its opening — the opening of a gated
        # session can be pre-roll silence (measured 2026-08-18: a forced wake's
        # onset window read 0-4 RMS while STT heard the sentence perfectly).
        # p90 of per-frame RMS = the speech; p25 = the floor; both in dB so
        # co-audible nodes' takes on the same utterance compare directly.
        frame_levels = sorted(
            _pcm_rms(pcm[i : i + 2560]) for i in range(0, len(pcm) - 2560, 2560)
        )
        if len(frame_levels) >= 4:
            p90 = max(frame_levels[int(0.9 * (len(frame_levels) - 1))], 1.0)
            p25 = max(frame_levels[int(0.25 * (len(frame_levels) - 1))], 1.0)
            log.info(
                "[%s/%s] capture level: p90=%.1f dBFS margin=%.1f dB (%d frames)",
                session.node_id,
                (session.session_id or "?")[:8],
                20.0 * math.log10(p90 / 32768.0),
                20.0 * math.log10(p90 / p25),
                len(frame_levels),
            )
        self._engagement_update(session.node_id, session.session_id, "thinking")
        if self._s2s_takes(session.node_id):
            # v6 follow-up: this capture is a conversation turn. Deliberately
            # NOT registered in _stt_tasks — a barge-in must never hard-kill
            # the turn task (the engine session holds the conversation
            # history); the bridge cancels in-engine instead.
            self._s2s_sid[session.node_id] = session.session_id or ""
            asyncio.create_task(
                self._s2s().take_turn(
                    session.node_id, session.room_id, session.session_id, pcm
                ),
                name=f"s2s-{session.node_id}",
            )
            return
        task = asyncio.create_task(
            self._transcribe(session.node_id, session.room_id, session.session_id, pcm),
            name=f"stt-{session.node_id}",
        )
        self._stt_tasks[session.node_id] = task

    def _on_safety_state(self, entity_id: str, state: str) -> None:
        """Raw HA state change → maybe a Tier A announcement.

        Sync (the socket's consume loop calls it) so the decision is immediate
        and delivery is a task: a slow TTS render must never stall the event
        stream that occupancy is also reading from.
        """
        watcher = self._safety
        if watcher is None:
            return
        announcement = watcher.consider(entity_id, state)
        if announcement is None:
            return
        asyncio.create_task(self._speak_safety(announcement), name=f"safety-{entity_id}")

    async def _speak_safety(self, announcement: Any) -> bool:
        """Say it in every room, at the alert floor, then start the repeat window.

        Synthesized ONCE and streamed to each node — a hazard in ten rooms is
        one TTS render, not ten, and every room says the identical sentence.

        A lead-in tone is prepended per node, the same as an alarm delivery and
        for the same reason: **the tone still plays when TTS is down.** A safety
        announcement must not depend on a healthy speech service to make noise,
        which matters more here than it does for a kitchen timer.
        """
        rooms = set(announcement.rooms)
        targets = [nid for nid, s in list(self._nodes.items()) if not rooms or s.room_id in rooms]
        if not targets:
            log.warning("Safety: %r but no node is connected to say it", announcement.text)
            return False

        pcm = await self._synthesize(announcement.text, _INTERCOM_VOICE_PROMPT)
        if pcm is None:
            log.warning("Safety: speech synthesis unavailable — tone only")

        delivered = False
        for node_id in targets:
            try:
                tone = self._schedule_tone(node_id, "alarm")
                audio = (tone or b"") + (pcm or b"")
                if not audio:
                    continue
                await self._stream_pcm(node_id, audio, alert=announcement.alert)
                delivered = True
            except Exception as exc:  # one dead room must not silence the others
                log.warning("Safety: delivery to %s failed: %s", node_id, exc)

        if delivered:
            # Only a delivered announcement starts the repeat window — the same
            # split as the gate's evaluate/commit, so a house that heard nothing
            # tries again instead of going quiet.
            self._safety.spoken(announcement.key)
            log.info("Safety announced: %s", announcement.text)
        return delivered

    def _record_proactive(self, key: str, decision: Any, text: str) -> None:
        """Append one decision to the audit trail (allowed or refused)."""
        record = {"ts": time.time(), "category": "safety", "key": key, "text": text}
        record.update(decision.as_record())
        self._proactive_log.append(record)

    def set_proactive_enabled(self, enabled: bool) -> bool:
        """The spoken off-switch. Applies live AND persists.

        Persisting matters: something switched off because it was misbehaving
        must not come back on after the next upgrade or power cut. The cost of
        persisting is that it can be off forever without anyone noticing, which
        is why the dashboard shows it — off-and-visible is fine, off-and-silent
        is not.

        Returns False when it could not be persisted, so the caller can say so
        rather than implying a durable change that isn't.
        """
        gate = self._proactive
        if gate is None:
            return False
        gate.enabled = bool(enabled)
        log.info("Proactive speech %s by voice", "enabled" if enabled else "DISABLED")
        if self._config_path is None:
            log.warning("Proactive change not persisted — server config path unknown")
            return False
        try:
            import yaml

            path = _server_override_path(self._config_path)
            data: dict[str, Any] = {}
            if path.is_file():
                loaded = yaml.safe_load(path.read_text()) or {}
                if isinstance(loaded, dict):
                    data = loaded
            block = data.get("proactive")
            if not isinstance(block, dict):
                block = {}
            block["enabled"] = bool(enabled)
            data["proactive"] = block
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(data, sort_keys=False))
            return True
        except Exception as exc:
            log.error("Could not persist proactive setting: %s", exc)
            return False

    async def test_proactive_alert(self) -> dict[str, Any]:
        """Fire a synthetic hazard through the REAL path, for verification.

        Deliberately goes through the gate rather than round it. Testing a
        safety announcement by bypassing the thing that decides whether to
        announce would verify the half that was never in doubt — and the common
        setup mistakes (category switched off, feature disabled by voice
        months ago) live in the gate, so a test that skips it reports success
        on a house that would stay silent in a fire.

        The alternative was setting off a real smoke detector to find out the
        phrasing is wrong.
        """
        gate = self._proactive
        if gate is None:
            return {"ok": False, "reason": "proactive speech is not available"}

        from kenzy.server.proactive import SAFETY

        key = "kenzy.test_alert"
        text = "This is a test of Kenzy's safety announcements."
        decision = gate.evaluate(SAFETY, key, ())
        self._record_proactive(key, decision, text)
        if not decision.allowed:
            return {"ok": False, "reason": decision.reason, "text": text}

        from kenzy.server.safety import Announcement

        spoke = await self._speak_safety(
            Announcement(key=key, text=text, rooms=decision.rooms, alert=decision.alert)
        )
        # Released immediately: a test is a one-off, and leaving it "asserted"
        # would mean the next test is refused as a repeat of this one.
        gate.clear(key)
        if not spoke:
            # The gate said yes and nothing came out. Reporting success here
            # would be the worst possible answer from a test whose entire job
            # is telling you whether the house can actually shout.
            return {"ok": False, "reason": "no room could play it", "text": text}
        return {"ok": True, "text": text}

    def _acknowledge_proactive(self, why: str) -> None:
        """Someone interacted with Kenzy — treat live alerts as heard.

        **Called from on_session_start, not on_wakeword.** A wake word spoken
        over playing audio never sends a ``wakeword`` frame: the node stops its
        own playback and opens a fresh session, so ``audio_start`` is all the
        server sees. Wiring an acknowledgement to ``on_wakeword`` alone is
        exactly the bug that left ringing alarms unstoppable until 5.0.5, and
        an alert announcement is playing audio by definition.
        """
        gate = self._proactive
        if gate is None:
            return
        silenced = gate.acknowledge()
        if silenced:
            log.info("Proactive: silenced %d alert(s) after %s", len(silenced), why)

    async def on_wakeword(self, session: NodeSession, model: str, score: float) -> None:
        self._cancel_stt(session.node_id)
        self._calib_saw_wake(session.node_id)  # mid-stream/TTS wake counts too
        # The wake word ALWAYS cancels a parked ask() (locked decision) — the
        # household's universal escape hatch.
        self._abandon_pending_ask(session.node_id, "wakeword")
        # The wake word acknowledges a ringing alarm (mirrors the intercom hang-up).
        self._stop_ringing(session.node_id)
        self._acknowledge_proactive("a wake word")
        # If the node is not currently streaming audio to us it may be playing
        # TTS or waiting idle — send STOP so it can interrupt and re-activate.
        if not session.streaming:
            await self.stop_node(session.node_id)
            self._tts_active.discard(session.node_id)

    # ------------------------------------------------------------------
    # TTS helpers  (called by the LLM/TTS pipeline phase)
    # ------------------------------------------------------------------

    async def send_tts_start(
        self,
        node_id: str,
        session_id: str,
        sample_rate: int = 22050,
        channels: int = 1,
        alert: bool = False,
        stream: bool = False,
        cue: bool = False,
    ) -> bool:
        """Tell a node to enter TTS mode and begin accepting audio frames.
        ``alert`` audio (doorbell chimes) plays at the muted floor on muted nodes;
        ``stream`` marks a sentence-streamed reply (play frames as they arrive);
        ``cue`` marks a processing acknowledgement (duck-mixed over the bed)."""
        async with self._lock:
            session = self._nodes.get(node_id)
        if session is None:
            return False
        try:
            await session.ws.send(
                protocol.tts_start(session_id, sample_rate, channels, alert, stream, cue)
            )
            self._tts_active.add(node_id)
            if not alert and not cue:
                # The engagement enters `speaking` only for conversational
                # audio to its OWNER (alerts and cue acknowledgements aren't
                # the conversation; _engagement_update ignores non-owners).
                self._engagement_update(node_id, session_id, "speaking")
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

    # Layer 1 stateful-group hooks (see AudioServer._group_cancel): the base
    # class arbitrates; only this subclass knows what a pipeline is.
    def _has_pipeline(self, node_id: str) -> bool:
        task = self._stt_tasks.get(node_id)
        return task is not None and not task.done()

    def _cancel_pipeline(self, node_id: str) -> None:
        self._cancel_stt(node_id)

    # ------------------------------------------------------------------
    # v6 follow-up bridge (s2s) — glue only: the bridge owns the flow, the
    # gate owns authority, this block just lends the server's hands.
    # ------------------------------------------------------------------

    def _s2s_takes(self, node_id: str) -> bool:
        """Route this node's capture through the engine? Cheap gate first so a
        disabled install never even constructs the bridge. When the feature is
        ON but a capture is skipped, SAY WHY — a silent classic fallback with
        the toggle enabled is undiagnosable (the 5.0.4 lesson)."""
        if self._s2s_bridge is None and not self._s2s_enabled:
            return False
        takes = bool(self._s2s().should_take(node_id))
        if not takes and self._s2s_enabled:
            if not self._node_aec(node_id):
                why = "node lacks hardware_aec (half-duplex) — classic pipeline"
            elif not self._s2s_engine_url():
                why = "no engine address (kenzy-s2s not registered?) — classic pipeline"
            else:
                why = "bridge declined — classic pipeline"
            log.info("[%s] follow-up skipped: %s", node_id, why)
        return takes

    def _s2s(self) -> Any:
        if self._s2s_bridge is None:
            from kenzy.s2s.conversation import WindowPolicy
            from kenzy.server.s2s_bridge import BridgeDeps, S2SBridge

            self._s2s_bridge = S2SBridge(
                BridgeDeps(
                    enabled=lambda: self._s2s_enabled,
                    engine_url=self._s2s_engine_url,
                    node_capable=self._node_aec,
                    engine_factory=self._s2s_engine_factory,
                    fetch_tools=self._s2s_fetch_tools,
                    identify=self._s2s_identify,
                    execute_tool=self._s2s_execute_tool,
                    deliver_start=self._s2s_deliver_start,
                    deliver_frame=self.send_tts_frame,
                    deliver_end=self._s2s_deliver_end,
                    hold_floor=lambda nid: self._maybe_hold_floor(nid, True, cue=False),
                    listen_now=self._s2s_listen_now,
                    activity=self._s2s_activity,
                    end_floor=self._s2s_end_floor,
                    classic=self._transcribe,
                    instructions=self._s2s_instructions,
                    audit=self._s2s_audit,
                    policy=WindowPolicy(hard_cap_s=self._s2s_hard_cap_s),
                )
            )
        return self._s2s_bridge

    def _s2s_engine_url(self) -> str:
        """The engine's ws endpoint: explicit config, else the registry.

        Auto-registration setattrs an http(s) URL into ``_s2s_url`` (the
        generic ``/register`` fill path) — normalize the scheme either way so
        both a hand-configured ws:// and a registered https:// resolve.
        """
        url = self._s2s_url
        if not url:
            announced = self._announced_services.get("s2s")
            base = str((announced or {}).get("base") or "")
            if not base:
                return ""
            url = base.rstrip("/") + "/v1/realtime"
        return url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)

    async def _s2s_engine_factory(self, url: str) -> Any:
        from kenzy.s2s.engine import EngineClient
        from kenzy.s2s.profiles import KENZY_S2S

        client = EngineClient(KENZY_S2S, url=url)
        await client.connect()
        return client

    def _s2s_llm_base(self) -> str | None:
        return self._llm_url.rsplit("/", 1)[0] if self._llm_url else None

    async def _s2s_fetch_tools(self, tier: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """The llm service's skill-host door: schemas + the tier policy."""
        base = self._s2s_llm_base()
        if not base:
            return [], {}
        import httpx

        url = f"{base}/tools"
        async with httpx.AsyncClient(verify=tlsutil.httpx_verify()) as client:
            resp = await client.get(
                url,
                params={"tier": tier},
                timeout=self._llm_timeout,
                headers=self._service_headers("GET", url),
            )
            resp.raise_for_status()
        data = resp.json()
        return list(data.get("tools") or []), dict(data.get("policy") or {})

    async def _s2s_identify(self, pcm: bytes, room: str) -> tuple[Any, float]:
        """Speaker-ID one capture and feed occupancy — the classic path's
        identity work, lent to the bridge's session."""
        from kenzy.s2s.gate import Speaker as GateSpeaker

        if not self._speaker_url:
            return GateSpeaker("", "unknown"), 0.0
        spk_name, spk_conf = await self._call_speaker(pcm, room)
        identity = resolve_voice_identity(
            self._people, spk_name, spk_conf, unknown_name=self._unknown_speaker
        )
        if self._occupancy is not None:
            self._occupancy.on_voice(
                occupancy_mod.room_slug(room),
                person_id=identity.person_id or "",
                person_name=identity.display,
                recognized=identity.tier != "unknown",
            )
        if identity.tier != "unknown":
            self._s2s_person[identity.display] = identity.person_id or ""
            return GateSpeaker(identity.display, identity.tier), float(identity.confidence)
        return GateSpeaker("", "unknown"), float(identity.confidence)

    async def _s2s_execute_tool(
        self, call: Any, node_id: str, room: str, speaker: Any
    ) -> str:
        """POST one gate-approved call to the llm's /tool door and dispatch any
        server actions it queued (announce, volume, …) — same actuator as the
        classic pipeline, same audit surface."""
        base = self._s2s_llm_base()
        if not base:
            return "error: no llm service configured"
        try:
            arguments = json.loads(call.arguments_json or "{}")
        except ValueError:
            arguments = {}
        body = {
            "name": call.name,
            "arguments": arguments if isinstance(arguments, dict) else {},
            "room_id": room,
            "speaker": speaker.name or None,
            "person_id": self._s2s_person.get(speaker.name) or None,
            "speaker_tier": speaker.tier,
            "channel": "voice",
            "rooms": sorted({s.room_id for s in self._nodes.values()}),
            "schedules": self._schedule_payload(node_id),
            "occupancy": self._occupancy_payload(),
        }
        import httpx

        url = f"{base}/tool"
        async with httpx.AsyncClient(verify=tlsutil.httpx_verify()) as client:
            resp = await client.post(
                url,
                json=body,
                timeout=self._llm_timeout,
                headers=self._service_headers("POST", url),
            )
            resp.raise_for_status()
        data = resp.json()
        actions = list(data.get("actions") or [])
        if actions:
            await self._dispatch_actions(actions, node_id, room, speaker.name or None)
        return str(data.get("result", ""))

    async def _s2s_deliver_start(self, node_id: str) -> None:
        self._tts_active.add(node_id)
        await self.send_tts_start(
            node_id, self._s2s_sid.get(node_id, ""), sample_rate=24000, channels=1, stream=True
        )

    async def _s2s_deliver_end(self, node_id: str) -> None:
        await self.send_tts_end(node_id, self._s2s_sid.get(node_id, ""))
        if node_id in self._tts_active:
            await self.stop_node(node_id)
            self._tts_active.discard(node_id)

    async def _s2s_listen_now(self, node_id: str) -> None:
        """Open the node's capture window during thinking — listening while
        she thinks. Armed after a short SETTLE (the mic's DSP tail and the
        VAD's cold state both live in the first ~500 ms after the user's own
        capture ends — arming into them phantom-barges the turn). Detached so
        the settle never delays the turn itself. Older nodes treat the flag as
        a plain post-TTS arm (graceful)."""

        async def _arm() -> None:
            await asyncio.sleep(0.6)
            node = self._nodes.get(node_id)
            if node is None:
                return
            try:
                await node.ws.send(protocol.expect_utterance(cue=False, immediate=True))
            except Exception as exc:  # noqa: BLE001 — a lost node just misses the window
                log.debug("[%s] could not arm thinking-gap listening: %s", node_id, exc)

        asyncio.get_running_loop().create_task(_arm(), name=f"s2s-listen-{node_id}")

    def _s2s_end_floor(self, node_id: str) -> None:
        """Close the node's dialog surface: clear the server's turn counter AND
        tell the NODE (end_dialog clears its pending window/arm — so the
        farewell's playback cannot re-open a window on a closed conversation —
        and plays the end cue after the final line)."""
        self._end_followup_dialog(node_id)
        node = self._nodes.get(node_id)
        if node is None:
            return

        async def _send() -> None:
            try:
                await node.ws.send(protocol.end_dialog())
            except Exception as exc:  # noqa: BLE001 — a lost node has no floor to close
                log.debug("[%s] could not send end_dialog: %s", node_id, exc)

        asyncio.get_running_loop().create_task(_send(), name=f"s2s-enddlg-{node_id}")

    def _s2s_activity(self, record: dict[str, Any]) -> None:
        """The follow-up turn's Activity record — same dashboard.logs gate as
        the classic pipeline (no listeners = no transcript kept)."""
        if self._session_listeners:
            self._notify_session(record)

    def _s2s_instructions(self, room: str) -> str:
        """v6.0 session instructions — persona, the ROOM anchor, the closure
        contract. Richer context staging (memory, occupancy at wake) is open
        question 3's follow-on."""
        where = room or "the house"
        # The clock, staged at conversation open (llm.py injects the same line
        # per-request on the classic pipeline, whose fast_datetime intent stays
        # first-class there; this path has no fast intents, so the model gets
        # the line here plus the get_datetime tool for a live read).
        now = datetime.datetime.now().astimezone()
        clock = now.strftime("%A, %B %d, %Y, %I:%M %p %Z").replace(" 0", " ").strip()
        return (
            "You are Kenzy, a helpful whole-home voice assistant, speaking with someone "
            f"in the {where}. Keep spoken replies short and natural — one or two "
            "sentences unless asked for more.\n"
            f"- Date and time as this conversation started: {clock}. For a precise "
            "current time, call the get_datetime tool.\n"
            f"- Device requests that don't name a room mean THIS room: always pass "
            f"room='{room}' to home-control tools unless the user names a different "
            "room. Pass the speaker name when you know it.\n"
            "- Use the provided tools for device actions and household questions; "
            "never claim an action you did not perform.\n"
            "- After completing a command or answering, the conversation CONTINUES — "
            "the user may follow up. Never call end_conversation just because a task "
            "is done.\n"
            "- Call end_conversation ONLY when the user clearly signals they are "
            "finished — 'end the conversation', 'that's all', 'never mind', "
            "'goodbye', 'thanks, we're done' — then say one short farewell."
        )

    def _s2s_audit(self, record: Any) -> None:
        """The turn's conduct record — server-side, like 5.0.6's audit trail."""
        log.info(
            "[s2s-audit] %s %s: %s (%s/%s)",
            record.turn_id,
            record.event,
            redact.loggable(record.detail),
            record.speaker or "?",
            record.tier,
        )

    async def _transcribe(
        self, node_id: str, room_name: str, session_id: str | None, pcm: bytes
    ) -> None:
        # node_id addresses the node (state + control); room_name is the semantic
        # label the backends see (STT/speaker/LLM/TTS `room_id`).
        try:
            # ask(capture="audio") (4.2): this capture IS the answer — an
            # enrollment sample, not a command. The raw PCM routes straight to
            # the parked skill; STT and speaker-id never run on it.
            pa = self._pending_ask.get(node_id)
            if pa is not None and pa.get("capture") == "audio":
                self._pending_ask.pop(node_id, None)
                reply = await self._call_llm_continue_audio(pa["id"], pcm)
                await self._deliver_reply(
                    node_id, room_name, session_id, reply, transcript="[voice sample]"
                )
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
                (text, stt_ms), (spk_result, spk_ms) = await asyncio.gather(
                    _timed(self._call_stt(pcm, room_name, session_id)),
                    _timed(self._call_speaker(pcm, room_name)),
                )
                spk_name, spk_conf = spk_result
            else:
                text, stt_ms = await _timed(self._call_stt(pcm, room_name, session_id))
                spk_name, spk_conf, spk_ms = self._unknown_speaker, 0.0, 0.0

            # A leaked wake-phrase tail is transport noise, not command text —
            # strip it before anything downstream (fast-intent, LLM, Activity)
            # sees the transcript.
            text = _strip_wake_prefix(text)

            # Identity core (F1): resolve the voiceprint to a person. Passthrough
            # when there are no records — `speaker` stays the raw name, exactly
            # as before; `identity` carries the tier/person for downstream gates.
            identity = resolve_voice_identity(
                self._people, spk_name, spk_conf, unknown_name=self._unknown_speaker
            )
            speaker = identity.display

            log.info(
                "[%s] STT: %s | speaker: %s (%s, %.2f)",
                node_id,
                redact.loggable(text) if text else "(none)",
                speaker,
                identity.tier,
                identity.confidence,
            )

            # v5 spine: the voice half of the occupancy tracker. Someone spoke
            # here, so the room is occupied whether or not we know who; the
            # identity anchor only fires for a RECOGNIZED voice.
            if self._occupancy is not None:
                self._occupancy.on_voice(
                    occupancy_mod.room_slug(room_name),
                    person_id=identity.person_id or "",
                    person_name=identity.display,
                    recognized=identity.tier != "unknown",
                )

            if not text:
                # Silence — including a held follow-up window the user let lapse: end
                # any multi-turn dialog (and any parked ask) and return to idle.
                self._abandon_pending_ask(node_id, "silence")
                self._end_followup_dialog(node_id)
                await self.stop_node(node_id)
                self._tts_active.discard(node_id)
                return

            normalized = re.sub(r"[^\w\s]", "", text).strip().lower()
            if normalized in _STOP_PHRASES:
                log.info("[%s] stop phrase detected (%r) — ending session", node_id, text)
                self._abandon_pending_ask(node_id, "stop phrase")
                self._end_followup_dialog(node_id)
                await self.stop_node(node_id)
                self._tts_active.discard(node_id)
                return

            if self._llm_url:
                _t = time.monotonic()
                sstate: _StreamSpeech | None = None  # set on the 4.4 streaming path
                pending_ask = self._pending_ask.pop(node_id, None)
                if pending_ask is not None:
                    # This utterance ANSWERS a parked ask(): resume the skill
                    # instead of a fresh dispatch — the whole point of ask().
                    # The cue ladder applies BY DEFAULT (a skill can do real
                    # work after the answer — list creation, enrollment upload);
                    # ask(busy_cues=False) opts a question out for tight
                    # conversational turnarounds where a canned "Working on it."
                    # would read as a barge (knock-knock is the canonical case).
                    cont = self._call_llm_continue(pending_ask["id"], text, identity)
                    if pending_ask.get("busy_cues", True):
                        reply = await self._with_backchannel(node_id, cont, started_at=t0)
                    else:
                        reply = await cont
                    if pending_ask.get("origin_node") not in (None, node_id):
                        # Cross-room ask (intercom consent): the outcome belongs
                        # to the ASKER's room; this room just answered.
                        self._end_followup_dialog(node_id)
                        await self._deliver_reply(
                            str(pending_ask["origin_node"]),
                            str(pending_ask.get("origin_room") or ""),
                            None,
                            reply,
                            transcript=text,
                        )
                        return
                else:
                    # A mid-dialog follow-up (Kenzy held the floor and the user
                    # is answering) gets NO processing cues: "Working on it."
                    # between the user's answer and her next line breaks the
                    # conversational rhythm. The cues acknowledge a FRESH command
                    # into the void; a running back-and-forth doesn't need them.
                    cue_this_turn = node_id not in self._followup_turns
                    # 4.4 streaming: speak sentences while the model writes.
                    # None ⇒ endpoint unavailable ⇒ buffered path, unchanged.
                    # Rungs the streaming attempt already spoke before falling
                    # back — the buffered path must not say them over again.
                    spoken_rungs: list[int] = []
                    streamed = (
                        await self._call_llm_stream(
                            text, room_name, session_id, speaker, node_id, identity,
                            started_at=t0, cues=cue_this_turn, cue_played=spoken_rungs,
                        )
                        if self._streaming_enabled
                        else None
                    )
                    if streamed is not None:
                        reply, sstate = streamed
                    else:
                        reply = await self._with_backchannel(
                            node_id,
                            self._call_llm(
                                text,
                                room_name,
                                session_id,
                                speaker,
                                node_id=node_id,
                                identity=identity,
                            ),
                            started_at=t0,
                            cues=cue_this_turn and not any(spoken_rungs),
                        )
                response_text, voice_prompt = reply.text, reply.voice_prompt
                actions, fast = reply.actions, reply.fast
                expect_response, secret, spans = reply.expect_response, reply.secret, reply.spans
                llm_ms = (time.monotonic() - _t) * 1000.0
                log.info(
                    "[%s] LLM%s: %s",
                    node_id,
                    " (fast)" if fast else "",
                    "[lockbox exchange — response withheld]" if secret else response_text,
                )
                log.debug("[%s] voice_prompt: %s", node_id, voice_prompt)

                # ask() (4.2): the reply IS a question from a parked skill — the
                # next captured utterance routes back to it. Stored before the
                # floor-hold arms so a fast answer can't race the bookkeeping.
                # A cross-room ask (ask_room) delivers the question at THAT
                # room instead; this node just hears the announcement.
                if reply.continuation and reply.ask_room:
                    await self._deliver_cross_ask(node_id, room_name, reply)
                elif reply.continuation:
                    self._pending_ask[node_id] = {
                        "id": reply.continuation,
                        "capture": reply.ask_capture,
                        "origin_node": node_id,
                        "origin_room": room_name,
                        "busy_cues": reply.ask_busy_cues,
                    }

                # Multi-turn: if the reply deliberately holds the floor, arm the node to
                # capture the user's answer after the prompt plays (no wake word), bounded
                # by _MAX_FOLLOWUP_TURNS. Arm BEFORE _run_tts so _capture_after_prompt is
                # set before playback finishes (mirrors the enrollment prompt flow).
                was_holding = node_id in self._followup_turns
                hold_here = expect_response and bool(response_text) and not reply.ask_room
                rearmed = await self._maybe_hold_floor(
                    node_id, hold_here, cue=reply.ask_cue, for_ask=bool(reply.continuation)
                )
                if reply.continuation and not reply.ask_room and not rearmed:
                    # Turn cap / arm failure: the question can never be answered —
                    # unpark it now rather than letting the backstop sweep it.
                    self._cancel_pending_ask(node_id, "floor not held")

                _t = time.monotonic()
                if sstate is not None:
                    # Streamed reply: sentences already played during the LLM
                    # stage; speak the remainder and close the session (the
                    # floor was armed above, so expect_utterance precedes
                    # tts_end exactly like the buffered order).
                    spoke_ok = await sstate.close(reply)
                else:
                    spoke_ok = await self._run_tts(
                        node_id, room_name, session_id, response_text, voice_prompt,
                        sensitive=secret,
                    )
                tts_ms = (time.monotonic() - _t) * 1000.0
                if not spoke_ok:
                    # The reply exists but couldn't be spoken (TTS down/failed):
                    # play the pre-recorded cue so the user isn't left in silence.
                    await self._play_error_cue(node_id)

                # A held dialog that concluded with a final spoken reply ends
                # SILENTLY — the reply itself is the closure (stage 1 sound
                # language: the end cue means only "I stopped waiting", which the
                # node now plays itself when a follow-up window expires).
                if was_holding and not rearmed:
                    log.debug("[%s] dialog closed by final reply (no cue)", node_id)

                # Record the completed pipeline for the dashboard (only when something
                # is listening, so no transcript is kept when observability is off).
                if self._session_listeners:
                    self._notify_session(
                        {
                            "ts": time.time(),
                            "node_id": node_id,
                            "room": room_name,
                            "speaker": speaker,
                            # A lockbox exchange never lands in the Activity ring:
                            # the transcript may carry the secret (store) and the
                            # response may speak it (recall). The timing row stays.
                            "transcript": "[lockbox exchange]" if secret else text,
                            "response": "[content withheld]" if secret else response_text,
                            "fast": fast,
                            # Names + durations only — safe even on secret exchanges.
                            "spans": spans,
                            "stt_ms": round(stt_ms),
                            "speaker_ms": round(spk_ms),
                            "llm_ms": round(llm_ms),
                            "tts_ms": round(tts_ms),
                            "total_ms": round((time.monotonic() - t0) * 1000.0),
                        }
                    )
                if actions:
                    await self._dispatch_actions(actions, node_id, room_name, speaker)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # From the couch, a swallowed failure is indistinguishable from being
            # ignored — say so instead (pre-recorded, so it works when TTS is the
            # broken part), and release any held multi-turn floor.
            log.error("[%s] pipeline error: %s", node_id, exc, exc_info=True)
            self._end_followup_dialog(node_id)
            await self._play_error_cue(node_id)
        finally:
            self._stt_tasks.pop(node_id, None)
            # A pipeline that ends without ever dispatching speech (empty
            # reply, TTS down, error, cancellation) must not leave the group
            # engagement claimed in `capturing`/`thinking` — nothing will ever
            # send the tts_done that a `speaking` engagement ends on. (A
            # cancelled pipeline usually finds the engagement already gone:
            # the group cancel or the new winner's claim beat it here.)
            group = self._node_audio_group(node_id)
            if group is not None:
                eng = self._engagements.get(group)
                if eng is not None and eng.owner == node_id and eng.phase in (
                    "capturing",
                    "thinking",
                ):
                    self._engagement_clear(node_id, "pipeline finished without speech")

    async def _play_error_cue(self, node_id: str) -> None:
        """Stream the pre-recorded failure cue (``sound_error``, read live like
        the timer/alarm tones; empty ⇒ silent opt-out; a voice-matched render in
        the sound library shadows the bundle). Best effort. Deliberately NOT
        cue-flagged: failure ends the interaction, so it replaces the bed."""
        try:
            spec = self._effective_node_config(node_id).get("sound_error", "error.wav")
        except Exception:
            spec = "error.wav"
        from . import tones

        pcm = tones.load_tone(self._cue_file_spec(str(spec))) if spec else b""
        if pcm:
            await self._stream_pcm(node_id, pcm)

    def _pick_cue(self, node_id: str, key: str, spec: Any) -> str:
        """Resolve a cue config value (string, or a POOL list) to one sound name.

        Pools get a uniform random pick with a per-(node, key) no-immediate-repeat
        guard so a small pool never plays the same phrase back-to-back. Empty /
        malformed ⇒ "" (silent opt-out, like the empty-string convention)."""
        if isinstance(spec, (list, tuple)):
            names = [str(s).strip() for s in spec if s and str(s).strip()]
            if not names:
                return ""
            last = self._last_cue.get((node_id, key))
            candidates = [n for n in names if n != last] or names
            choice = random.choice(candidates)
            self._last_cue[(node_id, key)] = choice
            return choice
        return str(spec).strip() if spec else ""

    async def _play_cue(self, node_id: str, key: str, default: str) -> float:
        """Stream one processing-cue clip (a ladder rung — ``sound_thinking`` /
        ``sound_working``, read live like the timer/alarm tones; empty ⇒ silent
        opt-out). Regenerated renders in the sound library shadow the bundled
        file; the session is cue-flagged so the node duck-mixes it over the bed.
        Returns the clip's audio duration in seconds (0.0 when nothing played) —
        the ladder times the next rung's gap from the end of the clip."""
        try:
            spec = self._effective_node_config(node_id).get(key, default)
        except Exception:
            spec = default
        name = self._pick_cue(node_id, key, spec)
        if not name:
            return 0.0
        from . import tones

        pcm = tones.load_tone(self._cue_file_spec(name))
        if not pcm:
            return 0.0
        await self._stream_pcm(node_id, pcm, cue=True)
        return len(pcm) / (24000 * 2)  # 24 kHz mono int16

    def _cue_file_spec(self, name: str) -> str:
        """Library-first cue resolution: a file in the operator sound library
        (``data/sounds/`` — where voice-matched regenerated cues land) shadows
        the bundled WAV of the same name; anything else (absolute path, bare
        bundled name) passes through to ``tones.load_tone`` unchanged."""
        from . import tones

        library = tones.resolve_sound(name, self._sound_roots)
        return str(library) if library is not None else name

    def cue_texts_state(self) -> dict[str, Any]:
        """The cue phrases + current sound-key values (dashboard Settings card)."""
        keys: dict[str, Any] = {}
        for _kind, skey in _CUE_KINDS:
            keys[skey] = self._node_defaults.get(skey)
        return {"texts": dict(self._cue_texts), "keys": keys, "tts": bool(self._tts_url)}

    async def regenerate_cues(self) -> dict[str, Any]:
        """Re-render every spoken cue phrase through the CONFIGURED TTS voice.

        Renders land in the sound library (``data/sounds/cues/`` — rides
        backups, shadows nothing since the returned key values point straight at
        them); the caller (dashboard) persists the returned ``node_defaults``
        sound-key patch to the server.local.yaml layer and live-applies it via
        :meth:`AudioServer.apply_node_defaults`. Because these cues are
        server-streamed, one regeneration covers the whole fleet. All phrases
        are synthesized BEFORE any old render is touched — a TTS failure leaves
        the previous set intact."""
        if not self._tts_url:
            raise RuntimeError("TTS service not configured")
        import wave

        from . import tones

        rendered: list[tuple[str, str, bytes]] = []  # (kind, filename, pcm)
        keys: dict[str, Any] = {}
        for kind, skey in _CUE_KINDS:
            texts = self._cue_texts.get(kind) or _DEFAULT_CUE_TEXTS[kind]
            names: list[str] = []
            for i, phrase in enumerate(texts, start=1):
                pcm = await self._synthesize(phrase, _CUE_VOICE_PROMPTS[kind])
                if not pcm:
                    raise RuntimeError(f"TTS failed rendering the {kind} cue ({phrase!r})")
                fname = f"cues/{kind}-{i}.wav"
                rendered.append((kind, fname, pcm))
                names.append(fname)
            keys[skey] = names[0] if len(names) == 1 else names
        outdir = self._sound_roots[0] / "cues"
        outdir.mkdir(parents=True, exist_ok=True)
        for stale in outdir.glob("*.wav"):  # a previously larger pool must not linger
            stale.unlink()
        for _kind, fname, pcm in rendered:
            with wave.open(str(self._sound_roots[0] / fname), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(24000)  # /speak returns 24 kHz mono int16 PCM
                w.writeframes(pcm)
        tones.clear_cache()  # same-path re-renders must not serve the old audio
        log.info(
            "Regenerated %d spoken cue(s) in the current voice → %s", len(rendered), outdir
        )
        return {"count": len(rendered), "keys": keys}

    async def _with_backchannel(
        self,
        node_id: str,
        coro: Coroutine[Any, Any, Any],
        *,
        started_at: float | None = None,
        cues: bool = True,
    ) -> Any:
        """Await the LLM stage under the processing-cue ladder (``_CUE_LADDER``):
        escalating spoken acknowledgements while the reply is still pending. If a
        cue is mid-play when the reply lands, the short clip finishes so its TTS
        session closes cleanly before the reply's session starts. ``started_at``
        (the wait-session start) anchors the first rung so STT latency doesn't
        push it later. ``cues=False`` runs the stage WITHOUT the ladder — a
        mid-dialog follow-up turn, where a canned "Working on it." between the
        user's answer and Kenzy's next line breaks the conversational rhythm."""
        if not cues:
            return await coro
        ladder = _CueLadder(self, node_id, started_at=started_at)
        try:
            return await coro
        finally:
            await ladder.finish()

    async def _maybe_hold_floor(
        self, node_id: str, hold: bool, *, cue: bool = False, for_ask: bool = False
    ) -> bool:
        """Arm/continue a multi-turn dialog, or end it. Returns whether we re-armed.

        When ``hold`` (the reply deliberately expects an answer) and we're under the
        per-node turn cap, send ``expect_utterance`` so the node re-opens the mic after
        the prompt plays — no wake word. Otherwise the exchange is over: end it.
        ``for_ask`` (a parked skill's question) raises the cap to _MAX_ASK_TURNS —
        the skill's conversation shape is the real bound there.
        """
        turns = self._followup_turns.get(node_id, 0)
        cap = max(self._max_followup_turns, _MAX_ASK_TURNS) if for_ask else self._max_followup_turns
        if hold and turns < cap:
            node = self._nodes.get(node_id)
            if node is not None:
                try:
                    # Dialog turns open silently (her question is the cue);
                    # record-after-the-tone flows (audio asks: enrollment) get
                    # the chime — the tone does real work there.
                    await node.ws.send(protocol.expect_utterance(cue=cue))
                    self._followup_turns[node_id] = turns + 1
                    self._engagement_update(node_id, None, "reply-window")
                    log.info("[%s] holding floor for follow-up (turn %d)", node_id, turns + 1)
                    return True
                except Exception as exc:
                    log.warning("[%s] could not arm follow-up capture: %s", node_id, exc)
        self._end_followup_dialog(node_id)
        return False

    def _followup_timed_out(self, node_id: str) -> None:
        # v6 follow-up: the node's window expired with no speech — the silence
        # end, anchored at the NODE's playback-complete by construction.
        if self._s2s_bridge is not None and self._s2s_bridge.on_followup_timeout(node_id):
            return
        pa = self._pending_ask.get(node_id)
        if pa is not None and pa.get("capture") == "audio":
            # An audio ask (enrollment) treats an expired window as an EMPTY
            # sample — the skill retries the same prompt ("I didn't catch
            # that"), mirroring the old enrollment retry path. Its attempt cap
            # bounds the loop.
            self._pending_ask.pop(node_id, None)
            node = self._nodes.get(node_id)
            room = node.room_id if node is not None else ""

            async def _empty() -> None:
                reply = await self._call_llm_continue_audio(pa["id"], b"")
                await self._deliver_reply(node_id, room, None, reply, transcript="[no sample]")

            asyncio.create_task(_empty())
            return
        # A parked ask() whose reply window expired: the skill gets None.
        self._abandon_pending_ask(node_id, "reply window expired")
        self._end_followup_dialog(node_id)

    def _end_followup_dialog(self, node_id: str) -> None:
        """Clear the per-node follow-up turn counter (a held dialog is over).

        Does not itself play the end cue — that fires from ``_transcribe`` after the
        final reply's TTS (see ``_play_dialog_end``), so the cue never clips the last
        spoken line and never sounds after a silent/stop exit.
        """
        if self._followup_turns.pop(node_id, None):
            log.info("[%s] multi-turn dialog ended", node_id)
        # Every exchange-over path funnels through here (the audio-ask retry
        # deliberately doesn't — its conversation continues). The group
        # engagement ends here too — UNLESS the reply is still audibly playing
        # (`speaking`): then it lives until the node's tts_done, so a wake
        # elsewhere in the group can still stop the playback tail. (Measured
        # 2026-08-18: dispatch-time clearing left a ~2 s deaf window between
        # "exchange over" and the audio actually ending at the speaker.)
        group = self._node_audio_group(node_id)
        eng = self._engagements.get(group) if group is not None else None
        node = self._nodes.get(node_id)
        if (
            eng is not None
            and eng.owner == node_id
            and node is not None
            and _supports_tts_done(node.kenzy_version)
            and (
                eng.phase == "speaking"  # audio already dispatched, still playing
                # Fast path: the floor decision lands when the reply is
                # COMPUTED, before TTS even dispatches (measured 2026-08-18:
                # "exchange over" at reply time, playback 7–10 s later) — so a
                # still-running pipeline in `thinking` also holds the
                # engagement; send_tts_start advances it to `speaking` and
                # tts_done ends it. A pipeline that never speaks clears in
                # _transcribe's finally.
                or (eng.phase == "thinking" and self._has_pipeline(node_id))
            )
        ):
            log.info("group '%s': exchange over — engagement held for reply delivery", group)
            return
        self._engagement_clear(node_id, "exchange over")

    async def _http_assist(self, request: Request) -> Response:
        """``GET /assist?text=…&ha_user=…`` — the HA Assist channel (F3).

        The second front door: the kenzy-hass conversation agent sends the
        typed/spoken text plus the HA **person entity id** it resolved from the
        session, and gets the pipeline's text reply. Identity resolves through
        the SAME person records as voice (``resolve_assist_identity``) — a
        mapped person is recognized (memory, gated skills), an unmapped HA user
        is unknown (fail closed). Query-string because the websockets request
        hook exposes no HTTP body (the /register precedent); token-gated like
        every always-on endpoint. Conversation continuity rides a per-person
        synthetic room lane (``assist:<person>``) through the existing history.
        """
        from urllib.parse import parse_qs, urlsplit

        if not self._check_service_token(request):
            return self._http_json(401, {"error": "invalid service token"})
        self.mark_assist_seen()  # reveals the dashboard's HA surfaces for app-only households
        qs = parse_qs(urlsplit(request.path).query)
        text = (qs.get("text") or [""])[0].strip()
        if not text:
            return self._http_json(400, {"error": "missing 'text' query parameter"})
        if not self._llm_url:
            return self._http_json(503, {"error": "LLM service not configured"})
        ha_user = (qs.get("ha_user") or [""])[0].strip()
        identity = resolve_assist_identity(
            self._people, ha_user, unknown_name=self._unknown_speaker
        )
        # Unmapped users get their OWN guest lane (keyed by HA user) so two
        # guests chatting concurrently never see each other's context.
        lane = f"assist:{identity.person_id or 'guest:' + (ha_user or 'anon')}"
        try:
            r = await self._call_llm(
                text, lane, None, speaker=identity.display, identity=identity, channel="assist"
            )
            if r.continuation:
                # Assist can't hold a voice floor yet (HA continue_conversation
                # arrives with the ask() Assist phase) — unpark immediately; the
                # spoken prompt still goes back as the reply text.
                await self._cancel_continuation_now(r.continuation, "assist channel")
            reply, actions, fast = r.text, r.actions, r.fast
        except Exception as exc:
            log.warning("Assist pipeline failed: %s", exc)
            return self._http_json(502, {"error": "assist pipeline failed"})
        if actions:
            # Room-targeting actions (announce, explicit-room schedules) work
            # from anywhere; node-bound ones no-op against the synthetic lane.
            await self._dispatch_actions(actions, "", lane, source_speaker=identity.display)
        log.info("[%s] assist (%s): %s", lane, identity.display, text)
        return self._http_json(
            200,
            {
                "text": reply,
                "speaker": identity.display,
                "recognized": identity.recognized,
                "fast": fast,
            },
        )

    async def _call_speaker(self, pcm: bytes, room_id: str) -> tuple[str, float]:
        """Identify the speaker: returns ``(name, confidence)``. The confidence is
        consumed by the identity resolver (F1) for tiering; the name is already
        the unknown-speaker name when the score is below the service threshold."""
        import base64

        import httpx  # type: ignore[import-untyped]

        payload = {"audio_b64": base64.b64encode(pcm).decode(), "room_id": room_id}
        try:
            async with httpx.AsyncClient(verify=tlsutil.httpx_verify()) as client:
                resp = await client.post(
                    self._speaker_url,  # type: ignore[arg-type]
                    json=payload,
                    timeout=self._speaker_timeout,
                    headers=self._service_headers("POST", self._speaker_url),
                )
                resp.raise_for_status()
            data = resp.json()
            return str(data["speaker"]), float(data.get("confidence", 0.0))
        except Exception as exc:
            log.warning("[%s] speaker ID failed: %s", room_id, exc)
            return self._unknown_speaker, 0.0

    async def _call_stt(self, pcm: bytes, room_id: str, session_id: str | None) -> str:
        import base64

        import httpx  # type: ignore[import-untyped]

        payload = {
            "audio_b64": base64.b64encode(pcm).decode(),
            "room_id": room_id,
            "session_id": session_id,
        }
        async with httpx.AsyncClient(verify=tlsutil.httpx_verify()) as client:
            resp = await client.post(
                self._stt_url,  # type: ignore[arg-type]
                json=payload,
                timeout=self._stt_timeout,
                headers=self._service_headers("POST", self._stt_url),
            )
            resp.raise_for_status()
        return str(resp.json()["text"])

    def _speaker_service_base(self) -> str | None:
        """The speaker service's base URL — static config first, else the
        auto-registered heartbeat (mirrors the dashboard's _service_base rule:
        never trust the static map alone)."""
        if self._speaker_url:
            return self._speaker_url.rsplit("/", 1)[0]
        announced = self._announced_services.get("speaker")
        if announced:
            return str(announced.get("base") or "") or None
        return None

    async def _tts_is_local(self) -> bool:
        """Whether kenzy-tts reports a local provider (``/health`` → ``local``).
        Cached ~15s (short: a stale True would speak a secret through a just-
        switched cloud provider); anything short of an explicit true is False."""
        import time as _time

        now = _time.monotonic()
        cached = getattr(self, "_tts_local_cache", None)
        if cached and now - cached[0] < 15:
            return bool(cached[1])
        local = False
        if self._tts_url:
            import httpx  # type: ignore[import-untyped]

            try:
                base = self._tts_url.rsplit("/", 1)[0]
                async with httpx.AsyncClient(
                    timeout=3.0, verify=tlsutil.httpx_verify()
                ) as client:
                    r = await client.get(f"{base}/health")
                    local = bool(r.json().get("local", False))
            except Exception:
                local = False
        self._tts_local_cache = (now, local)
        return local

    async def _deliver_reply(
        self,
        node_id: str,
        room_name: str,
        session_id: str | None,
        reply: LlmReply,
        *,
        transcript: str = "",
    ) -> None:
        """Speak an LlmReply at a node with full ask() semantics — pending-ask
        bookkeeping, cue-aware floor hold, Activity record, actions. The
        pipeline's main path inlines its own richer version (per-stage
        timings); this serves the paths with no STT stage: audio-ask resumes
        and dashboard-initiated enrollment."""
        log.info(
            "[%s] LLM%s: %s",
            node_id,
            " (fast)" if reply.fast else "",
            "[lockbox exchange — response withheld]" if reply.secret else reply.text,
        )
        if reply.continuation and reply.ask_room:
            await self._deliver_cross_ask(node_id, room_name, reply)
        elif reply.continuation:
            self._pending_ask[node_id] = {
                "id": reply.continuation,
                "capture": reply.ask_capture,
                "origin_node": node_id,
                "origin_room": room_name,
            }
        hold_here = reply.expect_response and bool(reply.text) and not reply.ask_room
        rearmed = await self._maybe_hold_floor(
            node_id, hold_here, cue=reply.ask_cue, for_ask=bool(reply.continuation)
        )
        if reply.continuation and not reply.ask_room and not rearmed:
            self._cancel_pending_ask(node_id, "floor not held")
        spoke_ok = await self._run_tts(
            node_id, room_name, session_id or str(uuid.uuid4()), reply.text,
            reply.voice_prompt, sensitive=reply.secret,
        )  # fmt: skip
        if not spoke_ok:
            await self._play_error_cue(node_id)
        if self._session_listeners and transcript:
            self._notify_session(
                {
                    "ts": time.time(),
                    "node_id": node_id,
                    "room": room_name,
                    "speaker": "",
                    "transcript": transcript,
                    "response": "[content withheld]" if reply.secret else reply.text,
                    "fast": reply.fast,
                    "spans": reply.spans,
                    "stt_ms": 0,
                    "speaker_ms": 0,
                    "llm_ms": 0,
                    "tts_ms": 0,
                    "total_ms": 0,
                }
            )
        if reply.actions:
            await self._dispatch_actions(reply.actions, node_id, room_name, None)

    async def _deliver_cross_ask(self, origin_node: str, origin_room: str, reply: LlmReply) -> None:
        """Deliver a cross-room ask: speak the question at the TARGET room and
        arm its window; the origin has already heard the announcement. Any
        failure resolves the continuation as an empty answer so the parked
        skill can tell the asker what happened."""
        assert reply.continuation is not None
        target_id = self._resolve_room_node(str(reply.ask_room), exclude=origin_node)
        target = self._nodes.get(target_id) if target_id else None
        busy = target_id is not None and (
            target_id in self._pending_ask
            or (target is not None and target.intercom_peer is not None)
        )
        if target is None or busy:
            reason = "busy" if busy else "unreachable"
            log.info(
                "[%s] cross-room ask to %r failed (%s)", origin_node, reply.ask_room, reason
            )
            asyncio.create_task(
                self._resume_ask_empty(reply.continuation, origin_node, origin_room)
            )
            return
        assert target_id is not None
        self._pending_ask[target_id] = {
            "id": reply.continuation,
            "capture": reply.ask_capture,
            "origin_node": origin_node,
            "origin_room": origin_room,
            "busy_cues": reply.ask_busy_cues,
        }
        # Ringback at the origin while the question travels (node stops it on
        # the next TTS / connect / wake). Best-effort.
        origin = self._nodes.get(origin_node)
        if origin is not None:
            try:
                await origin.ws.send(protocol.call_ringing())
            except Exception:
                pass
        rearmed = await self._maybe_hold_floor(target_id, True, cue=reply.ask_cue, for_ask=True)
        if not rearmed:
            self._pending_ask.pop(target_id, None)
            asyncio.create_task(
                self._resume_ask_empty(reply.continuation, origin_node, origin_room)
            )
            return
        await self._run_tts(
            target_id, target.room_id, str(uuid.uuid4()), reply.ask_prompt, reply.voice_prompt
        )

    async def _resume_ask_empty(self, cont_id: str, origin_node: str, origin_room: str) -> None:
        """Resolve a cross-room ask with an EMPTY answer (no answer / wake /
        target lost) and deliver the skill's outcome to the ASKER's room —
        unlike a cancel, the asker hears what happened ("No answer from…")."""
        try:
            reply = await self._call_llm_continue(cont_id, "", None)
        except Exception as exc:
            log.warning("cross-room ask %s could not resolve: %s", cont_id, exc)
            return
        await self._deliver_reply(origin_node, origin_room, None, reply)

    async def _call_llm_continue_audio(self, cont_id: str, pcm: bytes) -> LlmReply:
        """Deliver a RAW captured sample to an audio-mode ask() (enrollment).
        No STT, no speaker-id — the audio is the answer."""
        import base64

        import httpx  # type: ignore[import-untyped]

        url = self._llm_url_sibling("continue")
        payload = {
            "continuation": cont_id,
            "audio_b64": base64.b64encode(pcm).decode(),
            "tts_local": await self._tts_is_local(),
        }
        async with httpx.AsyncClient(verify=tlsutil.httpx_verify()) as client:
            resp = await client.post(
                url,
                json=payload,
                timeout=self._llm_timeout,
                headers=self._service_headers("POST", url),
            )
            if resp.status_code == 404:
                return LlmReply(
                    text="Sorry — I lost track of that. Let's start over.",
                    voice_prompt="apologetic, brief",
                )
            resp.raise_for_status()
        return _llm_reply_from(resp.json())

    async def _call_llm_continue(self, cont_id: str, text: str, identity: Any) -> LlmReply:
        """Deliver the user's answer to a parked ask() continuation. A 404
        (llm restarted — continuations are mortal by design) degrades to an
        honest 'lost track' reply instead of an error cue."""
        import httpx  # type: ignore[import-untyped]

        url = self._llm_url_sibling("continue")
        payload = {
            "continuation": cont_id,
            "text": text,
            "speaker": identity.display if identity else None,
            "person_id": identity.person_id if identity else None,
            "speaker_tier": identity.tier if identity else None,
            "confidence": round(identity.confidence, 4) if identity else None,
            "memory_opt_out": self._person_memory_opt_out(identity),
            "tts_local": await self._tts_is_local(),
        }
        async with httpx.AsyncClient(verify=tlsutil.httpx_verify()) as client:
            resp = await client.post(
                url,
                json=payload,
                timeout=self._llm_timeout,
                headers=self._service_headers("POST", url),
            )
            if resp.status_code == 404:
                return LlmReply(
                    text="Sorry — I lost track of that. Let's start over.",
                    voice_prompt="apologetic, brief",
                )
            resp.raise_for_status()
        return _llm_reply_from(resp.json())

    async def _cancel_continuation_now(self, cont_id: str, reason: str) -> None:
        """Awaited cancel for paths with no node bookkeeping (assist, scheduled)."""
        import httpx  # type: ignore[import-untyped]

        url = self._llm_url_sibling("cancel")
        try:
            async with httpx.AsyncClient(verify=tlsutil.httpx_verify()) as client:
                await client.post(
                    url,
                    json={"continuation": cont_id, "reason": reason},
                    timeout=5.0,
                    headers=self._service_headers("POST", url),
                )
        except Exception as exc:
            log.debug("ask cancel (%s) failed: %s — llm backstop will sweep", reason, exc)

    def _abandon_pending_ask(self, node_id: str, reason: str) -> None:
        """The asked node moved on (wake / silence / stop / window expiry).
        Same-room asks cancel (the asker IS the abandoner); a cross-room ask
        resolves as an EMPTY answer so the asker hears the outcome."""
        entry = self._pending_ask.get(node_id)
        if entry is None:
            return
        if entry.get("origin_node") not in (None, node_id):
            self._pending_ask.pop(node_id, None)
            log.info("[%s] cross-room ask abandoned (%s) — resolving empty", node_id, reason)
            asyncio.create_task(
                self._resume_ask_empty(
                    entry["id"], str(entry["origin_node"]), str(entry.get("origin_room") or "")
                )
            )
            return
        self._cancel_pending_ask(node_id, reason)

    def _cancel_pending_ask(self, node_id: str, reason: str) -> None:
        """Wake word / window expiry / stop phrase / disconnect: tell the LLM
        service to unpark the continuation with None. Fire-and-forget — the
        conversation has already moved on."""
        entry = self._pending_ask.pop(node_id, None)
        if entry is None:
            return
        self._cancel_by_id(entry["id"], reason)
        log.info("[%s] pending ask canceled (%s)", node_id, reason)

    def _cancel_by_id(self, cont_id: str, reason: str) -> None:
        """Fire-and-forget POST /process/cancel for a known continuation id."""

        async def _post() -> None:
            import httpx  # type: ignore[import-untyped]

            url = self._llm_url_sibling("cancel")
            try:
                async with httpx.AsyncClient(verify=tlsutil.httpx_verify()) as client:
                    await client.post(
                        url,
                        json={"continuation": cont_id, "reason": reason},
                        timeout=5.0,
                        headers=self._service_headers("POST", url),
                    )
            except Exception as exc:
                log.debug("ask cancel for %s failed (%s) — llm backstop will sweep", cont_id, exc)

        asyncio.create_task(_post())

    def _llm_url_sibling(self, leaf: str) -> str:
        """/process → /process/<leaf> on the same base."""
        return f"{self._llm_url}/{leaf}"

    async def _llm_payload(
        self,
        text: str,
        room_id: str,
        session_id: str | None,
        speaker: str | None,
        node_id: str | None,
        identity: Identity | None,
        channel: str,
    ) -> dict[str, Any]:
        """The /process request body — shared by the buffered and streaming calls."""
        return {
            "text": text,
            "room_id": room_id,
            "session_id": session_id,
            "speaker": speaker,
            # Identity core (F1): the resolved person + confidence tier, so skills
            # and (later) memory can gate on who's asking and how sure we are.
            "person_id": identity.person_id if identity else None,
            "speaker_tier": identity.tier if identity else None,
            "confidence": round(identity.confidence, 4) if identity else None,
            # F7.4 "don't remember me": the person's opt-out rides every request
            # so the LLM service keeps no ledger (context, writes, reads) on them.
            "memory_opt_out": self._person_memory_opt_out(identity),
            "memory_capture": self._person_memory_capture(identity),
            # Connected room names so the model can target real rooms (announce/intercom).
            "rooms": sorted({s.room_id for s in self._nodes.values()}),
            # The speaker service base (static config ← auto-registered heartbeat,
            # resolved per-request) — the enrollment skill POSTs samples to it.
            "speaker_url": self._speaker_service_base(),
            # Person records (light form) so skills can resolve spoken names —
            # enrollment's person-first profile keying lives in the skill now.
            "people": [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "voiceprints": p["voiceprints"],
                    # 5.0.3 slice D: what else this person answers to, for
                    # spoken-name resolution (kenzy.llm.names).
                    "aliases": p.get("aliases") or [],
                    # ha_user link (F1): what makes presence-on-demand zero-config.
                    "ha_user": p.get("ha_user"),
                }
                for p in self.list_people()
            ],
            # The asking node's active timers/alarms/reminders, so the schedule
            # skill / fast intents can answer status and cancel by id locally.
            # A nodeless channel (assist, F3) gets the whole house's entries —
            # from the phone, "what timers are set?" means everywhere.
            "schedules": self._schedule_payload(node_id),
            # Rooms whose speakers lack AEC (hardware_aec: false) — alarm and
            # intercom skills refuse these targets in the reply itself.
            "no_aec_rooms": self._no_aec_rooms(),
            # v5 spine: the occupancy snapshot — server-held room state, injected
            # exactly like rooms/schedules/no_aec_rooms above. 5.0.0 WIRES it;
            # nothing reads it until 5.0.1, and when a skill does it must
            # tier-gate ("who's home" is household information, like presence).
            "occupancy": self._occupancy_payload(),
            # Which front door (F3): node-bound skills refuse on nodeless channels.
            "channel": channel,
            # Lockbox spoken-recall gate (founder decision 2026-07-18): a secret
            # is only SPOKEN when the TTS provider keeps audio on-box. Fail-closed:
            # unknown/unreachable/nodeless-channel all read as not-local.
            "tts_local": (channel == "voice") and await self._tts_is_local(),
        }

    async def llm_occupancy_map(self) -> dict[str, Any]:
        """Fetch the occupancy evidence map from kenzy-llm (v5 spine, Slice A).

        The ONLY thing that crosses the service boundary for occupancy: the
        entity→room map, because the area knowledge and curation baking live in
        ``ha_model.py``. Config does not cross (the server holds both HA
        credentials); the event stream does not cross (it is consumed locally).
        """
        base = self._llm_url or ""
        if base.endswith("/process"):
            base = base[: -len("/process")]
        if not base:
            announced = self.announced_health_urls().get("llm", "")
            base = announced[: -len("/health")] if announced else ""
        if not base:
            raise RuntimeError("llm service is not reachable")
        import httpx

        from kenzy import tlsutil

        url = f"{base}/ha/map"
        async with httpx.AsyncClient(timeout=20.0, verify=tlsutil.httpx_verify()) as client:
            resp = await client.get(url, headers=self._service_headers("GET", url))
            resp.raise_for_status()
            data = resp.json()
        return data if isinstance(data, dict) else {}

    async def _call_llm(
        self,
        text: str,
        room_id: str,
        session_id: str | None,
        speaker: str | None = None,
        node_id: str | None = None,
        identity: Identity | None = None,
        channel: str = "voice",
    ) -> LlmReply:
        import httpx  # type: ignore[import-untyped]

        payload = await self._llm_payload(
            text, room_id, session_id, speaker, node_id, identity, channel
        )
        async with httpx.AsyncClient(verify=tlsutil.httpx_verify()) as client:
            resp = await client.post(
                self._llm_url,  # type: ignore[arg-type]
                json=payload,
                timeout=self._llm_timeout,
                headers=self._service_headers("POST", self._llm_url),
            )
            resp.raise_for_status()
            data = resp.json()
        return _llm_reply_from(data)

    async def _call_llm_stream(
        self,
        text: str,
        room_id: str,
        session_id: str | None,
        speaker: str | None = None,
        node_id: str | None = None,
        identity: Identity | None = None,
        started_at: float | None = None,
        cues: bool = True,
        cue_played: list[int] | None = None,
    ) -> tuple[LlmReply, _StreamSpeech] | None:
        """Streaming pipeline (4.4): POST /process/stream and SPEAK complete
        sentences while the model is still writing — the whole point of the era.

        Returns ``(reply, speech)`` with the tts session left OPEN: the caller
        does its bookkeeping (continuation, floor arming) and then
        ``speech.close(reply)`` speaks the unspoken remainder and ends the
        session — preserving the buffered order (expect_utterance before
        tts_end). Failure policy (review-hardened): None ⇒ caller falls back to
        buffered (404/405 old llm, any pre-body HTTP error, or a transport
        failure before ANY event arrived — nothing ran llm-side, retry is
        safe); failure AFTER speech began keeps what was spoken (never the
        error cue over speech); failure after events-but-before-speech raises
        honestly (tools may have run — a buffered retry could actuate twice).
        Wake-word cancellation propagates naturally: closing the HTTP stream
        cancels the llm-side pipeline task.

        The backchannel timer here measures time-to-FIRST-AUDIO: a fast first
        sentence suppresses the cue that a slow buffered reply would earn."""
        import httpx  # type: ignore[import-untyped]

        assert node_id is not None
        payload = await self._llm_payload(
            text, room_id, session_id, speaker, node_id, identity, "voice"
        )

        # Processing-cue ladder: escalating acks while no reply audio has
        # started — first audio finishes/disarms it (time-to-FIRST-AUDIO here:
        # a fast first sentence suppresses cues a slow buffered reply earns).
        # started_at anchors the first rung to the wait start (pre-STT).
        # cues=False (a mid-dialog follow-up) runs with no ladder at all.
        ladder = _CueLadder(self, node_id, started_at=started_at) if cues else None
        speech = _StreamSpeech(
            self, node_id, session_id, on_first_audio=ladder.finish if ladder else None
        )
        buf = ""  # delta text not yet spoken
        reply: LlmReply | None = None
        saw_event = False  # any body event ⇒ tools may have run llm-side
        stream_url = self._llm_url.replace("/process", "/process/stream")  # type: ignore[union-attr]
        timeout = httpx.Timeout(10.0, read=max(self._llm_timeout, 60.0))
        try:
            async with httpx.AsyncClient(verify=tlsutil.httpx_verify()) as client:
                async with client.stream(
                    "POST",
                    stream_url,
                    json=payload,
                    timeout=timeout,
                    headers=self._service_headers("POST", stream_url),
                ) as resp:
                    if resp.status_code in (404, 405):
                        log.info("LLM service has no /process/stream — buffered fallback")
                        return None
                    if resp.status_code >= 400:
                        # Transient 5xx / auth hiccup on the stream endpoint:
                        # nothing has run yet, so the buffered path gets its
                        # chance (it will surface a real, persistent failure).
                        log.warning(
                            "LLM /process/stream HTTP %d — buffered fallback", resp.status_code
                        )
                        return None
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        ev = json.loads(line)
                        saw_event = True
                        kind = ev.get("event")
                        if kind == "head":
                            speech.voice_prompt = str(ev.get("voice_prompt") or "")
                        elif kind == "delta":
                            buf += str(ev.get("text") or "")
                            sentences, buf = _split_sentences(buf)
                            for s in sentences:
                                await speech.speak(s)
                        elif kind == "end":
                            ev.pop("event", None)
                            reply = _llm_reply_from(ev)
                            break
                        elif kind == "error":
                            raise RuntimeError(f"llm stream error: {ev.get('detail')}")
        except asyncio.CancelledError:
            await speech.abort()
            raise
        except Exception as exc:
            # Failure policy (review finding): NEVER the error cue on top of
            # speech, and NEVER a silent re-execution of skill side effects.
            if speech.started:
                # What the room heard stands; any unspoken buffered text ships
                # via close() as the remainder. No actions (they ride the end
                # event, which never came).
                log.warning(
                    "[%s] llm stream failed after speech began (%s) — keeping what was spoken",
                    node_id,
                    exc,
                )
                return (
                    LlmReply(text=speech.spoken + buf, voice_prompt=speech.voice_prompt),
                    speech,
                )
            await speech.abort()
            if not saw_event:
                # Died before ANY event (connect/read-timeout, dead endpoint):
                # nothing ran llm-side, safe to retry buffered.
                log.warning("LLM stream failed before any output (%s) — buffered fallback", exc)
                return None
            # Events arrived (tools may have executed) — re-running the request
            # buffered could actuate twice. Honest failure, like a buffered 500.
            raise
        finally:
            # Idempotent (first audio may already have finished it). On the
            # buffered-fallback exits this stops the ladder before the buffered
            # path starts its own — a mid-play cue still closes cleanly (a bare
            # cancel would orphan its tts session). None ⇒ cues suppressed.
            if ladder is not None:
                await ladder.finish()
                # Tell the caller how much of the ladder was already spoken, so a
                # buffered fallback continues rather than restarting it.
                if cue_played is not None:
                    cue_played.append(ladder.played)
        if reply is None:
            if speech.spoken:
                # Clean EOF without an end event: what was heard is the record.
                log.warning("[%s] llm stream ended early — keeping spoken text", node_id)
                reply = LlmReply(text=speech.spoken, voice_prompt=speech.voice_prompt)
            else:
                raise RuntimeError("llm stream ended without a reply")
        return reply, speech

    async def _dispatch_actions(
        self,
        actions: list[dict[str, Any]],
        source_node_id: str,
        source_room: str,
        source_speaker: str | None = None,
    ) -> None:
        """Actuate server-side actions returned by the LLM (e.g. announce).

        ``announce()`` keys on ``node_id``, but the LLM targets human room names, so
        names are resolved here. The asking node is excluded so it doesn't hear the
        broadcast on top of its own spoken reply. ``source_speaker`` (identified at
        capture time) is stored with schedule entries so a deferred command replays
        with the authorizing voice's identity.
        """
        for action in actions:
            atype = action.get("type")
            # Node-bound actions need an asking node; a nodeless source (the
            # assist lane, F3) can't satisfy them. The skills refuse these on
            # the assist channel already — this is the server-side backstop so
            # a custom skill queueing one can't crash dispatch.
            if not source_node_id and atype in (
                "connect_call",
                "start_calibration",
                "set_volume",
            ):
                log.info("[%s] skipping node-bound action %r — no asking node", source_room, atype)
                continue
            if not source_node_id and atype == "set_schedule" and not action.get("room"):
                log.info("[%s] skipping roomless schedule — no asking node", source_room)
                continue
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
            elif atype == "connect_call":
                # Intercom bridge, AFTER the skill's cross-room consent ask got
                # its spoken yes. Server-side re-checks are the backstop (the
                # skill already refused no-AEC/unknown rooms in-reply).
                await self._action_connect_call(
                    source_node_id, source_room, str(action.get("room", ""))
                )
            elif atype == "adopt_voice":
                # Person-first invariant, actuated from the enrollment skill on
                # its FIRST stored sample: link the voiceprint to its person.
                self.adopt_enrolled_voice(
                    str(action.get("voiceprint", "")),
                    str(action.get("display", "")) or str(action.get("voiceprint", "")),
                    str(action.get("person_id") or "") or None,
                )
            elif atype == "start_calibration":
                # Voice-guided audio calibration on the asking node (spawns its own
                # task — the guided flow runs ~30-60s and must not block dispatch).
                await self.start_calibration(source_node_id, source_room)
            elif atype == "silence_proactive":
                # "Stop the alerts" — quiet what's sounding, change nothing
                # about the future. Not node-bound: an alert plays in every
                # room, so silencing it from any of them is the whole point.
                self._acknowledge_proactive("an explicit request")
            elif atype == "set_proactive":
                # The spoken off-switch. Not node-bound: it is a house-wide
                # setting, so it works from any room and on the assist channel.
                self.set_proactive_enabled(bool(action.get("enabled", True)))
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
            elif atype == "set_schedule":
                self._action_set_schedule(action, source_node_id, source_room, source_speaker)
            elif atype == "cancel_schedule":
                self._scheduler.cancel([str(i) for i in (action.get("ids") or [])])
            else:
                log.warning("[%s] unknown LLM action type: %r", source_node_id, atype)

    # ------------------------------------------------------------------
    # Timers / alarms / reminders (scheduler firing + delivery)
    # ------------------------------------------------------------------

    def _action_set_schedule(
        self,
        action: dict[str, Any],
        source_node_id: str,
        source_room: str,
        source_speaker: str | None = None,
    ) -> None:
        """Store a schedule entry from a skill action (already spoken as confirmed,
        so a bad spec is logged rather than answered)."""
        room = str(action.get("room") or source_room)
        node_id = source_node_id
        if action.get("room"):  # explicit room ("wake me at 7 in the bedroom")
            resolved = self._resolve_room_node(room)
            if resolved is not None:
                node_id = resolved
            # else: keep the asking node as the fallback target; the room name is
            # stored and re-resolved at fire time, so a node that joins later wins.
        seconds = action.get("seconds")
        speaker = source_speaker or ""
        if speaker.lower() == self._unknown_speaker.lower():
            speaker = ""
        try:
            self._scheduler.add(
                str(action.get("kind", "")),
                node_id,
                room,
                label=str(action.get("label", "")),
                seconds=float(seconds) if seconds is not None else None,
                at=str(action.get("at", "")),
                days=[str(d) for d in (action.get("days") or [])],
                speaker=speaker,
            )
        except ValueError as exc:
            log.warning("[%s] rejected schedule action %r: %s", source_node_id, action, exc)

    def _schedule_payload(self, node_id: str | None) -> list[dict[str, Any]]:
        """The asking node's active entries (all nodes' when ``node_id`` is
        None — the nodeless assist channel), as injected into /process."""
        return [
            {
                "id": e.id,
                "kind": e.kind,
                "label": e.label,
                "room": e.room,
                "at": e.at,
                "days": e.days,
                "seconds_left": int(e.seconds_left()),
            }
            for e in self._scheduler.entries(node_id)
        ]

    def list_schedules(self) -> list[dict[str, Any]]:
        """All active entries (dashboard Scheduled view)."""
        return [
            {**e.to_dict(), "seconds_left": int(e.seconds_left())}
            for e in self._scheduler.entries()
        ]

    def cancel_schedule_ids(self, ids: list[str]) -> int:
        return len(self._scheduler.cancel(ids))

    def add_schedule_listener(self, cb: Callable[[], None]) -> None:
        self._scheduler.add_listener(cb)

    async def create_backup_archive(
        self,
        *,
        include_secrets: bool = False,
        include_models: bool = False,
        include_lockbox_key: bool = True,
    ) -> bytes:
        """One complete archive even on a multi-host deployment: the local tree
        plus the stateful services' slices (speaker embeddings; the LLM host's
        skills + curation). Local wins on collisions — which is also what dedupes
        the co-located case, where the slices duplicate the local files. An
        unreachable service degrades to a partial archive, recorded in the
        manifest's ``service_slices`` and logged loudly."""
        from kenzy.backup import create_backup
        from kenzy.config import kenzy_data_root

        extra: dict[str, bytes] = {}
        notes: dict[str, str] = {}
        for svc, url in (("speaker", self._speaker_url), ("llm", self._llm_url)):
            if not url:
                notes[svc] = "not configured"
                continue
            try:
                entries = await self._fetch_backup_slice(url.rsplit("/", 1)[0])
                for name, data in entries.items():
                    extra.setdefault(name, data)
                notes[svc] = f"{len(entries)} file(s)"
            except Exception as exc:
                notes[svc] = "unreachable"
                log.warning(
                    "backup: %s slice unavailable (%s) — the archive may be missing "
                    "that host's state",
                    svc,
                    exc,
                )
        return create_backup(
            kenzy_data_root(),
            extra_entries=extra,
            notes={"service_slices": notes},
            include_secrets=include_secrets,
            include_models=include_models,
            include_lockbox_key=include_lockbox_key,
        )

    async def _fetch_backup_slice(self, base_url: str) -> dict[str, bytes]:
        import httpx  # type: ignore[import-untyped]

        from kenzy.backup import unpack_archive_bytes

        async with httpx.AsyncClient(verify=tlsutil.httpx_verify()) as client:
            resp = await client.get(
                base_url + "/backup",
                timeout=20.0,
                headers=self._service_headers("GET", base_url + "/backup"),
            )
            resp.raise_for_status()
        return unpack_archive_bytes(resp.content)

    def _schedule_target(self, entry: Any) -> str | None:
        """Resolve where an entry fires: its node if connected, else any connected
        node now serving that room name (survives reprovisioned devices)."""
        if entry.node_id in self._nodes:
            return str(entry.node_id)
        return self._resolve_room_node(entry.room) if entry.room else None

    def _schedule_tone(self, node_id: str, kind: str) -> bytes | None:
        """The lead-in tone for a timer/alarm announcement, per the node's
        ``sound_timer``/``sound_alarm`` config (read live at fire time — no
        restart to change it). Empty/null ⇒ voice only. Reminders have no tone."""
        key_default = _SCHEDULE_TONE_KEYS.get(kind)
        if key_default is None:
            return None
        key, default = key_default
        try:
            spec = self._effective_node_config(node_id).get(key, default)
        except Exception:
            spec = default
        from . import tones

        return tones.load_tone(spec)

    async def _deliver_schedule(self, node_id: str, room: str, text: str, kind: str) -> None:
        """Speak a schedule announcement, prepending the kind's tone when set.

        The tone and voice ride one PCM stream (gapless), and the tone still
        plays when TTS synthesis fails — an alarm must not depend on the TTS
        service being healthy to make noise.
        """
        tone = self._schedule_tone(node_id, kind)
        if tone is None:
            await self._say(node_id, room, text)
            return
        pcm = await self._synthesize(text, _INTERCOM_VOICE_PROMPT)
        await self._stream_pcm(node_id, tone + (pcm or b""))

    async def _fire_schedule(self, entry: Any) -> None:
        """Scheduler callback — must return quickly, so delivery is a task."""
        node_id = self._schedule_target(entry)
        if node_id is None:
            log.warning(
                "[%s] %s %r fired but no node is connected for the room — missed",
                entry.room or entry.node_id,
                entry.kind,
                entry.label,
            )
            return
        room = self._nodes[node_id].room_id if node_id in self._nodes else entry.room
        if entry.kind == "alarm":
            if not self._node_aec(node_id):
                # Half-duplex room: a ring loop can't be voice-stopped (the wake
                # word is the only off-switch, and it can't be heard over the
                # ringing) — degrade to a single timer-style delivery. A silent
                # miss would be the worst failure an alarm can have.
                hhmm = entry.at or time.strftime("%H:%M")
                h, m = int(hhmm[:2]), int(hhmm[3:])
                spoken = f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"
                asyncio.create_task(
                    self._deliver_schedule(
                        node_id, room, f"It's {spoken}. This is your alarm.", "alarm"
                    ),
                    name=f"alarm-once-{node_id}",
                )
                return
            old = self._ring_tasks.pop(node_id, None)
            if old is not None:
                old.cancel()
            self._ring_tasks[node_id] = asyncio.create_task(
                self._ring_alarm(node_id, room, entry.at), name=f"ring-{node_id}"
            )
        elif entry.kind == "command":
            asyncio.create_task(
                self._run_scheduled_command(node_id, room, entry.label, entry.speaker),
                name=f"cmd-{node_id}",
            )
        elif entry.kind == "reminder":
            label = entry.label
            if label and not re.match(r"^(?:to|that|about)\b", label):
                label = f"to {label}"
            text = f"You asked me to remind you {label}." if label else "This is your reminder."
            asyncio.create_task(
                self._deliver_schedule(node_id, room, text, "reminder"),
                name=f"remind-{node_id}",
            )
        else:  # timer
            name = f"{entry.label} timer" if entry.label else "timer"
            asyncio.create_task(
                self._deliver_schedule(node_id, room, f"Your {name} is done.", "timer"),
                name=f"timer-{node_id}",
            )

    async def _run_scheduled_command(
        self, node_id: str, room: str, command: str, speaker: str
    ) -> None:
        """Replay a deferred voice command through the normal intent pipeline.

        "Turn on the lights in 30 seconds" fires as if "turn on the lights" had
        just been spoken in that room: same fast path / LLM, same actions, same
        spoken confirmation. The set-time speaker identity rides along so
        speaker-gated skills see the voice that authorized it.
        """
        if not self._llm_url:
            log.warning("[%s] scheduled command %r fired but no LLM is configured", room, command)
            return
        try:
            r = await self._call_llm(
                command, room, str(uuid.uuid4()), speaker or None, node_id=node_id
            )
            if r.continuation:
                # A scheduled replay has nobody standing by to answer.
                await self._cancel_continuation_now(r.continuation, "scheduled command")
            response_text, voice_prompt, actions = r.text, r.voice_prompt, r.actions
            if response_text:
                await self._run_tts(node_id, room, str(uuid.uuid4()), response_text, voice_prompt)
            if actions:
                await self._dispatch_actions(actions, node_id, room, speaker or None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("[%s] scheduled command %r failed: %s", room, command, exc, exc_info=True)
            await self._say(node_id, room, "Sorry — I couldn't run your scheduled command.")

    async def _ring_alarm(self, node_id: str, room: str, at: str) -> None:
        """Repeat the alarm tone + announcement until acknowledged (wake word),
        the node drops, or the repeat cap — an alarm you can sleep through isn't
        one."""
        try:
            hhmm = at or time.strftime("%H:%M")
            h, m = int(hhmm[:2]), int(hhmm[3:])
            spoken = f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"
            for _ in range(self._alarm_ring_repeats):
                if node_id not in self._nodes:
                    return
                await self._deliver_schedule(
                    node_id, room, f"It's {spoken}. This is your alarm.", "alarm"
                )
                await asyncio.sleep(self._alarm_ring_interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[%s] alarm ring failed: %s", node_id, exc)
        finally:
            self._ring_tasks.pop(node_id, None)

    def _stop_ringing(self, node_id: str) -> None:
        task = self._ring_tasks.pop(node_id, None)
        if task is not None:
            task.cancel()
            log.info("[%s] alarm acknowledged", node_id)

    async def serve(self) -> None:
        self._scheduler.start()
        try:
            await super().serve()
        finally:
            self._scheduler.stop()

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

    async def start_enrollment(
        self,
        node_id: str,
        room: str,
        name: str,
        *,
        operator: bool = False,
        person_id: str | None = None,
    ) -> None:
        """Dashboard-initiated voice enrollment (4.2: the conversation itself
        lives in the enrollment SKILL, driven by ask_audio). This entry sends a
        deterministic internal directive through the normal pipeline; the
        skill's fast intent matches it and runs the prompt/sample loop, with
        person adoption riding back as an ``adopt_voice`` action.

        ``operator=True`` (the dashboard) bypasses the ``allow_voice_enroll``
        earshot gate — the request is already authenticated + controls-gated.
        """
        if not self._speaker_url and not self._announced_services.get("speaker"):
            await self._say(
                node_id, room, "Speaker identification isn't set up, so I can't enroll."
            )
            return
        directive = (
            f"[[enroll]] operator={1 if operator else 0} "
            f"person={person_id or ''} name={name.strip()}"
        )
        reply = await self._call_llm(
            directive, room, str(uuid.uuid4()), None, node_id=node_id, identity=None
        )
        await self._deliver_reply(node_id, room, None, reply)

    def _cleanup_on_disconnect(self, node_id: str) -> None:
        self._end_calib_session(node_id)
        self._abandon_pending_ask(node_id, "node disconnected")
        # An ORIGIN that vanished mid-cross-ask: nobody is left to hear the
        # outcome — cancel the continuation outright.
        for asked, entry in list(self._pending_ask.items()):
            if entry.get("origin_node") == node_id and asked != node_id:
                self._pending_ask.pop(asked, None)
                self._cancel_by_id(entry["id"], "asker disconnected")
        self._followup_turns.pop(node_id, None)
        task = self._ring_tasks.pop(node_id, None)
        if task is not None:
            task.cancel()

    def _calib_saw_wake(self, node_id: str) -> None:
        """Resolve a pending calibration Verify: a real wake word was heard."""
        sess = self._calib_sessions.get(node_id)
        if sess is not None:
            ev = sess.get("verify")
            if ev is not None and not ev.is_set():
                ev.set()

    def _on_calib_sample(self, node_id: str, sample: dict[str, Any]) -> None:
        """Tune-listener: route per-frame measurements into an active session."""
        sess = self._calib_sessions.get(node_id)
        if sess is None or sample.get("stopped"):
            return
        sess["samples"] += 1
        phase = sess.get("phase")
        rms = float(sample.get("rms") or 0.0)
        if phase == "echo":  # mic residual while the node plays the probe signal
            sess["echo"].append(rms)
        elif phase == "quiet":
            sess["quiet"].append(rms)
        elif phase == "wake":
            wake = float(sample.get("wake") or 0.0)
            sess["wake"].append(wake)
            sess["vad"].append(float(sample.get("vad") or 0.0))
            if rms > sess["gate"]:
                sess["speech"].append(rms)
            now = time.monotonic()
            if wake >= calibration.WAKE_PEAK and now - sess["last_peak"] > _CALIB_PEAK_REFRACTORY_S:
                sess["last_peak"] = now
                sess["peaks"] += 1
                self._calib_emit(node_id, sess, stage="wake_heard", count=sess["peaks"])

    def _calib_emit(self, node_id: str, sess: dict[str, Any], **event: Any) -> None:
        """Emit one calibration progress event (the dashboard renders these live —
        including for voice-initiated runs)."""
        self._notify_calib(node_id, {"mode": sess["mode"], **event})

    def _end_calib_session(self, node_id: str, *, force: bool = False) -> None:
        sess = self._calib_sessions.get(node_id)
        if sess is None:
            return
        if sess.get("expect_restart") and not force:
            return  # the node re-exec mid-flow is planned — the session survives it
        self._calib_sessions.pop(node_id, None)
        task = sess.get("task")
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def start_calibration(
        self, node_id: str, room: str, *, mode: str = "spoken"
    ) -> str | None:
        """Begin a guided calibration session; returns an error string or None."""
        if node_id not in self._nodes:
            return "node not connected"
        if node_id in self._calib_sessions:
            return "calibration already running"
        pa = self._pending_ask.get(node_id)
        if pa is not None and pa.get("capture") == "audio":
            return "enrollment in progress on this node"
        sess: dict[str, Any] = {
            "quiet": [],
            "wake": [],
            "vad": [],
            "speech": [],
            "echo": [],
            "samples": 0,
            "phase": None,
            "gate": 0.0,
            "peaks": 0,
            "last_peak": 0.0,
            "room": room,
            "mode": mode,
        }
        self._calib_sessions[node_id] = sess
        sess["task"] = asyncio.create_task(
            self._run_calibration(node_id, room), name=f"calibrate-{node_id}"
        )
        return None

    async def _calib_say(self, node_id: str, room: str, text: str) -> float | None:
        """Speak a prompt and wait out its PLAYBACK (streaming completes before the
        node finishes playing — the next phase must not start mid-prompt). Returns
        the spoken duration, or None when synthesis failed (a spoken flow can't
        run without a voice)."""
        pcm = await self._synthesize(text, _CALIB_VOICE_PROMPT)
        if not pcm:
            log.warning("[%s] calibration prompt TTS failed", node_id)
            return None
        await self._stream_pcm(node_id, pcm)
        duration = len(pcm) / 2 / 24000
        await asyncio.sleep(duration + _CALIB_SAY_MARGIN)
        return duration

    def _calib_beep(self) -> bytes | None:
        """The silent-mode probe signal: a bundled chime tiled to a deterministic
        couple of seconds. No TTS involved, works on a fully-local install."""
        from kenzy.server import tones

        pcm = tones.load_tone("ready.wav")
        if not pcm:
            return None
        need = int(_CALIB_PROBE_BEEP_S * 24000) * 2  # bytes of 24 kHz mono int16
        reps = -(-need // len(pcm))
        return (pcm * reps)[:need]

    def _calib_probe_allowed(self, node_id: str) -> bool:
        """A muted or near-silent speaker looks exactly like perfect AEC — skip
        the probe (and leave the flag alone) rather than misclassify."""
        try:
            if bool(self._transient_node_cfg.get(node_id, {}).get("muted")):
                return False
            vol = self._effective_node_config(node_id).get("volume", 100)
            return int(vol) >= _CALIB_VOLUME_FLOOR
        except Exception:
            return True

    async def _calib_open_window(self, node_id: str, seconds: float) -> bool:
        """Open one tune window and wait until samples actually flow. The node
        accepts tune_start only when idle — right after a prompt it may still be
        finishing TTS teardown, so re-request until the first sample arrives."""
        sess = self._calib_sessions.get(node_id)
        if sess is None:
            return False
        loop = asyncio.get_running_loop()
        for _ in range(3):
            if not await self.start_node_tuning(node_id, seconds):
                break
            baseline = sess["samples"]
            t0 = loop.time()
            while loop.time() - t0 < 2.5:
                if sess["samples"] > baseline:
                    return True
                await asyncio.sleep(0.15)
            await asyncio.sleep(0.7)
        return False

    def _calib_apply(self, node_id: str, patch: dict[str, Any]) -> None:
        """Merge calibration results into the node's override (other keys kept)."""
        existing = {
            k: v for k, v in self.read_node_override(node_id).items() if k in _ALLOWED_OVERRIDE_KEYS
        }
        self.write_node_override(node_id, {**existing, **patch})

    async def _calib_wait_reconnect(self, node_id: str) -> bool:
        """Wait out the node's planned re-exec: gone, then back."""
        loop = asyncio.get_running_loop()
        saw_drop = False
        t0 = loop.time()
        while loop.time() - t0 < _CALIB_RECONNECT_S:
            connected = node_id in self._nodes
            if not connected:
                saw_drop = True
            elif saw_drop:
                return True
            await asyncio.sleep(0.3)
        return False

    async def _calib_wait_exchange(self, node_id: str) -> None:
        """Wait out the full user↔Kenzy exchange the verify wake started: the
        capture session (``streaming``), the pipeline task (``_stt_tasks``), and
        the reply's TTS streaming (``_tts_active``) — plus a playback margin,
        since the node keeps playing after streaming to it ends."""
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await asyncio.sleep(min(0.5, _CALIB_CLOSE_MARGIN_S))  # let the pipeline register
        while loop.time() - t0 < 30.0:
            s = self._nodes.get(node_id)
            busy = (
                (s is not None and s.streaming)
                or node_id in self._stt_tasks
                or node_id in self._tts_active
            )
            if not busy:
                break
            await asyncio.sleep(0.3)
        await asyncio.sleep(_CALIB_CLOSE_MARGIN_S)

    async def _calib_wake_phase(self, node_id: str, sess: dict[str, Any], seconds: float) -> bool:
        """One wake-phase window: ends early once enough attempts were heard."""
        if not await self._calib_open_window(node_id, seconds + 8):
            return False
        sess["phase"] = "wake"
        self._calib_emit(
            node_id, sess, stage="wake", seconds=seconds, target=calibration.WAKE_TARGET
        )
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        while loop.time() < end:
            if sess["peaks"] >= calibration.WAKE_TARGET:
                await asyncio.sleep(0.8)  # tail: let the last utterance's frames land
                break
            await asyncio.sleep(0.2)
        sess["phase"] = None
        await self.stop_node_tuning(node_id)
        return True

    async def _run_calibration(self, node_id: str, room: str) -> None:
        sess = self._calib_sessions[node_id]
        mode = sess["mode"]

        async def say(text: str) -> bool:
            """A prompt: spoken via TTS in spoken mode, event-only in silent mode
            (the dashboard renders it). Emitted in both modes so the dashboard
            can watch a voice-initiated run too."""
            self._calib_emit(node_id, sess, stage="prompt", text=text)
            if mode != "spoken":
                return True
            return await self._calib_say(node_id, room, text) is not None

        def fail(summary: str) -> None:
            self._calib_emit(node_id, sess, stage="done", ok=False, summary=summary)

        try:
            async with asyncio.timeout(300):
                self._calib_emit(node_id, sess, stage="start")
                intro = (
                    "Let's calibrate my hearing for this room. "
                    "First, stay quiet for about six seconds."
                )
                # One window spans the AEC probe and the quiet phase.
                if not await self._calib_open_window(node_id, 60):
                    await say("I couldn't take measurements just now — please try again later.")
                    fail("no measurements — node busy or telemetry unavailable")
                    return

                # AEC probe: play a known signal through the node's own speaker and
                # read the mic residual. Spoken mode: the intro sentence IS the
                # signal. Silent mode: a beep (the browser shows the intro text).
                probe_ok = self._calib_probe_allowed(node_id)
                self._calib_emit(node_id, sess, stage="prompt", text=intro)
                pcm: bytes | None
                if mode == "spoken":
                    pcm = await self._synthesize(intro, _CALIB_VOICE_PROMPT)
                    if not pcm:
                        log.warning("[%s] calibration prompt TTS failed", node_id)
                        fail("TTS unavailable for spoken calibration")
                        return
                else:
                    pcm = self._calib_beep() if probe_ok else None
                if pcm is not None:
                    duration = len(pcm) / 2 / 24000
                    await self._stream_pcm(node_id, pcm)
                    if probe_ok and duration >= _CALIB_PROBE_MIN_S:
                        # Tag only mid-playback frames — playback lags streaming.
                        await asyncio.sleep(_CALIB_PROBE_LEAD_S)
                        sess["phase"] = "echo"
                        await asyncio.sleep(
                            max(0.0, duration - _CALIB_PROBE_LEAD_S - _CALIB_PROBE_TAIL_S)
                        )
                        sess["phase"] = None
                        await asyncio.sleep(_CALIB_PROBE_TAIL_S + _CALIB_SAY_MARGIN)
                    else:
                        await asyncio.sleep(duration + _CALIB_SAY_MARGIN)
                if not probe_ok:
                    self._calib_emit(
                        node_id,
                        sess,
                        stage="note",
                        text="Muted or very low volume — skipping the echo check.",
                    )

                # Phase 1: the quiet floor (one retry if a burst poisons it).
                for attempt in (1, 2):
                    sess["quiet"].clear()
                    sess["phase"] = "quiet"
                    self._calib_emit(
                        node_id, sess, stage="quiet", seconds=calibration.QUIET_SECONDS
                    )
                    await asyncio.sleep(calibration.QUIET_SECONDS)
                    sess["phase"] = None
                    if calibration.quiet_phase_bursty(sess["quiet"]) and attempt == 1:
                        if not await say(
                            "I heard a noise — let's try that once more. Quiet, please."
                        ):
                            fail("TTS failed mid-flow")
                            return
                        continue
                    break
                await self.stop_node_tuning(node_id)
                if not sess["quiet"]:
                    await say("I couldn't take measurements just now — please try again later.")
                    fail("no samples in the quiet phase")
                    return
                sess["gate"] = calibration.speech_gate(sess["quiet"])

                # AEC verdict — applied IMMEDIATELY, so the wake phase, verify, and
                # everything after run under the correct duplex semantics.
                aec = calibration.aec_verdict(sess["quiet"], sess["echo"])
                current_aec = bool(self._effective_node_config(node_id).get("hardware_aec", True))
                # Every verdict is logged — a probe that silently decides (or
                # silently declines to) is undebuggable from the journal, which
                # is exactly how the M1A misdetection cost a day.
                log.info(
                    "[%s] AEC probe verdict: %s (current hardware_aec=%s, quiet=%d echo=%d frames)",
                    node_id,
                    aec,
                    current_aec,
                    len(sess["quiet"]),
                    len(sess["echo"]),
                )
                if aec is not None and aec != current_aec:
                    self._calib_apply(node_id, {"hardware_aec": aec})
                    await self.push_config(node_id)
                    self._calib_emit(node_id, sess, stage="aec", aec=aec, changed=True)
                    log.info("[%s] AEC probe: hardware_aec %s -> %s", node_id, current_aec, aec)
                    consequence = (
                        "By the way — I can hear you even while I'm talking, so feel "
                        "free to interrupt me mid-sentence."
                        if aec
                        else "By the way — I can't hear anything while I'm talking, so "
                        "wait for me to finish speaking before you reply."
                    )
                    if not await say(consequence):
                        fail("TTS failed mid-flow")
                        return
                elif aec is not None:
                    self._calib_emit(node_id, sess, stage="aec", aec=aec, changed=False)
                else:
                    # Ambiguous probe: say so — the wizard showing nothing here
                    # read as "detected absent" to the operator.
                    self._calib_emit(node_id, sess, stage="aec", aec=None, changed=False)

                # Phase 2: the wake word ×N doubles as the speech-level sample.
                if not await say(
                    f"Now say 'Hey Kenzy' {calibration.WAKE_TARGET} times, with a "
                    "short pause between each, from where you'd normally speak."
                ):
                    fail("TTS failed mid-flow")
                    return
                await self._calib_wake_phase(node_id, sess, _CALIB_WAKE_WINDOW_S)
                if sess["peaks"] < 2 or len(sess["speech"]) < calibration.MIN_SPEECH_FRAMES:
                    if not await say(
                        "I didn't quite hear that — a few more times, a little louder."
                    ):
                        fail("TTS failed mid-flow")
                        return
                    await self._calib_wake_phase(node_id, sess, _CALIB_WAKE_EXTEND_S)

                # Compute (shared math) and apply what separated cleanly.
                enough = len(sess["speech"]) >= calibration.MIN_SPEECH_FRAMES
                speech = sess["speech"] if enough else []
                sil = calibration.suggest_silence(sess["quiet"], speech)
                wk = calibration.suggest_wake(sess["wake"])
                vd = calibration.suggest_vad(sess["vad"])
                verdict = calibration.separation_verdict(sess["quiet"], speech)
                if calibration.agc_suspected(sess["quiet"]):
                    # The floor moved during the quiet phase — device AGC. Say
                    # so: the operator seeing a lower-than-expected threshold
                    # (or a skipped VAD gate) should know it was deliberate.
                    log.info(
                        "[%s] quiet floor drifted during calibration (device AGC "
                        "suspected) — using conservative suggestions",
                        node_id,
                    )
                    self._calib_emit(
                        node_id,
                        sess,
                        stage="note",
                        text="This microphone adjusts its own gain — using "
                        "conservative thresholds.",
                    )
                patch: dict[str, Any] = {}
                kept: list[str] = []
                for key, value in (
                    ("silence_rms_threshold", sil),
                    ("wakeword_threshold", wk),
                    ("wakeword_vad_threshold", vd),
                ):
                    if value is not None:
                        patch[key] = value
                    else:
                        kept.append(key)
                self._calib_emit(
                    node_id, sess, stage="applied", patch=patch, kept=kept, verdict=verdict
                )
                if not patch:
                    msg = (
                        "I couldn't get a clean measurement, so I've left your settings unchanged."
                    )
                    if verdict == "poor":
                        msg += (
                            " The room noise and your voice sound too similar from here — "
                            "moving my microphone closer to where you talk would help."
                        )
                    await say(msg)
                    fail("nothing safe to apply")
                    return
                self._calib_apply(node_id, patch)
                await self.push_config(node_id)  # live keys apply immediately
                log.info("[%s] calibration applied: %s (separation=%s)", node_id, patch, verdict)
                summary = "I've tuned my hearing to this room."
                if verdict == "marginal":
                    summary += " The room is a bit noisy, but it should work."
                elif verdict == "poor":
                    summary += " Fair warning: this room is loud, so I may still mishear."
                if not await say(summary):
                    fail("TTS failed mid-flow")
                    return

                # The VAD gate is a boot key — restart to apply, then verify live.
                if "wakeword_vad_threshold" in patch:
                    if not await say(
                        "I'll restart my listener to finish up — give me a few seconds."
                    ):
                        fail("TTS failed mid-flow")
                        return
                    sess["expect_restart"] = True
                    self._calib_emit(node_id, sess, stage="restarting")
                    await self.restart_node(node_id)
                    reconnected = await self._calib_wait_reconnect(node_id)
                    sess["expect_restart"] = False
                    if not reconnected:
                        fail("node did not reconnect after restart")
                        return
                    await asyncio.sleep(1.0)  # let audio init settle

                # Verify: a REAL wake-word round trip, with bounded auto-nudges.
                # (The "never mind" closes the session she opens gracefully.)
                verify_ok = False
                nudges = 0
                sess["verify"] = asyncio.Event()
                if not await say("Let's test it — say 'Hey Kenzy', then 'never mind'."):
                    fail("TTS failed mid-flow")
                    return
                while True:
                    self._calib_emit(node_id, sess, stage="verify", nudges=nudges)
                    try:
                        await asyncio.wait_for(sess["verify"].wait(), _CALIB_VERIFY_S)
                        verify_ok = True
                        break
                    except TimeoutError:
                        cur = self._effective_node_config(node_id).get("wakeword_threshold")
                        if nudges >= _CALIB_MAX_NUDGES or not isinstance(cur, (int, float)):
                            break
                        lower = round(max(0.2, float(cur) - 0.07), 2)
                        self._calib_apply(node_id, {"wakeword_threshold": lower})
                        await self.push_config(node_id)
                        nudges += 1
                        sess["verify"] = asyncio.Event()
                        self._calib_emit(
                            node_id,
                            sess,
                            stage="note",
                            text=f"No wake heard — lowered the threshold to {lower}.",
                        )
                        if not await say("I didn't catch it — once more?"):
                            fail("TTS failed mid-flow")
                            return
                sess.pop("verify", None)
                self._calib_emit(node_id, sess, stage="verify_result", ok=verify_ok, nudges=nudges)
                if verify_ok:
                    # The verify wake opened a REAL session, and the user's
                    # "never mind" runs the whole pipeline — she answers it.
                    # Wait out that entire exchange (capture → pipeline → her
                    # reply's playback) before the closing line, or the two
                    # replies fight over the speaker.
                    await self._calib_wait_exchange(node_id)
                    await say("Perfect — you're all set.")
                else:
                    await say(
                        "I couldn't hear the wake word — check the microphone placement, "
                        "or run the audio setup from the dashboard."
                    )
                self._calib_emit(
                    node_id, sess, stage="done", ok=True, verify=verify_ok, verdict=verdict
                )
        except TimeoutError:
            log.info("[%s] calibration timed out", node_id)
            self._calib_emit(node_id, sess, stage="done", ok=False, summary="timed out")
            await self.stop_node_tuning(node_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("[%s] calibration failed", node_id, exc_info=True)
            self._calib_emit(node_id, sess, stage="done", ok=False, summary="internal error")
        finally:
            sess["task"] = None  # don't self-cancel from _end_calib_session
            sess["expect_restart"] = False
            self._calib_sessions.pop(node_id, None)

    async def _action_connect_call(self, caller_id: str, caller_room: str, room: str) -> None:
        """Resolve + re-check the target, then bridge (consent already given
        via the skill's cross-room ask)."""
        receiver_id = self._resolve_room_node(room, exclude=caller_id)
        receiver = self._nodes.get(receiver_id) if receiver_id else None
        if receiver_id is None or receiver is None:
            await self._say(caller_id, caller_room, f"I couldn't reach the {room}.")
            return
        if not self._node_aec(caller_id) or not self._node_aec(receiver_id):
            which = caller_room if not self._node_aec(caller_id) else receiver.room_id
            await self._say(
                caller_id,
                caller_room,
                f"Live calls need an echo-cancelling speaker, and the {which} doesn't have one.",
            )
            return
        caller = self._nodes.get(caller_id)
        if receiver.intercom_peer is not None or (
            caller is not None and caller.intercom_peer is not None
        ):
            await self._say(caller_id, caller_room, f"The {receiver.room_id} is busy.")
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
        self,
        node_id: str,
        room_name: str,
        session_id: str | None,
        text: str,
        voice_prompt: str,
        *,
        sensitive: bool = False,
    ) -> bool:
        """Synthesize + stream a reply. Returns False when synthesis/streaming
        *failed* (so the caller can play the error cue); a deliberately TTS-less
        config returns True — silence by choice isn't a failure."""
        if not self._tts_url:
            return True

        import httpx  # type: ignore[import-untyped]

        sid = session_id or str(uuid.uuid4())
        await self.send_tts_start(node_id, sid, sample_rate=24000, channels=1)
        try:
            async with httpx.AsyncClient(verify=tlsutil.httpx_verify()) as client:
                resp = await client.post(
                    self._tts_url,
                    json={
                        "text": text,
                        "voice_prompt": voice_prompt,
                        "room_id": room_name,
                        # Lockbox replies: the TTS service withholds its speak log.
                        "sensitive": sensitive,
                    },
                    timeout=self._tts_timeout,
                    headers=self._service_headers("POST", self._tts_url),
                )
                resp.raise_for_status()
            pcm = resp.content
            for i in range(0, len(pcm), self._tts_chunk_size):
                if not await self.send_tts_frame(node_id, pcm[i : i + self._tts_chunk_size]):
                    return False  # node disconnected mid-stream
            await self.send_tts_end(node_id, sid)
            log.info("[%s] TTS complete", node_id)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("[%s] TTS error: %s", node_id, exc, exc_info=True)
            await self.send_tts_end(node_id, sid)
            return False
        finally:
            if node_id in self._tts_active:
                await self.stop_node(node_id)
                self._tts_active.discard(node_id)

    # ------------------------------------------------------------------
    # Announcements: synth once, play on every (or selected) room
    # ------------------------------------------------------------------

    async def _synthesize(
        self, text: str, voice_prompt: str, *, sensitive: bool = False
    ) -> bytes | None:
        if not self._tts_url:
            return None
        import httpx

        try:
            async with httpx.AsyncClient(verify=tlsutil.httpx_verify()) as client:
                resp = await client.post(
                    self._tts_url,
                    json={
                        "text": text,
                        "voice_prompt": voice_prompt,
                        "room_id": "announce",
                        # Lockbox replies: the TTS service withholds its speak log.
                        "sensitive": sensitive,
                    },
                    timeout=self._tts_timeout,
                    headers=self._service_headers("POST", self._tts_url),
                )
                resp.raise_for_status()
            return resp.content
        except Exception as exc:
            log.error("TTS synth failed: %s", exc)
            return None

    async def _stream_pcm(
        self, node_id: str, pcm: bytes, *, alert: bool = False, cue: bool = False
    ) -> None:
        sid = str(uuid.uuid4())
        if not await self.send_tts_start(
            node_id, sid, sample_rate=24000, channels=1, alert=alert, cue=cue
        ):
            return
        for i in range(0, len(pcm), self._tts_chunk_size):
            if not await self.send_tts_frame(node_id, pcm[i : i + self._tts_chunk_size]):
                return
        await self.send_tts_end(node_id, sid)

    def _chime_spec(self, name: str) -> str | None:
        """Resolve a sound NAME to a tone spec: a configured ``chimes:`` alias,
        a file inside the operator's ``sounds.dirs`` library roots (relative
        subpaths fine, traversal/absolute rejected — resolve_sound is the
        boundary), or a bare bundled filename. Callers never get to point at
        arbitrary files on the server host."""
        from kenzy.server import tones

        name = name.strip()
        if not name:
            return None
        if name in self._chimes:
            return self._chimes[name]
        library = tones.resolve_sound(name, self._sound_roots)
        if library is not None:
            return str(library)
        from pathlib import PurePath

        if PurePath(name).name == name and not name.startswith("."):
            return name  # bare filename → tones resolves it in the bundled dir
        return None

    async def play_chime(
        self,
        sound: str | None = None,
        seconds: float = 0.0,
        rooms: list[str] | None = None,
        repeats: int = 0,
    ) -> int:
        """Play a named sound on every (or selected) node — the instant, TTS-free
        sibling of :meth:`announce` (the house-wide doorbell, the dog-bark MP3 on
        the NAS…). ``seconds`` loops the cue by duration; ``repeats`` by whole
        count — both capped. Alert audio: a muted node still plays it at the
        audible floor."""
        from kenzy.server import tones

        spec = self._chime_spec(str(sound or "") or _CHIME_DEFAULT)
        if spec is None:
            log.warning("chime refused: %r is not a configured, library, or bundled sound", sound)
            return 0
        pcm = tones.load_tone(spec)
        if not pcm:
            return 0
        try:
            loop_s = min(float(seconds or 0), _CHIME_MAX_S)
        except (TypeError, ValueError):
            loop_s = 0.0
        try:
            reps = max(0, int(repeats or 0))
        except (TypeError, ValueError):
            reps = 0
        if reps > 1:
            pcm = tones.repeat_pcm(pcm, reps, max_seconds=_CHIME_MAX_S * 2)
        elif loop_s > 0:
            pcm = tones.tile_pcm(pcm, loop_s)
        async with self._lock:
            if rooms:
                wanted = {str(r).strip().lower() for r in rooms}
                targets = [nid for nid, s in self._nodes.items() if s.room_id.lower() in wanted]
            else:
                targets = list(self._nodes)
        if not targets:
            return 0
        await asyncio.gather(
            *(self._stream_pcm(nid, pcm, alert=True) for nid in targets),
            return_exceptions=True,
        )
        log.info("Chime %r to %d node(s) (%.1fs)", spec, len(targets), len(pcm) / 2 / 24000)
        return len(targets)

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
    server._config_path = str(config_path)  # lets the voice off-switch persist

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
            properties={
                "version": version,
                "auth": auth,
                # Nodes build wss:// from this flag, so discovery keeps working
                # when the server terminates TLS.
                "tls": "1" if server._ssl is not None else "0",
            },
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
            elif cmd.action == "chime":
                v = cmd.value if isinstance(cmd.value, dict) else {}
                rooms = v.get("rooms")
                await server.play_chime(
                    v.get("sound"),
                    v.get("seconds") or 0,
                    [str(r) for r in rooms] if isinstance(rooms, list) else None,
                    repeats=int(v.get("repeats") or 0),
                )

        _hub = IntegrationHub()
        _dispatch = _dispatch_command if _mcfg.commands else None
        mqtt_transport = MqttTransport(_mcfg, dispatch=_dispatch)
        _hub.subscribe(mqtt_transport.submit)
        attach_to_server(_hub, server)
        server._integrations = _hub  # plugins publish to HA through the hub (5.1)

    # v5 occupancy spine: the HA event socket + the tracker. Default ON, but
    # only ever starts when HA is actually configured — the server holds BOTH
    # credentials already (HA_API_KEY in its own .env, the URL in its central
    # store), so nothing is fetched to decide this and nothing new is configured.
    ha_url = str(
        (
            (server._effective_service_config("llm").get("skills", {}) or {}).get(
                "home_assistant", {}
            )
            or {}
        ).get("url", "")
    ).strip()
    ha_token = os.environ.get("HA_API_KEY", "")
    occupancy_enabled = bool((cfg.get("occupancy", {}) or {}).get("enabled", True))
    if occupancy_enabled and ha_url and ha_token:
        from kenzy.server.ha_events import HaEventClient
        from kenzy.server.occupancy import OccupancyTracker

        tracker = OccupancyTracker()

        async def _fetch_map() -> dict[str, Any]:
            """Only the MAP crosses the service boundary — never the stream.

            Returns the whole envelope so the client can tell a switched-off
            integration from a broken one and park instead of retrying.
            """
            info = await server.llm_occupancy_map()
            return info if isinstance(info, dict) else {}

        # Tier A safety (5.0.6) rides the SAME socket: the hose already carries
        # every state change in the house, so this is a tee on it, not a second
        # connection. The gate is default-deny, so a fleet that never enables it
        # pays for one dict lookup per event and nothing else.
        from kenzy.server.proactive import ProactiveGate
        from kenzy.server.safety import SafetyWatcher

        gate = ProactiveGate.from_config(cfg.get("proactive") or {})
        watcher = SafetyWatcher(gate, on_decision=server._record_proactive)

        def _on_map_payload(payload: dict[str, Any]) -> None:
            watcher.set_map(payload.get("safety") or {})

        ha_client = HaEventClient(
            ha_url,
            ha_token,
            _fetch_map,
            on_map=tracker.prune_held,
            on_payload=_on_map_payload,
        )
        ha_client.subscribe(tracker.on_evidence)
        ha_client.subscribe_raw(server._on_safety_state)
        server._occupancy = tracker
        server._proactive = gate
        server._safety = watcher
        server._ha_events = ha_client
        log.info("Occupancy spine enabled (HA events → tracker); watch-only in 5.0")
        if "safety" in gate.categories_enabled:
            log.info("Tier A safety announcements ENABLED (proactive.safety.enabled)")
        else:
            log.info("Tier A safety announcements off (proactive.safety.enabled: false)")
    elif occupancy_enabled and not ha_token:
        log.info("Occupancy spine idle: Home Assistant is not configured")

    async def _main() -> None:
        coros: list[Any] = [server.serve()]
        if dashboard is not None:
            coros.append(dashboard.serve())
        if mqtt_transport is not None:
            coros.append(mqtt_transport.run())
        if server._ha_events is not None:
            server._ha_events.start()
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
