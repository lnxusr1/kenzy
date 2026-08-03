"""USB speakerphone volume buttons (5.0.4) — the node-side input watcher.

A headless node has no media-key daemon, so a speakerphone's physical ``+``/``-``
buttons emit perfectly valid input events that nothing consumes. This module
consumes them — narrowly. The buttons move Kenzy's *canonical* volume by sending
``volume_delta`` to the server, which owns clamping, persistence and the config
push; nothing here touches an ALSA mixer or edits config, because two volume
truths is how dashboards start lying.

Selection is deliberately conservative (design: usb-speakerphone-controls.md):

* ``auto`` considers only input endpoints that are **USB siblings of the node's
  own audio device** and advertise standard volume keys. A keyboard with media
  keys, another room's device, or a second speakerphone must never qualify.
* Ambiguity does nothing — the candidates are reported so the operator can set
  an explicit match. ``/dev/input/eventN`` is never persisted (boot-assigned);
  explicit matches are by device name or phys, which are stable.
* No candidate, hot-unplug, missing evdev, or a non-Linux host is a clean no-op
  with a status line — never a node fault, never a capture/playback problem.

The selection logic is pure (``select_endpoint``) in the calibration.py mould;
only the scanners touch ``/sys`` and evdev. Bitmap decoding is left entirely to
evdev's capability API — the design doc records how hand-parsed ``/proc``
bitmaps misread word order on the first attempt.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Linux input key codes (stable ABI constants — mirrored here so the pure
#: selection layer needs no evdev import).
KEY_VOLUMEUP = 115
KEY_VOLUMEDOWN = 114

#: Minimum spacing between deltas sent to the server — even a pathologically
#: chatty HID device cannot turn a held button into a flood.
_MIN_SEND_INTERVAL_S = 0.1

#: Synthesized hold-to-repeat (typematic). Consumer Control HIDs commonly ship
#: WITHOUT EV_REP (the SP300U does: EV=13 — no repeat capability), so neither
#: the device nor the kernel repeats while a button is held — a desktop only
#: appears to because its media-key daemon synthesizes exactly this. The first
#: repeat waits long enough that a normal tap never double-steps.
_REPEAT_DELAY_S = 0.5
_REPEAT_PERIOD_S = 0.2

#: How long to wait before re-scanning after the endpoint disappears (unplug)
#: or when nothing qualified. Cheap enough to be patient.
_RESCAN_S = 30.0

_USB_DEV_RE = re.compile(r"^\d+-[\d.]+$")  # "1-3", "3-1.4.2" — a device, not an interface


@dataclass(frozen=True)
class Candidate:
    """One input endpoint, reduced to what selection needs."""

    path: str  # /dev/input/eventN — used to open, never persisted
    name: str
    phys: str
    usb_parent: str  # "" when not USB
    keys: frozenset[int] = field(default_factory=frozenset)

    @property
    def has_volume_keys(self) -> bool:
        return KEY_VOLUMEUP in self.keys or KEY_VOLUMEDOWN in self.keys


def usb_parent_of(sysfs_realpath: str) -> str:
    """The USB *device* component of a resolved sysfs path, or "".

    An audio interface and its Consumer Control HID interface are different USB
    interfaces ("1-3:1.0" vs "1-3:1.3"); their association is the shared
    physical parent ("1-3"). Interfaces carry a colon and are skipped.
    """
    parent = ""
    for part in sysfs_realpath.split("/"):
        if _USB_DEV_RE.match(part):
            parent = part  # keep the LAST match: hubs nest ("3-1", then "3-1.4")
    return parent


def select_endpoint(
    candidates: list[Candidate],
    audio_parent: str,
    explicit: str,
    sound_parents: set[str] | None = None,
) -> tuple[Candidate | None, str]:
    """Pick the one endpoint allowed to move this node's volume, or explain why not.

    Returns ``(choice, status)`` — status is a human-readable line either way,
    surfaced on the dashboard. Ambiguity always picks nothing.

    Auto has two tiers. When the configured audio device resolves to a USB
    parent, only that parent's siblings qualify (precise). When it doesn't —
    ``audio_device: default`` hides the card behind an ALSA alias — the
    fallback accepts a candidate only if it is the ONLY volume-keyed endpoint
    in the system whose USB parent also hosts a sound card: "the one
    speakerphone-shaped thing plugged in". A keyboard still never qualifies
    (no audio on its parent), and two speakerphones still refuse ambiguously.
    """
    if not candidates:
        # NO endpoints visible at all — almost always the node's user missing
        # the `input` group (/dev/input is root:input 0660), not a naming or
        # hardware problem. Said plainly because the old message ("no input
        # device matches …") blamed the configured name and sent the operator
        # hunting the wrong thing.
        return None, (
            "no input devices are readable — add the node's user to the 'input' "
            "group and restart it (group changes need a fresh login)"
        )
    explicit = (explicit or "auto").strip()
    if explicit.lower() != "auto":
        want = explicit.lower()
        hits = [c for c in candidates if want in c.name.lower() or want in c.phys.lower()]
        if not hits:
            return None, f"no input device matches {explicit!r}"
        if len(hits) > 1:
            names = ", ".join(sorted({c.name for c in hits}))
            return None, f"{explicit!r} is ambiguous ({names}) — be more specific"
        return hits[0], f"using {hits[0].name!r} (explicit match)"
    if audio_parent:
        sibs = [c for c in candidates if c.usb_parent == audio_parent and c.has_volume_keys]
        if not sibs:
            return None, "audio device has no sibling endpoint with volume keys"
        if len(sibs) > 1:
            names = ", ".join(sorted({c.name for c in sibs}))
            return None, f"ambiguous candidates ({names}) — set volume_button_device explicitly"
        return sibs[0], f"using {sibs[0].name!r} (auto: sibling of the audio device)"
    sibs = [
        c
        for c in candidates
        if c.usb_parent and c.usb_parent in (sound_parents or set()) and c.has_volume_keys
    ]
    if not sibs:
        return None, "no USB audio device has a sibling endpoint with volume keys"
    if len(sibs) > 1:
        names = ", ".join(sorted({c.name for c in sibs}))
        return None, f"ambiguous candidates ({names}) — set volume_button_device explicitly"
    return sibs[0], f"using {sibs[0].name!r} (auto: the only audio-device endpoint)"


# ---------------------------------------------------------------------------
# Scanners (Linux + evdev; every failure degrades to "no candidates")
# ---------------------------------------------------------------------------


def _scan_inputs() -> list[Candidate]:
    import evdev  # type: ignore[import-untyped, import-not-found]

    out: list[Candidate] = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
            try:
                keys = frozenset(dev.capabilities().get(evdev.ecodes.EV_KEY, []))
                sys_link = Path("/sys/class/input") / Path(path).name / "device"
                parent = usb_parent_of(str(sys_link.resolve())) if sys_link.exists() else ""
                out.append(
                    Candidate(
                        path=path,
                        name=str(dev.name or ""),
                        phys=str(dev.phys or ""),
                        usb_parent=parent,
                        keys=keys,
                    )
                )
            finally:
                dev.close()
        except OSError:
            continue  # raced an unplug, or no permission on this one
    return out


def sound_card_usb_parents() -> set[str]:
    """USB parents of every ALSA sound card — auto's fallback whitelist."""
    out: set[str] = set()
    for card in Path("/sys/class/sound").glob("card[0-9]*"):
        try:
            parent = usb_parent_of(str(card.resolve()))
        except OSError:
            continue
        if parent:
            out.add(parent)
    return out


