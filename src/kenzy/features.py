"""Optional-feature availability probes (4.1 feature chips).

Optional features degrade honestly when their dependency is missing — but
"honestly" used to mean one log line. These helpers feed each service's
``GET /features`` report so the dashboard can show
``{configured, available, active}`` chips with a one-click Install (a
no-upgrade dependency fill) instead of a log-diving session.

``probe_import`` uses ``find_spec`` — no import cost, no side effects.
System binaries (espeak-ng) can't be pip-installed and services don't sudo:
those report an ``install`` of ``"apt"`` with a copy-paste ``note`` instead
of pretending a button can do it.
"""

from __future__ import annotations

import importlib.util
import shutil
from typing import Any


def probe_import(module: str) -> bool:
    """Is a Python dependency present? (No import executed.)"""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def probe_binary(name: str) -> bool:
    """Is a system binary on PATH?"""
    return shutil.which(name) is not None


def feature(
    name: str,
    *,
    configured: bool,
    available: bool,
    active: bool,
    install: str = "pip",
    note: str = "",
) -> dict[str, Any]:
    """One chip. ``install``: "pip" (the /install_deps button applies),
    "apt" (instruction only), or "" (nothing installable — informational)."""
    return {
        "name": name,
        "configured": configured,
        "available": available,
        "active": active,
        "install": install,
        "note": note,
    }
