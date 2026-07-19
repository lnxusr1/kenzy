"""systemd ``--user`` unit control (4.1 service enable/disable).

The per-user install runs each service as a ``systemd --user`` unit
(``kenzy-<svc>.service``). These helpers let a service manage ITS OWN unit
(self-disable survives ``Restart=`` policies) and let the server manage
co-located units — the two paths that need no remote agent. Non-systemd
environments (dev checkouts, containers) degrade to ``systemd: False`` and
the dashboard hides the controls.

Root installs are out of scope on purpose: these run ``systemctl --user``
only, never system units, never sudo.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Any

log = logging.getLogger(__name__)

_TIMEOUT = 10


def _run(*args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def unit_state(unit: str) -> dict[str, Any]:
    """``{"systemd": bool, "exists": bool, "enabled": bool, "active": bool}``.

    ``systemd: False`` = no systemctl / no user manager (dev) — the honest
    "these controls don't apply here" state.
    """
    if shutil.which("systemctl") is None:
        return {"systemd": False, "exists": False, "enabled": False, "active": False}
    code, out = _run("is-enabled", unit)
    if "not-found" in out or "No such file" in out:
        return {"systemd": True, "exists": False, "enabled": False, "active": False}
    if "Failed to connect" in out:  # no user manager running (container/dev)
        return {"systemd": False, "exists": False, "enabled": False, "active": False}
    enabled = code == 0 and out.strip().startswith("enabled")
    a_code, _ = _run("is-active", "--quiet", unit)
    return {"systemd": True, "exists": True, "enabled": enabled, "active": a_code == 0}


def disable_unit(unit: str) -> tuple[bool, str]:
    """``disable --now`` — stops AND prevents restart/resurrection."""
    code, out = _run("disable", "--now", unit)
    ok = code == 0
    log.warning("Unit %s disable --now: %s (%s)", unit, "ok" if ok else "FAILED", out[:200])
    return ok, out[:300]


def enable_unit(unit: str) -> tuple[bool, str]:
    """``enable --now`` — starts and enables at login/boot (with linger)."""
    code, out = _run("enable", "--now", unit)
    ok = code == 0
    log.warning("Unit %s enable --now: %s (%s)", unit, "ok" if ok else "FAILED", out[:200])
    return ok, out[:300]
