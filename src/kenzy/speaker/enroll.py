"""
kenzy-enroll: interactive speaker enrollment CLI.

Records several voice samples per speaker using VAD, converts each to
an embedding via the kenzy-speaker service, and stores the results.

The script uses voice TTS (pyttsx3) to speak instructions and prompts
aloud, and plays a beep tone before each recording starts.  If pyttsx3
is not installed, instructions are printed to the terminal instead.

Usage
-----
  kenzy-enroll --name alice [configs/speaker.yaml]
  kenzy-enroll --name alice --url http://server:8768 [configs/speaker.yaml]
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Any

import numpy as np
import sounddevice as sd  # type: ignore[import-untyped]

log = logging.getLogger(__name__)

DEFAULT_PROMPTS = [
    "The weather outside is looking pretty good today.",
    "Can you turn off the lights in the living room please.",
    "What time does the movie start tonight?",
    "I'd like to set a reminder for tomorrow morning at eight.",
    "Please add milk and eggs to the shopping list.",
]

# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

_SAMPLE_RATE = 24_000


def _play_beep(
    frequency: float = 880.0,
    duration: float = 0.25,
    volume: float = 0.4,
) -> None:
    """Play a short sine-wave beep to signal the start of recording."""
    n = int(_SAMPLE_RATE * duration)
    t = np.linspace(0, duration, n, endpoint=False)
    tone = np.sin(2 * np.pi * frequency * t)

    # Fade in/out over 10 ms to avoid clicks.
    fade = int(_SAMPLE_RATE * 0.010)
    ramp = np.linspace(0.0, 1.0, fade)
    tone[:fade] *= ramp
    tone[-fade:] *= ramp[::-1]

    samples = (tone * volume * 32767).astype(np.int16)
    sd.play(samples, samplerate=_SAMPLE_RATE)
    sd.wait()


def _play_confirm(frequency: float = 660.0, duration: float = 0.15) -> None:
    """Short lower-pitched confirmation tone after a successful recording."""
    _play_beep(frequency=frequency, duration=duration, volume=0.25)


def _record_utterance(
    sample_rate: int,
    silence_rms: float,
    silence_ms: int,
    min_speech_ms: int,
    max_duration_s: float = 15.0,
) -> bytes:
    """Block until speech is detected, then record until silence returns."""
    import queue as _queue

    frame_ms = 80
    frame_samples = sample_rate * frame_ms // 1000
    silence_frames = silence_ms // frame_ms
    min_speech_frames = min_speech_ms // frame_ms
    max_frames = int(max_duration_s * 1000 / frame_ms)

    q: _queue.Queue[np.ndarray[Any, Any]] = _queue.Queue()

    def _cb(indata: np.ndarray[Any, Any], frames: int, t: Any, status: Any) -> None:
        q.put(indata.copy())

    collected: list[np.ndarray[Any, Any]] = []
    speech_frames = 0
    silence_count = 0
    recording = False

    with sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="int16",
        blocksize=frame_samples,
        callback=_cb,
    ):
        print("  [listening…]", end="", flush=True)
        for _ in range(max_frames):
            try:
                frame = q.get(timeout=0.5)
            except _queue.Empty:
                continue

            rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))

            if rms >= silence_rms:
                if not recording:
                    recording = True
                    print("\r  [recording…]", end="", flush=True)
                speech_frames += 1
                silence_count = 0
                collected.append(frame)
            elif recording:
                silence_count += 1
                collected.append(frame)
                if silence_count >= silence_frames and speech_frames >= min_speech_frames:
                    break

    print("\r             \r", end="", flush=True)
    return np.concatenate(collected).flatten().tobytes()


# ---------------------------------------------------------------------------
# TTS helpers (OpenAI TTS, falls back to print-only)
# ---------------------------------------------------------------------------

_tts_url: str | None = None
_tts_timeout: float = 30.0


def _init_tts(cfg: dict[str, Any]) -> bool:
    """Read kenzy-tts service URL from config. Returns True if configured."""
    global _tts_url, _tts_timeout
    try:
        import httpx  # type: ignore[import-untyped]  # noqa: F401 — verify importable
        tcfg: dict[str, Any] = cfg.get("tts", {})
        url = tcfg.get("url")
        if not url:
            return False
        _tts_url = str(url)
        _tts_timeout = float(tcfg.get("timeout", 30.0))
        return True
    except Exception as exc:
        log.debug("TTS init failed: %s", exc)
        return False


def _speak(text: str, also_print: bool = True) -> None:
    """Speak text aloud via kenzy-tts and optionally print it."""
    if also_print:
        print(text)
    if not _tts_url:
        return
    try:
        import httpx  # type: ignore[import-untyped]
        resp = httpx.post(
            _tts_url,
            json={"text": text},
            timeout=_tts_timeout,
        )
        resp.raise_for_status()
        audio = np.frombuffer(resp.content, dtype=np.int16)
        sd.play(audio, samplerate=24_000)
        sd.wait()
    except Exception as exc:
        log.debug("TTS speak failed: %s", exc)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import base64

    import httpx  # type: ignore[import-untyped]
    import yaml  # type: ignore[import-untyped]

    parser = argparse.ArgumentParser(description="Enroll a speaker for kenzy-speaker.")
    parser.add_argument("--name", required=True, help="Speaker name to enroll")
    parser.add_argument("--url", default=None, help="kenzy-speaker base URL")
    parser.add_argument("config", nargs="?", default="configs/speaker.yaml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    cfg: dict[str, Any] = {}
    try:
        with open(args.config) as fh:
            cfg = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        log.warning("Config not found at %s — using defaults", args.config)

    base_url = args.url or f"http://{cfg.get('host', '127.0.0.1')}:{cfg.get('port', 8768)}"
    enroll_url = base_url.rstrip("/") + "/enroll"

    sample_rate = int(cfg.get("enroll_sample_rate", 16_000))
    silence_rms = float(cfg.get("enroll_silence_rms", 300))
    silence_ms = int(cfg.get("enroll_silence_ms", 800))
    min_speech_ms = int(cfg.get("enroll_min_speech_ms", 1_500))
    prompts: list[str] = cfg.get("enroll_prompts", DEFAULT_PROMPTS)

    tts_available = _init_tts(cfg)
    if not tts_available:
        print("(OpenAI TTS not available — voice guidance disabled, using text only)\n")

    # Introduction
    _speak(
        f"Hello {args.name}. I will guide you through recording {len(prompts)} "
        "short voice samples. Please speak naturally after each beep. "
        "Take your time — recording stops automatically when you finish speaking.",
        also_print=True,
    )
    time.sleep(0.5)
    _speak("Let's begin.", also_print=False)
    time.sleep(0.5)

    for i, prompt in enumerate(prompts, 1):
        print(f"\n[{i}/{len(prompts)}]")
        _speak(f"Please say: {prompt}", also_print=True)
        time.sleep(0.4)

        _play_beep()  # cue to start speaking

        pcm = _record_utterance(sample_rate, silence_rms, silence_ms, min_speech_ms)

        payload = {
            "audio_b64": base64.b64encode(pcm).decode(),
            "name": args.name,
        }
        try:
            resp = httpx.post(enroll_url, json=payload, timeout=30.0)
            resp.raise_for_status()
            count = resp.json()["sample_count"]
        except Exception as exc:
            _speak("There was a problem saving that sample. Please try again.", also_print=True)
            log.error("Enrollment failed for prompt %d: %s", i, exc)
            sys.exit(1)

        _play_confirm()  # lower-pitched "done" tone
        _speak(f"Got it. {len(prompts) - i} more to go." if i < len(prompts) else "That's the last one.", also_print=False)
        time.sleep(0.3)

    print()
    _speak(
        f"Enrollment complete. {count} voice samples have been saved for {args.name}.",
        also_print=True,
    )


if __name__ == "__main__":
    main()
