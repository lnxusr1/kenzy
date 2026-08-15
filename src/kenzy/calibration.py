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


# The voice anchor assumes one thing: that the mic's gain at calibration time is
# the gain a real command will be captured at. Onboard AGC breaks that. The
# quiet phase runs right after the probe beep, so on an AGC device the floor
# CLIMBS as the gain recovers — and that same clamp-then-recover cycle is what a
# real command lives inside (ready chime → clamped gain → speech), while the
# wake phase measures speech at fully recovered gain. Measured on the EMEET M1A
# (2026-08-14): wake-phase speech suggested silence_rms_threshold 151–175, and
# every post-chime command then died as reason=silence/no_speech; ~60 works.
_AGC_RISE_RATIO = 2.0  # late-third floor this many times the early third ⇒ gain moved
_AGC_RISE_PAD = 25.0  # plus this absolute pad, so near-zero floors need a real rise
_AGC_MIN_FRAMES = 24  # ~2 s at 80 ms — fewer frames isn't a trend
_AGC_SILENCE_FLOOR = 40.0  # never suggest below this on the AGC path


def agc_suspected(quiet: list[float]) -> bool:
    """True when the quiet floor ROSE markedly across the phase — the signature
    of a device AGC recovering gain after the probe played through the
    co-located speaker. Levels from such a device are not comparable across
    phases, so the voice anchor measured in the wake phase must not be trusted.
    ``quiet`` must be in capture order."""
    if len(quiet) < _AGC_MIN_FRAMES:
        return False
    third = len(quiet) // 3
    early = percentile(quiet[:third], 0.5)
    late = percentile(quiet[-third:], 0.5)
    return late > early * _AGC_RISE_RATIO + _AGC_RISE_PAD


def suggest_silence(quiet: list[float], speech: list[float]) -> int | None:
    """``silence_rms_threshold`` anchored to the voice, clamped by the quiet floor;
    ``None`` when the two don't separate (the previous value should stand).

    Under AGC drift (``agc_suspected``) the voice anchor was measured at the
    wrong gain, so the suggestion is instead capped near the EARLY-quiet floor —
    the post-playback, gain-clamped state, which is exactly the state a real
    command is captured in after the ready chime."""
    if not quiet or not speech:
        return None
    anchor, floor = _anchor_floor(quiet, speech)
    if agc_suspected(quiet):
        early_q90 = percentile(quiet[: max(1, len(quiet) // 3)], 0.9)
        cap = max(early_q90 * 1.5, early_q90 + 15, _AGC_SILENCE_FLOOR)
        return int(max(5, min(5000, round(min(anchor, cap)))))
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


# An ambient VAD floor that already reads voice-like means the measurement is
# untrustworthy (AGC pumping room noise into the speech band — the EMEET M1A's
# wake phase read ambient VAD ~0.74 and suggested a 0.82 gate, which then
# suppressed genuinely quiet wakes). And no gate above 0.6 is worth having:
# past that it costs more real wakes than it filters.
_VAD_AMBIENT_TRUST = 0.5
_VAD_SUGGEST_CAP = 0.6


def suggest_vad(vad: list[float]) -> float | None:
    """``wakeword_vad_threshold`` below speech VAD, above the silence floor;
    ``None`` when the ambient floor is itself voice-like (keep the current
    setting rather than gate out quiet wakes)."""
    if not vad:
        return None
    ambient = percentile(vad, 0.75)
    if ambient >= _VAD_AMBIENT_TRUST:
        return None
    gap = max(vad) - ambient
    if gap < 0.15:
        return None
    return round(max(0.0, min(_VAD_SUGGEST_CAP, ambient + gap * 0.3)), 2)


MIN_ECHO_FRAMES = 8  # fewer probe frames than this (after warm-up) ⇒ no AEC verdict
ECHO_WARMUP_FRAMES = 8  # ~0.6 s discarded: hardware AEC must converge on a fresh echo path
# An UN-cancelled co-located speaker is heard near clipping (thousands of RMS).
# Requiring the residual to actually be that loud is what keeps device DSP from
# faking a "no AEC" verdict: the EMEET M1A's AGC pushes ambient alone to ~1400
# in a quiet room, and a device that gates its mic on playback makes the quiet
# baseline near-zero — either way the RELATIVE bars alone would cry "absent"
# over a residual that is nothing like a real un-cancelled beep.
AEC_ABSENT_FLOOR = 2000


def aec_verdict(quiet: list[float], echo: list[float]) -> bool | None:
    """Does this node's hardware cancel its own speaker output in the mic feed?

    ``echo`` is the mic RMS measured WHILE the node played a known signal
    through its own speaker. With hardware AEC the residual sits near the quiet
    floor; without it, the co-located speaker is heard loud. The wide gap
    between those cases is what makes auto-detection trustworthy — and the band
    between the thresholds returns ``None``: ambiguous evidence never flips the
    flag, in either direction. (Callers must also skip the probe when the node
    is muted or very quiet — a silent speaker looks exactly like perfect AEC.)

    Two device behaviors, both measured on the EMEET M1A (2026-08-14), bound
    what this comparison may assume:

    - **The two phases don't share a gain.** Onboard AGC moves the mic level
      between the quiet baseline and the probe, and some devices gate the mic
      entirely while nothing plays — so the baseline can be near-zero while
      the probe window carries ordinary AGC-lifted ambient. A cross-phase
      *relative* test alone therefore cannot prove absence; declaring "no AEC"
      additionally requires the residual to be as loud as an actually
      un-cancelled co-located beep (``AEC_ABSENT_FLOOR``).
    - **AEC converges.** The first fraction of a second of a fresh echo path
      leaks even on hardware whose cancellation is real; those frames are
      warm-up (``ECHO_WARMUP_FRAMES``), not evidence.
    """
    echo = echo[ECHO_WARMUP_FRAMES:]
    if not quiet or len(echo) < MIN_ECHO_FRAMES:
        return None
    q90 = percentile(quiet, 0.9)
    residual = percentile(echo, 0.75)
    if residual <= max(q90 * 3, q90 + 40):
        return True
    if residual >= max(q90 * 12, q90 + 300) and residual >= AEC_ABSENT_FLOOR:
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
