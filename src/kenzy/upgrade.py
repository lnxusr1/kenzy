"""Shared self-upgrade helpers used by the server, the backend services
(``POST /upgrade``), and nodes.

Each component upgrades **its own** extra (server→``server``, llm→``llm``, node→
``node``, …) so a shared venv converges to the full dependency set across the fleet,
and a single-extra host never pulls another component's heavy deps. The operator's
constraints file (pins) is honored on every upgrade, and an optional ``version`` pins
the exact release; otherwise it's floored at ``>=3.0.0`` (never the legacy 2.x monolith).
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys

from kenzy.config import pip_constraint_args

log = logging.getLogger(__name__)

# A safe-looking PEP 440-ish version for the optional ``==`` pin. The value rides an
# argv (no shell), so this is defence-in-depth, not the only guard.
_VERSION_RE = re.compile(r"^[A-Za-z0-9.+!-]{1,32}$")


def valid_version(version: str | None) -> bool:
    return version is None or bool(_VERSION_RE.fullmatch(version))


def pip_upgrade_command(extra: str, version: str | None) -> list[str]:
    """Build the ``pip install -U kenzy[extra]`` argv (constraints + version pin)."""
    spec = f"kenzy[{extra}]" + (f"=={version}" if version else ">=3.0.0")
    return [sys.executable, "-m", "pip", "install", "-U", *pip_constraint_args(), spec]


async def run_pip_upgrade(extra: str, version: str | None = None) -> tuple[bool, str]:
    """Run the upgrade as a subprocess off the event loop. Returns ``(ok, output_tail)``.

    Does **not** re-exec — the caller re-execs on success so the new code loads.
    """
    if not valid_version(version):
        return False, f"invalid version: {version!r}"
    cmd = pip_upgrade_command(extra, version)
    log.warning("Upgrade: %s", " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
    except Exception as exc:  # pragma: no cover - environment-dependent
        return False, f"failed to launch pip: {exc}"
    text = out.decode("utf-8", "replace") if out else ""
    ok = proc.returncode == 0
    log.info("Upgrade %s (exit %s)", "ok" if ok else "FAILED", proc.returncode)
    return ok, text[-1500:].strip()
