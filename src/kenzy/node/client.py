"""
Kenzy room node client.

Captures 16 kHz / 16-bit / mono audio from the default (or configured)
ALSA device, runs openwakeword for local activation, and streams raw PCM
frames to the Kenzy server over a persistent WebSocket.

State machine
-------------
  IDLE      – openwakeword runs continuously; no audio sent to server.
  STREAMING – raw PCM binary frames sent each tick until silence/hard-cap
              or a server STOP command is received.
  TTS       – server is streaming TTS audio back to the node for playback.

External triggers
-----------------
  Wake word detected  → IDLE → STREAMING  (self-initiated)
  Server TRIGGER msg  → IDLE → STREAMING  (server-initiated)
  Server TTS_START    → IDLE → TTS        (server-initiated)
  Server STOP msg     → STREAMING → IDLE
  Server STOP msg     → TTS → IDLE
  Server TTS_END msg  → TTS → IDLE
  Silence (400 ms)    → STREAMING → IDLE
  Hard cap (30 s)     → STREAMING → IDLE

Note: openwakeword runs on every frame in all states.  A wake-word detected
during TTS sends MSG_WAKEWORD to the server; the server decides whether to
send STOP (interrupting TTS) followed by TRIGGER (starting a new session).
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import queue
import re
import signal
import socket
import sys
import threading
import time
import uuid
import wave
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

import numpy as np
import sounddevice as sd  # type: ignore[import-untyped]
import websockets
import websockets.exceptions
from websockets.asyncio.client import ClientConnection

from kenzy import kenzy_version, protocol
from kenzy.features import probe_import
from kenzy.logutil import TRACE
from kenzy.plugins import PluginScan

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Silence / hard-cap thresholds (derived from protocol constants)
# ---------------------------------------------------------------------------


_STATE_IDLE = "idle"
_STATE_STREAMING = "streaming"
_STATE_TTS = "tts"
_STATE_INTERCOM = "intercom"  # live two-way call: stream mic out, play peer audio live

# Barge-in onset floor multiplier on silence_rms_threshold while a reply plays —
# the RMS fallback must clear AEC residual of Kenzy's own voice. Internal
# constant (not config): promoted to a key only on real-hardware evidence.
_BARGE_RMS_FACTOR = 2.5

# Grace window at the START of a floor-holding reply during which barge-in may
# BUFFER (for pre-roll) but never duck or confirm: AEC needs a beat to converge
# on the freshly-started reply audio, and until it does the residual of Kenzy's
# OWN voice reads as speech — a false barge that cuts the question off in its
# first frames (observed on the rig: a question clipped ~220ms in). You cannot
# meaningfully answer a question that has barely started, so suppressing the
# window costs nothing. Internal constant (bench-tunable, like _BARGE_RMS_FACTOR).
_BARGE_GRACE_S = 0.7

# Rate at which the server sends TTS PCM (fixed by the TTS service).
_TTS_SERVER_RATE = 24_000

# When muted, alert audio (the wake-word ready chime) still plays at this floor gain
# so the user can hear the device acknowledge a wake word and knowingly unmute.
_MUTED_ALERT_FLOOR = 0.4

# How long a reply will wait for a still-playing spoken cue to finish rather
# than cutting it mid-word. The answer can land within a cue's length of the 5s
# mark, and "Working o—" sounds broken. Sized just past the longest bundled cue
# (working.wav, 1.73s) so a whole cue can always finish; anything longer than
# this is not worth delaying the answer for. Internal constant.
_CUE_GRACE_MAX_S = 2.0

# Hard ceiling on looping one-shot playback (the waiting bed under a long
# request). The reply/error cue always replaces the bed on a live pipeline;
# this bound only matters when the server dies mid-request without a disconnect
# — the bed must never loop forever in an empty room. Internal constant.
_LOOP_MAX_S = 90.0

# How long one mDNS browse is given, and how much slack before we declare the
# browse itself wedged. discover_server's internal wait is bounded, but the
# zeroconf socket setup and close() around it are not — and a hang there used to
# park the whole reconnect loop with no log and no retry.
_DISCOVERY_TIMEOUT_S = 5.0
_DISCOVERY_GRACE_S = 5.0

# How often the reconnect watchdog checks in. Comfortably shorter than any
# threshold it enforces, and cheap enough to be invisible on a Pi.
_WATCHDOG_TICK_S = 30.0

# Wake-gate pre-roll (frames): long enough for the whole wake phrase plus
# openwakeword's detection lag, so a one-breath command loses nothing to the
# hit firing late. The captured phrase is stripped server-side as TEXT.
_WAKE_PREROLL_FRAMES = 13  # ~1.0s at 80ms frames
# After losing co-audible arbitration, ignore wake hits this long: the score
# tail of the phrase we just lost must not re-open a session (observed tail
# ~300 ms; 0.8 s clears it while staying snappy for a genuine new wake).
_ARB_REFRACTORY_S = 0.8

#: Where the last-known-good server URL is cached. A cache, deliberately not
#: config: `server_url` in node.yaml is an operator's authoritative choice, while
#: this is only ever a remembered observation, and mDNS still wins once it fails.
_SERVER_CACHE_NAME = "last_server"


def _server_cache_path() -> Path | None:
    from kenzy.config import kenzy_data_root

    try:
        return kenzy_data_root() / "data" / _SERVER_CACHE_NAME
    except Exception:  # pragma: no cover - no resolvable data root
        return None


def _read_cached_server() -> str | None:
    """The server URL this node last registered with, or None."""
    path = _server_cache_path()
    if path is None or not path.is_file():
        return None
    try:
        url = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return url if url.startswith(("ws://", "wss://")) else None


def _write_cached_server(url: str) -> None:
    """Remember a server URL that actually worked. Best-effort: a read-only data
    root costs us the cache, never the connection."""
    path = _server_cache_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(url + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.debug("could not cache server url: %s", exc)


def _describe_close(exc: BaseException) -> str:
    """Human-readable close code + reason from a websockets ConnectionClosed."""
    rcvd = getattr(exc, "rcvd", None)
    sent = getattr(exc, "sent", None)
    frame = rcvd if rcvd is not None else sent
    if frame is None:
        return str(exc) or exc.__class__.__name__
    side = "server" if rcvd is not None else "we"
    code = int(getattr(frame, "code", 0))
    reason = str(getattr(frame, "reason", "") or "")
    return f"{side} closed with {code}" + (f" ({reason})" if reason else "")


def _ago(stamp: float) -> str:
    """"3m ago" / "never" for a monotonic stamp, for human-facing log lines."""
    if not stamp:
        return "never"
    secs = int(max(0.0, time.monotonic() - stamp))
    if secs < 90:
        return f"{secs}s ago"
    if secs < 5400:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h ago"


def _volume_to_gain(value: Any) -> float:
    """Convert a 0–100 volume config value to a 0.0–1.0 gain (clamped)."""
    try:
        pct = float(value)
    except (TypeError, ValueError):
        pct = 100.0
    return max(0.0, min(1.0, pct / 100.0))


def _parse_mic_volume(value: Any) -> int | None:
    """mic_volume config → clamped int, or None for unset/garbage. Garbage maps
    to None (unmanaged) rather than raising — a typo in one key must never cost
    the whole config apply."""
    if value in (None, ""):
        return None
    try:
        return max(0, min(100, int(float(value))))
    except (TypeError, ValueError):
        log.warning("Ignoring invalid mic_volume %r (want 0-100 or unset)", value)
        return None


# --- Calibration suggestion heuristics: shared math in kenzy.calibration (one
# source for the dashboard wizard's JS mirror, this CLI, and the server's
# voice-guided flow). Thin aliases keep this module's call sites readable.

from kenzy.calibration import (  # noqa: E402
    agc_suspected as _agc_suspected,
)
from kenzy.calibration import (  # noqa: E402  (grouped with the other kenzy imports)
    separation_verdict as _separation_verdict,
)
from kenzy.calibration import (  # noqa: E402
    suggest_silence as _suggest_silence_rms,
)
from kenzy.calibration import (  # noqa: E402
    suggest_vad as _suggest_vad_threshold,
)
from kenzy.calibration import (  # noqa: E402
    suggest_wake as _suggest_wake_threshold,
)


def _set_yaml_scalar(text: str, key: str, value: str) -> str:
    """Update or append a top-level scalar ``key: value`` in a YAML document.

    Preserves the rest of the file (comments and layout) — a regex edit, not a
    redump. ``value`` is JSON-quoted, which is valid YAML.
    """
    quoted = json.dumps(value)
    pattern = re.compile(rf"(?m)^{re.escape(key)}:.*$")
    if pattern.search(text):
        return pattern.sub(f"{key}: {quoted}", text, count=1)
    sep = "" if text == "" or text.endswith("\n") else "\n"
    return f"{text}{sep}{key}: {quoted}\n"


def _apply_node_env(cfg: dict[str, Any]) -> None:
    """Layer the env-only bootstrap vars over the loaded config (server-authority
    stage d), so a node can start from the environment alone — ``node.yaml``
    becomes optional:

    * ``KENZY_SERVER_URL`` → ``server_url`` (normalized to ``ws(s)://``).
    * ``KENZY_SERVER_TOKEN`` (or legacy ``KENZY_SERVICE_TOKEN``) → the join token.
    * ``KENZY_NODE_ID`` → a stable ``node_id`` from the env — authoritative, so
      it is neither generated nor persisted (this is how two node instances run
      on one machine: two units, two ids).
    """
    from kenzy.serviceauth import service_token_from_env

    url = os.environ.get("KENZY_SERVER_URL")
    if url:
        cfg["server_url"] = _normalize_ws_url(url)
    token = service_token_from_env()
    if token:
        disc = cfg.get("discovery")
        if not isinstance(disc, dict):
            disc = {}
            cfg["discovery"] = disc
        disc["token"] = token
    node_id = os.environ.get("KENZY_NODE_ID")
    if node_id:
        cfg["node_id"] = node_id


def _normalize_ws_url(raw: str) -> str:
    """Normalize a server URL for the node's WebSocket connection: ``http`` →
    ``ws``, ``https`` → ``wss``, ``ws``/``wss`` kept, a bare host → ``ws://``.
    (``KENZY_SERVER_URL`` is shared with the backend services, which use the
    http form; a node needs the ws form.)"""
    from urllib.parse import urlparse

    if "://" not in raw:
        return f"ws://{raw}"
    parsed = urlparse(raw)
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    return f"{scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def _ensure_node_id(cfg: dict[str, Any], config_path: Path | None) -> str:
    """Return the node's stable ``node_id``, generating + persisting one if absent.

    A generated id is written back into ``config_path`` (``node.yaml``) so the
    node keeps the same identity across restarts even though its room name may
    change.
    """
    existing = cfg.get("node_id")
    if existing:
        return str(existing)
    node_id = str(uuid.uuid4())
    cfg["node_id"] = node_id
    if config_path is not None:
        try:
            text = config_path.read_text() if config_path.is_file() else ""
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(_set_yaml_scalar(text, "node_id", node_id))
            log.info("Generated node_id %s (saved to %s)", node_id, config_path)
        except OSError as exc:
            log.warning("Generated node_id %s but could not save it (%s)", node_id, exc)
    return node_id


def _resample(audio: np.ndarray[Any, Any], from_rate: int, to_rate: int) -> np.ndarray[Any, Any]:
    """Resample a 1-D int16 array from from_rate to to_rate using linear interpolation."""
    if from_rate == to_rate:
        return audio
    n_out = int(round(len(audio) * to_rate / from_rate))
    return np.interp(
        np.linspace(0, len(audio) - 1, n_out),
        np.arange(len(audio)),
        audio.astype(np.float64),
    ).astype(np.int16)


def _wake_phrase_levels(frames: list[np.ndarray[Any, np.dtype[np.int16]]]) -> tuple[float, float]:
    """``(phrase_dbfs, margin_db)`` for the wake pre-roll: the level of the
    SPOKEN phrase, and its height above the same window's quiet floor.

    Per-frame RMS across the ~1.1 s pre-roll; the phrase is the loud end (p90)
    and the room floor the quiet end (p25 — the phrase fills the back of the
    window, leaving a few genuinely quiet frames at the front). One number
    each, in dB, because that is what co-audible arbitration can compare:
    absolute level (dBFS) wanders with a device's AGC state — the same clip
    measured 168 and ~1330 on the same M1A minutes apart — but gain multiplies
    the phrase and the floor alike, so phrase-minus-floor survives wandering
    gain. Whole-window RMS (the first cut of this) dilutes the phrase with
    however much silence rode along; measuring the frames the wake actually
    lived in is the point."""
    if not frames:
        return 0.0, 0.0
    per_frame = [
        float(np.sqrt(np.mean(f.astype(np.float64) ** 2))) for f in frames if f.size
    ]
    if not per_frame:
        return 0.0, 0.0
    per_frame.sort()
    phrase = max(per_frame[int(0.9 * (len(per_frame) - 1))], 1.0)
    floor = max(per_frame[int(0.25 * (len(per_frame) - 1))], 1.0)
    phrase_dbfs = 20.0 * float(np.log10(phrase / 32768.0))
    margin_db = 20.0 * float(np.log10(phrase / floor))
    return round(phrase_dbfs, 1), round(margin_db, 1)


# ---------------------------------------------------------------------------
# Bundled resource helpers
# ---------------------------------------------------------------------------


def _bundled_model_paths() -> list[str]:
    """The bundled wake-word model, in a format THIS host can actually run.

    openwakeword needs ``tflite-runtime`` to load a ``.tflite`` model, and that
    package publishes no wheel past cp311 and none current for macOS — while
    ``onnxruntime`` is one of its unconditional dependencies and is therefore
    always present. So the bundled ``.tflite`` is unusable on exactly the hosts
    where the runtime is missing, even though the file is right there.

    Choosing by CAPABILITY rather than by file extension means a Mac (or any
    host without the tflite runtime) uses the ONNX copy automatically, instead
    of failing at first detection with an error about a missing module nobody
    asked for. tflite stays preferred where it runs: it is the lighter path on
    Pi-class ARM, which is most of the fleet.
    """
    model_dir = files("kenzy.node").joinpath("models")
    tflite = model_dir.joinpath("hey_ken_zee.tflite")
    onnx = model_dir.joinpath("hey_ken_zee.onnx")
    if not probe_import("tflite_runtime") and onnx.is_file():
        return [str(onnx)]
    return [str(tflite)]


def _infer_framework(model_paths: list[str]) -> str:
    """Derive the openwakeword inference framework from the model file extensions."""
    exts = {Path(p).suffix.lower() for p in model_paths}
    if exts == {".onnx"}:
        return "onnx"
    return "tflite"  # default; handles .tflite and mixed sets


def _ensure_oww_resources() -> None:
    """Download openwakeword feature-extraction models if they are absent.

    Only fetches the three infrastructure models (melspectrogram, embedding,
    VAD) — not the built-in wakeword models we don't use.  Idempotent: skips
    files that already exist.
    """
    import pathlib

    import openwakeword  # type: ignore[import-untyped]
    import openwakeword.utils as oww_utils  # type: ignore[import-untyped]

    target = pathlib.Path(oww_utils.__file__).parent / "resources" / "models"
    target.mkdir(parents=True, exist_ok=True)

    need: list[str] = []
    for info in openwakeword.FEATURE_MODELS.values():
        url: str = info["download_url"]
        for u in (url, url.replace(".tflite", ".onnx")):
            if not (target / u.split("/")[-1]).exists():
                need.append(u)
    for info in openwakeword.VAD_MODELS.values():
        url = info["download_url"]
        if not (target / url.split("/")[-1]).exists():
            need.append(url)

    if not need:
        return

    log.info("Downloading %d openwakeword infrastructure model(s)…", len(need))
    for url in need:
        oww_utils.download_file(url, str(target))


def _load_sound(name_or_path: str) -> tuple[np.ndarray[Any, Any], int]:
    """Load a WAV file and return (audio_array, samplerate).

    name_or_path can be an absolute/relative filesystem path or a bare
    filename to load from the bundled sounds directory.
    """
    import os

    if os.path.isabs(name_or_path) or os.path.sep in name_or_path:
        wav_context = None
        wav_file = name_or_path
    else:
        wav_context = as_file(files("kenzy.node").joinpath("sounds").joinpath(name_or_path))
        wav_file = None

    def _read(path: str) -> tuple[np.ndarray[Any, Any], int]:
        with wave.open(path) as wf:
            n_channels = wf.getnchannels()
            samp_width = wf.getsampwidth()
            frame_rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        dtype_map: dict[int, Any] = {1: np.int8, 2: np.int16, 4: np.int32}
        audio = np.frombuffer(raw, dtype=dtype_map[samp_width]).copy()
        if n_channels > 1:
            audio = audio.reshape(-1, n_channels)
        return audio, frame_rate

    if wav_context is not None:
        with wav_context as p:
            return _read(str(p))
    else:
        return _read(wav_file)  # type: ignore[arg-type]


class _StreamBuffer:
    """Thread-safe FIFO of int16 mono PCM for live streaming playback.

    The producer (asyncio thread — incoming intercom/media frames) calls
    :meth:`feed`; the real-time audio callback calls :meth:`read`. A
    ``collections.deque`` of numpy chunks is the only shared state; ``append`` and
    ``popleft`` are individually atomic under the GIL, and the read cursor
    (``_cur``/``_pos``) is touched only by the callback thread — so no mutex is
    needed in the RT path. Underflow returns silence (zero-padding) rather than
    blocking, so a late frame just produces a brief gap, never a glitchy stall.
    """

    def __init__(self) -> None:
        self._q: collections.deque[np.ndarray[Any, np.dtype[np.int16]]] = collections.deque()
        self._cur: np.ndarray[Any, np.dtype[np.int16]] | None = None
        self._pos: int = 0

    def feed(self, pcm: np.ndarray[Any, Any]) -> None:
        """Append int16 mono PCM (at the player's sample rate) to the queue."""
        chunk = np.ascontiguousarray(pcm, dtype=np.int16).reshape(-1)
        if chunk.size:
            self._q.append(chunk)

    def read(self, frames: int) -> np.ndarray[Any, np.dtype[np.int16]]:
        """Return exactly ``frames`` samples, zero-padded if the buffer underflows."""
        out = np.zeros(frames, dtype=np.int16)
        n = 0
        while n < frames:
            if self._cur is None or self._pos >= self._cur.size:
                try:
                    self._cur = self._q.popleft()
                    self._pos = 0
                except IndexError:
                    break  # underflow → leave the rest as silence
            take = min(frames - n, self._cur.size - self._pos)
            out[n : n + take] = self._cur[self._pos : self._pos + take]
            self._pos += take
            n += take
        return out

    @property
    def pending(self) -> bool:
        """True while unplayed samples remain (drain check for streamed TTS)."""
        return bool(self._q) or (self._cur is not None and self._pos < self._cur.size)

    def clear(self) -> None:
        self._q.clear()
        self._cur = None
        self._pos = 0

    @property
    def empty(self) -> bool:
        return self._cur is None and not self._q


class _SoundPlayer:
    """
    Single persistent output stream for all audio output (chime + TTS).

    Keeping one stream open eliminates the DAC activation pop and avoids
    ALSA single-stream limits that would arise if sd.play() opened a second
    output stream alongside this one.

    All audio is played at _TTS_SAMPLE_RATE; the chime WAV is resampled to
    that rate on load.  GIL-atomic flag/reference writes are used instead of
    a mutex so the RT callback is never blocked.
    """

    def __init__(
        self,
        chime: np.ndarray[Any, Any],
        chime_rate: int,
        device: str | int | None = None,
        sample_rate: int = _TTS_SERVER_RATE,
        volume: float = 1.0,
        muted: bool = False,
    ) -> None:
        # Convert to mono then resample to the playback rate if needed.
        chime_1d = chime.mean(axis=1).astype(np.int16) if chime.ndim > 1 else chime.astype(np.int16)
        chime_1d = _resample(chime_1d, chime_rate, sample_rate)

        self._sample_rate: int = sample_rate
        self._chime: np.ndarray[Any, Any] = chime_1d.reshape(-1, 1)
        self._audio: np.ndarray[Any, Any] = self._chime  # currently queued audio
        self._pending: np.ndarray[Any, Any] = self._chime  # audio to switch to on restart
        self._pos: int = len(self._audio)  # past end → silent
        self._restart: bool = False
        # Like _restart, but swaps to _pending *immediately* (regardless of _pos)
        # on the next callback. Used to cut a chime/waiting sound the instant TTS is
        # ready, so a fast reply's audio always plays from the start (no clipped head).
        self._interrupt: bool = False
        # The waiting bed (4.4 presence audio). _bed marks the current one-shot
        # as the bed, which is what makes it overlay-able by a spoken cue and
        # abortable on teardown. It plays ONCE (4.4.1): _loop additionally
        # repeats it, bounded by _loop_left so a dead pipeline can't loop
        # forever. All state is GIL-atomic ints/refs, RT-callback safe.
        self._bed: bool = False
        self._loop: bool = False
        self._loop_left: int = 0
        # Sample index in _audio at which spoken-cue audio ends (standalone cue,
        # or the mixed-in region of a bed). Lets the reply hold off just long
        # enough to avoid guillotining "Working on it." mid-word — see
        # cue_remaining_s and _CUE_GRACE_MAX_S.
        self._cue_end: int | None = None
        # Duck-under overlay (spoken cue mixed OVER the looping bed): the clean
        # bed is parked in _overlay_backup while a mixed copy plays; the callback
        # restores it at the next loop wrap (by then the overlay region is behind
        # the cursor). When the cue straddles the loop seam, _overlay_next holds
        # a head-only-mixed copy played for one wrap first — so the cue tail
        # still plays, but the seam-start region can never replay.
        self._overlay_backup: np.ndarray[Any, Any] | None = None
        self._overlay_next: np.ndarray[Any, Any] | None = None
        # "alert" audio (the ready chime) stays audible when muted; TTS/stream do not.
        self._alert: bool = True
        self._pending_alert: bool = True

        # Output gain (0.0–1.0) and mute, set from the main thread and read in the RT
        # callback (GIL-atomic scalar reads, no mutex — same discipline as _restart).
        self._volume: float = max(0.0, min(1.0, volume))
        self._muted: bool = muted
        # Barge-in duck (stage 2): a transient multiplier on output while a
        # possible interruption is being confirmed. 1.0 = normal.
        self._duck: float = 1.0

        # Live streaming mode (intercom / media): when on, the callback drains a
        # ring buffer instead of the one-shot _audio array. Off by default.
        self._streaming: bool = False
        self._ring = _StreamBuffer()

        self._stream = sd.OutputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
            device=device,
            callback=self._callback,
        )
        self._stream.start()

    def _callback(
        self,
        outdata: np.ndarray[Any, Any],
        frames: int,
        time_info: Any,
        status: sd.CallbackFlags,
    ) -> None:
        if self._streaming:
            outdata[:, 0] = self._ring.read(frames)
            self._apply_gain(outdata, alert=False)
            return
        if self._interrupt or (self._restart and self._pos >= len(self._audio)):
            self._interrupt = False
            self._restart = False
            self._audio = self._pending
            self._alert = self._pending_alert
            self._pos = 0
        filled = 0
        while filled < frames:
            remaining = len(self._audio) - self._pos
            if remaining <= 0:
                if self._loop and self._loop_left > 0:
                    # Looping bed: wrap in-callback (no per-loop silence gap).
                    # Swap the overlay copies out as their regions finish so a
                    # later wrap never replays a cue remnant (see overlay()).
                    self._loop_left -= 1
                    if self._overlay_next is not None:
                        self._audio = self._overlay_next
                        self._overlay_next = None
                    elif self._overlay_backup is not None:
                        self._audio = self._overlay_backup
                        self._overlay_backup = None
                    self._pos = 0
                    continue
                break
            n = min(frames - filled, remaining)
            outdata[filled : filled + n] = self._audio[self._pos : self._pos + n]
            self._pos += n
            filled += n
        if filled == 0:
            outdata[:] = 0
            return
        if filled < frames:
            outdata[filled:] = 0
            self._restart = False  # discard restart queued while audio was playing
        self._apply_gain(outdata, alert=self._alert)

    def _apply_gain(self, outdata: np.ndarray[Any, Any], alert: bool) -> None:
        """Scale ``outdata`` in place by the current volume / mute.

        Alert audio (the ready chime) ignores mute and plays at an audible floor so
        a muted node still acknowledges the wake word; everything else is silenced.
        """
        if self._muted:
            gain = _MUTED_ALERT_FLOOR if alert else 0.0
        else:
            gain = self._volume * self._duck
        if gain == 1.0:
            return
        if gain == 0.0:
            outdata[:] = 0
            return
        outdata[:] = (outdata * gain).astype(np.int16)

    def play(self) -> None:
        """Play the chime (alert audio — stays audible when muted)."""
        self._clear_loop()
        self._pending = self._chime
        self._pending_alert = True
        self._restart = True

    def play_pcm(
        self,
        audio: np.ndarray[Any, Any],
        interrupt: bool = False,
        alert: bool = False,
        loop: bool = False,
        bed: bool = False,
        cue: bool = False,
    ) -> None:
        """Play arbitrary int16 mono PCM at _TTS_SAMPLE_RATE (honors mute unless
        ``alert`` — alert audio, e.g. a doorbell chime, plays at the muted floor).

        With ``interrupt=True`` the new audio replaces whatever is playing on the
        very next callback (from the start), rather than waiting for the current
        sound to drain — a single atomic swap, so a concurrently-queued chime can't
        wedge between an abort and this call and clip the new audio's head.

        With ``bed=True`` the clip is the waiting bed: a spoken cue may duck-mix
        over it via overlay() for as long as it is still playing, and connection
        teardown aborts it. The bed does NOT repeat (4.4.1) — `sound_waiting` is
        operator-configurable, and while the bundled 26 s clip is an ambient bed
        that comfortably covers a request, a short chime in that slot repeating
        every couple of seconds is just noise.

        With ``loop=True`` the clip additionally repeats seamlessly until
        replaced/aborted, bounded by _LOOP_MAX_S so a dead pipeline can never
        loop forever. Any other playback call clears both — a reply, chime, or
        stream always wins.
        """
        self._clear_loop()
        self._bed = bool(bed or loop)  # a looping bed is a bed
        if cue and len(audio):
            self._cue_end = len(audio)  # whole clip is cue speech
        if loop and len(audio):
            dur = max(len(audio) / float(self._sample_rate), 0.1)
            self._loop_left = max(int(_LOOP_MAX_S / dur), 0)
            self._loop = self._loop_left > 0
        self._pending = audio.reshape(-1, 1)
        self._pending_alert = alert
        if interrupt:
            self._interrupt = True
        self._restart = True

    def _clear_loop(self) -> None:
        """Drop bed/looping + overlay state (GIL-atomic writes, RT-safe)."""
        self._loop = False
        self._loop_left = 0
        self._bed = False
        self._cue_end = None
        self._overlay_backup = None
        self._overlay_next = None

    @property
    def looping(self) -> bool:
        """The bed is *repeating* — the wrap-restore chain in overlay() applies."""
        return self._loop and not self._streaming

    @property
    def bed_active(self) -> bool:
        """A waiting bed is the current one-shot source (overlay() may apply).

        True whether or not it repeats; overlay() additionally requires that the
        clip has not finished playing.
        """
        return self._bed and not self._streaming

    @property
    def cue_remaining_s(self) -> float:
        """Seconds of spoken-cue audio still to play, or 0.0 when none is.

        Covers both shapes a cue takes — played standalone, or duck-mixed into a
        bed. The reply uses this to wait out a cue that is nearly finished
        rather than cutting it mid-word (bounded by _CUE_GRACE_MAX_S).
        """
        end = self._cue_end
        if end is None or self._streaming:
            return 0.0
        left = end - int(self._pos)
        return max(0.0, left / float(self._sample_rate))

    def overlay(
        self, cue: np.ndarray[Any, Any], duck: float = 0.25, lead: int = 2400
    ) -> bool:
        """Mix ``cue`` (int16 mono at the player rate) OVER the playing bed —
        the bed ducks to ``duck`` underneath and continues at full level after.

        Main-thread pre-mix, zero RT-callback surgery: build a mixed copy of the
        bed array and atomically swap the reference (same length, so the RT
        cursor stays valid); on a *repeating* bed the callback swaps the clean
        bed back in at the next wrap. ``lead`` samples (~0.1 s) of headroom keep
        the mix ahead of the moving cursor.

        Returns False when there is no bed still playing to mix over, or when a
        non-repeating bed has too little left to carry the whole cue — in both
        cases the caller falls back to plain playback (which replaces the bed).
        A short `sound_waiting` chime that has already finished lands here, which
        is why the cue is simply spoken in that case."""
        if not self.bed_active or self._pos >= len(self._audio):
            return False
        base = self._overlay_backup if self._overlay_backup is not None else self._audio
        bed = base.reshape(-1)
        cue_1d = np.ascontiguousarray(cue, dtype=np.int16).reshape(-1)
        if not len(cue_1d) or len(cue_1d) >= len(bed):
            return False  # cue longer than the bed — overlay bookkeeping breaks down
        start = (int(self._pos) + lead) % len(bed)
        idx = (start + np.arange(len(cue_1d))) % len(bed)
        mixed32 = bed.astype(np.int32).copy()
        mixed32[idx] = (bed[idx].astype(np.int32) * duck).astype(np.int32) + cue_1d.astype(
            np.int32
        )
        mixed = np.clip(mixed32, -32768, 32767).astype(np.int16).reshape(-1, 1)
        if start + len(cue_1d) <= len(bed):
            head_mixed = None  # cue fits before the seam — clean bed returns next wrap
        elif not self._loop:
            # A non-repeating bed never wraps, so the tail region would simply
            # never be reached and the cue would be cut off mid-word. Refuse and
            # let the caller speak it plainly instead.
            return False
        else:
            # Straddles the loop seam: after the wrap, one pass of a head-only
            # mixed copy plays the cue tail; the clean bed returns the wrap after.
            n_tail = (start + len(cue_1d)) - len(bed)
            head32 = bed.astype(np.int32).copy()
            head32[:n_tail] = (bed[:n_tail].astype(np.int32) * duck).astype(
                np.int32
            ) + cue_1d[-n_tail:].astype(np.int32)
            head_mixed = np.clip(head32, -32768, 32767).astype(np.int16).reshape(-1, 1)
        # Publication order matters (GIL-atomic refs, no lock): park the clean
        # bed + the seam copy BEFORE the mixed array goes live, so a wrap can
        # never observe the overlay without its restore chain.
        self._overlay_backup = base
        self._overlay_next = head_mixed
        self._audio = mixed
        # Where the cue speech ends inside the bed, so a reply can wait it out
        # (a straddling cue on a repeating bed wraps — no single index, no grace).
        self._cue_end = start + len(cue_1d) if head_mixed is None else None
        return True

    def set_volume(self, volume: float) -> None:
        """Set output gain (0.0–1.0); clamped."""
        self._volume = max(0.0, min(1.0, float(volume)))

    def duck(self, factor: float = 0.25) -> None:
        """Drop output to ``factor`` of normal (barge-in "go ahead" signal)."""
        self._duck = max(0.0, min(1.0, float(factor)))

    def unduck(self) -> None:
        self._duck = 1.0

    def set_muted(self, muted: bool) -> None:
        """Mute/unmute all non-alert audio (the ready chime stays audible)."""
        self._muted = bool(muted)

    def abort(self) -> None:
        """Stop playback immediately."""
        self._clear_loop()
        self._restart = False
        self._interrupt = False
        self._pos = len(self._audio)

    def start_stream(self) -> None:
        """Switch to live streaming mode (a fresh, empty ring buffer)."""
        self._clear_loop()
        self._restart = False
        self._interrupt = False
        self._ring.clear()
        self._streaming = True

    def feed(self, pcm: np.ndarray[Any, Any]) -> None:
        """Append int16 mono PCM (at the player's sample rate) for live playback."""
        self._ring.feed(pcm)

    def stop_stream(self) -> None:
        """Leave streaming mode and fall back to silence.

        Any one-shot audio that was mid-play when streaming began (the waiting
        sound a streamed reply interrupted) is discarded FIRST — leaving stream
        mode must fall to silence, never resume a stale clip. (The buffered path
        never had this hazard: play_pcm(interrupt=True) replaces the clip.)"""
        self._clear_loop()  # a looping bed must not resurrect either
        self._pos = len(self._audio)  # before the mode flip — no stale callback tick
        self._streaming = False
        self._ring.clear()

    @property
    def stream_pending(self) -> bool:
        """Streaming mode with unplayed samples still queued (drain check)."""
        return self._streaming and self._ring.pending

    @property
    def active(self) -> bool:
        return self._streaming or self._pos < len(self._audio) or self._restart

    def close(self) -> None:
        # abort() (discard buffered audio) is more decisive than stop() (drain) and
        # less likely to block during shutdown.
        try:
            self._stream.abort()
        except Exception:
            pass
        self._stream.close()


# ---------------------------------------------------------------------------
# Node client
# ---------------------------------------------------------------------------


class NodeClient:
    """
    Async room-node client.  Call ``await client.run()`` to start; it loops
    forever with exponential-backoff reconnection.
    """

    def __init__(self, cfg: dict[str, Any], config_path: Path | None = None) -> None:
        # Path to this node's writable config file, for persisting identity
        # (node_id) and a server-pushed room name. None ⇒ no write-back.
        self._config_path: Path | None = config_path
        # server_url is optional: when unset (or empty), the node discovers the
        # server over mDNS. An explicit value short-circuits discovery.
        self._server_url: str | None = cfg.get("server_url") or None
        # TLS client posture for a wss:// server (bootstrap keys — needed before
        # any connection exists, so they live in the local node.yaml).
        self._tls_verify: bool = bool(cfg.get("tls_verify", False))
        self._tls_ca: str | None = cfg.get("tls_ca") or None
        _disc = cfg.get("discovery") or {}
        self._discovery_enabled: bool = bool(_disc.get("enabled", True))
        # Shared-secret presented in hello; must match the server's discovery.token.
        self._join_token: str | None = _disc.get("token") or None
        # Room name defaults to the hostname so a fresh node needs no config; it is
        # the human label sent to the backends and is editable from the dashboard.
        self._room_id: str = str(cfg.get("room_id") or socket.gethostname())
        # Stable primary identifier (generated/persisted in main()); falls back to
        # the room name if a caller constructs the client without ensuring one.
        self._node_id: str = str(cfg.get("node_id") or self._room_id)
        self._wakeword_models: list[str] = cfg.get("wakeword_models", [])
        self._wakeword_threshold: float = float(cfg.get("wakeword_threshold", 0.5))
        # openwakeword Silero VAD gate: predictions are suppressed unless the VAD
        # speech score exceeds this. 0 disables it (openwakeword default), which
        # lets near-silence "blips" produce spurious wake-word hits.
        self._wakeword_vad_threshold: float = float(cfg.get("wakeword_vad_threshold", 0.0))
        self._silence_rms: float = float(cfg.get("silence_rms_threshold", 50.0))
        self._audio_device: str | int | None = cfg.get("audio_device", None)
        self._sound_ready: str = str(cfg.get("sound_ready") or "ready.wav")
        _sw = cfg.get("sound_waiting", "waiting.wav")
        self._sound_waiting: str | None = str(_sw) if _sw else None
        # Intercom call chimes (null/empty disables). Bundled defaults.
        _sc = cfg.get("sound_connect", "connect.wav")
        self._sound_connect: str | None = str(_sc) if _sc else None
        _sd = cfg.get("sound_disconnect", "disconnect.wav")
        self._sound_disconnect: str | None = str(_sd) if _sd else None
        _rb = cfg.get("sound_ringback", "ringback.wav")
        self._sound_ringback: str | None = str(_rb) if _rb else None
        # End-of-dialog cue: played only when a multi-turn dialog concludes (never after
        # a single turn). Off by default; set to a sound (e.g. "disconnect.wav") to enable.
        _sde = cfg.get("sound_dialog_end")
        self._sound_dialog_end: str | None = str(_sde) if _sde else None
        # Orphan cue: played when the wake word fires while we have no server. Off by
        # default (the honest answer is silence plus a log), but a household that wants
        # audible feedback can point this at a distinct sound — it must NOT be the ready
        # chime, which means "I'm listening" and would be a lie here.
        _so = cfg.get("sound_offline")
        self._sound_offline: str | None = str(_so) if _so else None
        self._capture_rate: int = int(cfg.get("capture_sample_rate", protocol.SAMPLE_RATE))
        self._playback_rate: int = int(cfg.get("playback_sample_rate", _TTS_SERVER_RATE))
        # Playback volume (config key is 0–100; stored internally as 0.0–1.0) and mute.
        # Volume persists via config-pull; mute is a transient runtime toggle (the node
        # comes back un-muted after a restart since it isn't written to the override).
        self._volume: float = _volume_to_gain(cfg.get("volume", 100))
        self._muted: bool = bool(cfg.get("muted", False))

        # Hardware capability, declared not detected: whether this room's speaker
        # does acoustic echo cancellation. False ⇒ strict half-duplex — wake hits
        # are ignored while the node is emitting any audio (she can't hear you
        # over herself), and the server disables intercom / degrades alarms for
        # this node. Default true matches the published hardware guidance.
        self._hardware_aec: bool = bool(cfg.get("hardware_aec", True))

        # System-metrics sampler (dashboard fleet card): stdlib procfs reads,
        # None-safe on non-Linux. Sent every ~30 s while connected.
        from kenzy.node.sysinfo import MetricsSampler

        self._sys_sampler = MetricsSampler()

        # Timing thresholds, all stored as frame counts (min 1 to avoid ≥0 always-true).
        self._vad_enabled: bool = bool(cfg.get("vad_enabled", True))
        fm_ = protocol.FRAME_MS
        # Dialog-turn tuning (stage 1): the reply window during a held floor and
        # the sustained-speech onset gate that starts a turn.
        self._dialog_no_speech_frames: int = max(
            int(cfg.get("dialog_no_speech_timeout_ms", 8000)) // fm_, 1
        )
        self._dialog_onset_frames: int = max(int(cfg.get("dialog_onset_ms", 300)) // fm_, 1)
        self._dialog_onset_vad: float = float(cfg.get("dialog_onset_vad_threshold", 0.5))
        # One-breath commands ("Hey Kenzy turn on the lights", no pause): after a
        # wake hit, hold the ready chime for this long. Speech continuing within
        # the window means the command is already underway — the chime would
        # trample it, so the session opens silently and the buffered onset is
        # flushed. Silence for the whole window means the classic pause flow:
        # chime then listen, exactly as before, just this much later. 0 = off
        # (chime fires instantly, pre-gate behavior).
        self._wake_onset_frames: int = int(cfg.get("wake_onset_ms", 400)) // fm_
        self._silence_frames: int = max(int(cfg.get("silence_ms", 400)) // protocol.FRAME_MS, 1)
        self._speech_min_frames: int = max(
            int(cfg.get("speech_min_ms", 400)) // protocol.FRAME_MS, 1
        )
        self._no_speech_timeout_frames: int = max(
            int(cfg.get("no_speech_timeout_ms", 15_000)) // protocol.FRAME_MS, 1
        )
        self._hard_cap_frames: int = max(
            int(cfg.get("hard_cap_ms", 30_000)) // protocol.FRAME_MS, 1
        )

        # Thread-safe audio queue filled by the sounddevice callback.
        self._raw_q: queue.Queue[np.ndarray[Any, np.dtype[np.int16]]] = queue.Queue(maxsize=200)

        # Asyncio queue for inbound control messages from the server.
        self._cmd_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        # systemd --user unit state, probed once at startup (run()) and reported
        # in hello capabilities so the dashboard knows whether Disable applies.
        self._unit_info: dict[str, Any] | None = None

        # Queue for inbound TTS binary frames from the server.
        # No maxsize — dropping frames causes truncated playback for long responses.
        self._tts_q: asyncio.Queue[bytes] = asyncio.Queue()
        self._tts_sample_rate: int = 24000
        self._tts_alert: bool = False  # current TTS session is alert audio (beats mute)
        self._tts_stream: bool = False  # 4.4: play this session's frames as they arrive
        self._tts_stream_started: bool = False  # first streamed frame fed (for the log)
        self._tts_cue: bool = False  # 4.4: processing cue — duck-mix over the waiting bed
        # 4.4.1: seconds this reply waits for a nearly-finished cue (_CUE_GRACE_MAX_S),
        # and the matching "don't abort the audio" flag for cancelling its drain task.
        self._cue_grace_s: float = 0.0
        self._keep_audio_on_cancel: bool = False
        self._tts_task: asyncio.Task[None] | None = None

        self._state: str = _STATE_IDLE
        self._session_id: str | None = None
        # Set when the server wants one utterance captured after the next TTS prompt
        # finishes (intercom consent answer, or a voice-enrollment sample).
        self._capture_after_prompt: bool = False
        # Whether the armed capture should chime when it opens (expect_utterance's
        # cue flag): True = record-after-the-tone (enrollment); False = a
        # conversational follow-up — her question is the cue (stage 1).
        self._capture_cue: bool = True
        # The current capture session is a dialog follow-up: opened silently,
        # onset-gated, 8s window, no waiting sound between turns.
        self._followup_active: bool = False
        # v6 follow-up: the pending onset window is the THINKING GAP — no
        # expiry. It lasts until speech consumes it or the reply's TTS
        # supersedes it, so an interjection lands while the server is thinking.
        self._followup_unbounded: bool = False
        # Onset gating (follow-ups only): buffer mic frames and send nothing until
        # ~dialog_onset_ms of SUSTAINED speech — a clink or cough must not start a
        # turn, and a silent window must expire without ever bothering the server.
        self._onset_pending: bool = False
        self._onset_run: int = 0
        self._onset_elapsed: int = 0
        self._onset_burst: int = 0  # length of the most recent speech burst
        self._onset_gap: int = 0  # silent frames since that burst ended
        self._onset_buf: list[np.ndarray[Any, np.dtype[np.int16]]] = []
        # The pending onset window is a WAKE gate (one-breath command detection),
        # not a follow-up: different expiry (chime + open a normal session, never
        # followup_timeout — the server hasn't heard about this session at all).
        self._wake_gate: bool = False
        # Rolling pre-roll for the wake gate, sized to cover the WHOLE wake
        # phrase plus openwakeword's detection lag (~1s). Deliberately not
        # trimmed to the phrase boundary: the score crosses threshold a few
        # frames after the phrase ends, and anything said in that lag ("turn…")
        # would be lost to a short buffer — the rig lost exactly one word to a
        # 1-frame version. STT is far better at finding the phrase boundary
        # than a frame counter, so the phrase rides along as audio and the
        # server strips it as text (_strip_wake_prefix).
        self._idle_preroll: collections.deque[np.ndarray[Any, np.dtype[np.int16]]] = (
            collections.deque(maxlen=_WAKE_PREROLL_FRAMES)
        )
        # (wake_db, wake_margin_db, wake_score) measured at the moment an idle
        # wake fired, consumed (once) by the session's audio_start — see
        # protocol.audio_start.
        self._wake_meta: tuple[float, float, float] | None = None
        # Monotonic deadline before which wake hits are ignored — set when a
        # wake gate is cancelled by server_stop (lost arbitration; see
        # _ARB_REFRACTORY_S).
        self._wake_refractory_until: float = 0.0
        self._dialog_vad: Any = None  # lazy standalone Silero VAD; False = unavailable
        # Barge-in (stage 2): listen while a floor-holding reply plays and yield
        # if the user answers early. Only when hardware_aec (echo-cancelled feed).
        self._barge_run: int = 0
        self._barge_buf: list[np.ndarray[Any, np.dtype[np.int16]]] = []
        self._barge_ducked: bool = False
        # When the floor-holding reply's AUDIO started playing (monotonic); the
        # _BARGE_GRACE_S window is measured from here. 0.0 = no reply audio yet.
        self._barge_armed_at: float = 0.0
        # Set when the server signals a multi-turn dialog ended while TTS is still
        # playing; the end-of-dialog cue plays once that playback completes.
        self._end_dialog_after_tts: bool = False
        self._ws: ClientConnection | None = None
        self._oww: Any = None  # openwakeword Model
        self._player: _SoundPlayer | None = None
        # Audio hardware is built lazily, only after the first server config frame
        # arrives (zero-config nodes pull all hardware keys from the server). Until
        # then the node blocks: no mic, no wakeword, no playback.
        self._audio_ready: bool = False
        # Set when _init_audio fails (e.g. a bad audio_device). The node stays
        # connected and controllable so the device can be fixed + the node
        # restarted from the dashboard; audio is retried on the next restart.
        self._audio_failed: bool = False
        self._audio_error: str | None = None
        # Calibration: while tuning, the audio loop streams per-frame RMS/wake/VAD
        # scalars to the server (relayed to the dashboard) and does NOT act on wake
        # words, so the operator can repeat the wake word to measure scores.
        self._tuning: bool = False
        self._tune_deadline: float = 0.0
        self._tune_seq: int = 0
        self._tune_vad: Any = None  # standalone openwakeword.VAD for the window
        self._input_stream: sd.InputStream | None = None
        self._audio_task: asyncio.Task[None] | None = None
        # Opt-in log ring buffer for the dashboard log viewer; attached only when
        # the server asks (config `keep_logs: true`), so a dashboard-less server
        # induces no node-side overhead.
        self._log_buffer: Any = None
        # Console (display) level vs how deep the dashboard buffer captures. Both
        # are live-tunable from the dashboard via config-pull.
        from kenzy.logutil import level_value

        self._log_level: int = level_value(cfg.get("log_level"), logging.INFO)
        self._log_capture_level: int = level_value(cfg.get("log_capture_level"), logging.DEBUG)
        self._waiting_audio: np.ndarray[Any, Any] | None = None
        self._connect_audio: np.ndarray[Any, Any] | None = None
        self._disconnect_audio: np.ndarray[Any, Any] | None = None
        self._ringback_audio: np.ndarray[Any, Any] | None = None
        # Intercom ringback: a loop played on the CALLER while the target room is
        # rung. Armed after the "calling…" reply finishes; stopped on connect,
        # a spoken decline/timeout, wake, or disconnect.
        self._ringback_after_tts: bool = False
        self._ringback_task: asyncio.Task[None] | None = None
        self._dialog_end_audio: np.ndarray[Any, Any] | None = None
        self._offline_audio: np.ndarray[Any, Any] | None = None

        self._silence_count: int = 0
        self._speech_frames: int = 0
        self._frame_count: int = 0
        # Set on shutdown so a blocking mDNS browse returns promptly (otherwise the
        # worker thread is joined at interpreter exit, delaying Ctrl+C by ~5s).
        self._discovery_cancel = threading.Event()
        # Per-attempt discovery cancels. A browse that overruns its own deadline is
        # told to unwind through its own event; shutdown sets every live one.
        self._discovery_cancels: set[threading.Event] = set()
        # The address we last *registered* with (not merely connected to). A node
        # that has been talking to a server for days must never be orphaned because
        # one multicast query goes unanswered, so this is tried before mDNS and the
        # browse becomes the fallback. Loaded from / written to the cache file.
        self._cached_server_url: str | None = _read_cached_server()
        # Set when an attempt on the cached address failed to register, so the next
        # attempt asks the network instead of looping on a dead address.
        self._cache_stale: bool = False
        # The URL the in-flight attempt is using, promoted to the cache on register.
        self._connect_url: str | None = None
        # Liveness, for the watchdog. ``_loop_alive_at`` proves the reconnect loop is
        # still iterating (a wedged await stops updating it); ``_registered_at`` is the
        # last time a server accepted our hello and answered with a config frame.
        self._loop_alive_at: float = 0.0
        self._registered_at: float = 0.0
        # When the CURRENT outage began — stamped as a registered connection drops,
        # cleared as one is established. Deliberately NOT ``_registered_at``: that one
        # marks when we last *joined*, so measuring downtime from it counts the whole
        # healthy connected run as downtime, and a node up longer than reexec_minutes
        # re-execs on the first tick of any blip. Zero means "no outage in progress",
        # and downtime is then measured from process start.
        self._disconnected_at: float = 0.0
        self._registered: bool = False
        # Why the last connection closed, captured from the WS close frame. On a
        # reconnect this is the only place the server's reason is visible.
        self._close_reason: str | None = None
        # 5.0.4: USB speakerphone volume buttons — opt-in, server-owned. FLAT
        # keys like every other editable node key (dashboard grid + wizard),
        # not a nested dict — flatness is what makes them ordinary.
        self._mk_enabled: bool = bool(cfg.get("volume_buttons", False))
        self._mk_device: str = str(cfg.get("volume_button_device") or "auto")
        self._mk_step: int = max(1, min(20, int(cfg.get("volume_button_step", 5))))
        # Capture gain (mic_volume): unset = never touch the device's own gain.
        # The playback principle from 5.0.4, applied to the other direction —
        # one truth, no ALSA side-channel, survives reboots via re-apply.
        self._mic_volume: int | None = _parse_mic_volume(cfg.get("mic_volume"))
        self._micvol_status: dict[str, Any] | None = None
        self._mediakeys_task: asyncio.Task[None] | None = None
        #: What the watcher last reported — re-sent to the server on reconnect
        #: so the dashboard's endpoint-status line survives a server restart.
        self._mediakeys_status: dict[str, Any] | None = None
        #: The (enabled, match, step, audio_device) tuple the running task was
        #: built from; _sync_mediakeys restarts the task only when it changes.
        self._mediakeys_built_from: tuple[Any, ...] | None = None
        # 5.1 plugin seam: node-role plugins from installed kenzy-* distributions.
        # Scanned once per process (install/uninstall is restart-to-apply); the
        # scan is fail-closed per plugin and never raises, but a plugin must
        # never take the node down, so belt-and-braces anyway.
        self._plugin_scan: PluginScan | None
        try:
            from kenzy.plugins import scan_plugins

            self._plugin_scan = scan_plugins()
        except Exception as exc:  # pragma: no cover - scan_plugins is designed not to raise
            log.error("Plugin scan failed entirely: %s", exc, exc_info=True)
            self._plugin_scan = None
        #: Per-plugin config (server-owned ``addons.<id>`` namespace).
        self._addons_cfg: dict[str, Any] = {
            k: dict(v) for k, v in (cfg.get("addons") or {}).items() if isinstance(v, dict)
        }
        self._plugin_tasks: dict[str, asyncio.Task[None]] = {}
        #: Config each running plugin task was built from; _sync_plugins
        #: restarts a task only when its slice changed (mediakeys pattern).
        self._plugins_built_from: dict[str, dict[str, Any]] = {}
        #: Live context per plugin — shared by the run task and inbound
        #: server-half events, so both see the same config object.
        self._plugin_ctxs: dict[str, Any] = {}
        _wd = cfg.get("watchdog") or {}
        self._watchdog_enabled: bool = bool(_wd.get("enabled", True))
        self._watchdog_warn_s: float = float(_wd.get("warn_minutes", 5)) * 60.0
        # A reconnect loop that has not iterated in this long is wedged in an await
        # that will never return — the only way out is a fresh process.
        self._watchdog_wedge_s: float = float(_wd.get("wedge_minutes", 5)) * 60.0
        # Belt-and-braces: re-exec after this long with no registration even if the
        # loop is still turning (0 disables). Deliberately much longer than the wedge
        # timeout so an ordinary server outage doesn't make the fleet flap.
        self._watchdog_reexec_s: float = float(_wd.get("reexec_minutes", 30)) * 60.0
        self._force_exit_armed = False  # guards the one-shot shutdown watchdog
        # In-flight "I'm leaving on purpose" frame, awaited briefly during teardown.
        self._goodbye_task: asyncio.Task[None] | None = None
        # Cached audio-device probe, reported to the server so the dashboard can offer
        # a device picker. Probing touches PortAudio and can be slow/block, so it runs
        # in a daemon thread (never on the event loop); the result is pushed via a
        # `status` update when ready. None = not yet probed.
        self._device_probe: list[dict[str, Any]] | None = None
        self._device_probe_started = False

    # ------------------------------------------------------------------
    # Audio capture (sounddevice callback – runs in a C thread)
    # ------------------------------------------------------------------

    def _audio_callback(
        self,
        indata: np.ndarray[Any, np.dtype[np.int16]],
        frames: int,
        time_info: Any,
        status: sd.CallbackFlags,
    ) -> None:
        if status:
            log.warning("sounddevice status: %s", status)
        try:
            self._raw_q.put_nowait(indata.copy())
        except queue.Full:
            pass  # drop newest frame under backpressure

    # ------------------------------------------------------------------
    # openwakeword
    # ------------------------------------------------------------------

    def _load_wakeword(self) -> None:
        try:
            from openwakeword.model import Model  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "openwakeword is not installed — a node cannot detect its wake word "
                "without it. Install the wake-word extra: pip install 'kenzy[wakeword]'. "
                "On Linux with Python 3.12+ that extra fails (openwakeword requires "
                "tflite-runtime, which has no wheel past 3.11); install it without its "
                "dependencies instead: pip install --no-deps openwakeword && "
                "pip install onnxruntime tqdm scipy scikit-learn requests"
            ) from exc

        _ensure_oww_resources()

        model_paths = self._wakeword_models if self._wakeword_models else _bundled_model_paths()
        framework = _infer_framework(model_paths)

        self._oww = Model(
            wakeword_models=model_paths,
            inference_framework=framework,
            vad_threshold=self._wakeword_vad_threshold,
        )
        log.info(
            "openwakeword loaded: %s (framework=%s, vad_threshold=%.2f)",
            [Path(p).name for p in model_paths],
            framework,
            self._wakeword_vad_threshold,
        )

    # ------------------------------------------------------------------
    # Streaming helpers
    # ------------------------------------------------------------------

    def _dialog_vad_score(self, flat: np.ndarray[Any, Any]) -> float | None:
        """Silero VAD score for the dialog onset gate (lazy standalone instance,
        like calibration's). None = model unavailable → caller falls back to RMS."""
        if self._dialog_vad is False:
            return None
        if self._dialog_vad is None:
            try:
                import openwakeword  # type: ignore[import-untyped]

                self._dialog_vad = openwakeword.VAD()
            except Exception as exc:
                log.warning("VAD unavailable for dialog onset (%s) — using RMS fallback", exc)
                self._dialog_vad = False
                return None
        try:
            self._dialog_vad(flat)
            buf = self._dialog_vad.prediction_buffer
            return float(buf[-1]) if buf else 0.0
        except Exception:
            return 0.0

    async def _handle_onset_frame(self, flat: np.ndarray[Any, np.dtype[np.int16]]) -> None:
        """One mic frame during a pending follow-up window (nothing sent yet).

        A turn STARTS only on ~dialog_onset_ms of consecutive speech-classified
        frames — a clink or cough can't start (and then instantly end) a turn.
        Silero gates the start; the normal RMS machinery endpoints the finish
        once the turn is running. On expiry the server gets followup_timeout and
        the user gets the local end cue ("I stopped waiting").
        """
        if not self._onset_pending:  # already confirmed/expired — idempotent
            return
        self._onset_elapsed += 1
        self._onset_buf.append(flat)
        # The wake gate keeps everything it armed with (pre-roll + hit frame)
        # plus its window + the onset length: the command may have begun before
        # the gate armed, and a trimmed buffer would clip its first word on
        # the flush.
        cap = self._dialog_onset_frames + 8
        if self._wake_gate:
            cap += self._wake_onset_frames + _WAKE_PREROLL_FRAMES + 1
        if len(self._onset_buf) > cap:
            self._onset_buf.pop(0)

        if self._wake_gate:
            # The wake gate classifies by ENERGY, not Silero. Silero's internal
            # state rises too slowly from cold idle for a 400ms window — on the
            # rig it ruled a sentence in full flight "silent" and the chime
            # landed on top of it. The asymmetry favors leniency: a false
            # "speech" just skips the chime (the open session still captures
            # whatever follows); a false "silence" tramples the command. RMS
            # against the calibrated threshold is the same test that endpoints
            # every normal session.
            rms = float(np.sqrt(np.mean(flat.astype(np.float32) ** 2)))
            speech = rms >= self._silence_rms
        else:
            score = self._dialog_vad_score(flat)
            if score is None:  # no VAD model — degrade to sustained-energy gating
                rms = float(np.sqrt(np.mean(flat.astype(np.float32) ** 2)))
                speech = rms >= self._silence_rms
            else:
                speech = score >= self._dialog_onset_vad
                if speech and self._followup_unbounded:
                    # The thinking-gap window demands real acoustic energy TOO:
                    # a cold-state VAD blip on a suppressed mic's near-silent
                    # DSP floor must not phantom-barge the very turn this
                    # window serves (found live: a -90 dBFS "answer" confirmed
                    # 240 ms after opening, cancelling the real reply).
                    rms = float(np.sqrt(np.mean(flat.astype(np.float32) ** 2)))
                    speech = rms >= self._silence_rms
        if speech:
            self._onset_run += 1
            self._onset_burst = max(self._onset_burst, self._onset_run)
            self._onset_gap = 0
        else:
            self._onset_run = 0
            if self._onset_burst:
                self._onset_gap += 1

        # Two ways a turn starts (a single-frame blip is neither):
        #  a) sustained speech reaches dialog_onset_ms — a longer answer, mid-word;
        #  b) a short COMPLETE utterance: a burst of ≥ ~160 ms that then ended in
        #     silence ("Boo", "yes"). Consecutive-frames-only rejected any word
        #     shorter than the onset window — found live on a knock-knock punchline.
        min_burst = max(2, self._dialog_onset_frames // 2)
        confirmed = self._onset_run >= self._dialog_onset_frames or (
            # The short-complete-utterance route ("Boo") is for post-reply
            # answers; the thinking-gap window takes the sustained path only —
            # an interjection there is a sentence, and the shortcut is exactly
            # where a two-frame transient sneaks through.
            not self._followup_unbounded
            and self._onset_burst >= min_burst
            and self._onset_gap >= 2
        )
        if confirmed:
            # The user answered (follow-up) or kept talking through the wake
            # word (one-breath command). Open the real session and flush the
            # buffer so their first word survives whole. The wake gate's chime
            # stays unplayed — it would land on top of their sentence.
            was_wake_gate = self._wake_gate
            self._wake_gate = False
            self._onset_pending = False
            if self._followup_unbounded and self._player is not None:
                # A real interjection during the thinking gap: NOW the waiting
                # bed dies (the arm deliberately left it playing).
                self._player.abort()
            sid = self._session_id or str(uuid.uuid4())
            self._session_id = sid
            try:
                msg, _ = protocol.audio_start(sid, self._room_id, *self._take_wake_meta())
                if self._ws is None:
                    raise ConnectionError("no websocket")
                await self._ws.send(msg)
                for f in self._onset_buf:
                    await self._ws.send(f.tobytes())
            except Exception as exc:
                log.warning("Could not open session: %s", exc)
                self._state = _STATE_IDLE
                self._session_id = None
                self._followup_active = False
                self._onset_buf = []
                return
            # Seed the endpointing counters with observed truth — and mark
            # speech_min as SATISFIED: its anti-blip job is already done, by a
            # better classifier (the Silero onset gate). Without this, a short
            # complete answer ("Boo") never arms silence-endpointing and drags
            # to the no_speech timeout — found live, again on the knock-knock.
            self._speech_frames = max(self._onset_run, self._onset_burst, self._speech_min_frames)
            self._silence_count = self._onset_gap
            self._frame_count = len(self._onset_buf)
            self._onset_buf = []
            log.info(
                "[%s] %s — streaming",
                sid[:8],
                "one-breath command detected" if was_wake_gate else "follow-up answer detected",
            )
            return

        if self._wake_gate:
            # Wake-gate expiry: silence followed the wake word (run == 0 means no
            # speech in flight — a burst mid-word at the window edge is allowed to
            # finish and confirm above). This is the classic pause flow: play the
            # chime the gate was holding and open a normal wake session. The
            # buffered silence is dropped, not sent — the server should meet this
            # session at its audio_start, like any other.
            if self._onset_elapsed >= self._wake_onset_frames and self._onset_run == 0:
                self._wake_gate = False
                self._onset_pending = False
                self._onset_buf = []
                if self._player is not None:
                    self._player.play()
                sid = self._session_id or str(uuid.uuid4())
                self._session_id = sid
                try:
                    msg, _ = protocol.audio_start(sid, self._room_id, *self._take_wake_meta())
                    if self._ws is None:
                        raise ConnectionError("no websocket")
                    await self._ws.send(msg)
                except Exception as exc:
                    log.warning("Could not open wake session: %s", exc)
                    self._state = _STATE_IDLE
                    self._session_id = None
                    return
                self._silence_count = 0
                self._speech_frames = 0
                self._frame_count = 0
                log.info("[%s] wake gate silent — chime, normal session", sid[:8])
            return

        if self._followup_unbounded:
            return  # the thinking-gap window: no expiry — superseded or consumed
        if self._onset_elapsed >= self._dialog_no_speech_frames:
            # Window expired in silence: the dialog is over. The server never saw
            # a session; it just needs its floor state cleared.
            self._onset_pending = False
            self._state = _STATE_IDLE
            self._session_id = None
            self._followup_active = False
            self._onset_buf = []
            if self._ws is not None:
                try:
                    await self._ws.send(protocol.followup_timeout())
                except Exception:
                    pass
            if self._player is not None and self._dialog_end_audio is not None:
                self._player.play_pcm(self._dialog_end_audio, interrupt=True)
            log.info("Follow-up window expired — dialog ended (end cue)")

    async def _handle_barge_frame(self, flat: np.ndarray[Any, np.dtype[np.int16]]) -> None:
        """One mic frame WHILE a floor-holding reply is playing (stage 2 duplex).

        The user often answers before Kenzy finishes asking. On sustained speech
        over her voice (AEC feed, raised onset floor to reject her own residual):
        duck at first suspicion (a "go ahead" volume drop), and on confirmation
        stop the reply and open the answer session early — flushing the pre-roll
        so nothing said during the duck is lost. A false alarm just un-ducks.
        """
        if self._state != _STATE_TTS or not self._capture_after_prompt:
            return  # already confirmed / no longer a floor-holding reply
        self._barge_buf.append(flat)
        if len(self._barge_buf) > self._dialog_onset_frames + 8:
            self._barge_buf.pop(0)

        # Grace: until the reply audio has been playing for _BARGE_GRACE_S, only
        # BUFFER (a real early answer near the end of grace is still in pre-roll)
        # — no ducking, no confirming. AEC hasn't converged on the reply yet, so
        # detecting here catches Kenzy's own voice as a false barge. armed_at==0
        # means no reply audio has started (e.g. buffered collection); wait.
        if (
            not self._barge_armed_at
            or time.monotonic() - self._barge_armed_at < _BARGE_GRACE_S
        ):
            return

        score = self._dialog_vad_score(flat)
        rms = float(np.sqrt(np.mean(flat.astype(np.float32) ** 2)))
        if score is None:  # no VAD model → raised-energy fallback
            speech = rms >= self._silence_rms * _BARGE_RMS_FACTOR
        else:
            # Silero AND raised energy. The AEC residual of Kenzy's own reply
            # is speech-SHAPED — Silero alone confirms it and cuts her off
            # mid-sentence (found live: three replies truncated in one session
            # with the room silent) — but the residual is QUIET; a real
            # interjection over her voice carries real level. Same
            # shape-plus-level rule as the thinking-gap onset gate.
            speech = (
                score >= self._dialog_onset_vad
                and rms >= self._silence_rms * _BARGE_RMS_FACTOR
            )
        if speech:
            self._barge_run += 1
        else:
            self._barge_run = 0
            if self._barge_ducked:  # suspicion evaporated — resume the reply
                self._barge_ducked = False
                if self._player is not None:
                    self._player.unduck()

        if self._barge_run == 1 and not self._barge_ducked:
            self._barge_ducked = True
            if self._player is not None:
                self._player.duck()  # "I hear you, go ahead"

        if self._barge_run >= self._dialog_onset_frames:
            # Confirmed: the user is answering. Stop the reply, open the session
            # early (onset already proven), flush the pre-roll so the first word
            # spoken during the duck survives whole.
            log.info("Barge-in confirmed — yielding to the user's answer")
            preroll = list(self._barge_buf)
            self._barge_run = 0
            self._barge_buf = []
            self._barge_ducked = False
            self._capture_after_prompt = False
            if self._player is not None:
                self._player.unduck()
            await self._stop_tts_playback()  # cuts her reply, back to IDLE
            sid = str(uuid.uuid4())
            self._state = _STATE_STREAMING
            self._session_id = sid
            self._followup_active = True
            self._silence_count = 0
            # speech_min already satisfied by the confirmed barge-in (Silero) —
            # otherwise silence-endpointing never arms (same fix as the onset path).
            self._speech_frames = self._speech_min_frames
            self._frame_count = len(preroll)
            if self._ws is not None:
                try:
                    msg, _ = protocol.audio_start(sid, self._room_id)
                    await self._ws.send(msg)
                    for f in preroll:
                        await self._ws.send(f.tobytes())
                except Exception as exc:
                    log.warning("Could not open barge-in session: %s", exc)
                    self._state = _STATE_IDLE
                    self._session_id = None
                    self._followup_active = False

    def _reset_barge(self) -> None:
        self._barge_run = 0
        self._barge_buf = []
        self._barge_armed_at = 0.0  # next reply's grace re-arms on its first audio
        if self._barge_ducked and self._player is not None:
            self._player.unduck()
        self._barge_ducked = False

    def _take_wake_meta(self) -> tuple[float | None, float | None, float | None]:
        """Consume-once wake evidence for the session's audio_start — a stale
        value must never ride a later, unrelated session."""
        meta, self._wake_meta = self._wake_meta, None
        return meta if meta is not None else (None, None, None)

    async def _announce_wake(self, session_id: str, model: str, score: float) -> None:
        """Send ``wake_pending`` — the co-audible arbitration announcement —
        with the outcome VISIBLE either way. A field report (2026-08-17) hinged
        on whether a node had sent this frame at all, and the old silent
        try/except made the node's own journal useless for answering that."""
        if self._ws is None or self._wake_meta is None:
            return
        try:
            await self._ws.send(
                protocol.wake_pending(
                    session_id, model, score, self._wake_meta[0], self._wake_meta[1]
                )
            )
            log.info(
                "[%s] announced wake (db=%.1f margin=%.1f score=%.3f)",
                session_id[:8],
                self._wake_meta[0],
                self._wake_meta[1],
                score,
            )
        except Exception as exc:
            log.warning("[%s] wake announcement FAILED: %s", session_id[:8], exc)

    async def _begin_streaming(
        self,
        session_id: str,
        followup: bool = False,
        wake_gated: bool = False,
        gate_preroll: list[np.ndarray[Any, np.dtype[np.int16]]] | None = None,
        keep_player: bool = False,
    ) -> None:
        """Open a capture session.

        ``wake_gated`` is passed ONLY by the wake-word paths: it arms the
        one-breath gate (chime held until the window decides). A server
        ``trigger`` chimes immediately as always — there is no wake phrase the
        user might be talking through. ``gate_preroll`` carries the frames
        around the wake hit so the command's first syllable survives whole.
        """
        if not wake_gated:
            self._wake_meta = None  # not a wake session — never attach stale wake evidence
        self._reset_barge()
        self._stop_ringback()
        if self._ws is None:
            # Orphaned. The ready chime means "I'm listening", and playing it for a
            # room we cannot hear is a lie — it is exactly what made a node that had
            # been cut off for two days look like a working one. Check the connection
            # BEFORE the cue, say so in the log, and stay quiet unless the household
            # configured a distinct offline sound.
            log.warning(
                "Wake word ignored — no server connection (last registered %s)",
                _ago(self._registered_at),
            )
            if self._player:
                self._player.abort()  # a waiting bed must not outlive the connection
                if self._offline_audio is not None:
                    self._player.play_pcm(self._offline_audio, interrupt=True, alert=True)
            self._state = _STATE_IDLE
            self._session_id = None
            self._followup_active = False
            return
        gate = wake_gated and not followup and self._wake_onset_frames > 0
        if self._player:
            # The thinking-gap window (keep_player) opens as a silent onset
            # gate while the waiting bed covers processing — the bed keeps
            # playing; it dies at onset CONFIRM (a real interjection) or when
            # the reply's TTS supersedes it, never at arm time.
            if not keep_player:
                self._player.abort()  # stop waiting sound if still playing
            if not gate and (not followup or self._capture_cue):
                self._player.play()  # wake sessions + record-after-the-tone flows chime
        self._end_dialog_after_tts = False  # a new turn began; drop any stale end cue
        self._state = _STATE_STREAMING
        self._session_id = session_id
        self._followup_active = followup
        self._silence_count = 0
        self._speech_frames = 0
        self._frame_count = 0
        if gate:
            # One-breath gate: send NOTHING and hold the chime. Continued speech
            # confirms via the onset machinery below (session opens silently,
            # buffered frames flushed); a silent window falls back to the classic
            # chime-then-listen flow in _handle_onset_frame's expiry branch.
            self._wake_gate = True
            self._onset_pending = True
            self._onset_run = 0
            self._onset_elapsed = 0
            self._onset_burst = 0
            self._onset_gap = 0
            self._onset_buf = list(gate_preroll or [])
            log.info("[%s] wake gate open (one-breath window)", session_id[:8])
            return
        if followup:
            # Stage 1 onset gate: send NOTHING yet. Frames buffer locally until
            # ~dialog_onset_ms of sustained speech confirms a real answer (then
            # audio_start + the buffered onset flush), or the reply window expires
            # (then followup_timeout + the local end cue — the server never hears
            # a session that never happened).
            self._onset_pending = True
            self._onset_run = 0
            self._onset_elapsed = 0
            self._onset_burst = 0
            self._onset_gap = 0
            self._onset_buf = []
            log.info("[%s] follow-up window open (onset-gated)", session_id[:8])
            return
        msg, _ = protocol.audio_start(session_id, self._room_id, *self._take_wake_meta())
        await self._ws.send(msg)
        log.info("[%s] streaming started", session_id[:8])

    async def _end_streaming(self, reason: str = "silence") -> None:
        if self._state != _STATE_STREAMING:
            return
        sid = self._session_id
        was_followup = self._followup_active
        was_pending = self._onset_pending
        was_wake_gate = self._wake_gate
        self._state = _STATE_IDLE
        self._session_id = None
        self._followup_active = False
        self._onset_pending = False
        self._wake_gate = False
        self._onset_buf = []
        if self._ws is not None and sid is not None:
            try:
                if was_pending:
                    # No audio_start was ever sent — there's no session to end.
                    # A held follow-up floor needs releasing; a wake gate was
                    # never announced to the server at all, so say nothing.
                    if not was_wake_gate:
                        await self._ws.send(protocol.followup_timeout())
                else:
                    await self._ws.send(protocol.audio_end(sid, reason))
            except Exception:
                pass
        log.info("[%s] streaming ended (%s)", (sid or "?")[:8], reason)
        if was_pending and was_wake_gate:
            # A wake gate that never resolved — cancelled (e.g. this node lost
            # co-audible arbitration). Nothing was announced, nothing chimed:
            # end in total silence. The waiting bed would claim work is
            # happening on a session that never existed.
            if reason == "server_stop":
                # openwakeword's score stays above threshold for a few frames
                # after the phrase ends. Dropping straight back to IDLE let
                # that tail re-fire a "wake" 6 ms after losing arbitration —
                # a solo candidate with a silent pre-roll (-90 dBFS), which
                # then chimed and answered anyway (seen live 2026-08-15).
                self._wake_refractory_until = (
                    asyncio.get_running_loop().time() + _ARB_REFRACTORY_S
                )
            return
        if was_followup:
            return  # dialog turns get a silent processing beat — never hold music
        # Only start the "processing" sound if we're still idle. On a fast reply the
        # server's tts_start can arrive during the audio_end send above and flip us
        # to TTS on the cmd loop; starting the waiting sound now would queue it behind
        # (or clip) the reply. _begin_tts/_begin_streaming move us out of IDLE.
        if self._state == _STATE_IDLE and self._player and self._waiting_audio is not None:
            # The waiting bed: plays ONCE (4.4.1 — 4.4 looped it, which turned a
            # short `sound_waiting` chime into a chime every couple of seconds).
            # The bundled 26 s clip covers a long request on its own; a spoken
            # cue still duck-mixes over whatever is left of it, and once it has
            # finished the cue is simply spoken.
            self._player.play_pcm(self._waiting_audio, bed=True)

    # ------------------------------------------------------------------
    # TTS helpers
    # ------------------------------------------------------------------

    async def _cancel_tts_task(self, keep_audio: bool = False) -> None:
        """Cancel + await any running TTS wait/drain task (its cancel handler
        stops streaming mode and aborts playback).

        ``keep_audio`` leaves the sound playing: used when a reply supersedes a
        spoken cue that is a few hundred ms from finishing, so the cue gets to
        say its last word instead of being cut (see _CUE_GRACE_MAX_S)."""
        task, self._tts_task = self._tts_task, None
        if task is not None and not task.done():
            self._keep_audio_on_cancel = keep_audio
            try:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            finally:
                self._keep_audio_on_cancel = False

    async def _reset_tts_state(self) -> None:
        """Force the TTS half of the audio state machine back to idle.

        Connection teardown calls this: a session may have died at ANY point,
        and a 4.4 streamed session must never leave the player latched in ring
        mode — the next buffered sound would play into the (empty) ring and be
        silent, and on half-duplex hardware `player.active` stuck true would
        suppress wake words forever."""
        await self._cancel_tts_task()
        if self._player is not None and self._tts_stream:
            self._player.stop_stream()
        if self._player is not None and self._player.bed_active:
            self._player.abort()  # a waiting bed must not outlive its connection
        self._tts_stream = False
        self._tts_stream_started = False
        self._tts_cue = False
        self._end_dialog_after_tts = False
        if self._state == _STATE_TTS:
            self._state = _STATE_IDLE
            self._session_id = None

    async def _begin_tts(
        self,
        session_id: str,
        sample_rate: int,
        channels: int,
        alert: bool = False,
        stream: bool = False,
        cue: bool = False,
    ) -> None:
        # A spoken cue ("Working on it.") that is nearly finished gets to say its
        # last word: the answer can land within a cue's length of the 5s mark, and
        # cutting it mid-syllable sounds broken. Bounded, and never for a streamed
        # reply (start_stream cuts the player outright); the wait itself happens
        # inside the drain task, not here.
        self._cue_grace_s = 0.0
        if not stream and self._player is not None:
            remaining = self._player.cue_remaining_s
            if 0.0 < remaining <= _CUE_GRACE_MAX_S:
                self._cue_grace_s = remaining
        # A prior streamed session's drain task may still be finishing — it
        # must not outlive into THIS session (it would stop the new ring and
        # stomp the fresh session's state ~100ms in). Same interrupt-the-old
        # semantics the buffered path gets from play_pcm(interrupt=True).
        await self._cancel_tts_task(keep_audio=self._cue_grace_s > 0.0)
        self._stop_ringback()  # a spoken reply (decline/timeout) supersedes ringing
        self._reset_barge()
        # A silent thinking-gap window is superseded by the reply: nothing was
        # ever sent for it, so stand it down quietly before playback starts.
        if self._followup_unbounded and self._onset_pending:
            self._onset_pending = False
            self._onset_buf = []
            self._followup_active = False
            self._session_id = None
        self._followup_unbounded = False
        # Deliberately NO queue drain here: _recv_loop enqueues binary frames the
        # instant they arrive, so this session's own head frames may already be in
        # _tts_q before the cmd loop processes tts_start — draining now would eat
        # them (the clipped-first-word bug). Stale frames from an *aborted* session
        # are cleared at abort time (_stop_tts_playback); the completion path
        # consumes the queue, so it's always clean by the time a session starts.
        self._tts_sample_rate = sample_rate
        self._tts_alert = alert  # alert audio (doorbell chime) beats mute
        # 4.4 streamed reply: play frames the moment they arrive (the intercom's
        # live ring-buffer path) instead of collecting until tts_end.
        self._tts_stream = stream
        self._tts_stream_started = False
        # 4.4 processing cue ("Working on it."): collected like a buffered reply, then
        # duck-mixed OVER the looping waiting bed at tts_end instead of cutting it.
        self._tts_cue = cue
        if stream and self._player is not None:
            self._player.start_stream()  # cuts the waiting sound, like interrupt=True
        self._state = _STATE_TTS
        self._session_id = session_id
        log.info(
            "[%s] TTS started (rate=%d ch=%d%s%s)",
            session_id[:8],
            sample_rate,
            channels,
            " streamed" if stream else "",
            " cue" if cue else "",
        )

    async def _end_tts(self, reason: str = "complete") -> None:
        if self._state != _STATE_TTS:
            return

        if self._tts_stream:
            # Streamed session: audio has been playing since the first frame;
            # tts_end just means "no more is coming" — drain the ring, then run
            # the normal completion path (_tts_wait_done owns the transition).
            if reason == "complete" and self._tts_stream_started:
                self._tts_task = asyncio.create_task(self._tts_wait_done(), name="tts_wait")
            else:
                await self._stop_tts_playback()
                log.info("TTS stopped (%s)", reason)
            return

        frames: list[bytes] = []
        while not self._tts_q.empty():
            try:
                frames.append(self._tts_q.get_nowait())
            except asyncio.QueueEmpty:
                break

        if reason == "complete" and frames:
            audio = np.frombuffer(b"".join(frames), dtype=np.int16)
            if self._tts_sample_rate != self._playback_rate:
                audio = _resample(audio, self._tts_sample_rate, self._playback_rate)
            if self._tts_cue and self._player is not None and self._player.overlay(audio):
                # Processing cue over a looping bed: the cue is queued INTO the
                # bed (ducked underneath, bed continues after) — nothing to wait
                # for, so this session is over now. The bed itself keeps playing
                # through the IDLE wait exactly like the plain waiting sound.
                self._tts_cue = False
                self._state = _STATE_IDLE
                self._session_id = None
                log.info("Processing cue mixed over waiting bed")
                return
            was_cue, self._tts_cue = self._tts_cue, False
            # Re-read what's LEFT of the cue now: _begin_tts only decided that the
            # cue was worth preserving, and the reply's audio can arrive seconds
            # later (synthesis time), by which point the cue is usually long done.
            # Using the stale figure would delay every answer by a whole cue length.
            grace, self._cue_grace_s = self._cue_grace_s, 0.0
            if grace > 0.0 and self._player is not None:
                grace = min(self._player.cue_remaining_s, _CUE_GRACE_MAX_S)
            if grace > 0.0:
                # Hold the reply for the tail of a still-playing cue, then start
                # it — inside the task, so the command loop (and STOP) stay live.
                self._tts_task = asyncio.create_task(
                    self._tts_wait_done(pre_delay=grace, pending=audio), name="tts_wait"
                )
                log.info("TTS playback starts in %.2fs (letting the cue finish)", grace)
                return
            if self._player:
                # Atomic interrupt: cut the waiting sound and start TTS from the
                # first sample in one swap, so a fast reply is never clipped.
                self._player.play_pcm(audio, interrupt=True, alert=self._tts_alert, cue=was_cue)
            self._barge_armed_at = time.monotonic()  # reply audio live (barge grace)
            # Stay in TTS state while audio plays; _tts_wait_done transitions to IDLE.
            self._tts_task = asyncio.create_task(self._tts_wait_done(), name="tts_wait")
            log.info("TTS playback started")
        else:
            # Interrupted before or during playback — stop immediately.
            await self._stop_tts_playback()
            log.info("TTS stopped (%s)", reason)

    async def _tts_wait_done(
        self,
        pre_delay: float = 0.0,
        pending: np.ndarray[Any, Any] | None = None,
    ) -> None:
        """Poll until _SoundPlayer finishes TTS, then return the node to IDLE.

        Uses asyncio.sleep so the task is truly cancellable — unlike
        run_in_executor(sd.wait), which blocks a thread that cannot be
        interrupted once started.

        ``pre_delay``/``pending`` hold a reply back for the tail of a spoken cue
        (see _CUE_GRACE_MAX_S) and then start it. The wait lives here, inside the
        task, rather than in the caller: the command loop stays free, so a wake
        word's STOP still cancels this task and cuts everything instantly.
        """
        completed = False
        was_stream = self._tts_stream
        tts_sid = self._session_id  # captured now — the finally below clears it
        try:
            if pre_delay > 0:
                await asyncio.sleep(pre_delay)  # let the cue finish its last word
            if pending is not None and self._player is not None:
                self._player.play_pcm(pending, interrupt=True, alert=self._tts_alert)
                self._barge_armed_at = time.monotonic()
            if was_stream:
                # Streamed session: wait for the ring to drain, allow the DAC
                # tail, then leave streaming mode.
                while self._player is not None and self._player.stream_pending:
                    await asyncio.sleep(0.05)
                await asyncio.sleep(0.1)
                if self._player is not None:
                    self._player.stop_stream()
            else:
                while self._player is not None and self._player.active:
                    await asyncio.sleep(0.05)
            completed = True
        except asyncio.CancelledError:
            if self._player is not None and not self._keep_audio_on_cancel:
                if was_stream:
                    self._player.stop_stream()
                self._player.abort()
            raise
        finally:
            self._tts_stream = False
            self._tts_stream_started = False
            self._tts_cue = False
            self._state = _STATE_IDLE
            self._session_id = None
            self._tts_task = None
            log.info("TTS playback complete")
        if completed and self._ws is not None:
            # Tell the server the audio actually FINISHED PLAYING (tts_end only
            # marked the last frame arriving; buffered audio plays on after it).
            # The server's group engagement stays in `speaking` until this
            # lands, so a wake elsewhere in the audio_group can still stop a
            # reply during its playback tail. Old servers ignore the frame; a
            # cancelled playback sends nothing (the interrupting wake already
            # owns the state).
            try:
                await self._ws.send(protocol.tts_done(tts_sid or ""))
            except Exception:
                pass
        # If a prompt asked us to capture one utterance (intercom consent or voice
        # enrollment), start capturing now that the prompt has finished playing.
        if completed and self._ringback_after_tts:
            self._ringback_after_tts = False
            log.info("Calling reply finished — starting ringback")
            self._start_ringback()
        elif completed and self._capture_after_prompt:
            self._capture_after_prompt = False
            log.info("Prompt finished — capturing the spoken reply")
            await self._begin_streaming(str(uuid.uuid4()), followup=True)
        # A multi-turn dialog ended during this reply: play the end-of-dialog cue now
        # that the final line has finished (deferred so it never clips the reply).
        elif completed and self._end_dialog_after_tts:
            self._end_dialog_after_tts = False
            if self._player is not None and self._dialog_end_audio is not None:
                log.info("Multi-turn dialog ended — playing end-of-dialog cue")
                self._player.play_pcm(self._dialog_end_audio, interrupt=True)

    async def _stop_tts_playback(self) -> None:
        """Cancel any in-progress TTS playback and return to IDLE."""
        self._stop_ringback()
        self._reset_barge()
        if self._tts_task and not self._tts_task.done():
            self._tts_task.cancel()
            try:
                await self._tts_task
            except asyncio.CancelledError:
                pass
        elif self._player is not None:
            # No wait-done task running (interrupted before playback started).
            if self._tts_stream:
                self._player.stop_stream()
            self._player.abort()
        self._tts_stream = False
        self._tts_stream_started = False
        self._tts_cue = False
        # Discard this aborted session's undelivered frames so they can't leak into
        # the next session (session start must never drain — see _begin_tts).
        while not self._tts_q.empty():
            try:
                self._tts_q.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._end_dialog_after_tts = False  # playback was cut; skip the stale end cue
        self._state = _STATE_IDLE
        self._session_id = None

    # ------------------------------------------------------------------
    # Intercom (live two-way call)
    # ------------------------------------------------------------------

    async def _ringback_loop(self) -> None:
        """Replay the ringback clip on a cadence (its own length, so the WAV's
        trailing silence paces the rings) until cancelled by an outcome."""
        period = 5.0
        if self._ringback_audio is not None and self._playback_rate > 0:
            period = max(1.0, len(self._ringback_audio) / self._playback_rate)
        try:
            while True:
                if self._player is not None and self._ringback_audio is not None:
                    self._player.play_pcm(self._ringback_audio, interrupt=True)
                await asyncio.sleep(period)
        except asyncio.CancelledError:
            raise

    def _start_ringback(self) -> None:
        if self._ringback_task is not None or self._ringback_audio is None:
            return
        self._ringback_task = asyncio.create_task(self._ringback_loop(), name="ringback")

    def _stop_ringback(self) -> None:
        self._ringback_after_tts = False
        task = self._ringback_task
        self._ringback_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _begin_intercom(self, peer_room: str) -> None:
        """Enter a live call: stream mic out continuously, play peer audio live."""
        self._capture_after_prompt = False
        # Stop whatever we were doing (likely playing the "calling…" reply or idle).
        if self._state == _STATE_TTS:
            await self._stop_tts_playback()
        elif self._state == _STATE_STREAMING:
            await self._end_streaming(reason="intercom")
        self._state = _STATE_INTERCOM
        self._session_id = str(uuid.uuid4())
        if self._player is not None:
            self._player.abort()  # cut any residual one-shot audio
            self._player.start_stream()  # switch to live streaming playback
            if self._connect_audio is not None:
                # Play the connect chime first by feeding it ahead of the live stream.
                self._player.feed(self._connect_audio)
        log.info("Intercom connected with '%s'", peer_room)

    async def _end_intercom(self, reason: str = "ended") -> None:
        if self._state != _STATE_INTERCOM:
            return
        self._state = _STATE_IDLE
        self._session_id = None
        if self._player is not None:
            self._player.stop_stream()  # back to one-shot playback
            if self._disconnect_audio is not None:
                self._player.play_pcm(self._disconnect_audio)
        log.info("Intercom ended (%s)", reason)

    # ------------------------------------------------------------------
    # Receive loop – inbound server messages → _cmd_q / _tts_q
    # ------------------------------------------------------------------

    def _persist_config_key(self, key: str, value: str) -> None:
        """Write a top-level scalar back into this node's config file (best effort)."""
        path = self._config_path
        if path is None:
            return
        try:
            text = path.read_text() if path.is_file() else ""
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_set_yaml_scalar(text, key, value))
        except OSError as exc:
            log.warning("Could not persist %s to %s: %s", key, path, exc)

    def _set_log_capture(self, on: bool) -> None:
        """Attach/detach the dashboard log ring buffer (idempotent).

        When attached the buffer captures down to ``log_capture_level`` (default
        debug) and the logger is lowered to match; when detached the logger is
        restored to the console ``log_level`` so an idle node carries no extra
        logging overhead.
        """
        from kenzy.logutil import install_ring_handler, remove_ring_handler

        if on and self._log_buffer is None:
            self._log_buffer = install_ring_handler(
                "kenzy", capacity=500, level=self._log_capture_level
            )
        elif not on and self._log_buffer is not None:
            remove_ring_handler(self._log_buffer, "kenzy", display_level=self._log_level)
            self._log_buffer = None

    def _apply_pulled_config(self, patch: dict[str, Any], initial: bool = False) -> None:
        """Apply server-pushed config to the running node.

        Live-tunable parameters (thresholds and VAD timing) always take effect
        immediately. Hardware/identity keys (audio_device, sample rates, wakeword
        models/VAD gate, sounds) are applied **only on the initial pull** — the
        node now initializes audio *after* this first config arrives, so those
        keys must be in place before ``_init_audio`` runs. A later live change to
        a hardware key is reported as needing a restart, not applied in place.
        """
        applied: list[str] = []
        fm = protocol.FRAME_MS

        # Room name is server-owned: adopt + persist it whenever the server pushes
        # a new one (on connect or via a live rename).
        if "room_id" in patch:
            new_room = str(patch["room_id"] or "").strip()
            if new_room and new_room != self._room_id:
                log.info("Server set room name: '%s' → '%s'", self._room_id, new_room)
                self._room_id = new_room
                self._persist_config_key("room_id", new_room)
                applied.append("room_id")

        if "wakeword_threshold" in patch:
            self._wakeword_threshold = float(patch["wakeword_threshold"])
            applied.append("wakeword_threshold")
        if "silence_rms_threshold" in patch:
            self._silence_rms = float(patch["silence_rms_threshold"])
            applied.append("silence_rms_threshold")
        if "vad_enabled" in patch:
            self._vad_enabled = bool(patch["vad_enabled"])
            applied.append("vad_enabled")
        if "silence_ms" in patch:
            self._silence_frames = max(int(patch["silence_ms"]) // fm, 1)
            applied.append("silence_ms")
        if "speech_min_ms" in patch:
            self._speech_min_frames = max(int(patch["speech_min_ms"]) // fm, 1)
            applied.append("speech_min_ms")
        if "no_speech_timeout_ms" in patch:
            self._no_speech_timeout_frames = max(int(patch["no_speech_timeout_ms"]) // fm, 1)
            applied.append("no_speech_timeout_ms")
        if "hard_cap_ms" in patch:
            self._hard_cap_frames = max(int(patch["hard_cap_ms"]) // fm, 1)
            applied.append("hard_cap_ms")

        if "dialog_no_speech_timeout_ms" in patch:
            self._dialog_no_speech_frames = max(int(patch["dialog_no_speech_timeout_ms"]) // fm, 1)
            applied.append("dialog_no_speech_timeout_ms")
        if "dialog_onset_ms" in patch:
            self._dialog_onset_frames = max(int(patch["dialog_onset_ms"]) // fm, 1)
            applied.append("dialog_onset_ms")
        if "dialog_onset_vad_threshold" in patch:
            self._dialog_onset_vad = float(patch["dialog_onset_vad_threshold"])
            applied.append("dialog_onset_vad_threshold")
        if "wake_onset_ms" in patch:
            # 0 = gate off (instant chime); no min-1 clamp, unlike the others.
            self._wake_onset_frames = int(patch["wake_onset_ms"]) // fm
            applied.append("wake_onset_ms")

        if "hardware_aec" in patch:
            self._hardware_aec = bool(patch["hardware_aec"])
            applied.append("hardware_aec")

        if "volume" in patch:
            self._volume = _volume_to_gain(patch["volume"])
            if self._player is not None:
                self._player.set_volume(self._volume)
            applied.append("volume")
        if "muted" in patch:
            self._muted = bool(patch["muted"])
            if self._player is not None:
                self._player.set_muted(self._muted)
            applied.append("muted")

        if "log_level" in patch:
            from kenzy.logutil import level_value, set_display_level

            self._log_level = level_value(patch["log_level"], self._log_level)
            set_display_level(self._log_level)
            applied.append("log_level")
        if "log_capture_level" in patch:
            from kenzy.logutil import level_value

            self._log_capture_level = level_value(
                patch["log_capture_level"], self._log_capture_level
            )
            if self._log_buffer is not None:  # update the live capture depth
                self._log_buffer.setLevel(self._log_capture_level)
                logging.getLogger("kenzy").setLevel(min(self._log_level, self._log_capture_level))
            applied.append("log_capture_level")

        if "keep_logs" in patch:
            self._set_log_capture(bool(patch["keep_logs"]))
            applied.append("keep_logs")

        # Watchdog thresholds are read on every tick, so they tune live. (Whether
        # the loop exists at all is decided at startup — `enabled` needs a restart.)
        if "mic_volume" in patch:
            self._mic_volume = _parse_mic_volume(patch["mic_volume"])
            applied.append("mic_volume")
            # Live: an amixer set is instant and touches no stream. Clearing the
            # key stops MANAGING the gain (the prior hardware state is unknowable
            # — documented, not reverted).
            if self._audio_ready and self._mic_volume is not None:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._apply_mic_volume(), name="micvol")
                except RuntimeError:
                    pass  # sync tests: recorded, applied at next audio init

        if "volume_buttons" in patch:
            self._mk_enabled = bool(patch["volume_buttons"])
            applied.append("volume_buttons")
        if "volume_button_device" in patch:
            self._mk_device = str(patch["volume_button_device"] or "auto")
            applied.append("volume_button_device")
        if "volume_button_step" in patch:
            self._mk_step = max(1, min(20, int(patch["volume_button_step"])))
            applied.append("volume_button_step")

        if isinstance(patch.get("addons"), dict):
            # 5.1: per-plugin config namespace. The server merges this dict
            # per-addon before pushing (never shallowly — the watchdog-dict
            # trap), so here it can be adopted wholesale.
            self._addons_cfg = {
                k: dict(v) for k, v in patch["addons"].items() if isinstance(v, dict)
            }
            applied.append("addons")

        if isinstance(patch.get("watchdog"), dict):
            wd = patch["watchdog"]
            if "warn_minutes" in wd:
                self._watchdog_warn_s = float(wd["warn_minutes"]) * 60.0
            if "wedge_minutes" in wd:
                self._watchdog_wedge_s = float(wd["wedge_minutes"]) * 60.0
            if "reexec_minutes" in wd:
                self._watchdog_reexec_s = float(wd["reexec_minutes"]) * 60.0
            applied.append("watchdog")

        restart_keys = {
            "audio_device",
            "capture_sample_rate",
            "playback_sample_rate",
            "wakeword_models",
            "wakeword_vad_threshold",
            "sound_ready",
            "sound_waiting",
            "sound_connect",
            "sound_disconnect",
            "sound_ringback",
            "sound_dialog_end",
            "sound_offline",
        }

        if initial:
            # First pull, before audio is built: apply hardware/identity keys to
            # the instance so _init_audio constructs the stream from them.
            if "audio_device" in patch:
                self._audio_device = patch["audio_device"]
                applied.append("audio_device")
            if "capture_sample_rate" in patch:
                self._capture_rate = int(patch["capture_sample_rate"])
                applied.append("capture_sample_rate")
            if "playback_sample_rate" in patch:
                self._playback_rate = int(patch["playback_sample_rate"])
                applied.append("playback_sample_rate")
            if "wakeword_models" in patch:
                self._wakeword_models = list(patch["wakeword_models"] or [])
                applied.append("wakeword_models")
            if "wakeword_vad_threshold" in patch:
                self._wakeword_vad_threshold = float(patch["wakeword_vad_threshold"])
                applied.append("wakeword_vad_threshold")
            if "sound_ready" in patch:
                self._sound_ready = str(patch["sound_ready"] or "ready.wav")
                applied.append("sound_ready")
            if "sound_waiting" in patch:
                sw = patch["sound_waiting"]
                self._sound_waiting = str(sw) if sw else None
                applied.append("sound_waiting")
            if "sound_connect" in patch:
                sc = patch["sound_connect"]
                self._sound_connect = str(sc) if sc else None
                applied.append("sound_connect")
            if "sound_disconnect" in patch:
                sd = patch["sound_disconnect"]
                self._sound_disconnect = str(sd) if sd else None
                applied.append("sound_disconnect")
            if "sound_dialog_end" in patch:
                sde = patch["sound_dialog_end"]
                self._sound_dialog_end = str(sde) if sde else None
                applied.append("sound_dialog_end")
            if "sound_offline" in patch:
                so = patch["sound_offline"]
                self._sound_offline = str(so) if so else None
                applied.append("sound_offline")
            deferred: list[str] = []
        else:
            deferred = sorted(restart_keys & patch.keys())

        if applied:
            log.info("Applied server config: %s", ", ".join(applied))
        if deferred:
            log.info("Server config needs restart (not applied live): %s", ", ".join(deferred))
        if not applied and not deferred:
            log.debug("Server config had no applicable keys")
        # Media keys derive from the media_* keys AND audio_device — re-sync
        # after every apply; it's a no-op unless one of its inputs changed.
        self._sync_mediakeys()
        # Same contract for plugin tasks: re-sync after every apply, no-op
        # unless a plugin's config slice changed.
        self._sync_plugins()

    async def _metrics_loop(self, ws: ClientConnection) -> None:
        """Report system metrics (cpu/ram/disk/temp) every ~30 s while connected.

        First sample goes out quickly so a fresh connection's fleet card isn't
        blank for half a minute. Exits quietly on any send failure — the recv
        loop owns detecting the dead connection.
        """
        delay = 2.0
        while True:
            await asyncio.sleep(delay)
            delay = 30.0
            m = self._sys_sampler.sample()
            try:
                await ws.send(protocol.metrics(**m))
            except Exception:
                return

    async def _recv_loop(self, ws: ClientConnection) -> None:
        # Explicit recv() instead of `async for` so that task cancellation
        # raises CancelledError here without triggering a WebSocket close
        # handshake that could block for close_timeout seconds.
        while True:
            try:
                raw = await ws.recv()
            except websockets.exceptions.ConnectionClosed as exc:
                # Keep the close code and reason. On a reconnect this is the ONLY
                # place the server's "why" appears, and swallowing it is what left a
                # silent join rejection looking identical to an ordinary drop.
                self._close_reason = _describe_close(exc)
                break
            if isinstance(raw, bytes):
                if self._state == _STATE_INTERCOM and self._player is not None:
                    # Live peer audio (16 kHz mono) → resample to the playback rate and
                    # feed the streaming buffer. (Stays out of the cmd queue: latency.)
                    audio = np.frombuffer(raw, dtype=np.int16)
                    self._player.feed(_resample(audio, protocol.SAMPLE_RATE, self._playback_rate))
                    continue
                # TTS frames ride the COMMAND queue so their order relative to
                # tts_start/tts_end is preserved end-to-end: back-to-back streams
                # (end₁, start₂, frames₂ on the wire) used to race — frames₂ could
                # reach _tts_q before the cmd loop processed end₁, bleeding one
                # session's head into another's tail. One queue, one order.
                try:
                    self._cmd_q.put_nowait({"type": "_pcm", "raw": raw})
                except asyncio.QueueFull:
                    pass  # drop under backpressure
                continue
            try:
                self._cmd_q.put_nowait(json.loads(raw))
            except (json.JSONDecodeError, asyncio.QueueFull):
                pass

    # ------------------------------------------------------------------
    # Command loop – processes messages from _cmd_q
    # ------------------------------------------------------------------

    async def _cmd_loop(self) -> None:
        while True:
            msg = await self._cmd_q.get()
            mtype = msg.get("type")

            if mtype == "_pcm":
                # In-order TTS audio (see _recv_loop). Only a live TTS session may
                # buffer frames; anything else is stale leftovers from an abort.
                if self._state == _STATE_TTS:
                    if self._tts_stream and self._player is not None:
                        # 4.4 streamed reply: straight into the live ring buffer —
                        # playback begins with the first sentence, not at tts_end.
                        audio = np.frombuffer(msg["raw"], dtype=np.int16)
                        if self._tts_sample_rate != self._playback_rate:
                            audio = _resample(audio, self._tts_sample_rate, self._playback_rate)
                        self._player.feed(audio)
                        if not self._tts_stream_started:
                            self._tts_stream_started = True
                            self._barge_armed_at = time.monotonic()  # reply audio live
                            log.info("TTS playback started (streamed)")
                    else:
                        try:
                            self._tts_q.put_nowait(msg["raw"])
                        except asyncio.QueueFull:
                            pass
                continue

            if mtype == protocol.MSG_CONFIG:
                # Also the join ack: on a reconnect (audio already up) the initial
                # config read is skipped, so this is where registration lands.
                self._mark_registered()
                self._apply_pulled_config(msg.get("config") or {})

            elif mtype == protocol.MSG_PLUGIN_EVENT:
                # 5.1: a plugin's server half addressing its node half. Routed
                # by plugin id to the module's on_server_event hook, as a task
                # — a slow plugin must not stall the command loop, and a
                # crashing one is its own failure alone.
                self._dispatch_plugin_event(msg)

            elif mtype == protocol.MSG_TRIGGER and self._state == _STATE_IDLE:
                sid = msg.get("session_id") or str(uuid.uuid4())
                log.info("Server trigger → session %s", sid[:8])
                await self._begin_streaming(sid)

            elif mtype == protocol.MSG_FORCE_WAKE:
                # Test/ops: behave as if openwakeword fired THIS INSTANT — the
                # real idle-wake path with real evidence (the pre-roll ring
                # holds this room's actual last second of audio), a real
                # arbitration announcement, and the real one-breath gate.
                # `trigger` bypasses all of that; this exercises it. Idle-only:
                # a forced wake mid-anything would be testing a synthetic state
                # no real wake can reach.
                if self._state == _STATE_IDLE and self._oww is not None:
                    log.info("Force-wake from server (test) — running the wake path")
                    self._wake_meta = (
                        *_wake_phrase_levels(list(self._idle_preroll)),
                        1.0,  # synthetic score, distinct in logs via model name
                    )
                    sid = str(uuid.uuid4())
                    await self._announce_wake(sid, "forced", 1.0)
                    await self._begin_streaming(
                        sid,
                        wake_gated=True,
                        gate_preroll=list(self._idle_preroll),
                    )
                    self._idle_preroll.clear()
                else:
                    log.info(
                        "Force-wake ignored (state=%s, audio_ready=%s)",
                        self._state,
                        self._oww is not None,
                    )

            elif mtype == protocol.MSG_STOP:
                if self._state == _STATE_STREAMING:
                    await self._end_streaming(reason="server_stop")
                elif self._state == _STATE_TTS:
                    await self._end_tts(reason="server_stop")
                elif self._state == _STATE_IDLE and self._player is not None:
                    self._player.abort()

            elif mtype == protocol.MSG_TTS_START:
                sid = str(msg.get("session_id") or uuid.uuid4())
                sample_rate = int(msg.get("sample_rate", 22050))
                channels = int(msg.get("channels", 1))
                await self._begin_tts(
                    sid,
                    sample_rate,
                    channels,
                    alert=bool(msg.get("alert")),
                    stream=bool(msg.get("stream")),
                    cue=bool(msg.get("cue")),
                )

            elif mtype == protocol.MSG_TTS_END:
                await self._end_tts(reason="complete")

            elif mtype == protocol.MSG_CALL_REQUEST:
                # Incoming call rings: arm consent capture. The server streams the
                # spoken prompt next; when it finishes playing, _tts_wait_done opens a
                # capture window for the yes/no answer. No audio is bridged yet.
                self._capture_after_prompt = True
                self._capture_cue = False  # the consent prompt is the cue; no beep on top
                log.info("Incoming call from '%s' — prompting for consent", msg.get("from_room"))

            elif mtype == protocol.MSG_CALL_CANCEL:
                self._capture_after_prompt = False
                if self._state == _STATE_TTS:
                    await self._stop_tts_playback()
                elif self._state == _STATE_STREAMING:
                    await self._end_streaming(reason="call_cancelled")
                log.info("Call cancelled")

            elif mtype == protocol.MSG_CALL_RINGING:
                # Caller side: ring while the target room is asked to accept. If
                # the "calling…" reply is still playing, start when it finishes;
                # otherwise start now.
                if self._state == _STATE_TTS:
                    self._ringback_after_tts = True
                else:
                    self._start_ringback()

            elif mtype == protocol.MSG_INTERCOM_START:
                self._stop_ringback()
                await self._begin_intercom(str(msg.get("peer_room", "")))

            elif mtype == protocol.MSG_INTERCOM_END:
                await self._end_intercom(reason=str(msg.get("reason", "ended")))

            elif mtype == protocol.MSG_SET_ROOM:
                new_room = str(msg.get("room_id", "")).strip()
                if new_room and new_room != self._room_id:
                    log.info("Server set room name: '%s' → '%s'", self._room_id, new_room)
                    self._room_id = new_room
                    self._persist_config_key("room_id", new_room)

            elif mtype == protocol.MSG_RESTART:
                # Re-exec ourselves: re-reads config and re-inits audio, with no
                # dependence on a service manager's restart policy.
                log.warning("Server requested restart — re-executing node")
                os.execv(sys.executable, [sys.executable, *sys.argv])

            elif mtype == protocol.MSG_DISABLE:
                # Self-disable via systemd (disable --now) so Restart= can't
                # resurrect us — the same mechanic services use. systemd then
                # stops this process; the normal signal path shuts us down.
                from kenzy.unitctl import disable_unit

                log.warning(
                    "Server requested DISABLE — stopping; re-enable with "
                    "`systemctl --user enable --now kenzy-node.service` on this host"
                )
                ok, out = await asyncio.to_thread(disable_unit, "kenzy-node.service")
                if not ok:
                    log.error("Self-disable failed (not a systemd install?): %s", out)

            elif mtype == protocol.MSG_UPGRADE:
                # pip-upgrade kenzy[node] (honoring constraints + the version pin), then
                # re-exec to load the new code. On failure we stay on the old version.
                from kenzy.upgrade import run_pip_upgrade

                version = msg.get("version") or None
                log.warning("Server requested upgrade (%s) — installing…", version or "latest")
                # Carry the mediakeys extra ONLY when evdev is already importable:
                # a node that has it keeps it upgraded, and a node that doesn't
                # never grows a source build mid-upgrade (evdev is a C extension —
                # adding it here unconditionally would break upgrades on any Pi
                # without gcc, which is exactly why it isn't in the node extra).
                extra = "node"
                try:
                    import evdev  # type: ignore[import-untyped, import-not-found]  # noqa: F401

                    extra = "node,mediakeys"
                except ImportError:
                    pass
                ok, output = await run_pip_upgrade(extra, version)
                if ok:
                    log.warning("Upgrade installed — re-executing node")
                    os.execv(sys.executable, [sys.executable, *sys.argv])
                else:
                    tail = output.splitlines()[-1] if output else "see logs"
                    log.error("Node upgrade failed: %s", tail)

            elif mtype == protocol.MSG_REQUEST_LOGS and self._ws is not None:
                lv = logging.getLevelNamesMapping().get(str(msg.get("level", "")).upper(), 0)
                limit = int(msg.get("limit", 200))
                entries = self._log_buffer.tail(lv, limit) if self._log_buffer else []
                await self._ws.send(protocol.node_logs(str(msg.get("request_id", "")), entries))

            elif mtype == protocol.MSG_TUNE_START:
                # Calibration only runs on an idle node with working audio.
                if self._state == _STATE_IDLE and self._oww is not None:
                    self._start_tuning(float(msg.get("seconds", 20.0)))
                else:
                    log.info(
                        "Ignoring tune_start (state=%s, audio_ready=%s)",
                        self._state,
                        self._oww is not None,
                    )

            elif mtype == protocol.MSG_TUNE_STOP:
                self._stop_tuning()

            elif mtype == protocol.MSG_EXPECT_UTTERANCE:
                # Arm one-shot capture: the next TTS prompt's completion opens a
                # capture window. cue=True chimes when it opens (enrollment's
                # record-after-the-tone); cue=False opens silently (dialog turns,
                # where the prompt itself is the cue). Absent = legacy chime.
                self._capture_after_prompt = True
                self._capture_cue = bool(msg.get("cue", True))
                # v6 follow-up (immediate): open the window NOW — the user may
                # interject while the server is still thinking. Onset-gated and
                # silent, with no expiry (superseded by the reply's TTS or
                # consumed by speech). _capture_after_prompt stays set so the
                # eventual reply is still floor-holding as usual.
                if bool(msg.get("immediate")) and self._state == _STATE_IDLE:
                    await self._begin_streaming(
                        str(uuid.uuid4()), followup=True, keep_player=True
                    )
                    self._followup_unbounded = True

            elif mtype == protocol.MSG_END_DIALOG:
                # A multi-turn dialog ended. The conversation is OVER: nothing
                # may re-open the mic — clear the pending arm (set by any
                # expect_utterance this conversation sent, including the
                # thinking-gap ones) and stand down an open silent window, or
                # the farewell's own playback re-opens a window on a closed
                # conversation (found live: "are you still listening?" — yes,
                # embarrassingly, she was).
                self._capture_after_prompt = False
                self._followup_unbounded = False
                if self._onset_pending:
                    self._onset_pending = False
                    self._onset_buf = []
                    self._followup_active = False
                    self._session_id = None
                    if self._state == _STATE_STREAMING:
                        self._state = _STATE_IDLE
                # Play the end cue after any in-progress TTS (the final reply)
                # finishes, so it never clips the last line.
                if self._dialog_end_audio is None:
                    pass
                elif self._state == _STATE_TTS:
                    self._end_dialog_after_tts = True
                elif self._player is not None:
                    self._player.play_pcm(self._dialog_end_audio, interrupt=True)

    # ------------------------------------------------------------------
    # Audio loop – always running, routes frames by current state
    # ------------------------------------------------------------------

    async def _audio_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                frame: np.ndarray[Any, np.dtype[np.int16]] = await loop.run_in_executor(
                    None, lambda: self._raw_q.get(timeout=0.2)
                )
            except queue.Empty:
                continue

            flat = frame.flatten()
            # Downsample to 16 kHz if captured at a higher rate so openwakeword
            # and the server always receive standard protocol PCM.
            if self._capture_rate != protocol.SAMPLE_RATE:
                flat = _resample(flat, self._capture_rate, protocol.SAMPLE_RATE)

            # openwakeword runs on every frame regardless of state so that
            # mid-stream activations are forwarded to the server.
            if self._oww is not None:
                scores: dict[str, float] = await loop.run_in_executor(None, self._oww.predict, flat)
                # Calibration window: stream measurement scalars and DON'T act on
                # wake words (so the operator can repeat the wake word to gather
                # scores without starting sessions). Always IDLE while tuning.
                if self._tuning:
                    await self._emit_tune_sample(flat, scores, loop)
                    continue
                gate_armed_now = False  # this frame entered a gate buffer via preroll
                for name, score in scores.items():
                    if score >= self._wakeword_threshold:
                        if (
                            not self._hardware_aec
                            and self._player is not None
                            and self._player.active
                        ):
                            # Half-duplex hardware (hardware_aec: false): the mic
                            # hears our own output at full volume, so a wake hit
                            # during ANY local playback is untrustworthy — ignore
                            # it. Wake works again the instant playback ends.
                            log.debug("Wake hit ignored (no AEC, playback active)")
                            break
                        if (
                            self._state == _STATE_IDLE
                            and loop.time() < self._wake_refractory_until
                        ):
                            log.debug("Wake hit ignored (post-arbitration refractory)")
                            break
                        log.info("Wake word '%s' score=%.3f", name, score)
                        if self._state == _STATE_IDLE:
                            # Measure the phrase where it still exists: the
                            # pre-roll. Whatever flow the session takes from
                            # here (one-breath, classic pause), its audio_start
                            # carries this so co-audible nodes can be compared.
                            self._wake_meta = (
                                *_wake_phrase_levels([*self._idle_preroll, flat]),
                                float(score),
                            )
                            sid = str(uuid.uuid4())
                            # Announce the wake NOW, while the one-breath gate
                            # still holds the chime — the server's arbitration
                            # window for co-audible nodes lives inside that
                            # silence. (An old server ignores the frame; an
                            # orphaned node has no ws and skips.)
                            await self._announce_wake(sid, name, score)
                            # The hit frame (and one before it) ride along: the
                            # command's first syllable can start inside them.
                            await self._begin_streaming(
                                sid,
                                wake_gated=True,
                                gate_preroll=[*self._idle_preroll, flat],
                            )
                            self._idle_preroll.clear()
                            gate_armed_now = self._wake_gate
                        elif self._state == _STATE_STREAMING and self._wake_gate:
                            # The SAME utterance's score tail: openwakeword stays
                            # above threshold for several frames after the phrase
                            # ends. The gate is already open and this frame reaches
                            # it via the normal path below — restarting here would
                            # throw away everything buffered so far. (Rig finding:
                            # a low threshold rode the tail deep into the command
                            # and the transcript kept only its last word.)
                            pass
                        elif self._state == _STATE_STREAMING and self._onset_pending:
                            # Wake word instead of a follow-up answer: the user is
                            # starting over. Abandon the held floor (the server
                            # clears its turn counter) and open a fresh gated wake
                            # session. This is a WAKE, so it announces for
                            # arbitration like any other — this was the last
                            # wake-driven path with no wake_pending, and a
                            # field report (2026-08-17) showed exactly what an
                            # unannounced co-audible session costs: it can't be
                            # stood down. Evidence comes from the reply
                            # window's onset buffer (the pre-roll ring isn't
                            # fed while streaming).
                            self._wake_meta = (
                                *_wake_phrase_levels(
                                    [*self._onset_buf[-_WAKE_PREROLL_FRAMES:], flat]
                                ),
                                float(score),
                            )
                            sid = str(uuid.uuid4())
                            await self._announce_wake(sid, name, score)
                            self._onset_pending = False
                            self._onset_buf = []
                            self._followup_active = False
                            self._state = _STATE_IDLE
                            self._session_id = None
                            if self._ws is not None:
                                try:
                                    await self._ws.send(protocol.followup_timeout())
                                except Exception:
                                    pass
                            await self._begin_streaming(
                                sid, wake_gated=True, gate_preroll=[flat]
                            )
                            gate_armed_now = self._wake_gate
                        elif self._state == _STATE_STREAMING and self._ws is not None:
                            if self._player:
                                self._player.play()
                            try:
                                await self._ws.send(
                                    protocol.wakeword(self._session_id, name, score)
                                )
                            except Exception as exc:
                                log.warning("Wakeword send failed: %s", exc)
                        elif self._state == _STATE_TTS:
                            # Interrupt TTS: stop playback locally then start a
                            # new session.  on_session_start cancels the server
                            # pipeline so no STOP round-trip is needed. Same
                            # arbitration announcement as an idle wake — the
                            # pre-roll is fed during playback exactly so this
                            # path has phrase evidence. (The capture still
                            # starts from the hit frame alone, as it always
                            # has; the pre-roll is measurement, not audio.)
                            self._wake_meta = (
                                *_wake_phrase_levels([*self._idle_preroll, flat]),
                                float(score),
                            )
                            sid = str(uuid.uuid4())
                            await self._announce_wake(sid, name, score)
                            await self._stop_tts_playback()
                            await self._begin_streaming(
                                sid, wake_gated=True, gate_preroll=[flat]
                            )
                            self._idle_preroll.clear()
                            gate_armed_now = self._wake_gate
                        elif self._state == _STATE_INTERCOM:
                            # Wake word ends the call immediately (no command needed),
                            # then opens a fresh command session on this node.
                            if self._ws is not None:
                                try:
                                    await self._ws.send(protocol.intercom_end("wakeword"))
                                except Exception:
                                    pass
                            await self._end_intercom(reason="wakeword")
                            await self._begin_streaming(
                                str(uuid.uuid4()), wake_gated=True, gate_preroll=[flat]
                            )
                            gate_armed_now = self._wake_gate
                        break
                if gate_armed_now:
                    # This frame rode into the gate buffer as preroll; running it
                    # through the onset handler too would double-count it.
                    continue

            if self._state == _STATE_INTERCOM:
                if self._ws is None:
                    await self._end_intercom(reason="connection_lost")
                    continue
                try:
                    await self._ws.send(flat.tobytes())  # live mic → server relays to peer
                except Exception as exc:
                    log.warning("Intercom audio send failed: %s", exc)
                    await self._end_intercom(reason="connection_error")
                continue

            if self._state == _STATE_TTS:
                # Keep the rolling pre-roll fed during playback too: a wake
                # spoken OVER a reply (the TTS-interrupt path) needs the same
                # phrase evidence for co-audible arbitration as an idle wake —
                # its peers hear that utterance in their idle paths and send
                # wake_pending; without this, the interrupting node was
                # invisible to the window and both nodes answered.
                self._idle_preroll.append(flat)

            if (
                self._state == _STATE_TTS
                and self._capture_after_prompt  # a floor-holding reply is playing
                and self._hardware_aec  # can hear over her own voice
            ):
                await self._handle_barge_frame(flat)
                continue

            if self._state == _STATE_IDLE:
                # Rolling pre-roll for the wake gate (see _idle_preroll). Fed
                # while idle and during TTS (see above): the frames right
                # before a hit are all it may carry.
                self._idle_preroll.append(flat)
                continue

            if self._state == _STATE_STREAMING and self._onset_pending:
                await self._handle_onset_frame(flat)
                continue

            if self._state == _STATE_STREAMING:
                if self._ws is None:
                    # Lost connection mid-stream; reset state cleanly.
                    self._state = _STATE_IDLE
                    self._session_id = None
                    self._followup_active = False
                    continue

                try:
                    await self._ws.send(flat.tobytes())
                except Exception as exc:
                    log.warning("Audio send failed: %s", exc)
                    await self._end_streaming(reason="connection_error")
                    continue

                self._frame_count += 1

                if self._vad_enabled:
                    rms = float(np.sqrt(np.mean(flat.astype(np.float32) ** 2)))
                    # Per-frame hot path → TRACE (below DEBUG) so default debug
                    # capture isn't flooded; opt in with log_capture_level: trace.
                    log.log(
                        TRACE,
                        "Frame %d: RMS=%.1f speech=%d",
                        self._frame_count,
                        rms,
                        self._speech_frames,
                    )

                    if rms >= self._silence_rms:
                        self._speech_frames += 1
                        self._silence_count = 0
                    elif self._speech_frames >= self._speech_min_frames:
                        self._silence_count += 1

                    if self._frame_count >= self._hard_cap_frames:
                        await self._end_streaming(reason="hard_cap")
                    elif (
                        self._speech_frames < self._speech_min_frames
                        and self._frame_count >= self._no_speech_timeout_frames
                    ):
                        await self._end_streaming(reason="no_speech")
                    elif self._silence_count >= self._silence_frames:
                        await self._end_streaming(reason="silence")
                else:
                    pass  # stream until server sends STOP

    # ------------------------------------------------------------------
    # Server resolution (explicit config or mDNS discovery)
    # ------------------------------------------------------------------

    async def _discover_once(self) -> str | None:
        """One bounded mDNS browse, run on a throwaway daemon thread.

        Two hazards are handled here, both learned from a node that sat orphaned
        for two days while its wake word kept answering:

        * ``discover_server`` bounds its own wait, but the zeroconf socket setup
          and ``close()`` around it do not. Awaiting it unbounded parks the
          reconnect loop forever — no retry, no log, nothing server-side.
        * It must not run on the default executor. ``_audio_loop`` submits two
          ``run_in_executor`` calls per frame to that same pool, so a handful of
          wedged browses would starve the audio path and take the wake word down
          with the connection. A daemon thread costs nothing and cannot.
        """
        from kenzy.discovery import discover_server

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str | None] = loop.create_future()
        cancel = threading.Event()
        if self._discovery_cancel.is_set():
            cancel.set()  # already shutting down
        self._discovery_cancels.add(cancel)

        def _settle(value: str | None, exc: BaseException | None) -> None:
            if fut.done():
                return
            if exc is not None:
                fut.set_exception(exc)
            else:
                fut.set_result(value)

        def _work() -> None:
            try:
                url = discover_server(_DISCOVERY_TIMEOUT_S, cancel)
            except Exception as exc:  # zeroconf/socket failure — report, don't hang
                loop.call_soon_threadsafe(_settle, None, exc)
                return
            loop.call_soon_threadsafe(_settle, url, None)

        log.info("Discovering Kenzy server over mDNS…")
        threading.Thread(target=_work, daemon=True, name="mdns-discover").start()
        try:
            return await asyncio.wait_for(
                fut, timeout=_DISCOVERY_TIMEOUT_S + _DISCOVERY_GRACE_S
            )
        except TimeoutError:
            # The browse overran a deadline it should have enforced itself: zeroconf
            # is wedged. Tell the worker to unwind and carry on — we leak one daemon
            # thread at worst, and never the reconnect loop.
            cancel.set()
            log.warning("mDNS discovery timed out — treating as not found")
            return None
        finally:
            self._discovery_cancels.discard(cancel)

    async def _resolve_server_url(self) -> str:
        """Return the WebSocket URL: the configured value, else the address we last
        registered with, else mDNS discovery.

        Raises OSError when nothing resolves, so the caller's reconnect/backoff
        loop retries.
        """
        if self._server_url:
            return self._server_url
        # A server we were talking to minutes ago beats a fresh multicast query.
        # mDNS is a single point of failure on the way home and the node already
        # knows the address that worked, so the browse is the fallback, not the
        # only route. The cache is skipped once it has failed to register, so a
        # server that genuinely moved is still found.
        if self._cached_server_url and not self._cache_stale:
            log.info("Using last-known-good server %s", self._cached_server_url)
            return self._cached_server_url
        if not self._discovery_enabled:
            if self._cached_server_url:
                return self._cached_server_url
            raise OSError("no server_url configured and discovery is disabled")

        url = await self._discover_once()
        if url is None and self._cached_server_url:
            # Nothing answered, but we know where the server was. A stale address
            # beats no address; re-arm the cache so the next failure browses again.
            log.warning(
                "mDNS found nothing — retrying last-known-good %s", self._cached_server_url
            )
            self._cache_stale = False
            return self._cached_server_url
        if url is None:
            raise OSError("no Kenzy server found on the network (mDNS)")
        log.info("Discovered server at %s", url)
        return url

    def _mark_registered(self) -> None:
        """The server answered our hello with a config frame — we are joined.

        This is the one honest "the connection works" signal the node has: the
        server always pushes a config frame right after a successful hello, and a
        rejected join closes instead. Opening a socket proves nothing (a refused
        join gets a socket too), which is why the backoff, the URL cache and the
        watchdog all hang off this rather than off ``connect()``.
        """
        self._registered = True
        self._registered_at = time.monotonic()
        self._disconnected_at = 0.0  # the outage (if any) is over
        self._cache_stale = False
        # Re-deliver the media-keys endpoint status: the server's copy lives in
        # the connection session, so a server restart forgot it.
        self._push_mediakeys_status()
        url = self._connect_url
        if url and url != self._cached_server_url:
            self._cached_server_url = url
            _write_cached_server(url)
            log.info("Remembered server address %s", url)

    # ------------------------------------------------------------------
    # Media keys (5.0.4): speakerphone volume buttons → server volume_delta
    # ------------------------------------------------------------------

    async def _send_volume_delta(self, delta: int) -> None:
        """The watcher's only outlet. Registered connections only — a press on
        an orphaned node is dropped, not queued (stale volume moves arriving on
        reconnect would be worse than lost ones)."""
        ws = self._ws
        if ws is None or not self._registered:
            return
        await ws.send(protocol.volume_delta(delta))

    def _on_mediakeys_status(self, status: dict[str, Any]) -> None:
        self._mediakeys_status = status
        self._push_mediakeys_status()

    def _push_mediakeys_status(self) -> None:
        """Best-effort status frame carrying the endpoint state (dashboard line)."""
        ws = self._ws
        if ws is None or self._mediakeys_status is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        frame = protocol.status(
            audio_ok=not self._audio_failed,
            audio_error=self._audio_error,
            media_keys=self._mediakeys_status,
        )

        async def _send() -> None:
            try:
                await ws.send(frame)
            except Exception:
                pass

        loop.create_task(_send())

    def _sync_mediakeys(self) -> None:
        """Start/restart/stop the watcher to match config. Live-applied: called
        after every config apply; a no-op unless something it derives from
        changed. Safe without a loop (sync tests) — then it only records."""
        want: tuple[Any, ...] | None = (
            (self._mk_device, self._mk_step, self._audio_device) if self._mk_enabled else None
        )
        if want == self._mediakeys_built_from:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._mediakeys_task is not None:
            self._mediakeys_task.cancel()
            self._mediakeys_task = None
        self._mediakeys_built_from = want
        if want is None:
            if self._mediakeys_status is not None:
                self._mediakeys_status = {"enabled": False}
                self._push_mediakeys_status()
            return
        from kenzy.node.mediakeys import MediaKeyWatcher

        watcher = MediaKeyWatcher(
            step=self._mk_step,
            device_match=self._mk_device,
            audio_device=self._audio_device if isinstance(self._audio_device, str) else None,
            send_delta=self._send_volume_delta,
            on_status=self._on_mediakeys_status,
        )
        self._mediakeys_task = loop.create_task(watcher.run(), name="mediakeys")

    # ------------------------------------------------------------------
    # Plugins (5.1): node-role plugin tasks, run beside the node's loops
    # ------------------------------------------------------------------

    async def _plugin_send_event(self, plugin_id: str, payload: dict[str, Any]) -> None:
        """A node plugin's only outlet — a ``plugin_event`` frame to its server
        half. Registered connections only, best-effort: an event on an orphaned
        node is dropped, not queued (stale sensor events arriving on reconnect
        would be worse than lost ones)."""
        ws = self._ws
        if ws is None or not self._registered:
            return
        try:
            await ws.send(protocol.plugin_event(plugin_id, payload))
        except Exception as exc:
            log.debug("plugin_event send failed for %s: %s", plugin_id, exc)

    async def _run_plugin(self, plugin_id: str, run: Any, ctx: Any) -> None:
        """Run one plugin's ``node_run`` task, non-fatally — the same philosophy
        as ``_init_audio``: a plugin failing costs that plugin's capability,
        never the node. The error names the plugin so the journal says which."""
        try:
            await run(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Plugin '%s' task died: %s — node continues", plugin_id, exc, exc_info=True)

    def _sync_plugins(self) -> None:
        """Start/restart node-role plugin tasks to match config (the mediakeys
        pattern): called after every config apply, a no-op unless a plugin's
        config slice changed. Safe without a loop (sync tests) — then it only
        records."""
        if self._plugin_scan is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        for plugin in self._plugin_scan.for_role("node"):
            pid = plugin.manifest.id
            want = dict(self._addons_cfg.get(pid) or {})
            task = self._plugin_tasks.get(pid)
            if task is not None and not task.done() and self._plugins_built_from.get(pid) == want:
                continue
            if task is not None:
                task.cancel()
                self._plugin_tasks.pop(pid, None)
            self._plugins_built_from[pid] = want
            if not want.get("enabled", True):
                # The operator switched this add-on off for THIS node
                # (addons.<id>.enabled: false): no task, no device open, no
                # retry loop — without uninstalling the distribution. Generic
                # for every plugin; visible, once per config apply. Flipping it
                # back on live-applies the same way (this method runs after
                # every apply).
                log.info("Add-on '%s' disabled by config — node half not started", pid)
                continue
            ctx = self._node_plugin_ctx(pid, want)
            run = plugin.hook("node_run")
            if run is None:
                continue  # a panel-only or server-only dist also installed here
            self._plugin_tasks[pid] = loop.create_task(
                self._run_plugin(pid, run, ctx), name=f"addon-{pid}"
            )

    def _node_plugin_ctx(self, pid: str, cfg: dict[str, Any]) -> Any:
        """(Re)build and cache this plugin's context — shared by its run task
        and inbound server-half events."""
        from kenzy.plugins import NodePluginContext

        def _send(payload: dict[str, Any], _pid: str = pid) -> Any:
            return self._plugin_send_event(_pid, payload)

        ctx = NodePluginContext(
            node_id=self._node_id,
            config=cfg,
            send_event=_send,
            log=logging.getLogger(f"kenzy.addon.{pid}"),
        )
        self._plugin_ctxs[pid] = ctx
        return ctx

    def _dispatch_plugin_event(self, msg: dict[str, Any]) -> None:
        """An inbound ``plugin_event`` (server half → node half): route to the
        plugin's ``on_server_event`` hook as a task, fail-closed per plugin."""
        pid = str(msg.get("plugin") or "")
        plugin = self._plugin_scan.get(pid) if self._plugin_scan is not None else None
        hook = plugin.hook("on_server_event") if plugin is not None else None
        if hook is None:
            log.debug("plugin_event for absent node half (or no hook): %r", pid)
            return
        ctx = self._plugin_ctxs.get(pid)
        if ctx is None:  # no run task built one (a hook-only node half): build now
            ctx = self._node_plugin_ctx(pid, dict(self._addons_cfg.get(pid) or {}))
        payload = msg.get("payload")

        async def _run() -> None:
            try:
                await hook(ctx, payload if isinstance(payload, dict) else {})
            except Exception as exc:
                log.error("Plugin '%s' on_server_event failed: %s", pid, exc, exc_info=True)

        asyncio.get_running_loop().create_task(_run(), name=f"addon-evt-{pid}")

    # ------------------------------------------------------------------
    # Mic volume: the managed ALSA capture gain (unset = untouched)
    # ------------------------------------------------------------------

    def _resolved_input_name(self) -> str:
        """The actual input device's name (carries the hw:N the mixer needs),
        resolved the same way the stream resolves it. "" when unknowable."""
        try:
            if self._audio_device not in (None, ""):
                info = sd.query_devices(self._audio_device, "input")
            else:
                info = sd.query_devices(kind="input")
            return str(dict(info).get("name", ""))
        except Exception:
            return ""

    async def _apply_mic_volume(self) -> None:
        """Write the managed capture gain and report the outcome. Failure costs
        the setting, never the node — and the status says WHY (a device with no
        hw:N, a missing amixer, a card with no capture control)."""
        if self._mic_volume is None:
            return
        from kenzy.node.micvolume import set_capture_volume

        name = self._resolved_input_name()
        status = await asyncio.to_thread(set_capture_volume, name, self._mic_volume)
        self._micvol_status = status
        if status.get("applied"):
            log.info("Mic volume applied: %s", status.get("detail"))
        else:
            log.warning("Mic volume NOT applied: %s", status.get("detail"))
        self._push_micvol_status()

    def _push_micvol_status(self) -> None:
        """Best-effort status frame carrying the capture-gain outcome (the node
        page's status line — same pattern as the volume-button endpoint)."""
        ws = self._ws
        if ws is None or self._micvol_status is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        frame = protocol.status(
            audio_ok=not self._audio_failed,
            audio_error=self._audio_error,
            mic_volume=self._micvol_status,
        )

        async def _send() -> None:
            try:
                await ws.send(frame)
            except Exception:
                pass

        loop.create_task(_send())

    def _mark_disconnected(self) -> None:
        """A session ended — start the outage clock if we were actually joined.

        The mirror of ``_mark_registered``. Only a drop from a REGISTERED
        connection begins an outage: a join that was refused, or never completed,
        leaves an outage already in progress alone, so a run of failed attempts
        can't keep resetting the clock the watchdog counts on.
        """
        if self._registered:
            self._disconnected_at = time.monotonic()
        self._registered = False

    # ------------------------------------------------------------------
    # Audio hardware (built lazily after the first config pull)
    # ------------------------------------------------------------------

    async def _read_initial_config(self, ws: ClientConnection) -> dict[str, Any]:
        """Read inbound frames until the first ``config`` frame, returning its body.

        Any other control messages that arrive first are buffered onto ``_cmd_q``
        so the command loop sees them once it starts; binary frames (TTS) are
        ignored — none should precede config on a fresh connection.
        """
        while True:
            raw = await ws.recv()
            if isinstance(raw, bytes):
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == protocol.MSG_CONFIG:
                self._mark_registered()
                return msg.get("config") or {}
            try:
                self._cmd_q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    async def _init_audio(self) -> None:
        """Build wakeword model, sounds, output player, and mic input stream.

        Called once, after the first server config has been applied. Subsequent
        hardware-key changes require a restart (which re-runs this from scratch).
        """
        self._load_wakeword()

        sound_audio, sound_rate = _load_sound(self._sound_ready)
        self._player = _SoundPlayer(
            sound_audio,
            sound_rate,
            self._audio_device,
            self._playback_rate,
            volume=self._volume,
            muted=self._muted,
        )
        log.info(
            "Sound: %s (%d Hz → %d Hz stream)", self._sound_ready, sound_rate, self._playback_rate
        )

        if self._sound_waiting:
            try:
                wait_audio, wait_rate = _load_sound(self._sound_waiting)
                wait_1d = (
                    wait_audio.mean(axis=1).astype(np.int16)
                    if wait_audio.ndim > 1
                    else wait_audio.astype(np.int16)
                )
                self._waiting_audio = _resample(wait_1d, wait_rate, self._playback_rate)
                log.info(
                    "Waiting sound: %s (%d Hz → %d Hz)",
                    self._sound_waiting,
                    wait_rate,
                    self._playback_rate,
                )
            except Exception as exc:
                log.info("Waiting sound not loaded (%s) — silence during processing", exc)
        else:
            log.info("Waiting sound disabled — silence during processing")

        def _chime(name: str | None) -> np.ndarray[Any, Any] | None:
            if not name:
                return None
            try:
                a, r = _load_sound(name)
                mono = a.mean(axis=1).astype(np.int16) if a.ndim > 1 else a.astype(np.int16)
                return _resample(mono, r, self._playback_rate)
            except Exception as exc:
                log.info("Chime %s not loaded (%s)", name, exc)
                return None

        self._connect_audio = _chime(self._sound_connect)
        self._disconnect_audio = _chime(self._sound_disconnect)
        self._ringback_audio = _chime(self._sound_ringback)
        self._dialog_end_audio = _chime(self._sound_dialog_end)
        self._offline_audio = _chime(self._sound_offline)
        log.info(
            "Intercom chimes: connect=%s disconnect=%s; end-of-dialog=%s",
            self._sound_connect or "off",
            self._sound_disconnect or "off",
            self._sound_dialog_end or "off",
        )

        # Scale the blocksize so each callback still delivers ~80 ms of audio
        # regardless of the capture rate (e.g. 3840 samples at 48 kHz).
        capture_blocksize = int(protocol.FRAME_SAMPLES * self._capture_rate // protocol.SAMPLE_RATE)
        self._input_stream = sd.InputStream(
            samplerate=self._capture_rate,
            channels=protocol.CHANNELS,
            dtype="int16",
            blocksize=capture_blocksize,
            device=self._audio_device,
            callback=self._audio_callback,
        )
        self._input_stream.start()
        self._audio_task = asyncio.create_task(self._audio_loop(), name="audio")
        self._audio_ready = True
        log.info("Audio initialized from server config — node is live")
        # Re-apply the managed capture gain (if set) now that the device is
        # known — this is what makes mic_volume survive reboots and reinstalls,
        # unlike hand-set alsamixer state.
        if self._mic_volume is not None:
            await self._apply_mic_volume()

    def _teardown_audio(self) -> None:
        """Close any partially-initialized audio resources after a failed init."""
        if self._audio_task is not None:
            self._audio_task.cancel()
            self._audio_task = None
        if self._input_stream is not None:
            try:
                self._input_stream.abort()
                self._input_stream.close()
            except Exception:
                pass
            self._input_stream = None
        if self._player is not None:
            try:
                self._player.close()
            except Exception:
                pass
            self._player = None
        self._oww = None

    def _close_audio_hardware(self, timeout: float = 1.5) -> None:
        """Abort/close the audio streams off the main thread, bounded by ``timeout``.

        Some PortAudio/ALSA stacks block inside stream close; doing it in a daemon
        thread with a bounded join keeps shutdown prompt — if the close doesn't
        finish, the OS reclaims the device on process exit.
        """
        input_stream = self._input_stream
        player = self._player
        self._input_stream = None
        self._player = None
        if input_stream is None and player is None:
            return

        def _close() -> None:
            if input_stream is not None:
                try:
                    input_stream.abort()
                    input_stream.close()
                except Exception:
                    pass
            if player is not None:
                try:
                    player.close()
                except Exception:
                    pass

        t = threading.Thread(target=_close, daemon=True, name="audio-close")
        t.start()
        t.join(timeout)
        if t.is_alive():
            log.warning("Audio device close is slow — leaving it to process exit")

    def _device_capabilities(self) -> list[dict[str, Any]]:
        """Return the cached device probe (kicking it off if needed); never blocks.

        The probe runs in a daemon thread because PortAudio enumeration can be slow
        or hang. Until it finishes this returns ``[]``; the result is then pushed to
        the server via :meth:`_send_device_status` so a node that connected before the
        probe completed still gets its device list to the dashboard.
        """
        self._kick_device_probe()
        return self._device_probe or []

    def _kick_device_probe(self) -> None:
        if self._device_probe is not None or self._device_probe_started:
            return
        self._device_probe_started = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        def _probe() -> None:
            try:
                from kenzy.node.devices import probe_devices

                result = probe_devices()
            except Exception as exc:
                log.warning("audio device probe failed: %s", exc)
                result = []
            self._device_probe = result
            if loop is not None:
                try:
                    loop.call_soon_threadsafe(self._on_device_probe_ready)
                except RuntimeError:
                    pass  # loop already closed (e.g. shutting down)

        threading.Thread(target=_probe, daemon=True, name="device-probe").start()

    def _on_device_probe_ready(self) -> None:
        # The first hello may have gone out before the probe finished; push the list.
        if self._ws is not None:
            asyncio.create_task(self._send_device_status())

    async def _send_device_status(self) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(
                protocol.status(
                    audio_ok=not self._audio_failed,
                    audio_error=self._audio_error,
                    devices=self._device_capabilities(),
                )
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Calibration telemetry (on-demand, time-boxed)
    # ------------------------------------------------------------------

    def _start_tuning(self, seconds: float) -> None:
        """Begin a bounded calibration window: stream RMS/wake/VAD scalars per frame.

        A standalone Silero VAD is spun up for the window because the live wakeword
        model only computes VAD when ``wakeword_vad_threshold > 0`` (default 0).
        """
        seconds = max(1.0, min(float(seconds), 120.0))
        if self._tune_vad is None:
            try:
                import openwakeword  # type: ignore[import-untyped]

                self._tune_vad = openwakeword.VAD()
            except Exception as exc:
                log.warning("VAD model unavailable for tuning (%s) — vad scores will be 0", exc)
                self._tune_vad = None
        self._tune_deadline = asyncio.get_running_loop().time() + seconds
        self._tune_seq = 0
        self._tuning = True
        log.info("Calibration window started (%.0fs)", seconds)

    def _stop_tuning(self) -> None:
        if not self._tuning:
            return
        self._tuning = False
        self._tune_vad = None
        log.info("Calibration window stopped")

    def _vad_score(self, flat: np.ndarray[Any, Any]) -> float:
        vad = self._tune_vad
        if vad is None:
            return 0.0
        try:
            vad(flat)
            return float(vad.prediction_buffer[-1]) if vad.prediction_buffer else 0.0
        except Exception:
            return 0.0

    async def _emit_tune_sample(
        self, flat: np.ndarray[Any, Any], scores: dict[str, float], loop: asyncio.AbstractEventLoop
    ) -> None:
        """Send one calibration sample; auto-stop (and tell the server) when expired."""
        if loop.time() >= self._tune_deadline:
            self._stop_tuning()
            if self._ws is not None:
                try:
                    await self._ws.send(protocol.tune_sample(stopped=True))
                except Exception:
                    pass
            return
        rms = float(np.sqrt(np.mean(flat.astype(np.float32) ** 2)))
        wake = float(max(scores.values())) if scores else 0.0
        vad = await loop.run_in_executor(None, self._vad_score, flat)
        self._tune_seq += 1
        if self._ws is not None:
            try:
                await self._ws.send(
                    protocol.tune_sample(rms=rms, wake=wake, vad=vad, seq=self._tune_seq)
                )
            except Exception as exc:
                log.warning("tune_sample send failed: %s", exc)
                self._stop_tuning()

    # ------------------------------------------------------------------
    # Per-connection session
    # ------------------------------------------------------------------

    async def _run_session(self, ws: ClientConnection) -> None:
        self._ws = ws

        # Drain stale commands from a previous session.
        while not self._cmd_q.empty():
            try:
                self._cmd_q.get_nowait()
            except asyncio.QueueEmpty:
                break

        capabilities = {
            "audio_device": self._audio_device,
            "capture_sample_rate": self._capture_rate,
            "playback_sample_rate": self._playback_rate,
            "devices": self._device_capabilities(),
            "unit": self._unit_info,
            # 5.1: which plugin halves this node carries, with version + API so
            # the server can flag a node whose half skews from its own (treated
            # like an incompatible install: features off, reason in Fleet).
            "plugins": [
                {"id": p.manifest.id, "version": p.version, "api": p.manifest.api}
                for p in (self._plugin_scan.for_role("node") if self._plugin_scan else ())
            ],
        }
        # Prove possession of the join token by signature — the raw token never
        # rides the hello. (Requires a >=3.12 server.)
        auth = None
        if self._join_token:
            from kenzy import serviceauth

            auth = serviceauth.sign_node_hello(self._join_token, self._node_id)
        await ws.send(
            protocol.hello(
                self._room_id,
                node_id=self._node_id,
                capabilities=capabilities,
                auth=auth,
                kenzy_version=kenzy_version(),
            )
        )
        log.info("Connected; sent hello as room '%s' (node %s)", self._room_id, self._node_id)

        # Zero-config bootstrap: on the very first connection, block until the
        # server pushes our config, then build the audio hardware from it. On
        # later reconnects audio already exists; the fresh config frame arrives
        # via the normal recv/cmd path and is applied live (hardware deferred).
        if not self._audio_ready:
            log.info("Waiting for server config before initializing audio…")
            cfg = await asyncio.wait_for(self._read_initial_config(ws), timeout=20.0)
            self._apply_pulled_config(cfg, initial=True)
            try:
                await self._init_audio()
                self._audio_failed = False
                self._audio_error = None
            except Exception as exc:
                # Non-fatal: audio hardware couldn't start (e.g. a bad audio_device).
                # Stay connected so the device can be corrected and the node
                # restarted from the dashboard; audio is retried on that restart (or
                # the next reconnect). Without this the node would die before its
                # command loop ran, leaving it unreachable exactly when it needs
                # fixing.
                self._audio_failed = True
                self._audio_error = str(exc)
                self._teardown_audio()
                log.error(
                    "Audio init failed (%s) — node stays connected for remote fix/restart",
                    exc,
                    exc_info=True,
                )
                try:
                    await ws.send(protocol.status(audio_ok=False, audio_error=str(exc)))
                except Exception:
                    pass

        recv_task = asyncio.create_task(self._recv_loop(ws), name="recv")
        cmd_task = asyncio.create_task(self._cmd_loop(), name="cmd")
        metrics_task = asyncio.create_task(self._metrics_loop(ws), name="metrics")
        try:
            await asyncio.wait({recv_task, cmd_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            metrics_task.cancel()
            # Close the socket first so _recv_loop's ws.recv() unblocks via
            # ConnectionClosed rather than being hard-cancelled mid-handshake.
            try:
                await asyncio.wait_for(ws.close(), timeout=2.0)
            except Exception:
                pass
            recv_task.cancel()
            cmd_task.cancel()
            try:
                await asyncio.gather(recv_task, cmd_task, return_exceptions=True)
            except asyncio.CancelledError:
                pass
            self._stop_ringback()
            # A connection that died mid-TTS leaves undelivered frames behind; drop
            # them so they can't prefix the next session's audio after reconnect
            # (session start must never drain — see _begin_tts).
            while not self._tts_q.empty():
                try:
                    self._tts_q.get_nowait()
                except asyncio.QueueEmpty:
                    break
            # 4.4: and it may have died mid-STREAMED-reply — force the TTS
            # state machine (incl. the player's ring mode) back to idle so the
            # reconnected node hears and speaks normally.
            await self._reset_tts_state()
            self._ws = None
            self._mark_disconnected()
            if self._close_reason:
                # Surfaced here, not swallowed in _recv_loop: a join refused for a
                # bad token or a stale clock reads identically to a network drop
                # unless the server's own words make it out to the log.
                log.warning("Connection closed: %s", self._close_reason)
                self._close_reason = None

    # ------------------------------------------------------------------
    # Reconnect watchdog
    # ------------------------------------------------------------------

    async def _say_goodbye(self, reason: str) -> None:
        """Tell the server this absence is on purpose, so it doesn't raise a fault
        over a restart we chose. Strictly best-effort and strictly bounded — we are
        on our way out, and a wedged socket must not delay that."""
        ws = self._ws
        if ws is None:
            return
        try:
            await asyncio.wait_for(ws.send(protocol.goodbye(reason)), timeout=1.0)
        except Exception:  # pragma: no cover - the socket is already going away
            pass

    def _reexec(self, why: str) -> None:
        """Last resort: replace this process. Re-reads config and rebuilds audio,
        and — unlike any amount of retry logic — clears whatever in-process state
        got us stuck. Same mechanic the server's restart command uses."""
        log.error("Watchdog: %s — re-executing node", why)
        try:
            self._close_audio_hardware()
        except Exception:  # pragma: no cover - best effort before exec
            pass
        os.execv(sys.executable, [sys.executable, *sys.argv])

    async def _watchdog_loop(self) -> None:
        """Notice, say so, and as a last resort restart.

        A node that cannot reach its server is invisible: the server has nothing
        to report about a node that never knocks, and the node's own audio keeps
        working, so nothing looks wrong from the room either. This loop is the
        node's own smoke alarm.

        Two distinct failures, deliberately handled differently:

        * The reconnect loop **stopped turning** — parked in an await that will
          never return. Nothing recovers that but a new process, so re-exec
          quickly (``wedge_minutes``).
        * The loop is turning but nobody answers — an ordinary server outage.
          Complain loudly on a schedule, and only re-exec after a much longer
          ``reexec_minutes`` (0 disables), so a server reboot doesn't make every
          room in the house flap.
        """
        warned_at = 0.0
        start = time.monotonic()
        while True:
            await asyncio.sleep(_WATCHDOG_TICK_S)
            now = time.monotonic()
            if self._registered:
                warned_at = 0.0
                continue

            # The reconnect loop should touch this every iteration, and it sleeps
            # at most 60 s between attempts. If it has gone quiet for minutes it is
            # not retrying — it is stuck.
            loop_quiet = now - (self._loop_alive_at or start)
            if self._watchdog_wedge_s and loop_quiet >= self._watchdog_wedge_s:
                await self._say_goodbye("watchdog restart")
                self._reexec(f"reconnect loop has not run for {int(loop_quiet)}s")
                return  # unreachable after execv; keeps the contract explicit

            # Measured from the START OF THIS OUTAGE, not from the last join: a node
            # that has been happily connected for hours is not hours overdue the
            # instant its socket drops. Falls back to process start for a node that
            # has never registered at all.
            down = now - (self._disconnected_at or start)
            if down >= self._watchdog_warn_s and now - warned_at >= self._watchdog_warn_s:
                warned_at = now
                log.error(
                    "No server connection for %s (last registered %s, trying %s)",
                    f"{int(down // 60)}m" if down >= 60 else f"{int(down)}s",
                    _ago(self._registered_at),
                    self._connect_url or "…",
                )
            if self._watchdog_reexec_s and down >= self._watchdog_reexec_s:
                await self._say_goodbye("watchdog restart")
                self._reexec(f"no server connection for {int(down)}s")
                return

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        # Audio hardware is NOT built here. The node connects first, pulls its
        # config from the server, and only then initializes audio (in
        # _init_audio, on the first connection). Until the server is reachable
        # and answers, the node blocks in this reconnect/backoff loop.

        # Handle SIGINT/SIGTERM via the loop so Ctrl+C cancels promptly: cancel the
        # run task AND signal any in-flight mDNS browse to return at once (its worker
        # thread is joined at interpreter exit, so a blocking browse would otherwise
        # delay shutdown by the full discovery timeout).
        loop = asyncio.get_running_loop()
        main_task = asyncio.current_task()

        if self._unit_info is None:
            from kenzy.unitctl import unit_state

            self._unit_info = await asyncio.to_thread(unit_state, "kenzy-node.service")

        def _request_stop() -> None:
            log.info("Shutdown signal received — stopping node")
            # SIGTERM is what `systemctl restart`, kenzy-deploy and a manual stop
            # all send, so this is where a *planned* absence gets announced. It has
            # to be its own task: cancelling the run task below would otherwise
            # take the send with it. run()'s teardown waits briefly for it.
            if self._ws is not None:
                self._goodbye_task = loop.create_task(self._say_goodbye("shutdown"))
            self._discovery_cancel.set()
            for ev in list(self._discovery_cancels):
                ev.set()  # release any in-flight browse on its own event
            if main_task is not None:
                main_task.cancel()
            # Safety net: if graceful shutdown wedges in a blocking C call (PortAudio /
            # ALSA stream close, a stuck thread join) the main thread can't process
            # further signals, so Ctrl+C would appear dead. A daemon timer force-exits
            # if we haven't stopped in time. (No-op if we exit cleanly first — daemon
            # threads don't keep the process alive.)
            if not self._force_exit_armed:
                self._force_exit_armed = True

                def _force_exit() -> None:
                    time.sleep(4.0)
                    log.warning("Graceful shutdown timed out — forcing exit")
                    os._exit(0)

                threading.Thread(target=_force_exit, daemon=True, name="force-exit").start()

        installed: list[Any] = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _request_stop)
                installed.append(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                pass  # unsupported (Windows / non-main thread) — fall back to KeyboardInterrupt

        # Start the (off-thread) audio-device probe now so its result is usually ready
        # by the time we send hello; if not, it's pushed via a status update.
        self._kick_device_probe()

        watchdog_task = (
            asyncio.create_task(self._watchdog_loop(), name="watchdog")
            if self._watchdog_enabled
            else None
        )

        try:
            delay = 1
            while True:
                # Proof of life for the watchdog: a loop that stops turning is
                # parked in an await that will never return, and only a fresh
                # process gets out of that.
                self._loop_alive_at = time.monotonic()
                registered_before = self._registered_at
                try:
                    server_url = await self._resolve_server_url()
                    self._connect_url = server_url
                    ssl_ctx = None
                    if server_url.startswith("wss://"):
                        # Encrypted-but-unverified by default: a self-signed LAN
                        # cert isn't verifiable without an installed CA chain.
                        # tls_verify: true / tls_ca: <path> opt into verification.
                        from kenzy import tlsutil

                        ssl_ctx = tlsutil.client_context(verify=self._tls_verify, ca=self._tls_ca)
                    ws = await websockets.connect(server_url, ssl=ssl_ctx)
                    await self._run_session(ws)

                except TimeoutError:
                    log.warning("Timed out waiting for server config; retrying")

                except (
                    websockets.exceptions.WebSocketException,
                    OSError,
                    ConnectionRefusedError,
                ) as exc:
                    log.warning("Connection error: %s", exc)

                except asyncio.CancelledError:
                    raise  # propagate to the graceful-shutdown handler below

                except Exception as exc:
                    log.error("Unexpected error: %s", exc, exc_info=True)

                finally:
                    if self._state == _STATE_STREAMING:
                        self._state = _STATE_IDLE
                        self._session_id = None

                if self._registered_at != registered_before:
                    # We actually joined this time round, so the next drop deserves a
                    # prompt retry. Resetting on connect() instead treated a refused
                    # join as success and turned it into a 1 s hot loop that also
                    # tripped the server's per-IP rate limiter.
                    delay = 1
                elif self._connect_url and self._connect_url == self._cached_server_url:
                    # The remembered address didn't get us registered — ask the
                    # network next time round rather than hammering a dead address.
                    self._cache_stale = True

                log.info("Reconnecting in %d s…", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

        except asyncio.CancelledError:
            log.info("Node shutting down…")  # graceful exit on signal/cancel

        finally:
            for sig in installed:
                try:
                    loop.remove_signal_handler(sig)
                except Exception:
                    pass
            # Give the goodbye frame its moment before we tear the socket down —
            # this runs after the CancelledError was caught, so it is a normal
            # await, not one racing its own cancellation.
            if self._goodbye_task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(self._goodbye_task), timeout=1.5)
                except (TimeoutError, asyncio.CancelledError, Exception):
                    pass
            # Guaranteed cleanup regardless of how we exit (normal return,
            # CancelledError from connect or sleep, unexpected exception).
            if watchdog_task is not None:
                watchdog_task.cancel()
                try:
                    await asyncio.gather(watchdog_task, return_exceptions=True)
                except asyncio.CancelledError:
                    pass
            if self._mediakeys_task is not None:
                self._mediakeys_task.cancel()
                try:
                    await asyncio.gather(self._mediakeys_task, return_exceptions=True)
                except asyncio.CancelledError:
                    pass
            if self._audio_task is not None:
                self._audio_task.cancel()
                try:
                    await asyncio.gather(self._audio_task, return_exceptions=True)
                except asyncio.CancelledError:
                    pass
            if self._tts_task and not self._tts_task.done():
                self._tts_task.cancel()
                try:
                    await self._tts_task
                except asyncio.CancelledError:
                    pass
            self._close_audio_hardware()
            log.info("Node client stopped.")


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def run_calibration(cfg: dict[str, Any], node_id: str) -> None:
    """Headless audio calibration: measure RMS / wake / VAD locally and print
    suggested thresholds. The values are **server-owned** (the node pulls config),
    so they're printed for the operator to apply via the dashboard or the server's
    per-node override — not written to the node's local file.
    """
    import time

    import sounddevice as sd  # type: ignore[import-untyped]

    capture_rate = int(cfg.get("capture_sample_rate", protocol.SAMPLE_RATE))
    audio_device = cfg.get("audio_device")
    models = list(cfg.get("wakeword_models") or []) or _bundled_model_paths()

    print("\nKenzy audio calibration")
    print("=" * 60)
    dev_label = audio_device if audio_device is not None else "system default"
    print(f"device: {dev_label}  |  capture: {capture_rate} Hz")

    oww: Any = None
    vad: Any = None
    try:
        import openwakeword  # type: ignore[import-untyped]
        from openwakeword.model import Model  # type: ignore[import-untyped]

        _ensure_oww_resources()
        oww = Model(
            wakeword_models=models, inference_framework=_infer_framework(models), vad_threshold=0.0
        )
        vad = openwakeword.VAD()
    except Exception as exc:
        print(f"\nWake-word/VAD models unavailable ({exc}); measuring RMS only.")

    q: queue.Queue[Any] = queue.Queue(maxsize=200)

    def _cb(indata: Any, frames: int, t: Any, status: Any) -> None:
        try:
            q.put_nowait(indata.copy())
        except queue.Full:
            pass

    blocksize = int(protocol.FRAME_SAMPLES * capture_rate // protocol.SAMPLE_RATE)
    try:
        stream = sd.InputStream(
            samplerate=capture_rate,
            channels=1,
            dtype="int16",
            blocksize=blocksize,
            device=audio_device,
            callback=_cb,
        )
    except Exception as exc:
        print(f"\nERROR: could not open the audio device ({exc}).")
        print("Run 'kenzy-devices' to find a working device, then set audio_device.")
        return

    def _collect(seconds: float) -> tuple[list[float], list[float], list[float]]:
        rms: list[float] = []
        wake: list[float] = []
        vadv: list[float] = []
        while not q.empty():  # drop anything buffered before the phase
            try:
                q.get_nowait()
            except queue.Empty:
                break
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                frame = q.get(timeout=0.3)
            except queue.Empty:
                continue
            flat = frame.flatten()
            if capture_rate != protocol.SAMPLE_RATE:
                flat = _resample(flat, capture_rate, protocol.SAMPLE_RATE)
            rms.append(float(np.sqrt(np.mean(flat.astype(np.float32) ** 2))))
            if oww is not None:
                scores = oww.predict(flat)
                wake.append(float(max(scores.values())) if scores else 0.0)
            if vad is not None:
                vad(flat)
                vadv.append(float(vad.prediction_buffer[-1]) if vad.prediction_buffer else 0.0)
        return rms, wake, vadv

    def _countdown(msg: str, n: int = 3) -> None:
        print(f"\n{msg}")
        for i in range(n, 0, -1):
            print(f"  starting in {i}…", end="\r", flush=True)
            time.sleep(1)
        print("  measuring…          ")

    stream.start()
    try:
        # Phase 1: the quiet floor — with one retry if a loud burst poisons it
        # (same gate as the dashboard wizard).
        from kenzy.calibration import MIN_SPEECH_FRAMES, quiet_phase_bursty, speech_gate

        rms1: list[float] = []
        for attempt in (1, 2):
            _countdown("Phase 1/2 — stay QUIET to measure the room's noise floor (5s).")
            rms1, _, _ = _collect(5.0)
            if quiet_phase_bursty(rms1) and attempt == 1:
                print("  heard a noise — restarting the quiet phase…")
                continue
            break

        # Phase 2: the wake word doubles as the speech-level sample (both the
        # wake/VAD scores AND how loud YOUR VOICE is from where you speak). Runs
        # even without wake models — the silence math still needs speech RMS.
        prompt = (
            "say the wake word ('Hey Kenzy') a few times"
            if oww is not None
            else "speak a few sentences at normal volume"
        )
        _countdown(f"Phase 2/2 — {prompt}, from where you'd normally talk (12s).")
        rms2, wake_all, vad_all = _collect(12.0)

        gate = speech_gate(rms1)
        speech = [r for r in rms2 if r > gate]
        if len(speech) < MIN_SPEECH_FRAMES:
            print("  didn't hear enough speech — silence threshold left unchanged.")
            speech = []
        sil = _suggest_silence_rms(rms1, speech)
        verdict = _separation_verdict(rms1, speech)
        wk = _suggest_wake_threshold(wake_all)
        vd = _suggest_vad_threshold(vad_all)
    finally:
        try:
            stream.abort()
            stream.close()
        except Exception:
            pass

    print("\n" + "=" * 60)
    print("Suggested thresholds")
    print("=" * 60)
    if sil is not None:
        print(f"  silence_rms_threshold:  {sil}   # anchored to your voice level")
    elif speech:
        print("  silence_rms_threshold:  (noise and speech don't separate — kept; see below)")
    if wk is not None:
        print(f"  wakeword_threshold:     {wk}")
    elif oww is not None:
        print("  wakeword_threshold:     (no clear wake word heard — re-run and speak up)")
    if vd is not None:
        print(f"  wakeword_vad_threshold: {vd}   # needs a node restart to apply")
    if verdict is not None:
        print(f"\n  noise-to-speech separation: {verdict}")
        if verdict == "poor":
            print("  This room/mic can't reliably tell speech from noise — try moving the")
            print("  mic closer to where people talk, then re-run.")
    if _agc_suspected(rms1):
        print("\n  NOTE: the mic level drifted during the quiet phase — this device")
        print("  adjusts its own gain (AGC), so the suggestions above are")
        print("  deliberately conservative.")
    print()
    print("These are server-owned (the node pulls its config). Apply them from the")
    print("dashboard's Calibration panel, or add them on the SERVER to one of:")
    print(f"  - configs/nodes/{node_id}.yaml   (this node only)")
    print("  - server.yaml  ->  node_defaults:   (default for all nodes)")
    print("Editing this node's local node.yaml has no lasting effect.")


def main() -> None:
    import sys

    import yaml  # type: ignore[import-untyped]

    from kenzy.config import resolve_config, writable_config_path
    from kenzy.logutil import configure_logging, level_value

    args = sys.argv[1:]
    do_calibrate = "--calibrate" in args
    positional = [a for a in args if not a.startswith("-")]
    config_path = resolve_config("node", positional[0] if positional else None)
    try:
        with open(config_path) as fh:
            cfg = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        # Env-only bootstrap (stage d): no node.yaml at all — the environment
        # (KENZY_SERVER_URL / KENZY_SERVER_TOKEN / KENZY_NODE_ID) supplies
        # everything a node needs to start; the rest is pulled from the server.
        cfg = {}

    # Env vars override the file so a node can boot from the environment alone.
    _apply_node_env(cfg)

    # Console follows log_level (default info); the dashboard log buffer can go
    # deeper (log_capture_level) once the server enables capture (config-pull).
    # Calibration is quieter so its prompts/results stand out.
    display = logging.WARNING if do_calibrate else level_value(cfg.get("log_level"), logging.INFO)
    configure_logging(display, bool(cfg.get("verbose", False)))

    # Ensure a stable node_id, persisting a generated one to a writable config
    # file (redirected out of the packaged read-only default if needed).
    write_path = writable_config_path("node", config_path)
    node_id = _ensure_node_id(cfg, write_path)

    if do_calibrate:
        run_calibration(cfg, node_id)
        return

    try:
        asyncio.run(NodeClient(cfg, config_path=write_path).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