def audio_usb_parent(audio_device: str | None) -> str:
    """The USB device component behind the configured ALSA/PortAudio device.

    Matches the configured name against ``/proc/asound/cards`` long names, then
    resolves ``/sys/class/sound/cardN``. A blank/"default" device or any parse
    failure returns "" — auto mode then reports honestly instead of guessing.
    """
    want = (audio_device or "").strip().lower()
    if not want or want == "default":
        return ""
    try:
        cards = Path("/proc/asound/cards").read_text()
    except OSError:
        return ""
    for m in re.finditer(r"^\s*(\d+)\s+\[[^\]]*\]:\s*(.*)$", cards, re.MULTILINE):
        num, longname = m.group(1), m.group(2).strip().lower()
        if want in longname or longname.split(" - ")[-1] in want:
            card = Path(f"/sys/class/sound/card{num}")
            try:
                return usb_parent_of(str(card.resolve()))
            except OSError:
                return ""
    return ""


def candidates_payload(
    candidates: list[Candidate], sound_parents: set[str]
) -> list[dict[str, Any]]:
    """The volume-keyed endpoints, shaped for the dashboard's device dropdown.

    ``audio`` marks endpoints whose USB parent also hosts a sound card — the
    speakerphone-shaped ones, listed first. A keyboard still appears (an
    explicit operator choice was always allowed), just after them.
    """
    out = [
        {
            "name": c.name,
            "phys": c.phys,
            "audio": bool(c.usb_parent and c.usb_parent in sound_parents),
        }
        for c in candidates
        if c.has_volume_keys
    ]
    return sorted(out, key=lambda d: (not d["audio"], str(d["name"]).lower()))


