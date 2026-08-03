"""
kenzy-devices: list audio devices and test sample rate support.

Run after installing the node package to identify the correct values for
audio_device, capture_sample_rate, and playback_sample_rate in node.yaml.

``probe_devices()`` returns the same information as structured data — shared by
this CLI and the node's ``hello`` capability report (so the dashboard can offer a
device picker without anyone hand-running this command on the box).
"""

from __future__ import annotations

import sys
from typing import Any

# Rates tested for capture and playback.
_CAPTURE_RATES: list[int] = [16_000, 44_100, 48_000]
_PLAYBACK_RATES: list[int] = [24_000, 44_100, 48_000]

# Rates Kenzy uses internally.
_KENZY_CAPTURE = 16_000
_KENZY_PLAYBACK = 24_000


def _check(fn: Any, device: int, rate: int, channels: int = 1) -> bool:
    import sounddevice as sd  # type: ignore[import-untyped]

    try:
        fn(device=device, samplerate=rate, channels=channels, dtype="int16")
        return True
    except sd.PortAudioError:
        return False


def _suggest_rate(supported: list[int], preferred: int) -> int | None:
    """Pick the rate to open the stream at: the preferred (native) one if it
    works, else the first supported (resampled internally), else None."""
    if preferred in supported:
        return preferred
    return supported[0] if supported else None


def probe_devices() -> list[dict[str, Any]]:
    """Return audio devices with the Kenzy rates each supports (no printing).

    Each entry: ``index``, ``name``, ``inputs``, ``outputs``,
    ``default_samplerate``, ``capture_rates``/``playback_rates`` (supported
    subsets), and a ``suggested`` ``{audio_device, capture_sample_rate,
    playback_sample_rate}`` for devices that do both. Returns ``[]`` if
    ``sounddevice`` is unavailable.
    """
    try:
        import sounddevice as sd  # type: ignore[import-untyped]
    except Exception:  # pragma: no cover - exercised only without PortAudio
        return []

    out: list[dict[str, Any]] = []
    for i, dev in enumerate(sd.query_devices()):
        n_in = int(dev["max_input_channels"])
        n_out = int(dev["max_output_channels"])
        if n_in == 0 and n_out == 0:
            continue
        cap = [r for r in _CAPTURE_RATES if n_in > 0 and _check(sd.check_input_settings, i, r)]
        play = [r for r in _PLAYBACK_RATES if n_out > 0 and _check(sd.check_output_settings, i, r)]
        entry: dict[str, Any] = {
            "index": i,
            "name": str(dev["name"]),
            "inputs": n_in,
            "outputs": n_out,
            "default_samplerate": int(dev.get("default_samplerate") or 0),
            "capture_rates": cap,
            "playback_rates": play,
        }
        if cap and play:
            # The short name (before the first colon) is the stable substring to
            # match on, matching what the CLI suggests.
            short = entry["name"].split(":")[0].strip() if ":" in entry["name"] else entry["name"]
            entry["suggested"] = {
                "audio_device": short,
                "capture_sample_rate": _suggest_rate(cap, _KENZY_CAPTURE),
                "playback_sample_rate": _suggest_rate(play, _KENZY_PLAYBACK),
            }
        out.append(entry)
    # 5.0.4: mark devices whose USB parent also carries volume keys, so the
    # audio wizard can offer the buttons while the user is picking the device.
    try:
        from kenzy.node.mediakeys import annotate_volume_keys

        annotate_volume_keys(out)
    except Exception:  # the probe must never break over an optional extra
        pass
    return out


def _tick(ok: bool) -> str:
    return "✓" if ok else "✗"


def main() -> None:
    try:
        import sounddevice  # type: ignore[import-untyped]  # noqa: F401
    except ImportError:
        print("sounddevice is not installed — run: pip install -e '.[node]'")
        sys.exit(1)

    devices = probe_devices()

    print()
    print("Kenzy Audio Device Scanner")
    print("=" * 64)
    print(f"Kenzy default capture rate  : {_KENZY_CAPTURE} Hz  (mic → server)")
    print(f"Kenzy default playback rate : {_KENZY_PLAYBACK} Hz  (TTS → speaker)")
    print("Rates marked ✓ are supported by the device via PortAudio.")
    print()

    for dev in devices:
        print(f"[{dev['index']:2d}] {dev['name']}")
        n_in, n_out, rate = dev["inputs"], dev["outputs"], dev["default_samplerate"]
        print(f"      in={n_in}  out={n_out}  default={rate} Hz")
        if dev["inputs"] > 0:
            parts = [f"{r} {_tick(r in dev['capture_rates'])}" for r in _CAPTURE_RATES]
            native = "  ← native" if _KENZY_CAPTURE in dev["capture_rates"] else ""
            print(f"      capture  : {' | '.join(parts)}{native}")
        if dev["outputs"] > 0:
            parts = [f"{r} {_tick(r in dev['playback_rates'])}" for r in _PLAYBACK_RATES]
            native = "  ← native" if _KENZY_PLAYBACK in dev["playback_rates"] else ""
            print(f"      playback : {' | '.join(parts)}{native}")
        print()

    suggestions = [d for d in devices if "suggested" in d]
    if not suggestions:
        print("No devices found that support both capture and playback.")
        return

    print("=" * 64)
    print("Suggested node.yaml settings")
    print()
    for dev in suggestions:
        s = dev["suggested"]
        cap, play = s["capture_sample_rate"], s["playback_sample_rate"]
        resampling = []
        if cap != _KENZY_CAPTURE:
            resampling.append(f"capture {cap}→{_KENZY_CAPTURE} Hz")
        if play != _KENZY_PLAYBACK:
            resampling.append(f"playback {play}→{_KENZY_PLAYBACK} Hz")
        note = (
            f"  # resampling: {', '.join(resampling)}" if resampling else "  # no resampling needed"
        )

        print(f"  [{dev['index']}] {dev['name']}")
        print(f'      audio_device: "{s["audio_device"]}"{note}')
        if cap != _KENZY_CAPTURE:
            print(f"      capture_sample_rate: {cap}")
        if play != _KENZY_PLAYBACK:
            print(f"      playback_sample_rate: {play}")
        print()


if __name__ == "__main__":
    main()
