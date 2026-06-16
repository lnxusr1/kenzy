"""Configuration discovery for Kenzy services.

Resolves where a service's YAML config lives, supporting both the legacy
source-push layout (``./configs/<svc>.yaml``) and a PyPI install (packaged
defaults plus a user config home at ``$KENZY_HOME`` or ``~/.config/kenzy``).
"""

from __future__ import annotations

import os
from pathlib import Path

#: Directory of default configs bundled inside the package.
_PACKAGED_CONFIGS = Path(__file__).parent / "data" / "configs"

#: Service config files shipped as defaults (also what ``kenzy-init`` scaffolds).
SERVICES = ("node", "server", "stt", "tts", "llm", "speaker")


def kenzy_home() -> Path:
    """Return the user config home: ``$KENZY_HOME`` if set, else ``~/.config/kenzy``."""
    env = os.environ.get("KENZY_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "kenzy"


def kenzy_data_root() -> Path:
    """Return the operational-tree root that holds ``configs/``, ``skills/`` and ``data/``.

    Mirrors the precedence of :func:`resolve_config` so skills and the LLM
    service find their runtime files the same way services find their configs:

    1. ``$KENZY_HOME`` if set.
    2. The current directory, if it looks like a source/dev checkout
       (``./configs`` exists) — keeps running from a repo clone working.
    3. ``~/.config/kenzy`` (the PyPI / ``kenzy-init`` layout).
    """
    env = os.environ.get("KENZY_HOME")
    if env:
        return Path(env).expanduser()
    if (Path.cwd() / "configs").is_dir():
        return Path.cwd()
    return Path.home() / ".config" / "kenzy"


def packaged_config(service: str) -> Path:
    """Path to the default config bundled in the package for ``service``."""
    return _PACKAGED_CONFIGS / f"{service}.yaml"


def resolve_config(service: str, explicit: str | None = None) -> Path:
    """Resolve the config file path for ``service``.

    Resolution order:

    1. ``explicit`` — a CLI argument / ``--config`` value, used as given.
    2. ``$KENZY_HOME/configs/<service>.yaml`` (if ``KENZY_HOME`` is set).
    3. ``./configs/<service>.yaml`` — the source-push / dev layout (CWD).
    4. ``~/.config/kenzy/configs/<service>.yaml``.
    5. The packaged default (always present).
    """
    if explicit:
        return Path(explicit).expanduser()

    filename = f"{service}.yaml"
    candidates: list[Path] = []

    env = os.environ.get("KENZY_HOME")
    if env:
        candidates.append(Path(env).expanduser() / "configs" / filename)
    candidates.append(Path.cwd() / "configs" / filename)
    candidates.append(Path.home() / ".config" / "kenzy" / "configs" / filename)

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return packaged_config(service)
