"""Pipeline observability: the server records a per-pipeline session (timings, speaker,
transcript, fast-path flag) and the dashboard buffers + serves it (gated by `logs`)."""

from __future__ import annotations

import asyncio

import httpx

from kenzy.server.dashboard import Dashboard, DashboardConfig
from kenzy.server.server import AudioServer, NodeSession, TranscribingServer


class _StubWS:
    async def send(self, m):  # noqa: ANN001, ANN201
        pass


def _pipeline_server() -> TranscribingServer:
    return TranscribingServer(
        {
            "stt": {"url": "http://x/transcribe"},
            "speaker": {"url": "http://x/identify"},
            "llm": {"url": "http://x/process"},
        }
    )


def _mock_pipeline(srv, monkeypatch, *, fast: bool) -> None:
    async def stt(pcm, room, sid):  # noqa: ANN001, ANN202
        return "turn on the lights"

    async def spk(pcm, room):  # noqa: ANN001, ANN202
        return "alice", 0.9  # (name, confidence) — identity core (F1)

    async def llm(text, room, sid, speaker, node_id=None, identity=None):  # noqa: ANN001, ANN202
        return ("Done.", "vp", [], fast, False, False)

    async def tts(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return True

    monkeypatch.setattr(srv, "_call_stt", stt)
    monkeypatch.setattr(srv, "_call_speaker", spk)
    monkeypatch.setattr(srv, "_call_llm", llm)
    monkeypatch.setattr(srv, "_run_tts", tts)


async def test_transcribe_records_session(monkeypatch):
    srv = _pipeline_server()
    srv._nodes["k"] = NodeSession(ws=_StubWS(), node_id="k", room_id="kitchen")  # type: ignore[arg-type]
    _mock_pipeline(srv, monkeypatch, fast=True)
    records: list[dict] = []
    srv.add_session_listener(records.append)

    await srv._transcribe("k", "kitchen", "sid", b"1234")

    assert len(records) == 1
    r = records[0]
    assert r["transcript"] == "turn on the lights"
    assert r["response"] == "Done."
    assert r["fast"] is True
    assert r["speaker"] == "alice"
    assert r["room"] == "kitchen"
    assert all(k in r for k in ("stt_ms", "speaker_ms", "llm_ms", "tts_ms", "total_ms"))


async def test_no_record_without_listener(monkeypatch):
    srv = _pipeline_server()
    srv._nodes["k"] = NodeSession(ws=_StubWS(), node_id="k", room_id="kitchen")  # type: ignore[arg-type]
    _mock_pipeline(srv, monkeypatch, fast=False)
    # No listener registered → no record is built (no transcript kept).
    await srv._transcribe("k", "kitchen", "sid", b"1234")
    assert srv._session_listeners == []


def test_dashboard_records_only_when_logs_on(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    srv = AudioServer({})
    on = Dashboard(srv, {}, DashboardConfig(enabled=True, logs=True))
    srv._notify_session({"transcript": "hi", "response": "ok", "fast": True})
    assert list(on._sessions)[-1]["transcript"] == "hi"

    srv2 = AudioServer({})
    off = Dashboard(srv2, {}, DashboardConfig(enabled=True, logs=False))
    srv2._notify_session({"transcript": "secret", "response": "x", "fast": False})
    assert len(off._sessions) == 0  # not recorded when observability is off


async def test_sessions_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    srv = AudioServer({})
    dash = Dashboard(
        srv,
        {},
        DashboardConfig(enabled=True, logs=True, bind="127.0.0.1", port=8781, auth_token="t0ken"),
    )
    srv._notify_session({"transcript": "first", "response": "a", "fast": True})
    srv._notify_session({"transcript": "second", "response": "b", "fast": False})
    task = asyncio.create_task(dash.serve())
    await asyncio.sleep(0.25)
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8781") as c:
            r = await c.get("/api/sessions", headers={"Authorization": "Bearer t0ken"})
            sessions = r.json()["sessions"]
            assert [s["transcript"] for s in sessions] == ["second", "first"]  # most recent first
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_secret_exchange_withheld_from_activity(monkeypatch):
    # Review finding H2: a lockbox exchange (ProcessResponse.secret) must not
    # land its transcript or response in the Activity record — timing stays.
    srv = _pipeline_server()
    srv._nodes["k"] = NodeSession(ws=_StubWS(), node_id="k", room_id="kitchen")  # type: ignore[arg-type]

    async def stt(pcm, room, sid):  # noqa: ANN001, ANN202
        return "remember this secretly: the code is 8181"

    async def spk(pcm, room):  # noqa: ANN001, ANN202
        return "john", 0.9

    async def llm(text, room, sid, speaker, node_id=None, identity=None):  # noqa: ANN001, ANN202
        return ("Locked away — only you can ask me for it.", "vp", [], True, False, True)

    async def tts(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return True

    monkeypatch.setattr(srv, "_call_stt", stt)
    monkeypatch.setattr(srv, "_call_speaker", spk)
    monkeypatch.setattr(srv, "_call_llm", llm)
    monkeypatch.setattr(srv, "_run_tts", tts)
    records: list[dict] = []
    srv.add_session_listener(records.append)

    await srv._transcribe("k", "kitchen", "sid", b"1234")

    assert len(records) == 1
    r = records[0]
    assert "8181" not in str(r) and "Locked away" not in str(r)
    assert r["transcript"] == "[lockbox exchange]"
    assert r["response"] == "[content withheld]"
    assert r["total_ms"] >= 0  # the timing row survives
