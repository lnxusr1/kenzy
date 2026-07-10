"""MQTT transport for the integrations layer — Kenzy → Home Assistant (P1).

Subscribes to the :class:`~kenzy.integrations.hub.IntegrationHub` and publishes
Kenzy's state/events to an MQTT broker using **HA MQTT Discovery**, so each node
auto-appears in Home Assistant as a device with sensors — no HA-side code, no
custom integration required. HA reaches into Kenzy; Kenzy never lives inside HA.

The message *planning* (what to publish for a given event) is pure and unit-tested;
the :class:`MqttTransport` owns the async broker I/O. The ``aiomqtt`` import is lazy,
so the module loads (and the rest of the server runs) without the ``mqtt`` extra.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NamedTuple

log = logging.getLogger("kenzy.integrations.mqtt")

_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]+")


def _slug(node_id: str) -> str:
    """A topic/HA-object-id-safe form of a node_id."""
    return _UNSAFE.sub("_", node_id).strip("_") or "node"


def _iso(ts: float | None) -> str:
    """Epoch seconds → ISO-8601 UTC (HA ``timestamp`` device_class format)."""
    return datetime.fromtimestamp(float(ts or 0.0), tz=UTC).isoformat()


@dataclass
class MqttConfig:
    """Broker + topic settings. Credentials come from the environment, never config."""

    host: str = "127.0.0.1"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    base_topic: str = "kenzy"
    discovery_prefix: str = "homeassistant"
    commands: bool = True  # accept inbound commands (HA → Kenzy); false ⇒ read-only

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any]) -> MqttConfig:
        return cls(
            host=str(cfg.get("host", "127.0.0.1")),
            port=int(cfg.get("port", 1883)),
            username=os.environ.get("KENZY_MQTT_USERNAME"),
            password=os.environ.get("KENZY_MQTT_PASSWORD"),
            base_topic=str(cfg.get("base_topic", "kenzy")),
            discovery_prefix=str(cfg.get("discovery_prefix", "homeassistant")),
            commands=bool(cfg.get("commands", True)),
        )


@dataclass
class Command:
    """A parsed inbound command from HA. ``node_id`` is the resolved node (None for
    house-wide announce); ``value`` is the volume level / mute bool / announce text."""

    action: str  # trigger | stop | volume | mute | announce
    node_id: str | None
    value: Any = None


def command_filters(base_topic: str) -> list[str]:
    """MQTT subscription filters for the inbound command topics."""
    return [
        f"{base_topic}/+/trigger",
        f"{base_topic}/+/stop",
        f"{base_topic}/+/volume",
        f"{base_topic}/+/mute",
        f"{base_topic}/announce",
    ]


def parse_command(
    topic: str, payload: str, *, base_topic: str, slug_to_node: dict[str, str]
) -> Command | None:
    """Map an inbound (topic, payload) to a :class:`Command`, or None if unrecognised.

    The topic addresses a node by its slug; ``slug_to_node`` resolves it back to the
    real node_id (falling back to the slug, which equals the node_id for plain ids).
    """
    prefix = f"{base_topic}/"
    if not topic.startswith(prefix):
        return None
    toks = topic[len(prefix) :].split("/")

    if toks == ["announce"]:
        text = payload.strip()
        return Command("announce", None, text) if text else None

    if len(toks) == 2:
        slug, action = toks
        node_id = slug_to_node.get(slug, slug)
        if action in ("trigger", "stop"):
            return Command(action, node_id)
        if action == "volume":
            try:
                return Command("volume", node_id, max(0, min(100, int(payload.strip()))))
            except ValueError:
                return None
        if action == "mute":
            v = payload.strip().lower()
            if v in ("on", "true", "1", "mute", "muted"):
                return Command("mute", node_id, True)
            if v in ("off", "false", "0", "unmute"):
                return Command("mute", node_id, False)
    return None


class Message(NamedTuple):
    topic: str
    payload: str
    retain: bool = True
    qos: int = 1


def bridge_availability_topic(base_topic: str) -> str:
    return f"{base_topic}/bridge/availability"


# Entities published per node: (component, key, friendly name, extra discovery fields).
_ENTITIES: list[tuple[str, str, str, dict[str, Any]]] = [
    ("sensor", "state", "State", {"icon": "mdi:microphone"}),
    ("sensor", "last_speaker", "Last speaker", {"icon": "mdi:account-voice"}),
    ("sensor", "last_heard", "Last heard", {"device_class": "timestamp"}),
]
# Command (write) entities — HA controls that publish to a command_topic.
_COMMAND_ENTITIES: list[tuple[str, str, str, dict[str, Any]]] = [
    ("button", "trigger", "Trigger", {"payload_press": "PRESS", "icon": "mdi:microphone-message"}),
    ("button", "stop", "Stop", {"payload_press": "PRESS", "icon": "mdi:stop"}),
]


def discovery_messages(
    node_id: str,
    room: str | None,
    *,
    base_topic: str,
    discovery_prefix: str,
    include_commands: bool = False,
) -> list[Message]:
    """Retained HA MQTT Discovery config messages for one node's entities."""
    slug = _slug(node_id)
    device = {
        "identifiers": [f"kenzy_{slug}"],
        "name": f"Kenzy {room or node_id}",
        "manufacturer": "Kenzy",
        "model": "node",
    }
    if room:
        # HA assigns the device to this area only while it has none (creating the
        # area if needed); a manual assignment in HA always wins and is never reset.
        device["suggested_area"] = room
    availability = [
        {"topic": bridge_availability_topic(base_topic)},
        {"topic": f"{base_topic}/{slug}/availability"},
    ]
    base = {"availability": availability, "availability_mode": "all", "device": device}
    out: list[Message] = []

    for component, key, name, extra in _ENTITIES:
        payload: dict[str, Any] = {
            "name": name,
            "unique_id": f"kenzy_{slug}_{key}",
            "object_id": f"kenzy_{slug}_{key}",
            "state_topic": f"{base_topic}/{slug}/{key}",
            **base,
            **extra,
        }
        topic = f"{discovery_prefix}/{component}/kenzy_{slug}_{key}/config"
        out.append(Message(topic, json.dumps(payload)))

    if include_commands:
        for component, key, name, extra in _COMMAND_ENTITIES:
            payload = {
                "name": name,
                "unique_id": f"kenzy_{slug}_{key}",
                "object_id": f"kenzy_{slug}_{key}",
                "command_topic": f"{base_topic}/{slug}/{key}",
                **base,
                **extra,
            }
            out.append(
                Message(
                    f"{discovery_prefix}/{component}/kenzy_{slug}_{key}/config", json.dumps(payload)
                )
            )
        # Mute is a stateful toggle → a switch (command + state topic), so HA
        # reflects mutes made by voice or the dashboard, not just from HA.
        mute = {
            "name": "Mute",
            "unique_id": f"kenzy_{slug}_mute",
            "object_id": f"kenzy_{slug}_mute",
            "command_topic": f"{base_topic}/{slug}/mute",
            "state_topic": f"{base_topic}/{slug}/mute_state",
            "payload_on": "ON",
            "payload_off": "OFF",
            "icon": "mdi:volume-off",
            **base,
        }
        out.append(Message(f"{discovery_prefix}/switch/kenzy_{slug}_mute/config", json.dumps(mute)))
    return out


