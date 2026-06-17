"""Tests for the Phase-5 auth foundation: password hashing, signed session
cookies, the dashboard login flow, service-to-service bearer, and kenzy-passwd."""

from __future__ import annotations

import asyncio
import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient
from websockets.datastructures import Headers
from websockets.http11 import Request

from kenzy import serviceauth
from kenzy.fastapi_auth import install_service_auth
from kenzy.passwd import _current_username, set_auth
from kenzy.server.dashboard import Dashboard, DashboardConfig
from kenzy.server.server import AudioServer

# --- serviceauth: passwords -------------------------------------------------


def test_password_hash_roundtrip():
    h = serviceauth.hash_password("hunter2")
    assert h.startswith("scrypt$") or h.startswith("pbkdf2$")
    assert serviceauth.verify_password("hunter2", h) is True
    assert serviceauth.verify_password("wrong", h) is False
    # salts differ per call
    assert serviceauth.hash_password("hunter2") != h


def test_password_verify_bad_input():
    assert serviceauth.verify_password("x", "not-a-valid-hash") is False
    assert serviceauth.verify_password("x", "bogus$1$2$3") is False


# --- serviceauth: signed cookies --------------------------------------------


def test_cookie_sign_verify():
    tok = serviceauth.sign_cookie("admin", "sekret")
    assert serviceauth.verify_cookie(tok, "sekret") == "admin"
    assert serviceauth.verify_cookie(tok, "other-secret") is None
    assert serviceauth.verify_cookie(tok[:-3] + "xxx", "sekret") is None
    assert serviceauth.verify_cookie("garbage", "sekret") is None


def test_cookie_expiry():
    assert serviceauth.verify_cookie(serviceauth.sign_cookie("a", "s", ttl=-1), "s") is None


# --- serviceauth: service bearer --------------------------------------------


def test_check_bearer():
    assert serviceauth.check_bearer(None, None) is True  # no token => open
    assert serviceauth.check_bearer(None, "t") is False
    assert serviceauth.check_bearer("Bearer t", "t") is True
    assert serviceauth.check_bearer("Bearer wrong", "t") is False
    assert serviceauth.check_bearer("t", "t") is False  # missing "Bearer " prefix


# --- dashboard login flow ---------------------------------------------------


def _req(path: str, headers: dict[str, str] | None = None) -> Request:
    h = Headers()
    for k, v in (headers or {}).items():
        h[k] = v
    return Request(path, h)


def _basic(user: str, pw: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def _dash() -> Dashboard:
    dcfg = DashboardConfig(
        enabled=True,
        auth_username="admin",
        auth_password_hash=serviceauth.hash_password("password"),
        auth_token="apitok",
    )
    return Dashboard(AudioServer({}), {}, dcfg)


def test_login_sets_cookie_and_authenticates():
    dash = _dash()
    run = lambda r: asyncio.run(dash.process_request(None, r))  # noqa: E731

    assert run(_req("/api/login", _basic("admin", "wrong"))).status_code == 401
    ok = run(_req("/api/login", _basic("admin", "password")))
    assert ok.status_code == 200
    cookie = ok.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    val = cookie.split(";")[0].split("=", 1)[1]

    # cookie authenticates /api/me and mutations
    assert b'"authenticated": true' in run(_req("/api/me", {"Cookie": f"kenzy_dash={val}"})).body
    assert dash._authorized_mutation(_req("/x", {"Cookie": f"kenzy_dash={val}"})) is True


def test_mutation_auth_failclosed_and_bearer():
    dash = _dash()
    # anonymous => fail closed
    assert dash._authorized_mutation(_req("/x")) is False
    # api bearer works
    assert dash._authorized_mutation(_req("/x", {"Authorization": "Bearer apitok"})) is True
    assert dash._authorized_mutation(_req("/x", {"Authorization": "Bearer nope"})) is False


def test_logout_clears_cookie():
    r = asyncio.run(_dash().process_request(None, _req("/api/logout")))
    assert "Max-Age=0" in r.headers["Set-Cookie"]


# --- service-to-service middleware ------------------------------------------


def test_service_auth_middleware(monkeypatch):
    monkeypatch.setenv("KENZY_SERVICE_TOKEN", "shh")
    app = FastAPI()

    @app.get("/health")
    def health():  # noqa: ANN202
        return {"status": "ok"}

    @app.get("/work")
    def work():  # noqa: ANN202
        return {"ok": True}

    install_service_auth(app)
    c = TestClient(app)
    assert c.get("/health").status_code == 200  # open
    assert c.get("/work").status_code == 401  # needs token
    assert c.get("/work", headers={"Authorization": "Bearer shh"}).status_code == 200
    assert c.get("/work", headers={"Authorization": "Bearer no"}).status_code == 401


def test_service_auth_noop_without_env(monkeypatch):
    monkeypatch.delenv("KENZY_SERVICE_TOKEN", raising=False)
    app = FastAPI()

    @app.get("/work")
    def work():  # noqa: ANN202
        return {"ok": True}

    install_service_auth(app)
    assert TestClient(app).get("/work").status_code == 200  # auth disabled


def test_server_service_headers(monkeypatch):
    monkeypatch.setenv("KENZY_SERVICE_TOKEN", "envtok")
    assert AudioServer({"discovery": {"token": "disc"}})._service_headers() == {
        "Authorization": "Bearer envtok"
    }
    monkeypatch.delenv("KENZY_SERVICE_TOKEN", raising=False)
    assert AudioServer({"discovery": {"token": "disc"}})._service_headers() == {
        "Authorization": "Bearer disc"
    }
    assert AudioServer({})._service_headers() == {}


# --- kenzy-passwd -----------------------------------------------------------


def test_passwd_set_auth_update_existing():
    text = (
        'dashboard:\n  enabled: false\n  auth:\n    username: "admin"\n'
        '    password_hash: "old"\n  auth_token: null\n'
    )
    out = set_auth(text, "alice", serviceauth.hash_password("pw"))
    import yaml

    auth = yaml.safe_load(out)["dashboard"]["auth"]
    assert auth["username"] == "alice"
    assert serviceauth.verify_password("pw", auth["password_hash"]) is True
    assert "auth_token: null" in out  # other keys/comments preserved
    assert _current_username(text) == "admin"


def test_passwd_set_auth_insert_when_missing():
    text = "dashboard:\n  enabled: true\n  port: 8770\n"
    out = set_auth(text, "bob", serviceauth.hash_password("pw2"))
    import yaml

    d = yaml.safe_load(out)["dashboard"]
    assert d["auth"]["username"] == "bob"
    assert d["port"] == 8770
