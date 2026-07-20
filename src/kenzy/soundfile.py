"""Audio file decoding beyond WAV (4.2 sound system).

WAV loads everywhere via the stdlib. Anything else (MP3, OGG, FLAC, M4A…)
decodes through the optional ``av`` package (PyAV — FFmpeg in a wheel,
``pip install 'kenzy[sound]'``). Decode happens on the SERVER,
where the files are read for streamed alerts/chimes/lead-ins — nodes receive
raw PCM and need nothing (no Pi-class codec pain).
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def available() -> bool:
    """Whether the decode dependency exists (feature-chip signal)."""
    import importlib.util

    try:
        return importlib.util.find_spec("av") is not None
    except (ImportError, ValueError):
        return False


def decode(path: str | Path, *, rate: int, channels: int = 1) -> bytes | None:
    """Decode any FFmpeg-supported audio file to int16 PCM at ``rate``/``channels``.

    Returns None (with one log line) when ``av`` is missing or decoding fails —
    callers degrade exactly as they would for an unreadable WAV.
    """
    try:
        import av  # type: ignore[import-untyped]
    except ImportError:
        log.warning(
            "Cannot decode %s: the 'av' package is not installed "
            "(pip install 'kenzy[sound]')",
            path,
        )
        return None
    try:
        layout = "mono" if channels == 1 else "stereo"
        out = bytearray()
        with av.open(str(path)) as container:
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(format="s16", layout=layout, rate=rate)
            for frame in container.decode(stream):
                for converted in resampler.resample(frame):
                    out.extend(bytes(converted.planes[0]))
            # Flush the resampler's tail.
            for converted in resampler.resample(None):
                out.extend(bytes(converted.planes[0]))
        return bytes(out) or None
    except Exception as exc:
        log.warning("Could not decode %s: %s", path, exc)
        return None
