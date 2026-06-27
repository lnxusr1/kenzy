"""Integrations layer — surfacing Kenzy's state and events to external systems.

This is the in-repo foundation for the Home Assistant integration ("C" model in
``design/home-assistant-integration.md``): a versioned event :mod:`schema` and an
in-process :class:`~kenzy.integrations.hub.IntegrationHub` that translates the
server's existing observability listeners into that schema and fans it out to
subscribed transports.

P0 (this) ships the schema + hub + wiring only — no transport, nothing wired into
the running server. P1 adds an MQTT transport (with HA MQTT Discovery) that
subscribes to the hub; P2 adds the inbound command path.
"""

from __future__ import annotations

from kenzy.integrations import schema
from kenzy.integrations.hub import IntegrationHub, attach_to_server

__all__ = ["schema", "IntegrationHub", "attach_to_server", "SCHEMA_VERSION"]

SCHEMA_VERSION = schema.SCHEMA_VERSION
