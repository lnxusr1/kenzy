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


def test_running_version_is_frozen_installed_is_live(monkeypatch):
    """The running version is captured at import; installed_version() follows the
    disk. The gap between them is exactly "upgraded but needs a restart" — an
    un-recycled service used to claim the new version right after a pip upgrade
    (backlog #6, found during the grocery-list debugging loop)."""
    import importlib.metadata

    import kenzy

    running = kenzy.kenzy_version()
    monkeypatch.setattr(  # pip upgrades the package under a live process
        importlib.metadata, "version", lambda name: "99.0.0" if name == "kenzy" else "0"
    )
    assert kenzy.installed_version() == "99.0.0"  # follows the disk
    assert kenzy.kenzy_version() == running  # stays with the running code
    assert kenzy.version_info() == {"version": running, "installed": "99.0.0"}


async def test_upgrade_short_circuits_to_restart_when_installed(monkeypatch):
    """Clicking Upgrade on a service whose venv already holds the target version
    (co-located with an upgraded server) must restart it, not run pip again; and
    one already RUNNING the target must be left alone entirely."""
    server = AudioServer({"stt": {"url": "http://127.0.0.1:1/transcribe"}})
    dash = Dashboard(server, {"stt": {"url": "http://127.0.0.1:1/transcribe"}}, DashboardConfig())
    calls: list[str] = []

    async def fake_health(base):
        return {"version": "3.7.3", "installed": "3.7.4"}

    async def fake_restart(name):
        calls.append(f"restart:{name}")
        return True

    async def fake_latest():
        return "3.7.4"

    monkeypatch.setattr(dash, "_service_health", fake_health)
    monkeypatch.setattr(dash, "_restart_service", fake_restart)
    monkeypatch.setattr(dash, "_latest_pypi_version", fake_latest)

    ok, output = await dash._upgrade_service("stt", None)
    assert ok and calls == ["restart:stt"]
    assert "already installed" in output and "restarted" in output

    # Already running the target → nothing to do (no restart, no pip).
    async def fake_health_current(base):
        return {"version": "3.7.4", "installed": "3.7.4"}

    monkeypatch.setattr(dash, "_service_health", fake_health_current)
    calls.clear()
    ok, output = await dash._upgrade_service("stt", None)
    assert ok and calls == []
    assert "nothing to do" in output


def test_health_reports_installed_alongside_running(monkeypatch):
    import importlib.metadata

    monkeypatch.setattr(
        importlib.metadata, "version", lambda name: "99.0.0" if name == "kenzy" else "0"
    )
    body = TestClient(speaker_svc.app).get("/health").json()
    assert body["version"] == kenzy_version()  # running code
    assert body["installed"] == "99.0.0"  # on-disk (restart pending)


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
