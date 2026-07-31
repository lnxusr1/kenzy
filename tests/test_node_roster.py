"""The fleet roster: a node that drops off must become *absent*, not nonexistent.

Before this, a disconnected node vanished from the registry, the dashboard and
the fleet count together — so a four-room house quietly becoming a three-room
house looked exactly like a house that only ever had three rooms. One did, for
two days, and nothing anywhere said a word.
"""

from __future__ import annotations

import json

from kenzy.server.roster import NodeRoster, RosterEntry
from kenzy.server.server import AudioServer, NodeSession


class _StubWS:
    remote_address = ("192.168.1.9", 5000)

    async def send(self, m):  # noqa: ANN001, ANN201
        pass

    async def close(self, *a):  # noqa: ANN201
        pass


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


def test_roster_persists_and_reloads(tmp_path):
    path = tmp_path / "nodes.json"
    roster = NodeRoster(path)
    roster.touch("n1", room="Master Bedroom", version="4.4.2", ip="192.168.57.37", when=1000.0)

    reloaded = NodeRoster(path)
    entry = reloaded.known()["n1"]
    assert entry.room == "Master Bedroom"
    assert entry.version == "4.4.2"
    assert entry.last_seen == 1000.0


def test_absent_lists_known_nodes_that_are_not_connected(tmp_path):
    roster = NodeRoster(tmp_path / "nodes.json")
    roster.touch("bedroom", room="Master Bedroom", when=100.0)
    roster.touch("office", room="Office", when=200.0)

    absent = roster.absent(["office"])
    assert [e.node_id for e in absent] == ["bedroom"]
    # Oldest sighting first, so the longest outage reads at the top.
    roster.touch("kitchen", room="Kitchen", when=50.0)
    assert [e.node_id for e in roster.absent([])] == ["kitchen", "bedroom", "office"]


def test_alerting_needs_the_threshold(tmp_path):
    roster = NodeRoster(tmp_path / "nodes.json")
    entry = RosterEntry(node_id="n1", last_seen=1000.0)
    assert not roster.is_alerting(entry, 300, now=1200.0)  # gone 200s, threshold 300
    assert roster.is_alerting(entry, 300, now=1400.0)  # gone 400s
    assert not roster.is_alerting(entry, 0, now=9999.0)  # threshold 0 disables


def test_grace_suppresses_an_expected_absence(tmp_path):
    """An alert people learn to ignore is worth less than no alert, so downtime
    we asked for must not raise one."""
    roster = NodeRoster(tmp_path / "nodes.json")
    roster.touch("n1", when=1000.0)
    roster.grant_grace("n1", 600, now=1000.0)
    entry = roster.known()["n1"]

    assert not roster.is_alerting(entry, 60, now=1300.0)  # inside the window
    assert roster.is_alerting(entry, 60, now=1700.0)  # window elapsed, still gone


def test_returning_clears_the_grace(tmp_path):
    roster = NodeRoster(tmp_path / "nodes.json")
    roster.touch("n1", when=1000.0)
    roster.grant_grace("n1", 600, now=1000.0)
    roster.touch("n1", when=1100.0)  # it came back
    assert roster.known()["n1"].grace_until == 0.0


def test_grace_survives_the_disconnect_it_covers(tmp_path):
    """The window is granted *before* the node leaves, so stamping the departure
    must not clear it — otherwise the node is flagged for exactly the restart we
    asked it to perform, and the grace never covers anything at all."""
    roster = NodeRoster(tmp_path / "nodes.json")
    roster.touch("n1", when=1000.0)
    roster.grant_grace("n1", 600, now=1000.0)
    roster.touch("n1", when=1005.0, clear_grace=False)  # the disconnect itself
    assert roster.known()["n1"].grace_until == 1600.0
    assert not roster.is_alerting(roster.known()["n1"], 60, now=1200.0)


def test_forget_removes_a_node(tmp_path):
    path = tmp_path / "nodes.json"
    roster = NodeRoster(path)
    roster.touch("n1", room="Spare")
    assert roster.forget("n1") is True
    assert roster.forget("n1") is False  # already gone
    assert NodeRoster(path).known() == {}


def test_unreadable_roster_does_not_break_the_server(tmp_path):
    path = tmp_path / "nodes.json"
    path.write_text("{not json")
    assert NodeRoster(path).known() == {}


def test_missing_data_root_is_survivable():
    """A read-only or absent data root costs the roster, never a connection."""
    roster = NodeRoster(None)
    roster.touch("n1", room="Office")
    assert roster.known()["n1"].room == "Office"


def test_unknown_fields_survive_a_round_trip(tmp_path):
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps({"nodes": {"n1": {"room": "Loft", "future_key": 7}}}))
    NodeRoster(path).touch("n1", when=5.0)
    assert json.loads(path.read_text())["nodes"]["n1"]["future_key"] == 7


