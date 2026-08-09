#!/usr/bin/env python3
"""Post-install capability smoke test.

Asserts what an install PRODUCES, not that the installer exited zero. That
distinction is the whole point: 5.0.7's macOS bug had pip exiting 0, every
import succeeding, and a node that could not hear anything — because the
bundled model was ``.tflite`` on a host with no runtime able to read it. A
"did the install command succeed" check stays green through that.

Deliberately substrate-agnostic: no CI imports, no network assumptions beyond
what the install itself needs. The same script runs in a container, on a macOS
runner, and on a real board in the fleet lab, so one assertion suite covers
every tier instead of each growing its own.

Usage:
    python scripts/ci_smoke.py --profile node --expect-model onnx
    python scripts/ci_smoke.py --profile server
    python scripts/ci_smoke.py --profile speaker-setup-only
"""

from __future__ import annotations

import argparse
import importlib
import shutil
import subprocess
import sys

# The six long-running services take an optional config path POSITIONALLY and
# have no argument parser, so `kenzy-server --help` doesn't print help — it
# treats "--help" as a config path, warns that it doesn't exist, falls through
# to resolution, and boots the server. They're therefore verified by presence
# on PATH plus a real import of the target module, which is the stronger check
# regardless: it proves every dependency the module needs actually resolved.
DAEMONS = {
    "kenzy-node": "kenzy.node.client",
    "kenzy-server": "kenzy.server.server",
    "kenzy-stt": "kenzy.stt.stt",
    "kenzy-tts": "kenzy.tts.tts",
    "kenzy-llm": "kenzy.llm.llm",
    "kenzy-speaker": "kenzy.speaker.speaker",
}

# Console scripts are installed regardless of extras, so each profile names
# only what its own dependencies can support.
PROFILES: dict[str, dict[str, list[str]]] = {
    "node": {
        "daemons": ["kenzy-node"],
        "tools": ["kenzy-devices", "kenzy-init", "kenzy-setup"],
    },
    "server": {
        "daemons": ["kenzy-server"],
        "tools": ["kenzy-passwd", "kenzy-deploy", "kenzy-init", "kenzy-setup"],
    },
    "all": {
        "daemons": list(DAEMONS),
        "tools": ["kenzy-devices", "kenzy-passwd", "kenzy-deploy", "kenzy-init", "kenzy-setup"],
    },
    "speaker-setup-only": {"daemons": [], "tools": ["kenzy-setup"]},
}

_failures: list[str] = []
_checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _checks
    _checks += 1
    if ok:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}{'  — ' + detail if detail else ''}")
        _failures.append(label)


def check_entry_points(profile: str) -> None:
    spec = PROFILES[profile]
    print(f"\nEntry points ({profile}):")

    for ep in spec["daemons"]:
        check(f"{ep} on PATH", shutil.which(ep) is not None)
        module = DAEMONS[ep]
        try:
            importlib.import_module(module)
            check(f"{module} imports", True)
        except Exception as exc:  # noqa: BLE001 — an import failure IS the finding
            check(f"{module} imports", False, f"{type(exc).__name__}: {exc}")

    for ep in spec["tools"]:
        try:
            r = subprocess.run([ep, "--help"], capture_output=True, timeout=120)
            check(f"{ep} --help", r.returncode == 0, r.stderr.decode()[-300:])
        except (OSError, subprocess.TimeoutExpired) as exc:
            check(f"{ep} --help", False, str(exc))


def check_setup_is_tolerant() -> None:
    """kenzy-setup must SKIP absent optional deps, never abort on them.

    Regression for 5.0.7 bug #1: the `speaker` extra ships sounddevice, so the
    node client imported fine while the openwakeword call inside it raised —
    and because that step runs first, `pip install kenzy[speaker] && kenzy-setup`
    died before downloading the SpeechBrain model it was actually run for.
    """
    print("\nSetup tolerance:")
    r = subprocess.run(["kenzy-setup"], capture_output=True, timeout=1800)
    check("kenzy-setup exits 0 with optional deps absent", r.returncode == 0,
          r.stderr.decode()[-500:])


def check_wakeword(expect_model: str) -> None:
    """The model this host selected must exist, load, and actually infer."""
    print("\nWake-word capability:")
    try:
        from kenzy.node.client import _bundled_model_paths, _infer_framework
    except ImportError as exc:
        check("import node client", False, str(exc))
        return

    paths = _bundled_model_paths()
    check("a bundled model was selected", bool(paths))
    if not paths:
        return

    selected = paths[0]
    print(f"        selected: {selected}")
    if expect_model != "any":
        check(f"selected format is .{expect_model}", selected.endswith(f".{expect_model}"),
              f"got {selected}")

    framework = _infer_framework(paths)
    check(f"framework matches model ({framework})",
          framework == ("onnx" if selected.endswith(".onnx") else "tflite"))

    # The assertion that matters: load it and run one frame through. Selection
    # being right proves nothing if the runtime cannot open the file.
    try:
        import numpy as np
        from openwakeword.model import Model

        model = Model(wakeword_models=paths, inference_framework=framework)
        frame = np.zeros(1280, dtype=np.int16)  # 80 ms at 16 kHz — one protocol frame
        scores = model.predict(frame)
        check("model loads and infers on a frame", isinstance(scores, dict) and bool(scores),
              f"got {scores!r}")
    except Exception as exc:  # noqa: BLE001 — any failure here is the finding
        check("model loads and infers on a frame", False, f"{type(exc).__name__}: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", required=True, choices=sorted(PROFILES))
    ap.add_argument("--expect-model", default="none", choices=["tflite", "onnx", "any", "none"],
                    help="Model format this host should have selected")
    args = ap.parse_args()

    print(f"kenzy smoke test — profile={args.profile} python={sys.version.split()[0]}")
    check_entry_points(args.profile)
    check_setup_is_tolerant()
    if args.expect_model != "none":
        check_wakeword(args.expect_model)

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    if _failures:
        print("failed: " + ", ".join(_failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
