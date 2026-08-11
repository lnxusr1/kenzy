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


def pip_upgrade_command(
    extra: str, version: str | None, plugins: list[str] | None = None
) -> list[str]:
    """Build the ``pip install -U kenzy[extra]`` argv (constraints + version pin).

    ``plugins`` (default: auto-enumerated) are the installed ``kenzy.plugins``
    distributions, included so the resolver solves core + plugins **jointly**:
    a plugin that caps core holds the upgrade back (or, with an explicit
    ``version`` the set can't satisfy, fails it BEFORE anything changes) instead
    of pip stranding an incompatible pair with a warning nobody reads. 5.1.
    """
    if plugins is None:
        from kenzy.plugins import installed_plugin_dists

        plugins = installed_plugin_dists()
    spec = f"kenzy[{extra}]" + (f"=={version}" if version else ">=3.0.0")
    return [sys.executable, "-m", "pip", "install", "-U", *pip_constraint_args(), spec, *plugins]


def joint_failure_note(text: str, plugins: list[str]) -> str:
    """When a joint upgrade fails on dependency resolution, say what that means
    — pip's conflict dump names the packages but not the way out."""
    lowered = text.lower()
    if plugins and ("resolutionimpossible" in lowered or "conflicting dependencies" in lowered):
        return (
            text + "\nNothing was changed. An installed add-on ("
            + ", ".join(plugins)
            + ") likely caps the kenzy version it supports — upgrade or remove the add-on, "
            "then retry."
        )
    return text


def pip_fill_command(extra: str) -> list[str]:
    """``pip install kenzy[extra]`` WITHOUT ``-U``: pulls only missing
    dependencies (a feature's new lib), moves no installed versions, honors
    the operator's constraints. The feature-chip Install action (4.1)."""
    return [sys.executable, "-m", "pip", "install", *pip_constraint_args(), f"kenzy[{extra}]"]


async def run_pip_fill(extra: str) -> tuple[bool, str]:
    """Run the dependency fill off the event loop. Returns ``(ok, tail)``."""
    cmd = pip_fill_command(extra)
    log.warning("Dependency fill: %s", " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await proc.communicate()
    except Exception as exc:  # pragma: no cover - environment-dependent
        return False, f"failed to launch pip: {exc}"
    ok = proc.returncode == 0
    log.info("Dependency fill %s (exit %s)", "ok" if ok else "FAILED", proc.returncode)
    return ok, (out.decode("utf-8", "replace") if out else "")[-1500:].strip()


async def run_pip_upgrade(extra: str, version: str | None = None) -> tuple[bool, str]:
    """Run the upgrade as a subprocess off the event loop. Returns ``(ok, output_tail)``.

    Does **not** re-exec — the caller re-execs on success so the new code loads.
    """
    if not valid_version(version):
        return False, f"invalid version: {version!r}"
    from kenzy.plugins import installed_plugin_dists

    plugins = installed_plugin_dists()
    cmd = pip_upgrade_command(extra, version, plugins)
    if plugins:
        log.warning("Upgrade (jointly with add-ons %s): %s", ", ".join(plugins), " ".join(cmd))
    else:
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
    if not ok:
        text = joint_failure_note(text, plugins)
    return ok, text[-1500:].strip()
