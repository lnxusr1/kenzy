"""Versioned external event schema for the integrations layer.

These builders are the **contract** that transports (MQTT in P1) publish and that
external consumers (HA entities, automations, blueprints) depend on. Keep changes
**additive** within a ``SCHEMA_VERSION``; bump the version for a breaking change.

Every event is a plain JSON-serialisable dict carrying:

- ``schema_version`` — the contract version (see below)
- ``type`` — the event kind (``node_state`` | ``interaction`` | ``presence``)
- ``ts`` — Unix epoch seconds when the event was produced
- type-specific fields documented per builder

Privacy: no event carries spoken text. ``interaction`` reports only timing/metadata
and ``presence`` only who-was-heard-where — deliberately, so nothing sensitive leaves
the box over the integration.
"""

from __future__ import annotations

import time
from typing import Any

#: Bump on a breaking change to any event shape. Additive fields don't require it.
SCHEMA_VERSION = 1


def _event(type_: str, **fields: Any) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "type": type_, "ts": time.time(), **fields}


def node_state(
    *,
    node_id: str,
    room: str | None,
    online: bool,
    streaming: bool = False,
    audio_ok: bool = True,
    muted: bool = False,
    version: str | None = None,
) -> dict[str, Any]:
    """A node's connectivity/health. ``state`` is ``offline`` when not connected,
    else ``streaming`` (capturing audio) or ``idle``. ``muted`` is the transient
    runtime mute."""
    return _event(
        "node_state",
        node_id=node_id,
        room=room,
        online=online,
        state=("streaming" if streaming else "idle") if online else "offline",
        audio_ok=audio_ok,
        muted=muted,
        version=version,
    )


def interaction(
    *,
    node_id: str | None,
    room: str | None,
    speaker: str | None,
    fast: bool,
    latency_ms: int | None,
) -> dict[str, Any]:
    """A completed voice interaction — timing/metadata only. ``fast`` marks the
    deterministic fast path (no LLM). Carries no spoken text by design."""
    return _event(
        "interaction",
        node_id=node_id,
        room=room,
        speaker=speaker,
        fast=fast,
        latency_ms=latency_ms,
    )


def presence(*, node_id: str | None, room: str | None, speaker: str | None) -> dict[str, Any]:
    """Who was last heard, and where — the derived signal HA can't get elsewhere.
    Carries no transcript, so it's safe to publish regardless of text settings."""
    return _event("presence", node_id=node_id, room=room, speaker=speaker)


def radar(
    *,
    node_id: str,
    room: str | None,
    present: bool,
    targets: int = 0,
    nearest_mm: int | None = None,
) -> dict[str, Any]:
    """A node's in-node radar reading (5.1, the ld2450 add-on) — the RAW
    per-sensor signal, so HA automations can use each room's radar directly
    rather than only Kenzy's fused belief. Additive event type: consumers
    ignore types they don't know, so the schema version holds."""
    return _event(
        "radar",
        node_id=node_id,
        room=room,
        present=present,
        targets=targets,
        nearest_mm=nearest_mm,
    )
