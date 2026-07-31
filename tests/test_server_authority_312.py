"""3.12 server-authority: node hello token-proof (e) + central secrets (b)."""

from __future__ import annotations

import asyncio
import json
import subprocess

import httpx
import pytest

from kenzy import serviceauth, serviceboot
from kenzy.server.server import AudioServer

_NO_KEEPALIVE = httpx.Limits(max_keepalive_connections=0)


# ---------------------------------------------------------------------------
# e — node hello token-proof
# ---------------------------------------------------------------------------


def test_node_hello_proof_roundtrip():
    auth = serviceauth.sign_node_hello("jointok", "node-den")
    assert "jointok" not in json.dumps(auth)  # raw token never in the proof
    assert serviceauth.verify_node_hello(auth, "jointok", "node-den")
    assert not serviceauth.verify_node_hello(auth, "wrong", "node-den")
    assert not serviceauth.verify_node_hello(auth, "jointok", "other-node")  # bound to node_id


def test_node_hello_proof_stale_rejected():
    auth = serviceauth.sign_node_hello("t", "n", ts=1000)
    assert serviceauth.verify_node_hello(auth, "t", "n", now=1000)
    assert not serviceauth.verify_node_hello(auth, "t", "n", now=1200)  # outside skew


def test_node_hello_proof_garbage():
    for bad in (None, {}, {"ts": "x"}, {"ts": 1, "sig": "z"}, "notadict"):
        assert not serviceauth.verify_node_hello(bad, "t", "n", now=1)


def test_server_join_requires_proof():
    srv = AudioServer({"discovery": {"token": "jt"}})
    # signature proof, bound to node_id
    proof = serviceauth.sign_node_hello("jt", "n1")
    assert srv._join_check({"node_id": "n1", "auth": proof}) is None
    wrong = serviceauth.sign_node_hello("jt", "OTHER")
    assert srv._join_check({"node_id": "n1", "auth": wrong}) is not None
    # the raw token field is no longer accepted (even the correct value)
    assert srv._join_check({"node_id": "n1", "token": "jt"}) is not None
    assert srv._join_check({"node_id": "n1", "token": "wrong"}) is not None
    # missing proof entirely => rejected
    assert srv._join_check({"node_id": "n1"}) is not None
    # no token configured => open
    assert AudioServer({})._join_check({"node_id": "n"}) is None


def test_join_failures_are_distinguishable():
    """A drifted clock and a wrong token must not look the same in the log —
    they need opposite fixes, and the server already knows which is which."""
    srv = AudioServer({"discovery": {"token": "jt"}})

    stale = serviceauth.sign_node_hello("jt", "n1", ts=1000)
    tag, detail = srv._join_check({"node_id": "n1", "auth": stale})
    assert tag == serviceauth.JOIN_STALE
    assert "clock" in detail  # points the operator at NTP, not at the token

    wrong_token = serviceauth.sign_node_hello("nope", "n1")
    tag, detail = srv._join_check({"node_id": "n1", "auth": wrong_token})
    assert tag == serviceauth.JOIN_BAD_SIG
    assert "token" in detail

    tag, _ = srv._join_check({"node_id": "n1"})
    assert tag == serviceauth.JOIN_MISSING


def test_stale_hello_detail_reports_direction_and_delta():
    auth = serviceauth.sign_node_hello("t", "n", ts=1_000_000)
    # node 1816s behind the server — the master-bedroom failure, exactly
    failure = serviceauth.check_node_hello(auth, "t", "n", now=1_001_816)
    assert failure is not None
    tag, detail = failure
    assert tag == serviceauth.JOIN_STALE
    assert "1816s behind" in detail
    # and the other direction reads correctly too
    failure = serviceauth.check_node_hello(auth, "t", "n", now=998_184)
    assert failure is not None and "1816s ahead of" in failure[1]


# ---------------------------------------------------------------------------
# b — central secrets
# ---------------------------------------------------------------------------


def test_effective_config_includes_secrets_only_when_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-123")
    monkeypatch.delenv("HA_API_KEY", raising=False)
    srv = AudioServer({})
    # dashboard read (default) => no secrets
    assert "_secrets" not in srv._effective_service_config("tts")
    # authenticated pull => the keys this service needs that we hold
    secret_cfg = srv._effective_service_config("tts", include_secrets=True)
    assert secret_cfg["_secrets"] == {"OPENAI_API_KEY": "sk-live-123"}
    # a service we hold no secrets for gets no _secrets block
    monkeypatch.delenv("HF_TOKEN", raising=False)
    assert "_secrets" not in srv._effective_service_config("speaker", include_secrets=True)


def test_apply_secrets_sets_env_and_never_persists(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = {"port": 8769, "_secrets": {"OPENAI_API_KEY": "sk-from-server"}}
    serviceboot._apply_secrets(cfg)
    import os

    assert os.environ["OPENAI_API_KEY"] == "sk-from-server"  # applied to env
    assert "_secrets" not in cfg  # popped — never written to the on-disk record


def test_apply_secrets_server_wins_over_local(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-local-stale")
    serviceboot._apply_secrets({"_secrets": {"OPENAI_API_KEY": "sk-server-fresh"}})
    import os

    assert os.environ["OPENAI_API_KEY"] == "sk-server-fresh"  # central rotation wins


# ---------------------------------------------------------------------------
# b — end to end: secrets ride only the authenticated TLS channel
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def certpair(tmp_path_factory):
    d = tmp_path_factory.mktemp("s312")
    cert, key = d / "s.crt", d / "s.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-keyout", str(key), "-out", str(cert), "-subj", "/CN=s312"],
        check=True, capture_output=True,
    )
    return str(cert), str(key)


async def _serve(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(exist_ok=True)
    server = AudioServer(cfg)
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.3)
    return server, task


async def test_secrets_only_over_authed_tls(certpair, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-e2e")
    cert, key = certpair
    server, task = await _serve(
        {"host": "127.0.0.1", "port": 8871, "tls": {"cert": cert, "key": key},
         "discovery": {"token": "tok"}},
        tmp_path, monkeypatch,
    )
    try:
        async with httpx.AsyncClient(verify=False, limits=_NO_KEEPALIVE) as c:
            # token-proof request over TLS => secrets present
            hdr = serviceauth.sign_service_request("tok", "GET", "/config/tts")
            r = await c.get("https://127.0.0.1:8871/config/tts",
                            headers={serviceauth.SIG_HEADER: hdr})
            assert r.json().get("_secrets") == {"OPENAI_API_KEY": "sk-secret-e2e"}
            # legacy bearer (no signature) => rejected outright, even over TLS
            r2 = await c.get("https://127.0.0.1:8871/config/tts",
                             headers={"Authorization": "Bearer tok"})
            assert r2.status_code == 401
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_no_secrets_over_plaintext(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    server, task = await _serve(
        {"host": "127.0.0.1", "port": 8872, "discovery": {"token": "tok"}},
        tmp_path, monkeypatch,
    )
    try:
        async with httpx.AsyncClient(limits=_NO_KEEPALIVE) as c:
            hdr = serviceauth.sign_service_request("tok", "GET", "/config/tts")
            r = await c.get("http://127.0.0.1:8872/config/tts",
                            headers={serviceauth.SIG_HEADER: hdr})
            assert r.status_code == 200
            assert "_secrets" not in r.json()  # plaintext never carries secrets
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
