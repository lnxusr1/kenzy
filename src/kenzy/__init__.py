"""Kenzy — a distributed, self-hosted home voice assistant."""

from __future__ import annotations

import importlib.metadata


def installed_version() -> str:
    """The ``kenzy`` version currently on disk (fresh metadata read), or ``"dev"``
    in an unbuilt checkout. After a pip upgrade this moves immediately, while a
    still-running process keeps executing (and reporting) the old version."""
    try:
        return importlib.metadata.version("kenzy")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


#: Captured once at import, so a long-running process reports the version of the
#: code it is actually executing — NOT whatever pip later put on disk. (Reading
#: metadata per call made an un-recycled service claim the new version right
#: after an upgrade, hiding that it still ran the old code.)
_RUNNING_VERSION = installed_version()


def kenzy_version() -> str:
    """The version of the code this process is running, or ``"dev"`` in an
    unbuilt checkout.

    Reported by each component (server, services via ``/health``, nodes via
    ``hello``) so the dashboard can show what runs where — the visibility layer
    the upgrade flow builds on. Compare with :func:`installed_version` to detect
    an upgraded-on-disk process that needs a restart.
    """
    return _RUNNING_VERSION


def version_info() -> dict[str, str]:
    """Version fields for a service ``/health`` payload: ``version`` = running
    code; ``installed`` = on disk. They differ only when the package was
    upgraded under a live process (⇒ restart needed to apply)."""
    return {"version": kenzy_version(), "installed": installed_version()}
