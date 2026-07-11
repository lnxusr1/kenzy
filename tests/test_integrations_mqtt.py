"""P1 of the Home Assistant integration: MQTT publish + HA MQTT Discovery.

Covers the pure message-planning layer (no broker): discovery config payloads,
event→state-topic mapping, the announce-once behaviour, the bridge/node availability
model, and the transcript privacy gate.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

import pytest

from kenzy.integrations import schema
from kenzy.integrations.mqtt import (
    Command,
    MqttConfig,
    MqttTransport,
    _slug,
    command_filters,
    discovery_messages,
    parse_command,
    plan_messages,
)


def _plan(event: dict[str, Any], announced: set[str], *, include_commands: bool = False) -> list:
    return plan_messages(
        event,
        base_topic="kenzy",
        discovery_prefix="homeassistant",
        announced=announced,
        include_commands=include_commands,
    )


# --- config ------------------------------------------------------------------


def test_config_from_cfg_reads_creds_from_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("KENZY_MQTT_USERNAME", "u")
    monkeypatch.setenv("KENZY_MQTT_PASSWORD", "p")
    c = MqttConfig.from_cfg({"host": "broker.lan", "port": 8883})
    assert c.host == "broker.lan" and c.port == 8883
    assert c.username == "u" and c.password == "p"  # secrets from env, not config


def test_slug_sanitizes_node_ids() -> None:
    assert _slug("kitchen-pi") == "kitchen-pi"
    assert _slug("a.b c/d") == "a_b_c_d"


# --- discovery ---------------------------------------------------------------


def test_discovery_emits_one_retained_config_per_entity() -> None:
    msgs = discovery_messages(
        "kitchen", "Kitchen", base_topic="kenzy", discovery_prefix="homeassistant"
    )
    topics = [m.topic for m in msgs]
    assert topics == [
        "homeassistant/sensor/kenzy_kitchen_state/config",
        "homeassistant/sensor/kenzy_kitchen_last_speaker/config",
        "homeassistant/sensor/kenzy_kitchen_last_heard/config",
    ]
    assert all(m.retain for m in msgs)
    payload = json.loads(msgs[0].payload)
    assert payload["unique_id"] == "kenzy_kitchen_state"
    assert payload["state_topic"] == "kenzy/kitchen/state"
    assert payload["device"]["identifiers"] == ["kenzy_kitchen"]
    # the room name doubles as HA's suggested area (only applied while unset in HA)
    assert payload["device"]["suggested_area"] == "Kitchen"
    # availability gated on BOTH the bridge and the node being online
    assert payload["availability_mode"] == "all"
    assert {a["topic"] for a in payload["availability"]} == {
        "kenzy/bridge/availability",
        "kenzy/kitchen/availability",
    }


def test_discovery_no_suggested_area_without_room() -> None:
    msgs = discovery_messages("n1", None, base_topic="kenzy", discovery_prefix="homeassistant")
    assert "suggested_area" not in json.loads(msgs[0].payload)["device"]


def test_discovery_never_includes_text_entities() -> None:
    msgs = discovery_messages(
        "k", "K", base_topic="kenzy", discovery_prefix="homeassistant", include_commands=True
    )
    assert not any("last_transcript" in m.topic or "last_response" in m.topic for m in msgs)


# --- planning ----------------------------------------------------------------


def test_node_state_announces_discovery_once_then_only_state() -> None:
    announced: set[str] = set()
    first = _plan(schema.node_state(node_id="kitchen", room="Kitchen", online=True), announced)
    # discovery (3) + availability + state
    assert sum("/config" in m.topic for m in first) == 3
    assert ("kenzy/kitchen/availability", "online") == (first[-2].topic, first[-2].payload)
    assert ("kenzy/kitchen/state", "idle") == (first[-1].topic, first[-1].payload)
    assert announced == {"kitchen"}

    second = _plan(schema.node_state(node_id="kitchen", room="Kitchen", online=True), announced)
    assert not any("/config" in m.topic for m in second)  # not re-announced


def test_offline_node_state_sets_availability_offline() -> None:
    announced = {"kitchen"}
    msgs = _plan(schema.node_state(node_id="kitchen", room=None, online=False), announced)
    avail = next(m for m in msgs if m.topic.endswith("/availability"))
    assert avail.payload == "offline"


def test_presence_publishes_speaker_and_timestamp() -> None:
    msgs = _plan(schema.presence(node_id="kitchen", room="Kitchen", speaker="alice"), set())
    by_topic = {m.topic: m.payload for m in msgs}
    assert by_topic["kenzy/kitchen/last_speaker"] == "alice"
    assert by_topic["kenzy/kitchen/last_heard"].endswith("+00:00")  # ISO-8601 UTC


def test_interaction_events_publish_nothing() -> None:
    # interaction carries only timing/metadata and is not surfaced to MQTT.
    ev = schema.interaction(node_id="kitchen", room="Kitchen", speaker="alice", fast=False,
                            latency_ms=10)
    assert _plan(ev, set()) == []
    assert _plan(ev, set(), include_commands=True) == []


def test_event_without_node_id_is_ignored() -> None:
    assert _plan({"type": "presence", "speaker": "x"}, set()) == []


# --- transport queue ---------------------------------------------------------


def test_submit_is_nonblocking_and_drops_when_full() -> None:
    t = MqttTransport(MqttConfig())
    # fill beyond capacity — submit must never raise
    for i in range(2100):
        t.submit({"type": "presence", "node_id": "n", "i": i})
    assert t._queue.full()


# --- graceful shutdown (mocked broker) ---------------------------------------


async def test_graceful_shutdown_publishes_offline(monkeypatch: Any) -> None:
    """On cancellation the transport marks the bridge offline before disconnecting
    (a clean disconnect suppresses the LWT, so we must publish it ourselves)."""
    published: list[tuple[str, str]] = []

    class _FakeClient:
        def __init__(self, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *a: Any) -> bool:
            return False

        async def publish(
            self, topic: str, payload: str, qos: int = 0, retain: bool = False
        ) -> None:
            published.append((topic, payload))

    fake = types.ModuleType("aiomqtt")
    fake.Client = _FakeClient  # type: ignore[attr-defined]
    fake.Will = lambda *a, **k: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "aiomqtt", fake)

    transport = MqttTransport(MqttConfig(base_topic="kenzy"))
    task = asyncio.create_task(transport.run())
    await asyncio.sleep(0.05)  # let it "connect" and publish online
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    avail = "kenzy/bridge/availability"
    assert (avail, "online") in published
    assert (avail, "offline") in published
    assert published.index((avail, "offline")) > published.index((avail, "online"))


# --- P2: inbound commands ----------------------------------------------------


def _parse(topic: str, payload: str, slug_to_node: dict[str, str] | None = None) -> Command | None:
    return parse_command(topic, payload, base_topic="kenzy", slug_to_node=slug_to_node or {})


def test_parse_trigger_and_stop_resolve_node_id() -> None:
    # slug maps back to the real node_id when known…
    assert _parse("kenzy/kitchen/trigger", "PRESS", {"kitchen": "kitchen-pi"}) == Command(
        "trigger", "kitchen-pi"
    )
    # …and falls back to the slug otherwise
    assert _parse("kenzy/loft/stop", "PRESS") == Command("stop", "loft")


def test_parse_volume_clamps_and_rejects_garbage() -> None:
    assert _parse("kenzy/kitchen/volume", "150") == Command("volume", "kitchen", 100)
    assert _parse("kenzy/kitchen/volume", "-5") == Command("volume", "kitchen", 0)
    assert _parse("kenzy/kitchen/volume", "loud") is None


def test_parse_mute_accepts_common_truthy_forms() -> None:
    assert _parse("kenzy/kitchen/mute", "ON").value is True
    assert _parse("kenzy/kitchen/mute", "false").value is False
    assert _parse("kenzy/kitchen/mute", "maybe") is None


def test_parse_announce_is_house_wide() -> None:
    assert _parse("kenzy/announce", "dinner up") == Command("announce", None, "dinner up")
    assert _parse("kenzy/announce", "   ") is None  # empty text ignored


def test_parse_ignores_unknown_topics() -> None:
    assert _parse("kenzy/kitchen/explode", "x") is None
    assert _parse("other/kitchen/trigger", "x") is None


def test_command_buttons_in_discovery_only_when_enabled() -> None:
    no_cmd = discovery_messages(
        "kitchen", "Kitchen", base_topic="kenzy", discovery_prefix="homeassistant",
        include_commands=False,
    )
    with_cmd = discovery_messages(
        "kitchen", "Kitchen", base_topic="kenzy", discovery_prefix="homeassistant",
        include_commands=True,
    )
    assert not any("/button/" in m.topic or "/switch/" in m.topic for m in no_cmd)
    buttons = [m for m in with_cmd if "/button/" in m.topic]
    assert {m.topic for m in buttons} == {
        "homeassistant/button/kenzy_kitchen_trigger/config",
        "homeassistant/button/kenzy_kitchen_stop/config",
    }
    payload = json.loads(buttons[0].payload)
    assert payload["command_topic"] == "kenzy/kitchen/trigger"
    assert payload["payload_press"] == "PRESS"


def test_mute_is_a_stateful_switch() -> None:
    msgs = discovery_messages(
        "kitchen", "Kitchen", base_topic="kenzy", discovery_prefix="homeassistant",
        include_commands=True,
    )
    switch = next(m for m in msgs if "/switch/" in m.topic)
    assert switch.topic == "homeassistant/switch/kenzy_kitchen_mute/config"
    p = json.loads(switch.payload)
    assert p["command_topic"] == "kenzy/kitchen/mute"
    assert p["state_topic"] == "kenzy/kitchen/mute_state"  # reflects current state
    assert p["payload_on"] == "ON" and p["payload_off"] == "OFF"


def test_mute_state_published_only_with_commands() -> None:
    ev = schema.node_state(node_id="kitchen", room="Kitchen", online=True, muted=True)
    # commands off → no mute_state topic
    assert not any(m.topic.endswith("/mute_state") for m in _plan(ev, set()))
    # commands on → mute_state reflects the muted flag
    on = {m.topic: m.payload for m in _plan(ev, set(), include_commands=True)}
    assert on["kenzy/kitchen/mute_state"] == "ON"
    unmuted = schema.node_state(node_id="kitchen", room="Kitchen", online=True, muted=False)
    off = {m.topic: m.payload for m in _plan(unmuted, set(), include_commands=True)}
    assert off["kenzy/kitchen/mute_state"] == "OFF"


async def test_inbound_command_dispatched(monkeypatch: Any) -> None:
    """A message on a command topic is parsed and handed to the dispatch callback."""
    got: list[Command] = []

    class _FakeMessage:
        topic = "kenzy/kitchen-pi/trigger"
        payload = b"PRESS"

    class _FakeClient:
        def __init__(self, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *a: Any) -> bool:
            return False

        async def publish(self, *a: Any, **k: Any) -> None:
            pass

        async def subscribe(self, *a: Any, **k: Any) -> None:
            pass

        @property
        def messages(self) -> Any:
            async def _gen() -> Any:
                yield _FakeMessage()
                await asyncio.sleep(3600)  # then idle

            return _gen()

    fake = types.ModuleType("aiomqtt")
    fake.Client = _FakeClient  # type: ignore[attr-defined]
    fake.Will = lambda *a, **k: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "aiomqtt", fake)

    async def dispatch(cmd: Command) -> None:
        got.append(cmd)

    transport = MqttTransport(MqttConfig(base_topic="kenzy"), dispatch=dispatch)
    # seed the slug→node map as an outbound state event would
    transport.submit({"type": "node_state", "node_id": "kitchen-pi", "online": True})
    task = asyncio.create_task(transport.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert got == [Command("trigger", "kitchen-pi")]


# --- kenzy/chime (house-wide doorbell) ---------------------------------------


def test_chime_topic_subscribed() -> None:
    assert "kenzy/chime" in command_filters("kenzy")


def test_chime_parse_forms() -> None:
    kw = {"base_topic": "kenzy", "slug_to_node": {}}
    # Full JSON: caller picks the sound, loop seconds, and rooms.
    c = parse_command(
        "kenzy/chime", '{"sound": "gong", "seconds": 8, "rooms": ["kitchen"]}', **kw
    )
    assert c == Command("chime", None, {"sound": "gong", "seconds": 8, "rooms": ["kitchen"]})
    # Bare string = the sound name, played once.
    c = parse_command("kenzy/chime", "doorbell", **kw)
    assert c == Command("chime", None, {"sound": "doorbell"})
    # Empty payload = the default chime, once.
    c = parse_command("kenzy/chime", "", **kw)
    assert c == Command("chime", None, {})
    # A JSON string payload is also just a sound name.
    c = parse_command("kenzy/chime", '"gong"', **kw)
    assert c == Command("chime", None, {"sound": "gong"})
    # Partial dicts parse with None gaps (the server sanitizes).
    c = parse_command("kenzy/chime", '{"seconds": 5}', **kw)
    assert c is not None and c.value["sound"] is None and c.value["seconds"] == 5
