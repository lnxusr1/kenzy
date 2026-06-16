"""
kenzy-devices: list audio devices and test sample rate support.

Run after installing the node package to identify the correct values for
audio_device, capture_sample_rate, and playback_sample_rate in node.yaml.
"""

from __future__ import annotations

import sys

# Rates tested for capture and playback.
_CAPTURE_RATES: list[int] = [16_000, 44_100, 48_000]
_PLAYBACK_RATES: list[int] = [24_000, 44_100, 48_000]

# Rates Kenzy uses internally.
_KENZY_CAPTURE = 16_000
_KENZY_PLAYBACK = 24_000


def _check(fn: object, device: int, rate: int, channels: int = 1) -> bool:
    import sounddevice as sd  # type: ignore[import-untyped]

    try:
        fn(device=device, samplerate=rate, channels=channels, dtype="int16")  # type: ignore[call-arg]
        return True
    except sd.PortAudioError:
        return False


def _tick(ok: bool) -> str:
    return "✓" if ok else "✗"


def main() -> None:
    try:
        import sounddevice as sd  # type: ignore[import-untyped]
    except ImportError:
        print("sounddevice is not installed — run: pip install -e '.[node]'")
        sys.exit(1)

    devices = list(sd.query_devices())

    print()
    print("Kenzy Audio Device Scanner")
    print("=" * 64)
    print(f"Kenzy default capture rate  : {_KENZY_CAPTURE} Hz  (mic → server)")
    print(f"Kenzy default playback rate : {_KENZY_PLAYBACK} Hz  (TTS → speaker)")
    print("Rates marked ✓ are supported by the device via PortAudio.")
    print()

    suggestions: list[tuple[int, str, int, int]] = []

    for i, dev in enumerate(devices):
        n_in = int(dev["max_input_channels"])
        n_out = int(dev["max_output_channels"])
        if n_in == 0 and n_out == 0:
            continue

        name = str(dev["name"])
        default_rate = int(dev["default_samplerate"])

        print(f"[{i:2d}] {name}")
        print(f"      in={n_in}  out={n_out}  default={default_rate} Hz")

        best_capture: int | None = None
        best_playback: int | None = None

        if n_in > 0:
            parts = []
            for rate in _CAPTURE_RATES:
                ok = _check(sd.check_input_settings, i, rate)
                parts.append(f"{rate} {_tick(ok)}")
                if ok and (best_capture is None or rate == _KENZY_CAPTURE):
                    best_capture = rate
            native = "  ← native" if best_capture == _KENZY_CAPTURE else ""
            print(f"      capture  : {' | '.join(parts)}{native}")

        if n_out > 0:
            parts = []
            for rate in _PLAYBACK_RATES:
                ok = _check(sd.check_output_settings, i, rate)
                parts.append(f"{rate} {_tick(ok)}")
                if ok and (best_playback is None or rate == _KENZY_PLAYBACK):
                    best_playback = rate
            native = "  ← native" if best_playback == _KENZY_PLAYBACK else ""
            print(f"      playback : {' | '.join(parts)}{native}")

        # Only recommend devices that handle both capture and playback.
        if n_in > 0 and n_out > 0 and best_capture and best_playback:
            suggestions.append((i, name, best_capture, best_playback))

        print()

    if not suggestions:
        print("No devices found that support both capture and playback.")
        return

    print("=" * 64)
    print("Suggested node.yaml settings")
    print()
    for idx, name, cap, play in suggestions:
        # Suggest the portion of the name before the colon as the device string —
        # short enough to be readable, long enough to be unambiguous.
        short = name.split(":")[0].strip() if ":" in name else name
        resampling = []
        if cap != _KENZY_CAPTURE:
            resampling.append(f"capture {cap}→{_KENZY_CAPTURE} Hz")
        if play != _KENZY_PLAYBACK:
            resampling.append(f"playback {play}→{_KENZY_PLAYBACK} Hz")
        note = (
            f"  # resampling: {', '.join(resampling)}" if resampling else "  # no resampling needed"
        )

        print(f"  [{idx}] {name}")
        print(f'      audio_device: "{short}"{note}')
        if cap != _KENZY_CAPTURE:
            print(f"      capture_sample_rate: {cap}")
        if play != _KENZY_PLAYBACK:
            print(f"      playback_sample_rate: {play}")
        print()


if __name__ == "__main__":
    main()
