"""The plugin seam, end to end short of a live socket: a node-half task sends a
real ``plugin_event`` frame, the server routes it through the skew gate to the
server half, the panel serves from package data, and the addons namespace
merges per-addon. Driven through the protocol and the real dispatch paths —
not through the helpers — so a broken seam fails here, not in the house.

The sample plugin (tests/sample_plugin/) is the reference consumer; these
tests are what make its contract THE contract.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from kenzy import protocol
from kenzy.config import kenzy_data_root
from kenzy.node.client import NodeClient
from kenzy.plugins import PluginScan, scan_plugins
from kenzy.server.dashboard import Dashboard, DashboardConfig
from kenzy.server.server import AudioServer, NodeSession

sys.path.insert(0, str(Path(__file__).parent / "sample_plugin"))
import kenzy_sample_plugin as sample  # noqa: E402
import pytest  # noqa: E402


class _EP:
    def __init__(self, group: str, module: Any, dist: str = "kenzy-sample-plugin"):
        self.group = group
        self.name = dist
        self.dist = type("D", (), {"name": dist, "version": "1.0.0"})()
        self._module = module

    def load(self) -> Any:
        return self._module


def _scan() -> PluginScan:
    return scan_plugins([_EP("kenzy.plugins.v1", sample)])


@pytest.fixture(autouse=True)
def _fresh_sample() -> Any:
    sample.STARTED.clear()
    sample.RECEIVED.clear()
    sample.SERVER_EVENTS.clear()
    yield


class _StubWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, frame: str) -> None:
        self.sent.append(frame)


def _session(api: int = 1) -> NodeSession:
    return NodeSession(
        ws=_StubWS(),
        node_id="n1",
        room_id="office",
        capabilities={"plugins": [{"id": "sample", "version": "1.0.0", "api": api}]},
    )


# ---------------------------------------------------------------------------
# Server half: frame routing, skew gate, containment, startup config
# ---------------------------------------------------------------------------


async def test_a_plugin_event_frame_reaches_the_server_half() -> None:
    s = AudioServer({})
    s._plugins = _scan()
    frame = json.loads(protocol.plugin_event("sample", {"kind": "reading", "mm": 1200}))
    await s._handle_control(_session(), frame)
    assert sample.RECEIVED == [("n1", {"kind": "reading", "mm": 1200})]


async def test_api_skew_drops_the_event_with_a_stated_reason(caplog: Any) -> None:
    """A node running an older plugin half against a newer server half is an
    incompatible install: the event is dropped, not half-understood, and the
    log names both versions so the operator knows which side to upgrade."""
    s = AudioServer({})
    s._plugins = _scan()
    frame = json.loads(protocol.plugin_event("sample", {"kind": "reading"}))
    with caplog.at_level("WARNING"):
        await s._handle_control(_session(api=2), frame)
    assert sample.RECEIVED == []
    assert any("API skew" in r.message and "upgrade" in r.message for r in caplog.records)


async def test_an_event_for_an_absent_server_half_is_ignored() -> None:
    s = AudioServer({})
    # Pinned empty rather than trusting the venv: a stray egg-info under
    # tests/sample_plugin/ (build residue of `pip install ./tests/sample_plugin`)
    # sits on this file's sys.path insert and makes the REAL scan find the
    # sample — which happened, and made this test's premise silently false.
    s._plugins = PluginScan()
    frame = json.loads(protocol.plugin_event("sample", {"kind": "reading"}))
    await s._handle_control(_session(), frame)  # must not raise
    assert sample.RECEIVED == []


async def test_a_crashing_frame_handler_is_that_plugins_failure_alone(
    monkeypatch: Any, caplog: Any
) -> None:
    s = AudioServer({})
    s._plugins = _scan()

    async def _boom(ctx: Any, node_id: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("plugin bug")

    monkeypatch.setattr(sample, "on_plugin_frame", _boom)
    frame = json.loads(protocol.plugin_event("sample", {"kind": "reading"}))
    with caplog.at_level("ERROR"):
        await s._handle_control(_session(), frame)  # the server survives
    assert any("'sample'" in r.message and "frame handler" in r.message for r in caplog.records)


async def test_server_start_runs_with_defaults_merged_under_the_addon_file() -> None:
    """configs/addons/<id>.yaml deep-merges OVER the manifest defaults — an
    operator override wins, an untouched default survives."""
    addons_dir = kenzy_data_root() / "configs" / "addons"
    addons_dir.mkdir(parents=True)
    (addons_dir / "sample.yaml").write_text("greeting: howdy\n")
    s = AudioServer({})
    s._plugins = _scan()
    s._start_plugins()
    await asyncio.sleep(0.05)
    assert sample.STARTED == [{"greeting": "howdy", "interval_s": 3600}]


# ---------------------------------------------------------------------------
# Node half: task launch, the frame it sends, restart-on-config-change
# ---------------------------------------------------------------------------


async def test_the_node_half_runs_and_its_event_rides_the_wire() -> None:
    client = NodeClient({"node_id": "n1"})
    client._plugin_scan = _scan()
    client._addons_cfg = {"sample": {"greeting": "hi"}}
    ws = _StubWS()
    client._ws = ws  # type: ignore[assignment]
    client._registered = True
    client._sync_plugins()
    await asyncio.sleep(0.05)
    frames = [json.loads(f) for f in ws.sent]
    assert frames == [
        {
            "type": "plugin_event",
            "plugin": "sample",
            "payload": {"kind": "hello", "config": {"greeting": "hi"}},
        }
    ]


async def test_sync_plugins_restarts_only_on_a_config_change() -> None:
    client = NodeClient({"node_id": "n1"})
    client._plugin_scan = _scan()
    client._addons_cfg = {"sample": {"greeting": "hi"}}
    client._sync_plugins()
    task = client._plugin_tasks["sample"]
    client._sync_plugins()  # same config → same task (mediakeys contract)
    assert client._plugin_tasks["sample"] is task
    client._addons_cfg = {"sample": {"greeting": "yo"}}
    client._sync_plugins()
    assert client._plugin_tasks["sample"] is not task
    client._plugin_tasks["sample"].cancel()


async def test_disabled_addon_never_starts_and_the_toggle_live_applies() -> None:
    """addons.<id>.enabled: false — the off switch (2026-08-26): no task, no
    device open, no retry loop, without uninstalling the distribution. Flipping
    it back on applies live (this runs after every config apply)."""
    client = NodeClient({"node_id": "n1"})
    client._plugin_scan = _scan()
    client._addons_cfg = {"sample": {"enabled": False}}
    client._sync_plugins()
    assert "sample" not in client._plugin_tasks  # off: nothing starts
    client._addons_cfg = {"sample": {"enabled": True}}
    client._sync_plugins()
    assert "sample" in client._plugin_tasks  # on again — live, no restart
    client._addons_cfg = {"sample": {"enabled": False}}
    client._sync_plugins()
    assert "sample" not in client._plugin_tasks  # and off cancels the running task


async def test_a_crashing_node_half_never_takes_the_node_down(caplog: Any) -> None:
    import types

    from kenzy.plugins import PluginManifest

    mod = types.ModuleType("crashy")
    mod.MANIFEST = PluginManifest(id="crashy", label="Crashy", api=1, roles=("node",))  # type: ignore[attr-defined]

    async def node_run(ctx: Any) -> None:
        raise RuntimeError("driver bug")

    mod.node_run = node_run  # type: ignore[attr-defined]
    client = NodeClient({"node_id": "n1"})
    client._plugin_scan = scan_plugins([_EP("kenzy.plugins.v1", mod, dist="kenzy-crashy")])
    with caplog.at_level("ERROR"):
        client._sync_plugins()
        await client._plugin_tasks["crashy"]  # completes; the error stayed inside
    assert any("'crashy'" in r.message and "node continues" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Server half → node half (send_to_node) and the node's inbound dispatch
# ---------------------------------------------------------------------------


async def test_send_to_node_delivers_a_plugin_event_frame() -> None:
    s = AudioServer({})
    s._plugins = _scan()
    sess = _session()
    s._nodes["n1"] = sess
    ctx = s._plugin_context(s._plugins.get("sample"))
    assert await ctx.send_to_node("n1", {"kind": "ping"}) is True
    (raw,) = sess.ws.sent  # type: ignore[attr-defined]
    assert json.loads(raw) == {
        "type": "plugin_event",
        "plugin": "sample",
        "payload": {"kind": "ping"},
    }


async def test_send_to_node_refuses_absent_or_skewed_nodes() -> None:
    s = AudioServer({})
    s._plugins = _scan()
    ctx = s._plugin_context(s._plugins.get("sample"))
    assert await ctx.send_to_node("ghost", {"kind": "ping"}) is False  # not connected
    s._nodes["n1"] = _session(api=2)  # node half a different plugin API
    assert await ctx.send_to_node("n1", {"kind": "ping"}) is False
    assert not s._nodes["n1"].ws.sent  # type: ignore[attr-defined]


async def test_the_node_routes_a_server_event_to_the_hook() -> None:
    client = NodeClient({"node_id": "n1"})
    client._plugin_scan = _scan()
    frame = json.loads(protocol.plugin_event("sample", {"kind": "ping", "n": 1}))
    client._dispatch_plugin_event(frame)
    await asyncio.sleep(0.02)
    assert sample.SERVER_EVENTS == [{"kind": "ping", "n": 1}]
    # Unknown plugin id: ignored, never a crash.
    client._dispatch_plugin_event(json.loads(protocol.plugin_event("nope", {})))
    await asyncio.sleep(0.02)
    assert len(sample.SERVER_EVENTS) == 1


# ---------------------------------------------------------------------------
# The panel save path: per-addon node config write
# ---------------------------------------------------------------------------


async def test_addon_node_config_writes_merge_per_addon_and_push_live() -> None:
    s = AudioServer({})
    s._plugins = _scan()
    sess = _session()
    s._nodes["n1"] = sess
    nodes_dir = kenzy_data_root() / "configs" / "nodes"
    nodes_dir.mkdir(parents=True)
    (nodes_dir / "n1.yaml").write_text(
        "volume: 60\naddons:\n  other:\n    x: 1\n  sample:\n    old: true\n"
    )
    await s.write_addon_node_config("n1", "sample", {"zones": [[0, 0, 1, 1]]})
    effective = s._effective_node_config("n1")
    # This addon's slice replaced; the OTHER addon and grid keys survived.
    assert effective["addons"] == {"other": {"x": 1}, "sample": {"zones": [[0, 0, 1, 1]]}}
    assert effective["volume"] == 60
    # And the node heard about it immediately (live push).
    pushed = [json.loads(f) for f in sess.ws.sent]  # type: ignore[attr-defined]
    assert pushed and pushed[-1]["type"] == "config"
    assert pushed[-1]["config"]["addons"]["sample"] == {"zones": [[0, 0, 1, 1]]}


async def test_addon_node_config_refuses_secretish_keys_and_unknown_addons() -> None:
    s = AudioServer({})
    s._plugins = _scan()
    with pytest.raises(ValueError, match="secret-like"):
        await s.write_addon_node_config("n1", "sample", {"api_key": "x"})
    with pytest.raises(ValueError, match="no such addon"):
        await s.write_addon_node_config("n1", "nope", {"a": 1})


# ---------------------------------------------------------------------------
# Config authority: the addons namespace merges per-addon, secrets stripped
# ---------------------------------------------------------------------------


def test_the_addons_namespace_merges_per_addon_not_shallowly() -> None:
    """A per-node override touching ONE addon key must not drop the defaults'
    other addons or that addon's sibling keys (the watchdog-dict trap), and
    the secret-name invariant holds inside addon dicts too."""
    s = AudioServer(
        {"node_defaults": {"addons": {"sample": {"a": 1, "b": 2}, "other": {"x": 1}}}}
    )
    nodes_dir = kenzy_data_root() / "configs" / "nodes"
    nodes_dir.mkdir(parents=True)
    (nodes_dir / "n1.yaml").write_text("addons:\n  sample:\n    b: 3\n    api_token: leak\n")
    effective = s._effective_node_config("n1")
    assert effective["addons"] == {"sample": {"a": 1, "b": 3}, "other": {"x": 1}}
    # The merge built fresh dicts: neither source was mutated.
    assert s._node_defaults["addons"]["sample"] == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# Dashboard: the panel serves from package data, the flags carry the band
# ---------------------------------------------------------------------------


def test_the_panel_serves_from_package_data_and_nowhere_else() -> None:
    s = AudioServer({})
    s._plugins = _scan()
    d = Dashboard(s, {}, DashboardConfig())
    ok = d._addon_static("/addons/sample/panel.js")
    assert ok.status_code == 200
    assert "text/javascript" in ok.headers["Content-Type"]
    assert b"SamplePanel" in ok.body
    for path in (
        "/addons/sample/../__init__.py",  # traversal out of the panel dir
        "/addons/sample/missing.js",
        "/addons/nope/panel.js",  # not an installed plugin
        "/addons/sample/",
        "/addons/sample",
    ):
        assert d._addon_static(path).status_code == 404, path


async def test_the_panel_data_path_answers_through_panel_state() -> None:
    """GET /api/addons/<id>/state → the plugin's panel_state hook, with the
    real context (config + room_of). The seam a panel reads live data through."""
    s = AudioServer({})
    s._plugins = _scan()
    s._nodes["n1"] = _session()  # room_of("n1") should answer "office"
    d = Dashboard(s, {}, DashboardConfig())
    resp = await d._addon_state("sample", "/api/addons/sample/state?node=n1&x=1&x=2")
    assert resp.status_code == 200
    body = json.loads(resp.body)
    # The query string reaches the hook flattened (last value wins) — how a
    # panel scopes its ask (e.g. stream only the open tab's node).
    assert body == {
        "greeting": "hello",
        "room_of_n1": "office",
        "query": {"node": "n1", "x": "2"},
    }
    # Unknown addon (or no hook): a 404 that says so, not a crash.
    assert (await d._addon_state("nope")).status_code == 404


async def test_a_crashing_panel_state_is_a_500_not_a_dead_dashboard(monkeypatch: Any) -> None:
    s = AudioServer({})
    s._plugins = _scan()
    d = Dashboard(s, {}, DashboardConfig())

    async def _boom(ctx: Any, query: dict[str, str]) -> dict[str, Any]:
        raise RuntimeError("panel bug")

    monkeypatch.setattr(sample, "panel_state", _boom)
    resp = await d._addon_state("sample")
    assert resp.status_code == 500 and b"panel bug" in resp.body


def test_room_of_answers_for_connected_nodes_only() -> None:
    s = AudioServer({})
    s._plugins = _scan()
    s._nodes["n1"] = _session()
    ctx = s._plugin_context(s._plugins.get("sample"))
    assert ctx.room_of("n1") == "office"
    assert ctx.room_of("ghost") == ""  # disconnected/unknown: no placement


def test_the_settings_card_lists_every_install_loaded_or_refused() -> None:
    """The management card's contract: ALL loaded plugins (any role, panel or
    not) plus every fault with its reason — Settings is where "installed ·
    not loaded" becomes visible with the fix."""
    s = AudioServer({})
    s._plugins = scan_plugins(
        [
            _EP("kenzy.plugins.v1", sample),
            _EP("kenzy.plugins.v99", sample, dist="kenzy-future"),
        ]
    )
    d = Dashboard(s, {}, DashboardConfig())
    state = d._addons_settings_state()
    assert state["loaded"] == [
        {
            "id": "sample",
            "label": "Sample",
            "dist": "kenzy-sample-plugin",
            "version": "1.0.0",
            "api": 1,
            "roles": ["node", "server"],
            "panel": True,
        }
    ]
    (fault,) = state["faults"]
    assert fault["dist"] == "kenzy-future" and fault["kind"] == "incompatible"
    assert "v99" in fault["error"] and fault["api"] == 99


def test_the_flags_carry_the_addons_band_and_the_faults() -> None:
    s = AudioServer({})
    s._plugins = _scan()
    d = Dashboard(s, {}, DashboardConfig())
    assert d._addons_state() == [
        {
            "id": "sample",
            "label": "Sample",
            "ico": "✚",
            "panel": "/addons/sample/panel.js",
            "version": "1.0.0",
        }
    ]
    # An installed-but-refused plugin is VISIBLE with its reason — never a
    # silent nothing.
    s._plugins = scan_plugins([_EP("kenzy.plugins.v99", sample, dist="kenzy-future")])
    assert d._addons_state() == []
    (fault,) = d._addon_faults_state()
    assert fault["dist"] == "kenzy-future" and fault["kind"] == "incompatible"
    assert "v99" in fault["error"]
