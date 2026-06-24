"""Auth helpers shared across Kenzy services (stdlib only — no new deps).

Three concerns live here, all small:

* **Password hashing** for the dashboard's username/password login —
  ``hash_password`` / ``verify_password`` using ``hashlib.scrypt`` (with a
  ``pbkdf2_hmac`` fallback for builds without scrypt). Stored as a single
  ``algo$params$salt$hash`` string in ``server.yaml``.
* **Signed session cookies** — ``sign_cookie`` / ``verify_cookie`` produce and
  check a tamper-proof ``payload.sig`` token (HMAC-SHA256) with an expiry, so the
  dashboard needs no server-side session store.
* **Service-to-service auth** — ``check_bearer`` verifies a shared bearer token
  (framework-free); ``kenzy.fastapi_auth`` wraps it in middleware for the backend
  services. No-op when no token is configured.
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
# Service-to-service bearer
# ---------------------------------------------------------------------------


def check_bearer(authorization: str | None, token: str | None) -> bool:
    """Return True if the request is authorized for service-to-service calls.

    No-op (always True) when ``token`` is falsy, so service auth is opt-in. Kept
    free of any web-framework import — the server (no fastapi) imports this module
    too; each FastAPI service wraps it in a tiny dependency.
    """
    if not token:
        return True
    auth = authorization or ""
    presented = auth[7:] if auth.startswith("Bearer ") else ""
    return hmac.compare_digest(presented, token)
