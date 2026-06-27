"""P0 of the Home Assistant integration: the event schema + in-process hub.

The hub translates the server's existing observability records into the versioned
schema and fans them out to subscribers, with transcripts/responses gated off by
default (privacy), online/offline transitions tracked, and a no-op when nobody
subscribes.
"""

from __future__ import annotations

from typing import Any

from kenzy.integrations import IntegrationHub, attach_to_server, schema
from kenzy.integrations.hub import _server_snapshot


def _collect() -> tuple[list[dict[str, Any]], IntegrationHub]:
    events: list[dict[str, Any]] = []
    hub = IntegrationHub()
    hub.subscribe(events.append)
    return events, hub


# --- schema ------------------------------------------------------------------


def test_every_event_carries_version_type_ts() -> None:
    for ev in (
        schema.node_state(node_id="a", room="Kitchen", online=True),
        schema.interaction(node_id="a", room="Kitchen", speaker="alice", fast=True, latency_ms=42),
        schema.presence(node_id="a", room="Kitchen", speaker="alice"),
    ):
        assert ev["schema_version"] == schema.SCHEMA_VERSION
        assert ev["type"] in {"node_state", "interaction", "presence"}
        assert isinstance(ev["ts"], float)


def test_node_state_derives_state_field() -> None:
    streaming = schema.node_state(node_id="a", room="r", online=True, streaming=True)
    assert streaming["state"] == "streaming"
    assert schema.node_state(node_id="a", room="r", online=True)["state"] == "idle"
    assert schema.node_state(node_id="a", room=None, online=False)["state"] == "offline"


# --- interaction + presence + privacy gate -----------------------------------


def _record(**kw: Any) -> dict[str, Any]:
    base = dict(
        node_id="n1", room="Kitchen", speaker="alice", fast=False,
        transcript="turn on the lights", response="done", total_ms=512,
    )
    base.update(kw)
    return base


def test_interaction_never_carries_text() -> None:
    events, hub = _collect()
    hub.on_interaction(_record())  # record includes transcript/response — must be dropped
    interaction = next(e for e in events if e["type"] == "interaction")
    assert "transcript" not in interaction and "response" not in interaction
    assert interaction["speaker"] == "alice" and interaction["latency_ms"] == 512


def test_interaction_also_emits_presence() -> None:
    events, hub = _collect()
    hub.on_interaction(_record())
    presence = next(e for e in events if e["type"] == "presence")
    assert presence == {
        "schema_version": schema.SCHEMA_VERSION,
        "type": "presence",
        "ts": presence["ts"],
        "node_id": "n1",
        "room": "Kitchen",
        "speaker": "alice",
    }


# --- node_state online/offline transitions -----------------------------------


def test_node_state_emits_per_node_and_offline_on_drop() -> None:
    events, hub = _collect()
    hub.on_node_state([{"node_id": "a", "room": "Kitchen", "audio_ok": True}])
    hub.on_node_state([])  # 'a' disappeared
    states = [e for e in events if e["type"] == "node_state"]
    assert states[0]["node_id"] == "a" and states[0]["online"] is True
    assert states[-1]["node_id"] == "a" and states[-1]["online"] is False
    assert states[-1]["state"] == "offline"


def test_no_subscribers_is_a_noop() -> None:
    hub = IntegrationHub()  # nobody subscribed
    hub.on_interaction(_record())
    hub.on_node_state([{"node_id": "a", "room": "r"}])  # must not raise


def test_subscriber_exception_does_not_break_others() -> None:
    seen: list[dict[str, Any]] = []
    hub = IntegrationHub()
    hub.subscribe(lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
    hub.subscribe(seen.append)
    hub.on_interaction(_record())
    assert seen, "second subscriber should still receive events"


# --- server wiring -----------------------------------------------------------


class _FakeSession:
    def __init__(self, room: str, streaming: bool = False) -> None:
        self.room_id = room
        self.streaming = streaming
        self.audio_ok = True
        self.kenzy_version = "3.2.1"


class _FakeServer:
    def __init__(self) -> None:
        self._nodes = {"a": _FakeSession("Kitchen"), "b": _FakeSession("Loft", streaming=True)}
        self._state_fns: list[Any] = []
        self._session_fns: list[Any] = []

    def add_state_listener(self, fn: Any) -> None:
        self._state_fns.append(fn)

    def add_session_listener(self, fn: Any) -> None:
        self._session_fns.append(fn)


def test_server_snapshot_reads_node_registry() -> None:
    snap = {n["node_id"]: n for n in _server_snapshot(_FakeServer())}
    assert snap["a"]["room"] == "Kitchen" and snap["a"]["streaming"] is False
    assert snap["b"]["streaming"] is True and snap["b"]["version"] == "3.2.1"


def test_attach_wires_both_listeners_and_flows_events() -> None:
    events, hub = _collect()
    server = _FakeServer()
    attach_to_server(hub, server)
    assert len(server._state_fns) == 1 and len(server._session_fns) == 1
    # Fire the state listener as the server would.
    server._state_fns[0]()
    rooms = {e["room"] for e in events if e["type"] == "node_state"}
    assert rooms == {"Kitchen", "Loft"}
    # Fire the session listener.
    server._session_fns[0](_record())
    assert any(e["type"] == "interaction" for e in events)
