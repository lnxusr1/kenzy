"""
Server-side loading of short WAV cues (timer/alarm tones).

Schedule announcements are composed on the server — it synthesizes the spoken
part and streams raw 24 kHz mono int16 PCM to the node — so the lead-in tone is
prepended server-side too: no protocol change, gapless tone→voice sequencing,
and the tone still plays when the TTS service is down (an alarm that depends on
TTS being healthy isn't an alarm).

The bundled node sounds ship inside the package (extras gate dependencies, not
package data), so the server host always has them. Conversion to the TTS stream
format (mono-ize + linear resample) is stdlib-only: the ``server`` extra has no
numpy, and these are one-or-two-second files converted once and cached.
"""

from __future__ import annotations

import array
import logging
import wave
from pathlib import Path

log = logging.getLogger(__name__)

#: The TTS stream format nodes play (see ``_run_tts`` / ``_stream_pcm``).
TARGET_RATE = 24000

#: Bare filenames resolve against the bundled node sounds (shared with the node's
#: own sound_* keys, so the same names work in both).
BUNDLED_DIR = Path(__file__).resolve().parent.parent / "node" / "sounds"

_cache: dict[str, bytes | None] = {}


def clear_cache() -> None:
    """Forget cached tone PCM — cue regeneration rewrites files at the SAME
    paths, so the cache would otherwise keep serving the previous voice."""
    _cache.clear()


def load_tone(spec: str | None) -> bytes | None:
    """Load a WAV cue as 24 kHz mono int16 PCM bytes.

    ``spec`` is a bare filename (bundled sound) or a path **on the server host**
    (unlike the node-played ``sound_*`` keys, these are read where the schedule
    fires from). Empty/None ⇒ no tone. Failures log a warning once and return
    None (the announcement then plays voice-only). Results are cached.
    """
    if not spec:
        return None
    spec = str(spec)
    if spec in _cache:
        return _cache[spec]
    path = Path(spec)
    if len(path.parts) == 1 and not path.is_absolute():
        path = BUNDLED_DIR / spec
    out: bytes | None
    try:
        with wave.open(str(path), "rb") as w:
            channels = w.getnchannels()
            width = w.getsampwidth()
            rate = w.getframerate()
            frames = w.readframes(w.getnframes())
        if width != 2:
            raise ValueError(f"{width * 8}-bit samples unsupported (need 16-bit PCM)")
        samples = _to_mono(frames, channels)
        out = _resample(samples, rate, TARGET_RATE).tobytes()
    except Exception as exc:
        # Not a WAV (or a broken one): the optional decode path handles MP3 &
        # friends server-side (kenzy[sound]); nodes keep receiving plain PCM.
        from kenzy import soundfile

        out = soundfile.decode(path, rate=TARGET_RATE) if path.is_file() else None
        if out is None:
            log.warning("Could not load tone %r (%s): %s", spec, path, exc)
    _cache[spec] = out
    return out


def repeat_pcm(pcm: bytes, count: int, *, max_seconds: float = 60.0) -> bytes:
    """Repeat a cue ``count`` whole times (the count-shaped sibling of
    :func:`tile_pcm`), bounded by a duration cap."""
    if count <= 1 or not pcm:
        return pcm
    max_reps = max(1, int(max_seconds * TARGET_RATE * 2 // len(pcm)))
    return pcm * min(int(count), max_reps)


def resolve_sound(name: str, roots: list[Path]) -> Path | None:
    """Resolve a payload-supplied sound NAME within the operator's library
    roots — the security boundary of the sound system. Relative subpaths are
    fine (``alerts/dog.mp3``); absolute paths and traversal are rejected
    outright; the resolved file must live under one of the roots. First root
    with the file wins."""
    name = str(name or "").strip()
    if not name:
        return None
    p = Path(name)
    if p.is_absolute() or ".." in p.parts or name.startswith("."):
        return None
    for root in roots:
        try:
            candidate = (root / p).resolve()
            root_resolved = root.resolve()
        except OSError:
            continue
        if not candidate.is_relative_to(root_resolved):
            continue  # symlink escape — the roots list is the boundary
        if candidate.is_file():
            return candidate
    return None


def tile_pcm(pcm: bytes, seconds: float) -> bytes:
    """Repeat a cue to AT LEAST ``seconds`` of audio, in whole repeats — a
    doorbell cut off mid-"dong" sounds broken, so the last ring completes."""
    if not pcm or seconds <= 0:
        return pcm
    need = int(seconds * TARGET_RATE) * 2  # bytes of 24 kHz mono int16
    reps = max(1, -(-need // len(pcm)))
    return pcm * reps


def _to_mono(frames: bytes, channels: int) -> array.array[int]:
    samples: array.array[int] = array.array("h")
    samples.frombytes(frames)
    if channels <= 1:
        return samples
    return array.array(
        "h",
        (
            sum(samples[i : i + channels]) // channels
            for i in range(0, len(samples) - channels + 1, channels)
        ),
    )


def _resample(samples: array.array[int], src_rate: int, dst_rate: int) -> array.array[int]:
    """Linear-interpolation resample — plenty for a one-shot cue."""
    if src_rate == dst_rate or not samples:
        return samples
    n_out = max(1, int(len(samples) * dst_rate / src_rate))
    ratio = src_rate / dst_rate
    last = len(samples) - 1
    out: array.array[int] = array.array("h", bytes(2 * n_out))
    for j in range(n_out):
        pos = j * ratio
        i = int(pos)
        frac = pos - i
        s0 = samples[i if i <= last else last]
        s1 = samples[i + 1 if i + 1 <= last else last]
        out[j] = int(s0 + (s1 - s0) * frac)
    return out
