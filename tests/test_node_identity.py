"""Tests for node identity: stable node_id generation/persistence, the
writable-config redirect, and the server-pushed room-name (set_room) handler."""

from __future__ import annotations

import asyncio

import yaml

from kenzy import protocol
from kenzy.config import packaged_config, writable_config_path
from kenzy.node.client import NodeClient, _ensure_node_id, _set_yaml_scalar


def test_set_yaml_scalar_update_and_append():
    text = 'room_id: "kitchen"\nverbose: false\n'
    updated = _set_yaml_scalar(text, "room_id", "office")
    assert 'room_id: "office"' in updated
    assert "verbose: false" in updated  # other keys preserved
    # Appending a missing key keeps the document valid YAML.
    appended = _set_yaml_scalar(updated, "node_id", "abc-123")
    data = yaml.safe_load(appended)
    assert data["room_id"] == "office" and data["node_id"] == "abc-123"


def test_ensure_node_id_generates_persists_and_reuses(tmp_path):
    cfg_path = tmp_path / "node.yaml"
    cfg_path.write_text("room_id: null\n")
    cfg: dict = {"room_id": None}
    nid = _ensure_node_id(cfg, cfg_path)
    assert nid and cfg["node_id"] == nid
    assert yaml.safe_load(cfg_path.read_text())["node_id"] == nid
    # Re-running with the persisted id returns it unchanged (no new id).
    cfg2 = yaml.safe_load(cfg_path.read_text())
    assert _ensure_node_id(cfg2, cfg_path) == nid


def test_writable_config_path_redirects_packaged(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    # A packaged default is read-only → writes redirect into the config home.
    redirected = writable_config_path("node", packaged_config("node"))
    assert redirected == tmp_path / "configs" / "node.yaml"
    # A real user/dev path is returned as-is.
    user_path = tmp_path / "configs" / "node.yaml"
    assert writable_config_path("node", user_path) == user_path


async def test_node_applies_and_persists_set_room(tmp_path):
    cfg_path = tmp_path / "node.yaml"
    cfg_path.write_text('room_id: "kitchen"\nnode_id: "n-1"\n')
    client = NodeClient({"room_id": "kitchen", "node_id": "n-1"}, config_path=cfg_path)
    assert client._room_id == "kitchen"
    await client._cmd_q.put({"type": protocol.MSG_SET_ROOM, "room_id": "office"})
    task = asyncio.create_task(client._cmd_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert client._room_id == "office"
    assert yaml.safe_load(cfg_path.read_text())["room_id"] == "office"


def test_hello_carries_node_id():
    msg = protocol.parse(protocol.hello("kitchen", node_id="n-7"))
    assert msg["room_id"] == "kitchen" and msg["node_id"] == "n-7"
    # Legacy form (no node_id) omits the field.
    assert "node_id" not in protocol.parse(protocol.hello("kitchen"))
