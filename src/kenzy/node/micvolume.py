"""ALSA capture gain as a managed node key (``mic_volume``).

Playback volume has been canonical since 5.0.4 — server-owned, live-applied,
never an ALSA side-channel. Capture gain was the missing half: tuning it meant
SSH + alsamixer, it lived in no config or backup, and hand-set ALSA state can
reset on reboot. This module closes that by omission-violation: ``mic_volume``
(0–100) is an ordinary flat node key, applied to the device's ALSA capture
control at audio init and on live config pushes, so it survives reboots and
reinstalls like every other setting.

**Unset means untouched.** No value ⇒ the device's own gain is never written —
and *clearing* the key stops managing the gain rather than reverting it (the
previous hardware state is unknowable). The practical use is the co-audible
rooms recipe: lower the gain until the calibration meter shows the far room's
wake scores separated from the near room's, then set the threshold in the gap.

Linux-only (like volume buttons) and driven through ``amixer`` — no new Python
dependency (the evdev lesson: a C extension in the node path is a tax on every
install). Every failure path returns an explained status instead of raising or
going silently inert (the 5.0.4 diagnostics lesson: when the code knows why,
the text must say why):

- no ``amixer`` binary → says to install alsa-utils
- an audio device whose name carries no ``hw:N`` (Pulse/PipeWire alias) → says
  the card can't be resolved and what to do
- a card with no capture-volume control → names the controls it *did* find
- onboard AGC eating the setting is called out in the key's help text — that
  one the code cannot detect, only the calibration meter can.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from typing import Any

#: (rc, combined output). Injectable for tests; 127 = binary missing.
Runner = Callable[[list[str]], tuple[int, str]]


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "amixer not found"
    except subprocess.SubprocessError as exc:
        return 1, str(exc)


# PortAudio's ALSA device names carry the card ("Anker PowerConf S330: USB
# Audio (hw:2,0)") — the one honest bridge from a sounddevice name to amixer.
_HW_RE = re.compile(r"\(hw:(\d+),\d+\)")
_CONTROL_RE = re.compile(r"Simple mixer control '([^']+)'")


def card_from_device_name(name: str) -> int | None:
    m = _HW_RE.search(name or "")
    return int(m.group(1)) if m else None


def pick_capture_control(card: int, run: Runner = _run) -> tuple[str | None, list[str]]:
    """The first mixer control on ``card`` with a capture volume (``cvolume``),
    plus every control name found — so a miss can say what WAS there."""
    rc, out = run(["amixer", "-c", str(card), "scontrols"])
    if rc != 0:
        return None, []
    names = _CONTROL_RE.findall(out)
    for name in names:
        rc, out = run(["amixer", "-c", str(card), "sget", name])
        if rc == 0 and "cvolume" in out:
            return name, names
    return None, names


def set_capture_volume(
    device_name: str, percent: int, run: Runner = _run
) -> dict[str, Any]:
    """Set the capture gain for the device named ``device_name`` (a resolved
    sounddevice input name). Returns a status dict — ``applied`` plus a
    ``detail`` that always says what happened, especially when it didn't."""
    status: dict[str, Any] = {"applied": False, "control": "", "card": None, "detail": ""}
    pct = max(0, min(100, int(percent)))
    if not sys.platform.startswith("linux"):
        status["detail"] = "mic_volume is Linux-only (ALSA capture control)"
        return status
    card = card_from_device_name(device_name)
    if card is None:
        status["detail"] = (
            f"can't map audio device {device_name!r} to an ALSA card — its name carries "
            "no hw:N (a Pulse/PipeWire alias?); set audio_device to the hardware device"
        )
        return status
    status["card"] = card
    control, names = pick_capture_control(card, run)
    if control is None:
        rc, _ = run(["amixer", "-c", str(card), "scontrols"])
        if rc == 127:
            status["detail"] = "amixer not found — install alsa-utils on the node host"
        elif not names:
            status["detail"] = f"amixer lists no mixer controls on card {card}"
        else:
            status["detail"] = (
                f"no capture-volume control on card {card} (found: {', '.join(names)})"
            )
        return status
    # The `capture` selector matters on COMBINED controls (the Anker S330 is
    # one simple control carrying pvolume AND cvolume): without it, the write
    # can move the playback side too — a second volume truth behind Kenzy's
    # canonical playback volume, the exact 5.0.4 sin. It does NOT make sset
    # refuse a playback-only control (measured: it applies anyway) — that
    # safety is pick_capture_control only ever returning cvolume controls.
    rc, out = run(["amixer", "-c", str(card), "sset", control, f"{pct}%", "capture"])
    if rc != 0:
        status["control"] = control
        status["detail"] = f"amixer sset '{control}' failed: {out.strip()[:200]}"
        return status
    status.update(applied=True, control=control, detail=f"'{control}' on card {card} → {pct}%")
    return status
