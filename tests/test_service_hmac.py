"""Token-proof service auth (server-authority stage a): the HMAC scheme that
replaces the transmitted bearer token."""

from __future__ import annotations

from kenzy import serviceauth, tlsutil

TOKEN = "s3cret-fleet-token"


def test_request_roundtrip():
    hdr = serviceauth.sign_service_request(TOKEN, "GET", "/config/stt")
    assert hdr.startswith("KENZY-HMAC ")
    ts = serviceauth.verify_service_request(hdr, TOKEN, "GET", "/config/stt")
    assert isinstance(ts, int)


def test_token_never_appears_in_header():
    hdr = serviceauth.sign_service_request(TOKEN, "GET", "/config/stt")
    assert TOKEN not in hdr  # the whole point


def test_wrong_token_rejected():
    hdr = serviceauth.sign_service_request(TOKEN, "GET", "/config/stt")
    assert serviceauth.verify_service_request(hdr, "other", "GET", "/config/stt") is None


def test_path_and_method_are_bound():
    hdr = serviceauth.sign_service_request(TOKEN, "GET", "/config/stt")
    assert serviceauth.verify_service_request(hdr, TOKEN, "GET", "/config/tts") is None
    assert serviceauth.verify_service_request(hdr, TOKEN, "POST", "/config/stt") is None


def test_stale_timestamp_rejected():
    hdr = serviceauth.sign_service_request(TOKEN, "GET", "/config/stt", ts=1000)
    assert serviceauth.verify_service_request(hdr, TOKEN, "GET", "/config/stt", now=1000) == 1000
    assert serviceauth.verify_service_request(hdr, TOKEN, "GET", "/config/stt", now=1200) is None


def test_garbage_header_is_none_not_crash():
    for bad in (None, "", "Bearer abc", "KENZY-HMAC nonsense", "KENZY-HMAC ts=x, sig=y"):
        assert serviceauth.verify_service_request(bad, TOKEN, "GET", "/x") is None


def test_response_roundtrip_with_binding():
    body = b'{"model":"base"}'
    binding = b"\x11" * 32
    ts = 42
    sig = serviceauth.sign_service_response(TOKEN, ts, body, binding=binding)
    assert serviceauth.verify_service_response(sig, TOKEN, ts, body, binding=binding)
    # a relay presenting a different cert => different binding => rejected
    assert not serviceauth.verify_service_response(sig, TOKEN, ts, body, binding=b"\x22" * 32)
    # tampered body => rejected
    assert not serviceauth.verify_service_response(sig, TOKEN, ts, body + b"!", binding=binding)
    # missing signature => rejected
    assert not serviceauth.verify_service_response(None, TOKEN, ts, body, binding=binding)


def test_cert_binding_own_matches_peer(tmp_path):
    import subprocess

    cert, key = tmp_path / "c.crt", tmp_path / "c.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-keyout", str(key), "-out", str(cert), "-subj", "/CN=bindtest"],
        check=True, capture_output=True,
    )
    own = tlsutil.own_cert_binding(str(cert))
    assert len(own) == 32
    import ssl

    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    assert own == tlsutil.peer_cert_binding(der)
    assert tlsutil.own_cert_binding(None) == b""
    assert tlsutil.peer_cert_binding(None) == b""
