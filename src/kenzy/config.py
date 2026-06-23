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


def constraints_path(home: Path | None = None) -> Path:
    """Path to the operator's pip constraints file in the config home.

    Pins recorded here (e.g. ``transformers==4.30.0`` for a host that needs a
    specific version) are honored on install AND on every auto-upgrade, so an
    upgrade can't silently move a pinned dependency. Standard pip constraints
    format (``pip install -c <file>``).
    """
    return (home or kenzy_home()) / "constraints.txt"


def pip_constraint_args(home: Path | None = None) -> list[str]:
    """Return ``["-c", <constraints>]`` if the operator has a constraints file,
    else ``[]``. Used by every Kenzy-driven ``pip install`` (install + upgrade) so
    pins survive upgrades. An all-comment file is fine (pip treats it as empty)."""
    path = constraints_path(home)
    return ["-c", str(path)] if path.is_file() else []


def packaged_config(service: str) -> Path:
    """Path to the default config bundled in the package for ``service``."""
    return _PACKAGED_CONFIGS / f"{service}.yaml"


def writable_config_path(service: str, resolved: Path) -> Path:
    """Return a writable config path for ``service`` given its ``resolved`` path.

    Used when a service needs to persist back into its own config (e.g. a node
    writing its generated ``node_id``). ``resolved`` is returned as-is unless it
    is the packaged read-only default, in which case writes are redirected to
    ``kenzy_home()/configs/<service>.yaml``.
    """
    try:
        resolved.resolve().relative_to(_PACKAGED_CONFIGS.resolve())
    except ValueError:
        return resolved  # a real user/dev/explicit path — write in place
    return kenzy_home() / "configs" / f"{service}.yaml"


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
