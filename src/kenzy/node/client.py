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
import json
import logging
import queue
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

from kenzy import protocol

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Silence / hard-cap thresholds (derived from protocol constants)
# ---------------------------------------------------------------------------


_STATE_IDLE = "idle"
_STATE_STREAMING = "streaming"
_STATE_TTS = "tts"

# Rate at which the server sends TTS PCM (fixed by the TTS service).
_TTS_SERVER_RATE = 24_000


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

# ---------------------------------------------------------------------------
# Bundled resource helpers
# ---------------------------------------------------------------------------

def _bundled_model_paths() -> list[str]:
    """Return real filesystem path to the bundled hey_kenzie.tflite wake-word model."""
    model_dir = files("kenzy.node").joinpath("models")
    return [str(model_dir.joinpath("hey_kenzie.tflite"))]


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
    ) -> None:
        self._sample_rate = sample_rate
        # Convert to mono then resample to the playback rate if needed.
        chime_1d = chime.mean(axis=1).astype(np.int16) if chime.ndim > 1 else chime.astype(np.int16)
        chime_1d = _resample(chime_1d, chime_rate, sample_rate)

        self._chime: np.ndarray[Any, Any] = chime_1d.reshape(-1, 1)
        self._audio: np.ndarray[Any, Any] = self._chime   # currently queued audio
        self._pending: np.ndarray[Any, Any] = self._chime # audio to switch to on restart
        self._pos: int = len(self._audio)                 # past end → silent
        self._restart: bool = False

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
        if self._restart and self._pos >= len(self._audio):
            self._restart = False
            self._audio = self._pending
            self._pos = 0
        remaining = len(self._audio) - self._pos
        if remaining <= 0:
            outdata[:] = 0
            return
        n = min(frames, remaining)
        outdata[:n] = self._audio[self._pos : self._pos + n]
        if n < frames:
            outdata[n:] = 0
            self._restart = False  # discard restart queued while audio was playing
        self._pos += n

    def play(self) -> None:
        """Play the chime."""
        self._pending = self._chime
        self._restart = True

    def play_pcm(self, audio: np.ndarray[Any, Any]) -> None:
        """Play arbitrary int16 mono PCM at _TTS_SAMPLE_RATE."""
        self._pending = audio.reshape(-1, 1)
        self._restart = True

    def abort(self) -> None:
        """Stop playback immediately."""
        self._restart = False
        self._pos = len(self._audio)

    @property
    def active(self) -> bool:
        return self._pos < len(self._audio) or self._restart

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()


# ---------------------------------------------------------------------------
# Node client
# ---------------------------------------------------------------------------


