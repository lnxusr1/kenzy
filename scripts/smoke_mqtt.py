#!/usr/bin/env python3
"""Manual smoke test for the MQTT / Home Assistant integration (P1).

Connects to a broker and publishes the same events the server would when a node
comes online and a voice interaction completes — without needing a node or the
STT/LLM/TTS pipeline. Use it to confirm Kenzy's entities appear in Home Assistant.

Run (point it at HA's Mosquitto broker):

    export KENZY_MQTT_HOST=homeassistant.lan        # your HA host
    export KENZY_MQTT_USERNAME=kenzy                 # an MQTT/HA user
    export KENZY_MQTT_PASSWORD=secret
    python scripts/smoke_mqtt.py

Then look in Home Assistant → Settings → Devices & Services → MQTT for a
"Kenzy Kitchen" device. Ctrl-C to exit; the broker will mark the bridge offline
(the LWT), which makes the entities go unavailable in HA.
"""

from __future__ import annotations

import asyncio
import os

from kenzy.integrations import IntegrationHub
from kenzy.integrations.mqtt import Command, MqttConfig, MqttTransport


async def main() -> None:
    # from_cfg reads KENZY_MQTT_USERNAME / KENZY_MQTT_PASSWORD from the environment.
    cfg = MqttConfig.from_cfg(
        {
            "host": os.environ.get("KENZY_MQTT_HOST", "127.0.0.1"),
            "port": int(os.environ.get("KENZY_MQTT_PORT", "1883")),
        }
    )
    print(f"Connecting to broker {cfg.host}:{cfg.port} as {cfg.username or '(anonymous)'} …")

    # A dispatch callback (a) makes the transport publish the Trigger/Stop buttons,
    # and (b) lets us see inbound commands. The real server wires these to node
    # actions; here we just print them, so clicking a button in HA shows up below.
    async def on_command(cmd: Command) -> None:
        print(f"  ← received command from HA: {cmd}")

    transport = MqttTransport(cfg, dispatch=on_command)
    hub = IntegrationHub()
    hub.subscribe(transport.submit)
    task = asyncio.create_task(transport.run())

    await asyncio.sleep(1.5)  # let it connect (watch for the "connected" log line)

    print("Publishing: node 'kitchen' online …")
    hub.on_node_state([{"node_id": "kitchen", "room": "Kitchen", "audio_ok": True}])

    print("Publishing: a completed interaction in the Kitchen …")
    hub.on_interaction(
        {
            "node_id": "kitchen",
            "room": "Kitchen",
            "speaker": "alice",
            "fast": False,
            "transcript": "turn on the lights",
            "response": "Done.",
            "total_ms": 512,
        }
    )

    await asyncio.sleep(2.0)  # let the publishes flush
    print(
        "\nDone publishing. In Home Assistant → Settings → Devices & Services → MQTT,"
        "\nopen the 'Kenzy Kitchen' device — you should see the sensors plus Trigger"
        "\nand Stop buttons. Click a button and watch for a 'received command' line"
        "\nhere. Press Ctrl-C to disconnect (entities then go unavailable via the LWT)."
    )
    try:
        await task  # keep the bridge 'online' until you Ctrl-C
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDisconnected.")
