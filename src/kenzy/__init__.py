"""Kenzy — a distributed, self-hosted home voice assistant."""

from __future__ import annotations

import importlib.metadata


def kenzy_version() -> str:
    """Installed ``kenzy`` package version, or ``"dev"`` in an unbuilt checkout.

    Reported by each component (server, services via ``/health``, nodes via
    ``hello``) so the dashboard can show what's installed where — the visibility
    layer the upgrade flow builds on.
    """
    try:
        return importlib.metadata.version("kenzy")
    except importlib.metadata.PackageNotFoundError:
        return "dev"
