"""Server self-upgrade: the pip command builder (extras/version/constraints) and
run_self_upgrade's success/failure/validation handling (pip subprocess mocked)."""

from __future__ import annotations

import asyncio

from kenzy.server.server import AudioServer
from kenzy.upgrade import pip_upgrade_command


def test_pip_upgrade_cmd_basic(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))  # no constraints file present
    cmd = pip_upgrade_command("server", None, plugins=[])
    assert cmd[1:5] == ["-m", "pip", "install", "-U"]
    assert cmd[-1] == "kenzy[server]>=3.0.0"  # floored, never the 2.x monolith
    assert "-c" not in cmd


def test_pip_upgrade_cmd_version_and_constraints(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "constraints.txt").write_text("transformers==4.30.0\n")
    cmd = pip_upgrade_command("llm", "3.2.0", plugins=[])
    assert "-c" in cmd  # operator pins honored on upgrade
    assert cmd[-1] == "kenzy[llm]==3.2.0"


class _FakeProc:
    def __init__(self, rc: int, out: bytes) -> None:
        self.returncode = rc
        self._out = out

    async def communicate(self):
        return (self._out, None)


async def test_run_self_upgrade_success(monkeypatch):
    srv = AudioServer({})
    captured: dict = {}

    async def fake_exec(*cmd, **kw):
        captured["cmd"] = list(cmd)
        return _FakeProc(0, b"Successfully installed kenzy-3.1.2")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    ok, out = await srv.run_self_upgrade("server", "3.1.2")
    assert ok is True
    assert "kenzy[server]==3.1.2" in " ".join(captured["cmd"])
    assert "Successfully installed" in out


async def test_run_self_upgrade_failure(monkeypatch):
    srv = AudioServer({})

    async def fake_exec(*cmd, **kw):
        return _FakeProc(1, b"ERROR: No matching distribution found for kenzy==9.9.9")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    ok, out = await srv.run_self_upgrade("server", "9.9.9")
    assert ok is False
    assert "No matching distribution" in out


async def test_run_self_upgrade_rejects_bad_version():
    srv = AudioServer({})
    ok, msg = await srv.run_self_upgrade("server", "3.1.2; rm -rf /")
    assert ok is False and "invalid version" in msg


def test_protocol_upgrade_message():
    import json

    from kenzy import protocol

    assert json.loads(protocol.upgrade("3.1.2")) == {"type": "upgrade", "version": "3.1.2"}
    assert json.loads(protocol.upgrade()) == {"type": "upgrade"}  # version omitted = latest


async def test_upgrade_node_sends_message():
    import json

    from kenzy.server.server import NodeSession

    srv = AudioServer({})
    sent: list[str] = []

    class _WS:
        async def send(self, m):
            sent.append(m)

    srv._nodes["n1"] = NodeSession(ws=_WS(), node_id="n1", room_id="r")
    assert await srv.upgrade_node("n1", "3.1.2") is True
    assert json.loads(sent[0]) == {"type": "upgrade", "version": "3.1.2"}
    # not connected → False, no send
    assert await srv.upgrade_node("ghost") is False


def test_service_upgrade_endpoint(monkeypatch):
    """The service POST /upgrade runs the upgrade and reports the result; it never
    actually re-execs in the test (os.execv stubbed)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import kenzy.fastapi_auth as fa
    from kenzy import upgrade as up

    seen: dict = {}

    async def fake_run(extra, version=None):
        seen["extra"] = extra
        seen["version"] = version
        return True, "Successfully installed kenzy-3.1.2"

    # Patch before installing the route — the endpoint binds run_pip_upgrade at install.
    monkeypatch.setattr(up, "run_pip_upgrade", fake_run)
    monkeypatch.setattr(fa.os, "execv", lambda *a: None)  # must not re-exec the test proc

    app = FastAPI()
    fa.install_upgrade_endpoint(app, "stt")
    r = TestClient(app).post("/upgrade", json={"version": "3.1.2"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and "Successfully installed" in body["output"]
    assert seen == {"extra": "stt", "version": "3.1.2"}