_HW_CARD_RE = re.compile(r"\(hw:(\d+),")


def mark_volume_key_devices(
    devices: list[dict[str, Any]],
    keyed_parents: dict[str, str],
    card_parents: dict[int, str],
) -> None:
    """Annotate probed AUDIO devices whose USB device also has volume keys.

    Pure join (testable without hardware): a PortAudio name carries the ALSA
    card ("(hw:N,0)"); the card maps to a USB parent; the parent may host a
    volume-keyed HID endpoint. The wizard's device step reads the flag to
    offer enabling the buttons in the same breath as picking the device — and
    reads ``volume_key_device`` to record WHICH endpoint, so the setting never
    has to be re-derived later by ``auto`` (which cannot resolve an alias like
    ``default``, and refuses outright when several devices qualify).
    """
    for d in devices:
        m = _HW_CARD_RE.search(str(d.get("name") or ""))
        endpoint = keyed_parents.get(card_parents.get(int(m.group(1)), "")) if m else None
        if endpoint:
            d["volume_keys"] = True
            d["volume_key_device"] = endpoint


def annotate_volume_keys(devices: list[dict[str, Any]]) -> None:
    """The impure wrapper: scan endpoints + cards, then join. No-op on any
    failure or without evdev — the probe must never break over an extra."""
    try:
        keyed = {
            c.usb_parent: c.name for c in _scan_inputs() if c.usb_parent and c.has_volume_keys
        }
        cards: dict[int, str] = {}
        for card in Path("/sys/class/sound").glob("card[0-9]*"):
            try:
                cards[int(card.name[4:])] = usb_parent_of(str(card.resolve()))
            except (OSError, ValueError):
                continue
    except Exception:
        return
    if keyed:
        mark_volume_key_devices(devices, keyed, cards)


# ---------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------


