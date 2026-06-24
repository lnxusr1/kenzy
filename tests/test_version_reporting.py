"""Per-host version reporting: kenzy_version(), the hello field, the service /health
field, and the node version surfaced in the dashboard state (visibility for upgrades)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from kenzy import kenzy_version, protocol
from kenzy.server.dashboard import Dashboard, DashboardConfig
from kenzy.server.server import AudioServer, NodeSession
from kenzy.speaker import speaker as speaker_svc


class _StubWS:
    pass


def test_kenzy_version_nonempty():
    assert isinstance(kenzy_version(), str) and kenzy_version()


def test_hello_carries_kenzy_version():
    msg = json.loads(protocol.hello("den", node_id="n1", kenzy_version="3.1.0"))
    assert msg["kenzy_version"] == "3.1.0"
    # omitted by default (legacy nodes simply don't send it)
    assert "kenzy_version" not in json.loads(protocol.hello("den"))


def test_service_health_reports_version():
    r = TestClient(speaker_svc.app).get("/health")
    assert r.status_code == 200
    assert r.json()["version"] == kenzy_version()


def test_node_version_surfaced_in_state():
    server = AudioServer({})
    server._nodes["n1"] = NodeSession(
        ws=_StubWS(), node_id="n1", room_id="kitchen", kenzy_version="3.1.0"
    )
    dash = Dashboard(server, {}, DashboardConfig(enabled=True))
    node = dash._nodes_state()[0]
    assert node["version"] == "3.1.0"
    # a legacy node that didn't report one shows None, not an error
    server._nodes["n2"] = NodeSession(ws=_StubWS(), node_id="n2", room_id="den")
    assert {n["node_id"]: n["version"] for n in dash._nodes_state()}["n2"] is None
