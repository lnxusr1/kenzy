"""Tests for the speaker service's profile-management endpoints (list with sample
counts, rename, delete) and the dashboard's proxy/management helpers. These touch
only the filesystem (no SpeechBrain model needed)."""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from kenzy.speaker import speaker as svc


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "_embeddings_dir", tmp_path)
    # Two profiles with different sample counts.
    np.save(tmp_path / "alice.npy", np.zeros((3, 8), dtype=np.float32))
    np.save(tmp_path / "bob.npy", np.zeros((1, 8), dtype=np.float32))
    return TestClient(svc.app)


def test_list_speakers_with_sample_counts(client):
    r = client.get("/speakers")
    assert r.status_code == 200
    by_name = {s["name"]: s["samples"] for s in r.json()["speakers"]}
    assert by_name == {"alice": 3, "bob": 1}


def test_delete_speaker(client, tmp_path):
    assert client.delete("/speakers/bob").status_code == 200
    assert not (tmp_path / "bob.npy").exists()
    assert client.delete("/speakers/bob").status_code == 404


def test_rename_speaker(client, tmp_path):
    r = client.post("/speakers/alice/rename", json={"new_name": "alison"})
    assert r.status_code == 200
    assert r.json()["sample_count"] == 3
    assert (tmp_path / "alison.npy").exists()
    assert not (tmp_path / "alice.npy").exists()


def test_rename_conflicts_and_validation(client):
    # target already exists
    assert client.post("/speakers/alice/rename", json={"new_name": "bob"}).status_code == 409
    # source missing
    assert client.post("/speakers/nope/rename", json={"new_name": "x"}).status_code == 404
    # empty / unsafe names
    assert client.post("/speakers/alice/rename", json={"new_name": "  "}).status_code == 400
    assert client.post("/speakers/alice/rename", json={"new_name": "../etc"}).status_code == 400


def test_unsafe_enroll_names_rejected(client):
    """F-5: enroll takes the name in the JSON body (no URL normalization), so every
    traversal/separator value reaches the validator and is refused before the model."""
    for bad in ("../evil", "a/b", "..", "a\\b", "", "   "):
        assert client.post("/enroll", json={"audio_b64": "", "name": bad}).status_code == 400


def test_unsafe_path_names_rejected(client):
    """F-5: a backslash survives as a single path segment and is refused on delete/rename."""
    assert client.delete("/speakers/a%5Cb").status_code == 400
    assert client.post("/speakers/a%5Cb/rename", json={"new_name": "x"}).status_code == 400


def test_safe_speaker_name_unit():
    from fastapi import HTTPException

    from kenzy.speaker.speaker import _safe_speaker_name

    assert _safe_speaker_name("  Alice  ") == "Alice"
    for bad in ("", "  ", "..", ".", "a/b", "a\\b", "x\x00y"):
        with pytest.raises(HTTPException):
            _safe_speaker_name(bad)


# --- dashboard management helpers (proxy mocked) ---


async def test_dashboard_speakers_state(monkeypatch):
    from kenzy.server.dashboard import Dashboard, DashboardConfig
    from kenzy.server.server import AudioServer, NodeSession

    server = AudioServer({})
    server._nodes["n1"] = NodeSession(ws=object(), node_id="n1", room_id="Kitchen")
    # identify_threshold is owned by the speaker *service* config, not server.yaml.
    monkeypatch.setattr(
        server, "_effective_service_config", lambda svc: {"identify_threshold": 0.3}
    )
    dash = Dashboard(server, {}, DashboardConfig(controls=True))

    async def fake_req(method, sub_path, payload=None):
        assert method == "GET" and sub_path == "/speakers"
        return 200, {"speakers": [{"name": "alice", "samples": 3}]}

    monkeypatch.setattr(dash, "_speaker_request", fake_req)
    state = await dash._speakers_state()
    assert state["reachable"] is True
    assert state["identify_threshold"] == 0.3
    assert state["speakers"][0]["name"] == "alice"
    assert state["rooms"] == [{"node_id": "n1", "room": "Kitchen"}]


async def test_dashboard_delete_and_rename_errors(monkeypatch):
    from kenzy.server.dashboard import Dashboard, DashboardConfig
    from kenzy.server.server import AudioServer

    dash = Dashboard(AudioServer({}), {}, DashboardConfig(controls=True))

    async def unreachable(method, sub_path, payload=None):
        return None

    monkeypatch.setattr(dash, "_speaker_request", unreachable)
    ok, err = await dash._delete_speaker("alice")
    assert not ok and "not reachable" in err

    async def conflict(method, sub_path, payload=None):
        return 409, {"detail": "exists"}

    monkeypatch.setattr(dash, "_speaker_request", conflict)
    ok, err = await dash._rename_speaker("alice", "bob")
    assert not ok and err == "exists"
