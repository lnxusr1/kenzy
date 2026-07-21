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
from kenzy.passwd import current_username, override_path, set_auth
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
    # legacy bearer is no longer accepted — token-proof only
    assert c.get("/work", headers={"Authorization": "Bearer shh"}).status_code == 401
    # a valid token-proof signature is accepted
    sig = serviceauth.sign_service_request("shh", "GET", "/work")
    assert c.get("/work", headers={serviceauth.SIG_HEADER: sig}).status_code == 200
    # a signature for the wrong token is rejected
    bad = serviceauth.sign_service_request("nope", "GET", "/work")
    assert c.get("/work", headers={serviceauth.SIG_HEADER: bad}).status_code == 401


def test_service_auth_noop_without_env(monkeypatch):
    monkeypatch.delenv("KENZY_SERVICE_TOKEN", raising=False)
    app = FastAPI()

    @app.get("/work")
    def work():  # noqa: ANN202
        return {"ok": True}

    install_service_auth(app)
    assert TestClient(app).get("/work").status_code == 200  # auth disabled


def test_server_service_headers(monkeypatch):
    from kenzy import serviceauth

    monkeypatch.setenv("KENZY_SERVICE_TOKEN", "envtok")
    hdrs = AudioServer({"discovery": {"token": "disc"}})._service_headers("GET", "http://h:8767/config/stt")
    # 3.12: token-proof signature only — NO bearer, token never on the wire.
    assert "Authorization" not in hdrs
    assert serviceauth.verify_service_request(
        hdrs[serviceauth.SIG_HEADER], "envtok", "GET", "/config/stt"
    ) is not None
    assert "envtok" not in hdrs[serviceauth.SIG_HEADER]  # token not in the signature

    monkeypatch.delenv("KENZY_SERVICE_TOKEN", raising=False)
    hdrs = AudioServer({"discovery": {"token": "disc"}})._service_headers("POST", "http://h/speak")
    assert "Authorization" not in hdrs
    assert serviceauth.verify_service_request(
        hdrs[serviceauth.SIG_HEADER], "disc", "POST", "/speak"
    ) is not None

    assert AudioServer({})._service_headers("GET", "http://h/x") == {}


# --- kenzy-passwd -----------------------------------------------------------


def test_passwd_writes_override_layer_not_server_yaml(tmp_path):
    """The login must land in server.local.yaml: server.yaml is overwritten by
    kenzy-deploy's upgrade sync (a password there silently reverts to the
    operator tree's copy — usually the default) and may be the read-only
    packaged file. The override layer is protected from both."""
    import yaml

    cfg = tmp_path / "server.yaml"
    cfg.write_text('dashboard:\n  enabled: true\n  auth:\n    username: "admin"\n')
    out = set_auth(cfg, "alice", serviceauth.hash_password("pw"))
    assert out == tmp_path / "server.local.yaml"
    assert 'username: "admin"' in cfg.read_text()  # server.yaml untouched
    auth = yaml.safe_load(out.read_text())["dashboard"]["auth"]
    assert auth["username"] == "alice"
    assert serviceauth.verify_password("pw", auth["password_hash"]) is True
    # The override layer wins for the effective username…
    assert current_username(cfg) == "alice"
    # …and the server boots with the new hash (load_server_config merges it).
    from kenzy.server.server import load_server_config

    merged = load_server_config(cfg)
    assert serviceauth.verify_password("pw", merged["dashboard"]["auth"]["password_hash"])


def test_passwd_preserves_other_override_keys(tmp_path):
    import yaml

    cfg = tmp_path / "server.yaml"
    cfg.write_text("dashboard:\n  enabled: true\n")
    (tmp_path / "server.local.yaml").write_text(
        "integrations:\n  mqtt:\n    host: 10.0.0.9\nexperimental: true\n"
    )
    out = set_auth(cfg, "bob", serviceauth.hash_password("pw2"))
    data = yaml.safe_load(out.read_text())
    assert data["integrations"]["mqtt"]["host"] == "10.0.0.9"  # dashboard edits kept
    assert data["experimental"] is True
    assert data["dashboard"]["auth"]["username"] == "bob"


def test_passwd_redirects_out_of_packaged_dir(tmp_path, monkeypatch):
    # A server running off the packaged default must not get its password
    # written into site-packages — redirect to the config home.
    import kenzy.config as kconfig

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "server.yaml").write_text("dashboard:\n  enabled: true\n")
    home = tmp_path / "home"
    monkeypatch.setattr(kconfig, "_PACKAGED_CONFIGS", pkg)
    monkeypatch.setenv("KENZY_HOME", str(home))
    assert override_path(pkg / "server.yaml") == home / "configs" / "server.local.yaml"
    out = set_auth(pkg / "server.yaml", "eve", serviceauth.hash_password("pw3"))
    assert out == home / "configs" / "server.local.yaml"
    assert not (pkg / "server.local.yaml").exists()
