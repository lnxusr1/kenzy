"""
Shared WebSocket protocol constants and message helpers.

Control messages are JSON text frames; audio data is binary frames.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Message type constants
# ---------------------------------------------------------------------------

MSG_HELLO = "hello"
MSG_CONFIG = "config"
MSG_AUDIO_START = "audio_start"
MSG_AUDIO_END = "audio_end"
MSG_WAKEWORD = "wakeword"
MSG_WAKE_PENDING = "wake_pending"
MSG_TRIGGER = "trigger"
MSG_STOP = "stop"
MSG_ACK = "ack"
MSG_TTS_START = "tts_start"
MSG_TTS_END = "tts_end"
MSG_RESTART = "restart"
MSG_DISABLE = "disable"
MSG_UPGRADE = "upgrade"
MSG_SET_ROOM = "set_room"
MSG_REQUEST_LOGS = "request_logs"
MSG_LOGS = "logs"
MSG_STATUS = "status"  # node→server: report node health (e.g. audio init failed)
MSG_GOODBYE = "goodbye"  # node→server: I am going away on purpose (restart/upgrade/shutdown)
MSG_VOLUME_DELTA = "volume_delta"  # node→server: physical volume button (5.0.4; own node only)
MSG_METRICS = "metrics"  # node→server: periodic system metrics (cpu/ram/disk/temp)
MSG_TUNE_START = "tune_start"  # server→node: begin a bounded calibration window
MSG_TUNE_STOP = "tune_stop"  # server→node: end calibration early
MSG_TUNE_SAMPLE = "tune_sample"  # node→server: one calibration sample (rms/wake/vad)
MSG_EXPECT_UTTERANCE = "expect_utterance"  # server→node: capture one utterance after the next TTS
MSG_FOLLOWUP_TIMEOUT = "followup_timeout"  # node→server: held-floor reply window expired silently
MSG_TTS_DONE = "tts_done"  # node→server: TTS audio finished PLAYING (tts_end = finished arriving)
MSG_FORCE_WAKE = "force_wake"  # server→node (test/ops): behave as if the wake word fired NOW
MSG_CALL_RINGING = "call_ringing"  # server→caller: play the ringback loop while the callee is rung
MSG_END_DIALOG = "end_dialog"  # server→node: a multi-turn dialog ended — play the end cue
# Intercom (live two-way call between two rooms; gated by the receiver's consent).
MSG_PLUGIN_EVENT = "plugin_event"  # node↔server: a plugin's payload, routed by plugin id (5.1)
MSG_CALL_REQUEST = "call_request"  # server→node: ring the receiver (no audio yet)
MSG_CALL_CANCEL = "call_cancel"  # server→node: caller hung up before accept
MSG_INTERCOM_START = "intercom_start"  # server→node: consent accepted, begin the call
MSG_INTERCOM_END = "intercom_end"  # server↔node: end the call

# ---------------------------------------------------------------------------
# Audio format (shared by node and server)
# ---------------------------------------------------------------------------

SAMPLE_RATE: int = 16_000  # Hz
CHANNELS: int = 1  # mono
SAMPLE_WIDTH: int = 2  # bytes per sample (int16)
FRAME_MS: int = 80  # milliseconds per frame
FRAME_SAMPLES: int = SAMPLE_RATE * FRAME_MS // 1000  # 1 280 samples

# ---------------------------------------------------------------------------
# Message constructors
# ---------------------------------------------------------------------------


def hello(
    room_id: str,
    node_id: str | None = None,
    version: str = "1.0",
    capabilities: dict[str, Any] | None = None,
    token: str | None = None,
    kenzy_version: str | None = None,
    auth: dict[str, Any] | None = None,
) -> str:
    """Node→server registration.

    ``room_id`` is the human room *name* (sent to the backends as context).
    ``node_id`` is the node's stable primary identifier; when omitted the server
    falls back to using ``room_id`` as the key (legacy nodes). ``version`` is the
    wire-protocol version; ``kenzy_version`` is the installed package version (for
    the dashboard's per-host version view).
    """
    payload: dict[str, Any] = {"type": MSG_HELLO, "room_id": room_id, "version": version}
    if node_id is not None:
        payload["node_id"] = node_id
    if capabilities is not None:
        payload["capabilities"] = capabilities
    if token is not None:
        payload["token"] = token
    if auth is not None:
        payload["auth"] = auth  # 3.12+ token-proof join (raw token stays off the wire)
    if kenzy_version is not None:
        payload["kenzy_version"] = kenzy_version
    return json.dumps(payload)


def config(node_config: dict[str, Any]) -> str:
    """Server→node frame carrying the node's effective configuration."""
    return json.dumps({"type": MSG_CONFIG, "config": node_config})


def audio_start(
    session_id: str | None = None,
    room_id: str | None = None,
    wake_db: float | None = None,
    wake_margin_db: float | None = None,
    wake_score: float | None = None,
) -> tuple[str, str]:
    """Return (json_str, session_id).

    The ``wake_*`` fields ride only on wake-opened sessions — measured AT THE
    NODE, at wake time, from the pre-roll (the only audio that still contains
    the spoken phrase). ``wake_db`` is the phrase level in dBFS;
    ``wake_margin_db`` is the phrase's height above the same window's quiet
    floor — the gain-invariant quantity, since a device AGC moves both ends
    together; ``wake_score`` is the peak wake score. Together they are the
    comparable evidence a louder-wins arbiter needs for co-audible nodes.
    Server-side session audio can't provide any of this: a classic (paused)
    session's capture starts at the chime, after the phrase is gone."""
    sid = session_id or str(uuid.uuid4())
    payload: dict[str, Any] = {"type": MSG_AUDIO_START, "session_id": sid}
    if room_id is not None:
        payload["room_id"] = room_id
    if wake_db is not None:
        payload["wake_db"] = round(float(wake_db), 1)
    if wake_margin_db is not None:
        payload["wake_margin_db"] = round(float(wake_margin_db), 1)
    if wake_score is not None:
        payload["wake_score"] = round(float(wake_score), 3)
    return json.dumps(payload), sid


def audio_end(session_id: str, reason: str = "silence") -> str:
    return json.dumps({"type": MSG_AUDIO_END, "session_id": session_id, "reason": reason})


def wakeword(session_id: str | None, model: str, score: float) -> str:
    return json.dumps(
        {
            "type": MSG_WAKEWORD,
            "session_id": session_id,
            "model": model,
            "score": round(float(score), 4),
        }
    )


def wake_pending(
    session_id: str,
    model: str,
    score: float,
    wake_db: float | None = None,
    wake_margin_db: float | None = None,
) -> str:
    """Node→server, sent the instant an IDLE wake fires — while the one-breath
    gate still holds the ready chime. This is the arbitration hook: co-audible
    nodes all hear the phrase and all send this within ~150 ms; the server has
    the rest of the gate window (~400 ms) to pick the best-placed node and
    ``stop`` the losers BEFORE their chimes play or their pipelines run. The
    session may still open (audio_start follows) or may never (stopped, or the
    gate resolves otherwise) — this frame is evidence, not a session event.
    Unknown to older servers, which ignore unrecognized frame types."""
    payload: dict[str, Any] = {
        "type": MSG_WAKE_PENDING,
        "session_id": session_id,
        "model": model,
        "score": round(float(score), 4),
    }
    if wake_db is not None:
        payload["wake_db"] = round(float(wake_db), 1)
    if wake_margin_db is not None:
        payload["wake_margin_db"] = round(float(wake_margin_db), 1)
    return json.dumps(payload)


def trigger(session_id: str | None = None) -> str:
    return json.dumps({"type": MSG_TRIGGER, "session_id": session_id or str(uuid.uuid4())})


def stop() -> str:
    return json.dumps({"type": MSG_STOP})


def force_wake() -> str:
    """Server→node (test/ops): run the REAL idle-wake path as if openwakeword
    just fired — measure the actual pre-roll (true room audio at this node),
    announce it for arbitration with that genuine evidence, open the gate, and
    proceed. Unlike ``trigger`` (which opens a session *bypassing* the wake
    machinery), this exercises everything a real wake does, so scripted tests
    can force collisions and who-woke-where scenarios without staging
    acoustics. Ignored unless the node is idle with working audio."""
    return json.dumps({"type": MSG_FORCE_WAKE})


def restart() -> str:
    return json.dumps({"type": MSG_RESTART})


def disable() -> str:
    """Server → node: self-disable the node's systemd --user unit (stop AND
    stay stopped). Ignored with a log on non-systemd nodes."""
    return json.dumps({"type": MSG_DISABLE})


def upgrade(version: str | None = None) -> str:
    """Server→node: pip-upgrade ``kenzy[node]`` (to ``version`` or latest) and re-exec."""
    payload: dict[str, Any] = {"type": MSG_UPGRADE}
    if version is not None:
        payload["version"] = version
    return json.dumps(payload)


def set_room(room_id: str) -> str:
    """Server→node: set the node's room name (the node persists + applies it)."""
    return json.dumps({"type": MSG_SET_ROOM, "room_id": room_id})


def request_logs(request_id: str, level: str = "", limit: int = 200) -> str:
    return json.dumps(
        {"type": MSG_REQUEST_LOGS, "request_id": request_id, "level": level, "limit": limit}
    )


def node_logs(request_id: str, entries: list[dict[str, Any]]) -> str:
    return json.dumps({"type": MSG_LOGS, "request_id": request_id, "logs": entries})


def status(
    audio_ok: bool,
    audio_error: str | None = None,
    devices: list[dict[str, Any]] | None = None,
    media_keys: dict[str, Any] | None = None,
    mic_volume: dict[str, Any] | None = None,
) -> str:
    """Node→server health update: sent when audio init fails (so the node can be
    fixed/restarted remotely while staying connected), when the audio-device
    probe finishes (to deliver the device list for the dashboard picker), when
    the media-keys endpoint status changes (5.0.4 — present/absent/why, for the
    node page's status line), and when the managed capture gain is applied or
    refused (mic_volume — applied/why-not, same status line)."""
    payload: dict[str, Any] = {"type": MSG_STATUS, "audio_ok": audio_ok, "audio_error": audio_error}
    if devices is not None:
        payload["devices"] = devices
    if media_keys is not None:
        payload["media_keys"] = media_keys
    if mic_volume is not None:
        payload["mic_volume"] = mic_volume
    return json.dumps(payload)


def volume_delta(delta: int) -> str:
    """Node→server: a physical volume button was pressed (5.0.4).

    The FIRST node→server frame that requests a change to server-owned config —
    kept deliberately this narrow (a signed delta, nothing else) so the
    precedent stays as small as the feature. The node's connection is its
    identity: a node may only ever move its own volume. The server owns
    clamping, persistence and the config push (``set_node_volume``), which is
    the invariant that keeps hardware buttons from becoming a second volume
    system."""
    return json.dumps({"type": MSG_VOLUME_DELTA, "delta": int(delta)})


def goodbye(reason: str = "shutdown") -> str:
    """Node→server: this absence is deliberate.

    The server otherwise cannot tell "someone restarted me" from "my power went
    out" — and it must, because one deserves an alert and the other is Tuesday.
    Sent on SIGTERM/SIGINT (which is what ``systemctl restart``, ``kenzy-deploy``
    and a manual stop all deliver) and before a watchdog re-exec. Best-effort by
    nature: a node that dies without warning simply doesn't send one, which is
    exactly the signal the fleet view wants."""
    return json.dumps({"type": MSG_GOODBYE, "reason": reason})


def metrics(
    cpu: float | None = None,
    ram: float | None = None,
    disk: float | None = None,
    temp: float | None = None,
) -> str:
    """Node→server periodic system metrics (percentages / °C; None = unavailable
    on this platform). Shown on the dashboard's fleet card."""
    return json.dumps({"type": MSG_METRICS, "cpu": cpu, "ram": ram, "disk": disk, "temp": temp})


def tune_start(seconds: float = 20.0) -> str:
    return json.dumps({"type": MSG_TUNE_START, "seconds": seconds})


def tune_stop() -> str:
    return json.dumps({"type": MSG_TUNE_STOP})


def expect_utterance(cue: bool = True) -> str:
    """Arm one-shot capture after the next TTS prompt finishes playing.

    ``cue`` controls whether the node plays its ready chime when the capture
    window opens: True for record-after-the-tone flows (voice enrollment),
    False for conversational follow-ups (her question IS the cue — a beep on
    top reads as "wait, was I supposed to wait?"). Absent = True, so an older
    server keeps today's chime behavior on a newer node.
    """
    return json.dumps({"type": MSG_EXPECT_UTTERANCE, "cue": bool(cue)})


def followup_timeout() -> str:
    """Node→server: a held-floor reply window expired with no speech — the
    dialog is over (the server clears its turn counter). The node plays its own
    end-of-dialog cue locally ("I stopped waiting")."""
    return json.dumps({"type": MSG_FOLLOWUP_TIMEOUT})


def tts_done(session_id: str) -> str:
    """Node→server: playback of a reply has actually finished at the speaker.
    ``tts_end`` (server→node) only bounds the audio *stream*; buffered audio
    keeps playing after it. Stateful audio groups (Layer 1) hold the group's
    engagement in ``speaking`` until this arrives, so a wake elsewhere in the
    group can stop a reply during its playback tail. Additive: old servers
    ignore it, old nodes never send it (their engagements clear at dispatch —
    the pre-5.1.3 behavior)."""
    return json.dumps({"type": MSG_TTS_DONE, "session_id": session_id})


def call_ringing() -> str:
    """Server→caller: start the intercom ringback loop while the target room is
    rung and asked to accept. The caller node loops its ``sound_ringback`` until
    the call connects (intercom_start), is declined/times out (a spoken reply),
    or is cancelled — so the caller isn't left in silence during the wait."""
    return json.dumps({"type": MSG_CALL_RINGING})


def end_dialog() -> str:
    """Tell the node a multi-turn dialog just ended — play the end-of-dialog cue
    (after any in-progress TTS finishes)."""
    return json.dumps({"type": MSG_END_DIALOG})


def tune_sample(
    rms: float = 0.0, wake: float = 0.0, vad: float = 0.0, seq: int = 0, stopped: bool = False
) -> str:
    """One calibration measurement frame (or a final ``stopped`` marker)."""
    return json.dumps(
        {
            "type": MSG_TUNE_SAMPLE,
            "rms": rms,
            "wake": wake,
            "vad": vad,
            "seq": seq,
            "stopped": stopped,
        }
    )


def ack(session_id: str) -> str:
    return json.dumps({"type": MSG_ACK, "session_id": session_id})


def tts_start(
    session_id: str,
    sample_rate: int = 22050,
    channels: int = 1,
    alert: bool = False,
    stream: bool = False,
    cue: bool = False,
) -> str:
    """``alert=True`` marks alert audio (doorbell chimes): a muted node still
    plays it at the audible floor, like the wake-word ready chime. ``stream=True``
    (4.4) asks the node to play frames AS THEY ARRIVE (live ring-buffer playback)
    instead of collecting until ``tts_end`` — the sentence-streamed reply path.
    ``cue=True`` (4.4) marks a short processing acknowledgement ("Working on it."): the
    node mixes it OVER a looping waiting bed (bed ducks underneath and continues)
    instead of hard-cutting it; with no bed active it plays normally. Older
    nodes ignore the extra keys (the chime honors mute; a streamed reply plays
    whole at tts_end; a cue interrupts the bed — correct, just less polished)."""
    payload: dict[str, Any] = {
        "type": MSG_TTS_START,
        "session_id": session_id,
        "sample_rate": sample_rate,
        "channels": channels,
    }
    if alert:
        payload["alert"] = True
    if stream:
        payload["stream"] = True
    if cue:
        payload["cue"] = True
    return json.dumps(payload)


def tts_end(session_id: str) -> str:
    return json.dumps({"type": MSG_TTS_END, "session_id": session_id})


def plugin_event(plugin_id: str, payload: dict[str, Any]) -> str:
    """A plugin's own traffic (5.1). ONE generic frame for every plugin, routed
    by ``plugin_id`` to that plugin's other half — the core protocol never
    grows per-plugin message types. The payload schema is the plugin's own
    concern (and its own compatibility problem, versioned by the plugin API)."""
    return json.dumps({"type": MSG_PLUGIN_EVENT, "plugin": plugin_id, "payload": payload})


def call_request(from_room: str) -> str:
    """Server→node: ring the receiver for an intercom call. No audio is bridged yet."""
    return json.dumps({"type": MSG_CALL_REQUEST, "from_room": from_room})


def call_cancel() -> str:
    """Server→node: the caller cancelled before the receiver accepted."""
    return json.dumps({"type": MSG_CALL_CANCEL})


def intercom_start(peer_room: str, sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS) -> str:
    """Server→node: consent accepted — begin live two-way audio with the peer room."""
    return json.dumps(
        {
            "type": MSG_INTERCOM_START,
            "peer_room": peer_room,
            "sample_rate": sample_rate,
            "channels": channels,
        }
    )


def intercom_end(reason: str = "ended") -> str:
    """End an intercom call (server→node to tear down, or node→server on wake word)."""
    return json.dumps({"type": MSG_INTERCOM_END, "reason": reason})


def parse(raw: str | bytes) -> dict[str, Any]:
    return json.loads(raw)  # type: ignore[no-any-return]
