"""P1 hardening: origin/host guard + Secure cookie (F-6/F-7) and the resource caps
(connection rate limit, capture-buffer cap — F-10)."""

from __future__ import annotations

from websockets.datastructures import Headers
from websockets.http11 import Request

from kenzy.server.dashboard import Dashboard, DashboardConfig
from kenzy.server.server import (
    _CONN_RATE_MAX,
    _MAX_SESSION_PCM_BYTES,
    AudioServer,
    NodeSession,
    TranscribingServer,
)


def _req(**headers) -> Request:
    h = Headers()
    for k, v in headers.items():
        h[k.replace("_", "-")] = v
    return Request("/ws", h)


class _StubWS:
    pass


# --- F-6: origin/host guard --------------------------------------------------


def test_origin_host_guard():
    d = Dashboard(AudioServer({}), {}, DashboardConfig(enabled=True))
    # CLI/bearer client (no Origin) is allowed
    assert d._origin_host_ok(_req(Host="127.0.0.1:8770")) is True
    # same-origin browser is allowed
    assert d._origin_host_ok(_req(Host="127.0.0.1:8770", Origin="http://127.0.0.1:8770")) is True
    # cross-site Origin is rejected (CSWSH)
    assert d._origin_host_ok(_req(Host="127.0.0.1:8770", Origin="http://evil.example")) is False


def test_allowed_hosts_enforced():
    cfg = DashboardConfig(enabled=True, allowed_hosts=("kenzy.local",))
    d = Dashboard(AudioServer({}), {}, cfg)
    assert d._origin_host_ok(_req(Host="kenzy.local:8770")) is True
    # a Host not in the allow-list is rejected (DNS-rebinding defense)
    assert d._origin_host_ok(_req(Host="192.168.1.5:8770")) is False


def test_allowed_hosts_parsed_from_cfg():
    d = DashboardConfig.from_cfg({"dashboard": {"allowed_hosts": ["a.local", "b.local"]}})
    assert d.allowed_hosts == ("a.local", "b.local")


# --- F-7: Secure cookie under TLS --------------------------------------------


def test_cookie_secure_only_under_tls():
    d = Dashboard(AudioServer({}), {}, DashboardConfig(enabled=True))
    plain = d._cookie_header("tok", _req(Host="x"), max_age=10)
    assert "HttpOnly" in plain and "SameSite=Strict" in plain
    assert "Secure" not in plain  # plaintext default

    tls = d._cookie_header("tok", _req(Host="x", X_Forwarded_Proto="https"), max_age=10)
    assert "; Secure" in tls


# --- F-10: resource caps -----------------------------------------------------


def test_connection_rate_limit():
    s = AudioServer({})
    for _ in range(_CONN_RATE_MAX):
        assert s._allow_connection("1.2.3.4") is True
    assert s._allow_connection("1.2.3.4") is False  # window full → denied
    assert s._allow_connection("5.6.7.8") is True  # other IP independent


async def test_capture_buffer_cap():
    s = TranscribingServer({})
    s._buffers["n"] = bytearray(_MAX_SESSION_PCM_BYTES)  # already at the cap
    sess = NodeSession(ws=_StubWS(), node_id="n", room_id="r")
    await s.on_audio_frame(sess, b"\x00" * 256)
    assert len(s._buffers["n"]) == _MAX_SESSION_PCM_BYTES  # did not grow past the cap

    # under the cap it still appends normally
    s._buffers["n"] = bytearray(10)
    await s.on_audio_frame(sess, b"\x00" * 256)
    assert len(s._buffers["n"]) == 266
