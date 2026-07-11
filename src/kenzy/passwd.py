"""``kenzy-passwd``: set the dashboard login credentials (server node only).

Prompts for a username and password, hashes the password, and writes the
``dashboard.auth`` block into the server's ``server.local.yaml`` **override
layer** — deliberately NOT ``server.yaml`` itself: on a managed host
(``kenzy-deploy``) server.yaml is overwritten by every upgrade's config sync
(which would silently revert the login to whatever the operator tree holds,
usually the default), and on a packaged-default install server.yaml lives
read-only in site-packages. The override layer is protected from the deploy
sync, redirected into the config home, and merged over server.yaml by
``load_server_config()`` at boot. Run this on the host that runs
``kenzy-server`` to move off the default ``admin`` / ``password``.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Any

from kenzy.config import resolve_config, writable_config_path
from kenzy.serviceauth import hash_password


def override_path(config_path: Path) -> Path:
    """``server.local.yaml`` for a given server.yaml, redirected to the config
    home when the config is the packaged read-only default. (Mirrors the
    server's ``_server_override_path`` — keep them aligned.)"""
    return writable_config_path("server.local", Path(config_path).parent / "server.local.yaml")


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml  # type: ignore[import-untyped]

    try:
        data = yaml.safe_load(path.read_text()) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def set_auth(config_path: Path, username: str, password_hash: str) -> Path:
    """Persist ``dashboard.auth`` into the override layer; returns the file written.

    Other override keys (dashboard-written server settings) are preserved.
    """
    import yaml

    path = override_path(config_path)
    data = _load_yaml(path)
    dashboard = data.get("dashboard")
    if not isinstance(dashboard, dict):
        dashboard = {}
        data["dashboard"] = dashboard
    auth = dashboard.get("auth")
    if not isinstance(auth, dict):
        auth = {}
        dashboard["auth"] = auth
    auth["username"] = username
    auth["password_hash"] = password_hash
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=True))
    return path


def current_username(config_path: Path) -> str | None:
    """The effective dashboard username: the override layer wins over server.yaml."""
    for candidate in (override_path(config_path), Path(config_path)):
        if not candidate.is_file():
            continue
        dashboard = _load_yaml(candidate).get("dashboard")
        auth = dashboard.get("auth") if isinstance(dashboard, dict) else None
        name = auth.get("username") if isinstance(auth, dict) else None
        if name:
            return str(name)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="kenzy-passwd",
        description=(
            "Set the Kenzy dashboard login (writes dashboard.auth to the "
            "server.local.yaml override, which survives upgrades)."
        ),
    )
    parser.add_argument(
        "config",
        nargs="?",
        default=None,
        metavar="server.yaml",
        help="Path to server.yaml (default: resolved like kenzy-server)",
    )
    parser.add_argument("--username", default=None, help="Username (else prompt)")
    args = parser.parse_args()

    config_path = Path(resolve_config("server", args.config))
    if not config_path.is_file():
        raise SystemExit(f"server.yaml not found: {config_path}")

    current = current_username(config_path) or "admin"
    if args.username:
        username = args.username
    else:
        username = input(f"Username [{current}]: ").strip() or current

    pw = getpass.getpass("New password: ")
    if not pw:
        raise SystemExit("Password must not be empty.")
    if pw != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords did not match.")

    out = set_auth(config_path, username, hash_password(pw))
    print(f"Updated dashboard login for '{username}' in {out}")
    print("Restart kenzy-server for it to take effect.", file=sys.stderr)


if __name__ == "__main__":
    main()
