"""Shared audio-calibration math — the single source the three calibration
surfaces must agree on: the dashboard wizard (``dashboard_static/js/views/
calibrate.js`` mirrors these formulas in JS), the headless ``kenzy-node
--calibrate`` CLI, and the server's voice-guided flow ("Hey Kenzy, calibrate").

The silence threshold is two-sided and VOICE-anchored: the speaker's voice is
the stable quantity (ambient noise can rise long after calibration — a washing
machine — so the quiet floor is never the anchor, only a sanity clamp).
Stdlib-only so the server can import it without the node's audio dependencies.
"""

from __future__ import annotations

# Distance derate × margin: people calibrate closer to the mic than they later
# speak from (0.8), and the threshold sits ~6 dB below that voice level (/2).
_VOICE_ANCHOR_FACTOR = 0.8 / 2

QUIET_SECONDS = 6.0  # quiet-phase length (all surfaces)
WAKE_TARGET = 4  # wake-word repetitions asked for
WAKE_PEAK = 0.3  # a wake score this high counts as one attempt heard
MIN_SPEECH_FRAMES = 15  # fewer speech-level frames than this ⇒ don't trust the phase


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(q * (len(s) - 1))))]


def _anchor_floor(quiet: list[float], speech: list[float]) -> tuple[float, float]:
    anchor = _VOICE_ANCHOR_FACTOR * percentile(speech, 0.25)
    q90 = percentile(quiet, 0.9)
    return anchor, max(q90 * 1.5, q90 + 15)


def suggest_silence(quiet: list[float], speech: list[float]) -> int | None:
    """``silence_rms_threshold`` anchored to the voice, clamped by the quiet floor;
    ``None`` when the two don't separate (the previous value should stand)."""
    if not quiet or not speech:
        return None
    anchor, floor = _anchor_floor(quiet, speech)
    if anchor < floor:
        return None
    return int(max(5, min(5000, round(anchor))))


def separation_verdict(quiet: list[float], speech: list[float]) -> str | None:
    """``good`` / ``marginal`` / ``poor`` — how well the distributions separate."""
    if not quiet or not speech:
        return None
    anchor, floor = _anchor_floor(quiet, speech)
    ratio = anchor / max(floor, 1.0)
    return "good" if ratio >= 2 else "marginal" if ratio >= 1 else "poor"


def suggest_wake(wake: list[float]) -> float | None:
    """``wakeword_threshold`` in the gap between ambient scores (p75) and the
    utterance peak; ``None`` when no clear wake word was heard."""
    if not wake:
        return None
    ambient = percentile(wake, 0.75)
    gap = max(wake) - ambient
    if gap < 0.15:
        return None
    return round(max(0.05, min(0.95, ambient + gap * 0.4)), 2)


def suggest_vad(vad: list[float]) -> float | None:
    """``wakeword_vad_threshold`` below speech VAD, above the silence floor."""
    if not vad:
        return None
    ambient = percentile(vad, 0.75)
    gap = max(vad) - ambient
    if gap < 0.15:
        return None
    return round(max(0.0, min(0.9, ambient + gap * 0.3)), 2)


MIN_ECHO_FRAMES = 8  # fewer probe frames than this ⇒ no AEC verdict


def aec_verdict(quiet: list[float], echo: list[float]) -> bool | None:
    """Does this node's hardware cancel its own speaker output in the mic feed?

    ``echo`` is the mic RMS measured WHILE the node played a known signal
    through its own speaker. With hardware AEC the residual sits near the quiet
    floor; without it, the co-located speaker is heard loud. The wide gap
    between those cases is what makes auto-detection trustworthy — and the band
    between the thresholds returns ``None``: ambiguous evidence never flips the
    flag. (Callers must also skip the probe when the node is muted or very
    quiet — a silent speaker looks exactly like perfect AEC.)
    """
    if not quiet or len(echo) < MIN_ECHO_FRAMES:
        return None
    q90 = percentile(quiet, 0.9)
    residual = percentile(echo, 0.75)
    if residual <= max(q90 * 3, q90 + 40):
        return True
    if residual >= max(q90 * 8, q90 + 150):
        return False
    return None


def quiet_phase_bursty(quiet: list[float]) -> bool:
    """True when a loud burst (door slam, speech) poisoned the quiet phase —
    the phase should be re-run once."""
    return bool(quiet) and max(quiet) > percentile(quiet, 0.5) * 6 + 30


def speech_gate(quiet: list[float]) -> float:
    """Frames louder than this during the wake phase count as speech."""
    q90 = percentile(quiet, 0.9)
    return max(q90 * 2, q90 + 20)