# ---------------------------------------------------------------------------
# Wired into the server
# ---------------------------------------------------------------------------


async def test_disconnect_leaves_the_node_visible(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    srv = AudioServer({})
    session = NodeSession(ws=_StubWS(), node_id="bedroom", room_id="Master Bedroom")
    srv._nodes["bedroom"] = session

    await srv._deregister(session)

    assert "bedroom" not in srv._nodes  # gone from the live registry…
    absent = srv.absent_nodes()  # …but not from the fleet
    assert [n["node_id"] for n in absent] == ["bedroom"]
    assert absent[0]["room"] == "Master Bedroom"
    assert absent[0]["offline_seconds"] >= 0


async def test_goodbye_makes_a_planned_absence_quiet(tmp_path, monkeypatch):
    """`systemctl restart`, a kenzy-deploy sweep and a manual stop all send
    SIGTERM, and the node announces itself on the way out. Without that the server
    cannot tell a restart from a power cut — and a fleet deploy would light up
    every room on the dashboard at once."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    from kenzy import protocol

    srv = AudioServer({})
    session = NodeSession(ws=_StubWS(), node_id="office", room_id="Office")
    srv._nodes["office"] = session

    await srv._handle_control(session, {"type": protocol.MSG_GOODBYE, "reason": "shutdown"})
    await srv._deregister(session)

    gone = srv.absent_nodes()[0]
    assert gone["node_id"] == "office"
    assert gone["alerting"] is False  # absent, but expected — no fault
    assert gone["grace_until"] > 0


async def test_unannounced_disappearance_still_alerts(tmp_path, monkeypatch):
    """The other half: a node that dies without warning gets no grace, because
    that is precisely the case the alert exists for."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    srv = AudioServer({})
    session = NodeSession(ws=_StubWS(), node_id="bedroom", room_id="Master Bedroom")
    srv._nodes["bedroom"] = session

    await srv._deregister(session)  # no goodbye — the power went out

    entry = srv._roster.known()["bedroom"]
    assert entry.grace_until == 0.0
    # An hour later, with the default five-minute threshold, this is a fault.
    assert srv._roster.is_alerting(entry, srv._offline_alert_s, now=entry.last_seen + 3600)


async def test_forget_node_drops_it(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    srv = AudioServer({})
    session = NodeSession(ws=_StubWS(), node_id="spare", room_id="Spare")
    srv._nodes["spare"] = session
    await srv._deregister(session)

    assert srv.forget_node("spare") is True
    assert srv.absent_nodes() == []


async def test_offline_threshold_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    srv = AudioServer({"fleet": {"offline_alert_minutes": 30}})
    assert srv._offline_alert_s == 1800.0
    # A fresh disconnect is a note, not yet a fault.
    session = NodeSession(ws=_StubWS(), node_id="n1", room_id="Office")
    srv._nodes["n1"] = session
    await srv._deregister(session)
    assert srv.absent_nodes()[0]["alerting"] is False


async def test_dashboard_state_includes_absent_nodes(tmp_path, monkeypatch):
    """The dashboard must render the gap. A room that disappears from the list is
    indistinguishable from a room that was never installed."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    from kenzy.server.dashboard import Dashboard, DashboardConfig

    srv = AudioServer({})
    session = NodeSession(ws=_StubWS(), node_id="bedroom", room_id="Master Bedroom")
    srv._nodes["bedroom"] = session
    await srv._deregister(session)

    dash = Dashboard(srv, {}, DashboardConfig(enabled=True))
    nodes = dash._nodes_state()
    assert len(nodes) == 1
    assert nodes[0]["node_id"] == "bedroom"
    assert nodes[0]["connected"] is False
    assert nodes[0]["last_seen"]


async def test_hub_reports_a_node_that_was_already_missing(tmp_path, monkeypatch):
    """A server restart must not absolve a missing node: without seeding from the
    roster, the hub starts with an empty set and never emits the offline event."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    from kenzy.integrations.hub import IntegrationHub, attach_to_server

    srv = AudioServer({})
    session = NodeSession(ws=_StubWS(), node_id="bedroom", room_id="Master Bedroom")
    srv._nodes["bedroom"] = session
    await srv._deregister(session)

    # A brand-new server process, same data root: the node is still missing.
    restarted = AudioServer({})
    events: list[dict] = []
    hub = IntegrationHub()
    hub.subscribe(events.append)
    attach_to_server(hub, restarted)

    restarted._notify_state()
    offline = [e for e in events if e.get("type") == "node_state" and not e.get("online")]
    assert [e["node_id"] for e in offline] == ["bedroom"]
