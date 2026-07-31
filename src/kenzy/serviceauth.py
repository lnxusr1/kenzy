"""Auth helpers shared across Kenzy services (stdlib only — no new deps).

Three concerns live here, all small:

* **Password hashing** for the dashboard's username/password login —
  ``hash_password`` / ``verify_password`` using ``hashlib.scrypt`` (with a
  ``pbkdf2_hmac`` fallback for builds without scrypt). Stored as a single
  ``algo$params$salt$hash`` string in ``server.yaml``.
* **Signed session cookies** — ``sign_cookie`` / ``verify_cookie`` produce and
  check a tamper-proof ``payload.sig`` token (HMAC-SHA256) with an expiry, so the
  dashboard needs no server-side session store.
* **Service-to-service auth** — a token-proof HMAC scheme
  (``sign_service_request`` / ``verify_service_request``) so the shared token
  never rides the wire; ``kenzy.fastapi_auth`` wraps it in middleware for the
  backend services. No-op when no token is configured.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

# scrypt cost parameters (interactive-login appropriate; ~tens of ms).
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_PBKDF2_ROUNDS = 600_000


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Hash a password into a self-describing ``algo$params$salt$hash`` string."""
    salt = salt if salt is not None else os.urandom(16)
    try:
        dk = hashlib.scrypt(
            password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
        )
        return f"scrypt${_SCRYPT_N}:{_SCRYPT_R}:{_SCRYPT_P}${_b64e(salt)}${_b64e(dk)}"
    except (ValueError, MemoryError):  # scrypt unavailable / memory-limited
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS, dklen=32)
        return f"pbkdf2${_PBKDF2_ROUNDS}${_b64e(salt)}${_b64e(dk)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against a ``hash_password`` string."""
    try:
        algo, params, salt_s, hash_s = stored.split("$")
        salt, expected = _b64d(salt_s), _b64d(hash_s)
        if algo == "scrypt":
            n, r, p = (int(x) for x in params.split(":"))
            dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=len(expected))
        elif algo == "pbkdf2":
            dk = hashlib.pbkdf2_hmac(
                "sha256", password.encode(), salt, int(params), dklen=len(expected)
            )
        else:
            return False
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(dk, expected)


# ---------------------------------------------------------------------------
# Signed session cookies (stateless)
# ---------------------------------------------------------------------------

COOKIE_NAME = "kenzy_dash"
_DEFAULT_TTL = 12 * 3600  # 12 hours


def _derive_key(secret: str) -> bytes:
    return hashlib.sha256(b"kenzy-dashboard-cookie\x00" + secret.encode()).digest()