class NodeClient:
    """
    Async room-node client.  Call ``await client.run()`` to start; it loops
    forever with exponential-backoff reconnection.
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        self._server_url: str = cfg["server_url"]
        self._room_id: str = cfg["room_id"]
        self._wakeword_models: list[str] = cfg.get("wakeword_models", [])
        self._wakeword_threshold: float = float(cfg.get("wakeword_threshold", 0.5))
        self._silence_rms: float = float(cfg.get("silence_rms_threshold", 50.0))
        self._audio_device: str | int | None = cfg.get("audio_device", None)
        self._sound_ready:   str = str(cfg.get("sound_ready")   or "ready.wav")
        self._sound_waiting: str = str(cfg.get("sound_waiting") or "waiting.wav")
        self._capture_rate:  int = int(cfg.get("capture_sample_rate",  protocol.SAMPLE_RATE))
        self._playback_rate: int = int(cfg.get("playback_sample_rate", _TTS_SERVER_RATE))

        # Timing thresholds, all stored as frame counts (min 1 to avoid ≥0 always-true).
        self._vad_enabled: bool = bool(cfg.get("vad_enabled", True))
        self._silence_frames: int = max(int(cfg.get("silence_ms", 400)) // protocol.FRAME_MS, 1)
        self._speech_min_frames: int = max(int(cfg.get("speech_min_ms", 500)) // protocol.FRAME_MS, 1)
        self._no_speech_timeout_frames: int = max(int(cfg.get("no_speech_timeout_ms", 15_000)) // protocol.FRAME_MS, 1)
        self._hard_cap_frames: int = max(int(cfg.get("hard_cap_ms", 30_000)) // protocol.FRAME_MS, 1)

        # Thread-safe audio queue filled by the sounddevice callback.
        self._raw_q: queue.Queue[np.ndarray[Any, np.dtype[np.int16]]] = queue.Queue(maxsize=200)

        # Asyncio queue for inbound control messages from the server.
        self._cmd_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        # Queue for inbound TTS binary frames from the server.
        # No maxsize — dropping frames causes truncated playback for long responses.
        self._tts_q: asyncio.Queue[bytes] = asyncio.Queue()
        self._tts_sample_rate: int = 24000
        self._tts_channels: int = 1
        self._tts_task: asyncio.Task[None] | None = None

        self._state: str = _STATE_IDLE
        self._session_id: str | None = None
        self._ws: ClientConnection | None = None
        self._oww: Any = None  # openwakeword Model
        self._player: _SoundPlayer | None = None
        self._waiting_audio: np.ndarray[Any, Any] | None = None

        self._silence_count: int = 0
        self._speech_frames: int = 0
        self._frame_count: int = 0

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
                "openwakeword is not installed – run: pip install openwakeword"
            ) from exc

        _ensure_oww_resources()

        model_paths = self._wakeword_models if self._wakeword_models else _bundled_model_paths()
        framework = _infer_framework(model_paths)

        self._oww = Model(wakeword_models=model_paths, inference_framework=framework)
        log.info(
            "openwakeword loaded: %s (framework=%s)",
            [Path(p).name for p in model_paths],
            framework,
        )

    # ------------------------------------------------------------------
    # Streaming helpers
    # ------------------------------------------------------------------

    async def _begin_streaming(self, session_id: str) -> None:
        if self._player:
            self._player.abort()  # stop waiting sound if still playing
            self._player.play()
        self._state = _STATE_STREAMING
        self._session_id = session_id
        self._silence_count = 0
        self._speech_frames = 0
        self._frame_count = 0
        msg, _ = protocol.audio_start(session_id, self._room_id)
        if self._ws is None:
            self._state = _STATE_IDLE
            self._session_id = None
            return
        await self._ws.send(msg)
        log.info("[%s] streaming started", session_id[:8])

    async def _end_streaming(self, reason: str = "silence") -> None:
        if self._state != _STATE_STREAMING:
            return
        sid = self._session_id
        self._state = _STATE_IDLE
        self._session_id = None
        if self._ws is not None and sid is not None:
            try:
                await self._ws.send(protocol.audio_end(sid, reason))
            except Exception:
                pass
        log.info("[%s] streaming ended (%s)", (sid or "?")[:8], reason)
        if self._player and self._waiting_audio is not None:
            self._player.play_pcm(self._waiting_audio)

    # ------------------------------------------------------------------
    # TTS helpers
    # ------------------------------------------------------------------

    async def _begin_tts(self, session_id: str, sample_rate: int, channels: int) -> None:
        while not self._tts_q.empty():
            try:
                self._tts_q.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._tts_sample_rate = sample_rate
        self._tts_channels = channels
        self._state = _STATE_TTS
        self._session_id = session_id
        log.info("[%s] TTS started (rate=%d ch=%d)", session_id[:8], sample_rate, channels)

    async def _end_tts(self, reason: str = "complete") -> None:
        if self._state != _STATE_TTS:
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
            if self._player:
                self._player.abort()  # cut waiting sound immediately before TTS
                self._player.play_pcm(audio)
            # Stay in TTS state while audio plays; _tts_wait_done transitions to IDLE.
            self._tts_task = asyncio.create_task(self._tts_wait_done(), name="tts_wait")
            log.info("TTS playback started")
        else:
            # Interrupted before or during playback — stop immediately.
            await self._stop_tts_playback()
            log.info("TTS stopped (%s)", reason)

    async def _tts_wait_done(self) -> None:
        """Poll until _SoundPlayer finishes TTS, then return the node to IDLE.

        Uses asyncio.sleep so the task is truly cancellable — unlike
        run_in_executor(sd.wait), which blocks a thread that cannot be
        interrupted once started.
        """
        try:
            while self._player is not None and self._player.active:
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            if self._player is not None:
                self._player.abort()
            raise
        finally:
            self._state = _STATE_IDLE
            self._session_id = None
            self._tts_task = None
            log.info("TTS playback complete")

    async def _stop_tts_playback(self) -> None:
        """Cancel any in-progress TTS playback and return to IDLE."""
        if self._tts_task and not self._tts_task.done():
            self._tts_task.cancel()
            try:
                await self._tts_task
            except asyncio.CancelledError:
                pass
        elif self._player is not None:
            # No wait-done task running (interrupted before playback started).
            self._player.abort()
        self._state = _STATE_IDLE
        self._session_id = None

    # ------------------------------------------------------------------
    # Receive loop – inbound server messages → _cmd_q / _tts_q
    # ------------------------------------------------------------------

    async def _recv_loop(self, ws: ClientConnection) -> None:
        # Explicit recv() instead of `async for` so that task cancellation
        # raises CancelledError here without triggering a WebSocket close
        # handshake that could block for close_timeout seconds.
        while True:
            try:
                raw = await ws.recv()
            except websockets.exceptions.ConnectionClosed:
                break
            if isinstance(raw, bytes):
                try:
                    self._tts_q.put_nowait(raw)
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

            if mtype == protocol.MSG_TRIGGER and self._state == _STATE_IDLE:
                sid = msg.get("session_id") or str(uuid.uuid4())
                log.info("Server trigger → session %s", sid[:8])
                await self._begin_streaming(sid)

            elif mtype == protocol.MSG_STOP:
                if self._state == _STATE_STREAMING:
                    await self._end_streaming(reason="server_stop")
                elif self._state == _STATE_TTS:
                    await self._end_tts(reason="server_stop")

            elif mtype == protocol.MSG_TTS_START:
                sid = str(msg.get("session_id") or uuid.uuid4())
                sample_rate = int(msg.get("sample_rate", 22050))
                channels = int(msg.get("channels", 1))
                await self._begin_tts(sid, sample_rate, channels)

            elif mtype == protocol.MSG_TTS_END:
                await self._end_tts(reason="complete")

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
                scores: dict[str, float] = await loop.run_in_executor(
                    None, self._oww.predict, flat
                )
                for name, score in scores.items():
                    if score >= self._wakeword_threshold:
                        log.info("Wake word '%s' score=%.3f", name, score)
                        if self._state == _STATE_IDLE:
                            await self._begin_streaming(str(uuid.uuid4()))
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
                            # pipeline so no STOP round-trip is needed.
                            await self._stop_tts_playback()
                            await self._begin_streaming(str(uuid.uuid4()))
                        break

            if self._state == _STATE_STREAMING:
                if self._ws is None:
                    # Lost connection mid-stream; reset state cleanly.
                    self._state = _STATE_IDLE
                    self._session_id = None
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
                    log.debug("Frame %d: RMS=%.1f speech=%d", self._frame_count, rms, self._speech_frames)

                    if rms >= self._silence_rms:
                        self._speech_frames += 1
                        self._silence_count = 0
                    elif self._speech_frames >= self._speech_min_frames:
                        self._silence_count += 1

                    if self._frame_count >= self._hard_cap_frames:
                        await self._end_streaming(reason="hard_cap")
                    elif self._speech_frames < self._speech_min_frames and self._frame_count >= self._no_speech_timeout_frames:
                        await self._end_streaming(reason="no_speech")
                    elif self._silence_count >= self._silence_frames:
                        await self._end_streaming(reason="silence")
                else:
                    pass  # stream until server sends STOP

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

        await ws.send(protocol.hello(self._room_id))
        log.info("Connected; sent hello as room '%s'", self._room_id)

        recv_task = asyncio.create_task(self._recv_loop(ws), name="recv")
        cmd_task = asyncio.create_task(self._cmd_loop(), name="cmd")
        try:
            await asyncio.wait({recv_task, cmd_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
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
            self._ws = None

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._load_wakeword()
        sound_audio, sound_rate = _load_sound(self._sound_ready)
        self._player = _SoundPlayer(sound_audio, sound_rate, self._audio_device, self._playback_rate)
        log.info("Sound: %s (%d Hz → %d Hz stream)", self._sound_ready, sound_rate, self._playback_rate)

        try:
            wait_audio, wait_rate = _load_sound(self._sound_waiting)
            wait_1d = wait_audio.mean(axis=1).astype(np.int16) if wait_audio.ndim > 1 else wait_audio.astype(np.int16)
            self._waiting_audio = _resample(wait_1d, wait_rate, self._playback_rate)
            log.info("Waiting sound: %s (%d Hz → %d Hz)", self._sound_waiting, wait_rate, self._playback_rate)
        except Exception as exc:
            log.info("Waiting sound not loaded (%s) — silence during processing", exc)

        audio_task = asyncio.create_task(self._audio_loop(), name="audio")

        try:
            # Scale the blocksize so each callback still delivers ~80 ms of audio
            # regardless of the capture rate (e.g. 3840 samples at 48 kHz).
            capture_blocksize = int(
                protocol.FRAME_SAMPLES * self._capture_rate // protocol.SAMPLE_RATE
            )
            with sd.InputStream(
                samplerate=self._capture_rate,
                channels=protocol.CHANNELS,
                dtype="int16",
                blocksize=capture_blocksize,
                device=self._audio_device,
                callback=self._audio_callback,
            ):
                delay = 1
                while True:
                    try:
                        ws = await websockets.connect(self._server_url)
                        delay = 1
                        await self._run_session(ws)

                    except (
                        websockets.exceptions.WebSocketException,
                        OSError,
                        ConnectionRefusedError,
                    ) as exc:
                        log.warning("Connection error: %s", exc)

                    except asyncio.CancelledError:
                        raise  # propagate; outer finally handles cleanup

                    except Exception as exc:
                        log.error("Unexpected error: %s", exc, exc_info=True)

                    finally:
                        if self._state == _STATE_STREAMING:
                            self._state = _STATE_IDLE
                            self._session_id = None

                    log.info("Reconnecting in %d s…", delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)

        finally:
            # Guaranteed cleanup regardless of how we exit (normal return,
            # CancelledError from connect or sleep, unexpected exception).
            audio_task.cancel()
            try:
                await asyncio.gather(audio_task, return_exceptions=True)
            except asyncio.CancelledError:
                pass
            if self._tts_task and not self._tts_task.done():
                self._tts_task.cancel()
                try:
                    await self._tts_task
                except asyncio.CancelledError:
                    pass
            if self._player:
                self._player.close()
                self._player = None
            log.info("Node client stopped.")


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def setup() -> None:
    """Download openwakeword infrastructure models (melspectrogram, embedding, VAD).
    Run once after install, before starting the node for the first time."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    _ensure_oww_resources()
    log.info("Setup complete.")


def main() -> None:
    import sys

    import yaml  # type: ignore[import-untyped]

    config_path = sys.argv[1] if len(sys.argv) > 1 else "configs/node.yaml"
    with open(config_path) as fh:
        cfg = yaml.safe_load(fh)

    log_level: int = getattr(logging, str(cfg.get("log_level", "info")).upper(), logging.INFO)
    verbose: bool = bool(cfg.get("verbose", False))
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    # Root logger: verbose passes everything through; otherwise suppress
    # noisy third-party loggers (websockets, asyncio, sounddevice).
    logging.basicConfig(level=log_level if verbose else logging.WARNING, format=fmt)
    logging.getLogger("kenzy").setLevel(log_level)

    try:
        asyncio.run(NodeClient(cfg).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
