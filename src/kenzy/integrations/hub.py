"""In-process event hub for the integrations layer.

The hub sits on the server's existing observability listeners, translates raw
internal state/records into the versioned :mod:`schema`, and fans the events out to
subscribed transports. There is no transport here — P1 adds an MQTT subscriber.

Zero-overhead contract: with no subscribers the hub does no work beyond a list
check, and ``attach_to_server`` registers listeners that the server already invokes
(themselves no-ops when nothing observes). Nothing is wired into the running server
until a transport attaches.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from kenzy.integrations import schema

log = logging.getLogger("kenzy.integrations")

Subscriber = Callable[[dict[str, Any]], None]


class IntegrationHub:
    """Fan-out of schema events to subscribed transports."""

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        # Track which nodes we've reported online so we can emit explicit offline
        # transitions when one disappears from a later snapshot.
        self._known_nodes: set[str] = set()

    def subscribe(self, fn: Subscriber) -> None:
        self._subscribers.append(fn)

    def unsubscribe(self, fn: Subscriber) -> None:
        if fn in self._subscribers:
            self._subscribers.remove(fn)

    def _emit(self, event: dict[str, Any]) -> None:
        for fn in list(self._subscribers):
            try:
                fn(event)
            except Exception:  # a transport must never break the pipeline
                log.debug("integration subscriber error", exc_info=True)

    def emit(self, event: dict[str, Any]) -> None:
        """Publish a schema event from OUTSIDE the server's own listeners —
        the plugin seam (5.1): an add-on's server half hands in events built
        with :mod:`kenzy.integrations.schema` (e.g. ``schema.radar``) and the
        transports fan them out like any other."""
        self._emit(event)

    def on_node_state(self, snapshot: list[dict[str, Any]]) -> None:
        """Emit a ``node_state`` per connected node, plus an offline event for any
        node that has dropped out since the previous snapshot."""
        present: set[str] = set()
        for n in snapshot:
            nid = str(n["node_id"])
            present.add(nid)
            self._emit(
                schema.node_state(
                    node_id=nid,
                    room=n.get("room"),
                    online=True,
                    streaming=bool(n.get("streaming")),
                    audio_ok=bool(n.get("audio_ok", True)),
                    muted=bool(n.get("muted")),
                    version=n.get("version"),
                )
            )
        for gone in self._known_nodes - present:
            self._emit(schema.node_state(node_id=gone, room=None, online=False))
        self._known_nodes = present

    def on_interaction(self, record: dict[str, Any]) -> None:
        """Translate a completed-pipeline record into ``interaction`` + ``presence``
        (timing + who/where only — no spoken text)."""
        node_id = record.get("node_id")
        room = record.get("room")
        speaker = record.get("speaker")
        self._emit(
            schema.interaction(
                node_id=node_id,
                room=room,
                speaker=speaker,
                fast=bool(record.get("fast")),
                latency_ms=record.get("total_ms"),
            )
        )
        self._emit(schema.presence(node_id=node_id, room=room, speaker=speaker))


def _server_snapshot(server: Any) -> list[dict[str, Any]]:
    """Build the node snapshot the hub needs from the server's live registry.

    Reads the connected ``NodeSession`` set; mirrors the dashboard's view but
    without dashboard-specific fields, so the hub works with the dashboard off.
    """
    transient = getattr(server, "_transient_node_cfg", {})
    out: list[dict[str, Any]] = []
    for node_id, s in server._nodes.items():
        out.append(
            {
                "node_id": node_id,
                "room": getattr(s, "room_id", None),
                "streaming": bool(getattr(s, "streaming", False)),
                "audio_ok": bool(getattr(s, "audio_ok", True)),
                "muted": bool(transient.get(node_id, {}).get("muted", False)),
                "version": getattr(s, "kenzy_version", None),
            }
        )
    return out


def attach_to_server(hub: IntegrationHub, server: Any) -> None:
    """Wire the hub to the server's existing observability listeners.

    Safe to call even with no transport configured: the registered listeners only
    feed the hub, which is a no-op while it has no subscribers.
    """
    # Seed from the durable roster, not from an empty set: a node that was already
    # missing when the server started is still missing, and without this it would
    # never be reported offline — the restart would quietly absolve it.
    roster = getattr(server, "_roster", None)
    if roster is not None:
        hub._known_nodes |= set(roster.known())
    server.add_state_listener(lambda: hub.on_node_state(_server_snapshot(server)))
    server.add_session_listener(hub.on_interaction)