def sign_cookie(username: str, secret: str, *, ttl: int = _DEFAULT_TTL) -> str:
    """Return a ``payload.sig`` token authenticating ``username`` until now+ttl."""
    payload = {"u": username, "exp": int(time.time()) + ttl}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(_derive_key(secret), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_cookie(token: str, secret: str) -> str | None:
    """Return the username if ``token`` is valid and unexpired, else ``None``."""
    try:
        body, sig = token.split(".", 1)
        expected = _b64e(hmac.new(_derive_key(secret), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload: dict[str, Any] = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload.get("exp"), int) or payload["exp"] < int(time.time()):
        return None
    u = payload.get("u")
    return u if isinstance(u, str) else None


# ---------------------------------------------------------------------------
# Token-proof service auth (HMAC — the token is NEVER transmitted)
# ---------------------------------------------------------------------------
#
# Each service-to-service request carries a signature, not the token: an
# eavesdropper (even a TLS-terminating relay on the LAN, which our
# encrypted-but-unverified posture permits) learns nothing replayable, and an
# impostor without the token cannot mint a valid config. Freshness is bounded
# by a timestamp; the config RESPONSE additionally binds to the server's TLS
# certificate (channel binding) so a relay presenting a different cert is
# detected when the client checks the reply against the cert it actually saw.
# Request signatures deliberately omit the body and the channel binding —
# response-side binding alone defeats relays, and this keeps verifiers from
# having to read the request body (the starlette middleware footgun).

_HMAC_SCHEME = "KENZY-HMAC"
_MAX_SKEW = 120  # seconds; assumes NTP (Raspberry Pi OS default-on)
#: The signature rides its own header, not Authorization.
SIG_HEADER = "X-Kenzy-Auth"


def service_token_from_env() -> str | None:
    """The shared fleet token from the environment: ``KENZY_SERVER_TOKEN``
    (preferred, 3.11+) or the legacy ``KENZY_SERVICE_TOKEN`` alias. Part of the
    env-only bootstrap contract (server-authority stage d)."""
    return os.environ.get("KENZY_SERVER_TOKEN") or os.environ.get("KENZY_SERVICE_TOKEN") or None


def _svc_key(token: str) -> bytes:
    return hashlib.sha256(b"kenzy-svc-hmac\x00" + token.encode()).digest()


def _req_material(ts: int, method: str, path: str) -> bytes:
    return b"\x00".join([b"req", str(ts).encode(), method.upper().encode(), path.encode()])


def _resp_material(ts: int, body: bytes, binding: bytes) -> bytes:
    return b"\x00".join([b"resp", str(ts).encode(), hashlib.sha256(body).digest(), binding])


def sign_service_request(token: str, method: str, path: str, *, ts: int | None = None) -> str:
    """Build the ``X-Kenzy-Auth: KENZY-HMAC …`` header value for a token-proof request."""
    ts = int(time.time()) if ts is None else ts
    sig = hmac.new(_svc_key(token), _req_material(ts, method, path), hashlib.sha256).hexdigest()
    return f"{_HMAC_SCHEME} ts={ts}, sig={sig}"


def verify_service_request(
    authorization: str | None,
    token: str,
    method: str,
    path: str,
    *,
    max_skew: int = _MAX_SKEW,
    now: int | None = None,
) -> int | None:
    """Return the request timestamp if the KENZY-HMAC header is valid, else None.

    None means "not a valid signature" — the caller rejects. ``token`` must be
    truthy (the caller handles the auth-disabled case).
    """
    auth = authorization or ""
    prefix = _HMAC_SCHEME + " "
    if not auth.startswith(prefix):
        return None
    try:
        fields = dict(part.strip().split("=", 1) for part in auth[len(prefix) :].split(","))
        ts = int(fields["ts"])
        sig = fields["sig"]
    except (ValueError, KeyError):
        return None
    now = int(time.time()) if now is None else now
    if abs(now - ts) > max_skew:
        return None
    expected = hmac.new(
        _svc_key(token), _req_material(ts, method, path), hashlib.sha256
    ).hexdigest()
    return ts if hmac.compare_digest(sig, expected) else None


def sign_node_hello(token: str, node_id: str, *, ts: int | None = None) -> dict[str, Any]:
    """A node's join proof for its ``hello`` — token-proof + timestamp-fresh, so
    the raw join token never rides the WebSocket handshake. Returns
    ``{"ts": …, "sig": …}`` to carry in ``hello.auth`` (3.12+)."""
    ts = int(time.time()) if ts is None else ts
    material = b"\x00".join([b"hello", str(ts).encode(), node_id.encode()])
    sig = hmac.new(_svc_key(token), material, hashlib.sha256).hexdigest()
    return {"ts": ts, "sig": sig}


#: Short, stable tags for why a join was refused. The wire-facing close reason
#: uses these (a node's own log is where an operator looks first); the full
#: detail — including how far out a clock is — stays server-side.
JOIN_MISSING = "missing"
JOIN_MALFORMED = "malformed"
JOIN_STALE = "stale timestamp"
JOIN_BAD_SIG = "bad signature"


def check_node_hello(
    auth: Any, token: str, node_id: str, *, max_skew: int = _MAX_SKEW, now: int | None = None
) -> tuple[str, str] | None:
    """Verify a node's ``hello.auth`` proof; return ``None`` when it is good.

    On failure returns ``(tag, detail)`` — a short wire-safe tag and a full
    explanation for the server's log. Three very different faults used to be
    reported identically as "bad/missing join token": no proof at all (a node
    older than 3.12), a proof that doesn't match (wrong token), and a proof
    whose timestamp is outside the freshness window (a drifting clock, which no
    amount of retrying fixes and which the node cannot self-diagnose because it
    is never told why it was refused).
    """
    if not isinstance(auth, dict):
        return (JOIN_MISSING, "no auth block in hello (node older than 3.12?)")
    try:
        ts = int(auth["ts"])
        sig = str(auth["sig"])
    except (KeyError, ValueError, TypeError):
        return (JOIN_MALFORMED, "auth block missing or non-numeric ts/sig")
    now = int(time.time()) if now is None else now
    skew = now - ts
    if abs(skew) > max_skew:
        direction = "behind" if skew > 0 else "ahead of"
        return (
            JOIN_STALE,
            f"hello timestamp is {abs(skew)}s {direction} this server "
            f"(limit {max_skew}s) — check the node's clock/NTP, not its token",
        )
    material = b"\x00".join([b"hello", str(ts).encode(), node_id.encode()])
    expected = hmac.new(_svc_key(token), material, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return (JOIN_BAD_SIG, f"signature does not match discovery.token for node_id '{node_id}'")
    return None


def verify_node_hello(
    auth: Any, token: str, node_id: str, *, max_skew: int = _MAX_SKEW, now: int | None = None
) -> bool:
    """Verify a node's ``hello.auth`` proof against the claimed ``node_id``."""
    return check_node_hello(auth, token, node_id, max_skew=max_skew, now=now) is None


def sign_service_response(token: str, ts: int, body: bytes, *, binding: bytes = b"") -> str:
    """Signature the server attaches (``X-Kenzy-Sig``) so the client can confirm
    the reply came, unforged, over the TLS channel it observed."""
    return hmac.new(_svc_key(token), _resp_material(ts, body, binding), hashlib.sha256).hexdigest()


def verify_service_response(
    sig: str | None, token: str, ts: int, body: bytes, *, binding: bytes = b""
) -> bool:
    """Client-side check of the server's ``X-Kenzy-Sig`` against the response
    body and the certificate the client actually saw (``binding``)."""
    if not sig:
        return False
    expected = sign_service_response(token, ts, body, binding=binding)
    return hmac.compare_digest(sig, expected)