class MediaKeyWatcher:
    """Owns one endpoint's lifecycle; emits rate-limited volume deltas.

    Runs as a single asyncio task beside the node's other loops. Everything it
    does is off the audio path: losing the HID endpoint re-enters the discovery
    loop, and cancellation closes the device and stops.
    """

    def __init__(
        self,
        *,
        step: int,
        device_match: str,
        audio_device: str | None,
        send_delta: Callable[[int], Awaitable[None]],
        on_status: Callable[[dict[str, Any]], None],
    ) -> None:
        self._step = max(1, min(20, int(step)))
        self._match = device_match or "auto"
        self._audio_device = audio_device
        self._send_delta = send_delta
        self._on_status = on_status
        self._last_sent = 0.0
        self._last_status: dict[str, Any] | None = None
        self._repeat_task: asyncio.Task[None] | None = None
        self._last_candidates: list[dict[str, Any]] = []

    def _status(self, present: bool, detail: str, device: str = "") -> None:
        status = {
            "enabled": True,
            "present": present,
            "detail": detail,
            "device": device,
            # What the dashboard's device dropdown offers (5.0.4 follow-up:
            # config parity with the audio picker — the node is the only one
            # who can see the endpoints, so it reports them).
            "candidates": self._last_candidates,
        }
        if status != self._last_status:
            self._last_status = status
            log.info("Media keys: %s", detail)
            self._on_status(status)

    async def run(self) -> None:
        try:
            import evdev  # noqa: F401  # type: ignore[import-untyped, import-not-found]
        except ImportError:
            self._status(False, "evdev not installed — media keys unavailable")
            return
        while True:
            try:
                cands = await asyncio.to_thread(_scan_inputs)
                parent = await asyncio.to_thread(audio_usb_parent, self._audio_device)
                sound = await asyncio.to_thread(sound_card_usb_parents)
                self._last_candidates = candidates_payload(cands, sound)
                choice, why = select_endpoint(cands, parent, self._match, sound)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # discovery must never kill the node
                choice, why = None, f"discovery failed: {exc}"
            if choice is None:
                self._status(False, why)
                await asyncio.sleep(_RESCAN_S)
                continue
            self._status(True, why, device=choice.name)
            await self._pump(choice)
            # _pump returned ⇒ the endpoint went away; rescan after a beat.
            self._status(False, f"lost {choice.name!r} — rescanning")
            await asyncio.sleep(_RESCAN_S)

    async def _maybe_send(self, code: int) -> None:
        """Rate-limited delta for one key code (shared by real + synthetic events)."""
        delta = self._step if code == KEY_VOLUMEUP else -self._step
        now = time.monotonic()
        if now - self._last_sent < _MIN_SEND_INTERVAL_S:
            return  # coalesce: drop, never queue a burst
        self._last_sent = now
        try:
            await self._send_delta(delta)
        except Exception as exc:
            log.debug("volume_delta send failed (offline?): %s", exc)

    async def _repeat_while_held(self, code: int) -> None:
        """The synthesized typematic: grace, then steady steps until cancelled."""
        await asyncio.sleep(_REPEAT_DELAY_S)
        while True:
            await self._maybe_send(code)
            await asyncio.sleep(_REPEAT_PERIOD_S)

    async def _on_key_event(self, code: int, value: int) -> None:
        """One EV_KEY event for a volume key. Factored off the read loop so the
        hold logic is testable without hardware.

        value 1 (press): step now, arm the synthetic repeater.
        value 2 (device-native repeat): step; the DEVICE is repeating, so the
        synthetic repeater stands down rather than doubling the rate.
        value 0 (release): stop repeating. Releases never send.
        """
        if value == 1:
            self._cancel_repeat()
            await self._maybe_send(code)
            self._repeat_task = asyncio.get_running_loop().create_task(
                self._repeat_while_held(code)
            )
        elif value == 2:
            self._cancel_repeat()
            await self._maybe_send(code)
        elif value == 0:
            self._cancel_repeat()

    def _cancel_repeat(self) -> None:
        if self._repeat_task is not None:
            self._repeat_task.cancel()
            self._repeat_task = None

    async def _pump(self, choice: Candidate) -> None:
        """Read one endpoint until it disappears."""
        import evdev  # type: ignore[import-untyped, import-not-found]

        try:
            dev = evdev.InputDevice(choice.path)
        except OSError:
            return
        try:
            async for event in dev.async_read_loop():
                if event.type != evdev.ecodes.EV_KEY:
                    continue
                if event.code not in (KEY_VOLUMEUP, KEY_VOLUMEDOWN):
                    continue
                await self._on_key_event(event.code, event.value)
        except asyncio.CancelledError:
            raise
        except OSError:
            return  # unplugged mid-read — run() rescans
        finally:
            self._cancel_repeat()
            try:
                dev.close()
            except OSError:
                pass