def plan_messages(
    event: dict[str, Any],
    *,
    base_topic: str,
    discovery_prefix: str,
    announced: set[str],
    include_commands: bool = False,
) -> list[Message]:
    """Translate one hub event into the MQTT messages to publish.

    On first sight of an online node, discovery configs are emitted (and the node is
    recorded in ``announced``) before its state, so HA has the entity before a value
    lands on its state topic.
    """
    etype = event.get("type")
    node_id = event.get("node_id")
    if not node_id:
        return []
    slug = _slug(str(node_id))
    out: list[Message] = []

    if etype == "node_state":
        online = bool(event.get("online"))
        if online and node_id not in announced:
            out += discovery_messages(
                str(node_id),
                event.get("room"),
                base_topic=base_topic,
                discovery_prefix=discovery_prefix,
                include_commands=include_commands,
            )
            announced.add(str(node_id))
        out.append(Message(f"{base_topic}/{slug}/availability", "online" if online else "offline"))
        out.append(Message(f"{base_topic}/{slug}/state", str(event.get("state", ""))))
        if online and include_commands:  # mute switch state (only when the switch exists)
            out.append(
                Message(f"{base_topic}/{slug}/mute_state", "ON" if event.get("muted") else "OFF")
            )

    elif etype == "presence":
        out.append(Message(f"{base_topic}/{slug}/last_speaker", str(event.get("speaker") or "")))
        out.append(Message(f"{base_topic}/{slug}/last_heard", _iso(event.get("ts"))))

    # interaction events carry only timing/metadata and aren't surfaced to MQTT;
    # presence (above) provides the who/where/when. node text is never published.
    return out


