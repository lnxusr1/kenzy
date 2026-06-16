"""
kenzy-setup: download all required model files for offline operation.

Downloads whichever models are supported by the currently installed
optional-dependency groups — skips any that are not installed.

  openwakeword  — melspectrogram, embedding, and VAD infrastructure models
  SpeechBrain   — ECAPA-TDNN speaker embedding model

Run once after installation, before starting any kenzy service for the
first time.  Safe to re-run; already-present files are skipped.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

log = logging.getLogger(__name__)


def _setup_openwakeword() -> None:
    try:
        from kenzy.node.client import _ensure_oww_resources  # type: ignore[import-untyped]
    except ImportError:
        log.info("openwakeword not installed — skipping")
        return
    log.info("Checking openwakeword infrastructure models…")
    _ensure_oww_resources()
    log.info("openwakeword models ready.")


def _setup_speechbrain(model_source: str, model_save_dir: str) -> None:
    try:
        from speechbrain.pretrained import EncoderClassifier  # type: ignore[import-untyped]
    except ImportError:
        log.info("speechbrain not installed — skipping")
        return
    import os

    sentinel = os.path.join(model_save_dir, "hyperparams.yaml")
    if os.path.exists(sentinel):
        log.info("SpeechBrain model already present at %s — skipping", model_save_dir)
        return
    log.info("Downloading SpeechBrain model '%s' → %s…", model_source, model_save_dir)
    EncoderClassifier.from_hparams(
        source=model_source,
        savedir=model_save_dir,
        run_opts={"device": "cpu"},
    )
    log.info("SpeechBrain model ready.")


def _setup_kokoro(voice: str, device: str, lang_code: str) -> None:
    try:
        from kokoro import KPipeline  # type: ignore[import-untyped]
    except ImportError:
        log.info("kokoro not installed — skipping")
        return

    log.info("Initializing Kokoro pipeline (downloads model if needed)…")
    try:
        pipeline = KPipeline(lang_code=lang_code, device=device)
        # Run a short silent synthesis to confirm the model is fully loaded.
        for _ in pipeline("Hello.", voice=voice, speed=1.0):
            break
        log.info("Kokoro models ready.")
    except Exception as exc:
        log.warning("Kokoro setup failed: %s", exc)


def main() -> None:
    import yaml  # type: ignore[import-untyped]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from kenzy.config import resolve_config

    # Read speaker config to locate model paths (optional).
    speaker_config = resolve_config("speaker", sys.argv[1] if len(sys.argv) > 1 else None)
    cfg: dict[str, Any] = {}
    try:
        with open(speaker_config) as fh:
            cfg = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        log.debug("Speaker config not found at %s — using defaults", speaker_config)

    model_source = str(cfg.get("model_source", "speechbrain/spkrec-ecapa-voxceleb"))
    model_save_dir = str(cfg.get("model_save_dir", "models/speaker"))

    _setup_openwakeword()
    _setup_speechbrain(model_source, model_save_dir)

    # Kokoro: initialize if tts.yaml specifies provider: kokoro.
    tts_config = resolve_config("tts")
    tts_cfg: dict[str, Any] = {}
    try:
        with open(tts_config) as fh:
            tts_cfg = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        log.debug("TTS config not found at %s — skipping Kokoro setup", tts_config)

    if str(tts_cfg.get("provider", "openai")).lower() == "kokoro":
        kcfg = tts_cfg.get("kokoro", {})
        voice = str(kcfg.get("voice", "af_heart"))
        device = str(kcfg.get("device", "cpu"))
        lang_code = str(kcfg.get("lang_code") or voice[0])
        _setup_kokoro(voice, device, lang_code)

    log.info("Setup complete.")


if __name__ == "__main__":
    main()