class MqttTransport:
    """Bridges the hub to an MQTT broker, reconnecting with backoff.

    Outbound: ``submit`` is the hub subscriber (called in-loop; never blocks) —
    events are queued and published by :meth:`run`. Inbound: when a ``dispatch``
    callback is given, command topics are subscribed and parsed commands are handed
    to it (HA → Kenzy). Without ``dispatch`` the bridge is publish-only (read-only).
    """

    def __init__(
        self,
        config: MqttConfig,
        dispatch: Callable[[Command], Awaitable[None]] | None = None,
    ) -> None:
        self._cfg = config
        self._dispatch = dispatch
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2000)
        self._announced: set[str] = set()
        self._slug_to_node: dict[str, str] = {}

    def submit(self, event: dict[str, Any]) -> None:
        # Track slug→node_id from outbound state so inbound commands resolve back.
        if event.get("type") == "node_state":
            nid = event.get("node_id")
            if nid:
                self._slug_to_node[_slug(str(nid))] = str(nid)
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            log.debug("MQTT queue full — dropping event %s", event.get("type"))

    async def run(self) -> None:
        try:
            import aiomqtt  # type: ignore[import-not-found,import-untyped]
        except ImportError:
            log.error(
                "integrations.mqtt is enabled but 'aiomqtt' is not installed "
                "(pip install 'kenzy[mqtt]') — MQTT integration disabled."
            )
            return

        avail = bridge_availability_topic(self._cfg.base_topic)
        backoff = 1.0
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self._cfg.host,
                    port=self._cfg.port,
                    username=self._cfg.username,
                    password=self._cfg.password,
                    will=aiomqtt.Will(avail, "offline", qos=1, retain=True),
                ) as client:
                    backoff = 1.0
                    self._announced.clear()  # re-publish discovery after a reconnect
                    await client.publish(avail, "online", qos=1, retain=True)
                    log.info("MQTT integration connected to %s:%s", self._cfg.host, self._cfg.port)
                    try:
                        await self._serve(client)
                    except asyncio.CancelledError:
                        # Graceful shutdown: a clean disconnect suppresses the LWT, so
                        # mark the bridge offline ourselves first (qos=1 waits for the
                        # ack, so it lands before the connection closes).
                        with contextlib.suppress(Exception):
                            await client.publish(avail, "offline", qos=1, retain=True)
                        raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # broker down / dropped — back off and retry
                log.warning("MQTT integration error: %s — reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _serve(self, client: Any) -> None:
        """Run the publish loop and (if commands are enabled) the inbound message loop
        concurrently; surface the first failure so :meth:`run` can reconnect."""
        tasks = [asyncio.create_task(self._publish_loop(client))]
        if self._dispatch is not None:
            for filt in command_filters(self._cfg.base_topic):
                await client.subscribe(filt)
            tasks.append(asyncio.create_task(self._consume(client)))
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                task.result()  # re-raise (e.g. broker disconnect → reconnect)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task

    async def _publish_loop(self, client: Any) -> None:
        while True:
            event = await self._queue.get()
            for m in plan_messages(
                event,
                base_topic=self._cfg.base_topic,
                discovery_prefix=self._cfg.discovery_prefix,
                announced=self._announced,
                include_commands=self._dispatch is not None,
            ):
                await client.publish(m.topic, m.payload, qos=m.qos, retain=m.retain)

    async def _consume(self, client: Any) -> None:
        async for message in client.messages:
            await self._handle_message(message)

    async def _handle_message(self, message: Any) -> None:
        if self._dispatch is None:
            return
        try:
            payload = bytes(message.payload).decode("utf-8", "replace")
        except Exception:
            return
        cmd = parse_command(
            str(message.topic),
            payload,
            base_topic=self._cfg.base_topic,
            slug_to_node=self._slug_to_node,
        )
        if cmd is None:
            return
        try:
            await self._dispatch(cmd)
        except Exception:  # a bad command must never break the bridge
            log.debug("command dispatch error for %s", cmd, exc_info=True)
