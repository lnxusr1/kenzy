"""``kenzy-passwd``: set the dashboard login credentials (server node only).

Prompts for a username and password, hashes the password, and rewrites the
``dashboard.auth`` block in the server's ``server.yaml`` — preserving the file's
comments and layout (regex edit, not a YAML redump). Run this on the host that
runs ``kenzy-server`` to move off the default ``admin`` / ``password``.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from pathlib import Path

from kenzy.config import resolve_config
from kenzy.serviceauth import hash_password


def _current_username(text: str) -> str | None:
    m = re.search(r"(?m)^\s+username:\s*[\"']?([^\"'\n]+)", text)
    return m.group(1).strip() if m else None


def set_auth(text: str, username: str, password_hash: str) -> str:
    """Return ``text`` with dashboard.auth username/password_hash set.

    Updates the existing keys when present, else inserts a fresh ``auth:`` block
    under the ``dashboard:`` section.
    """
    user_q = json.dumps(username)
    hash_q = json.dumps(password_hash)
    if re.search(r"(?m)^\s+password_hash:", text):
        text = re.sub(
            r"(?m)^(\s+)username:.*$", lambda m: f"{m.group(1)}username: {user_q}", text, count=1
        )
        text = re.sub(
            r"(?m)^(\s+)password_hash:.*$",
            lambda m: f"{m.group(1)}password_hash: {hash_q}",
            text,
            count=1,
        )
        return text
    block = f"  auth:\n    username: {user_q}\n    password_hash: {hash_q}\n"
    new, n = re.subn(r"(?m)^(dashboard:[ \t]*)$\n", lambda m: m.group(0) + block, text, count=1)
    if n == 0:
        raise SystemExit("Could not find a `dashboard:` section in server.yaml")
    return new


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="kenzy-passwd",
        description="Set the Kenzy dashboard login (writes dashboard.auth in server.yaml).",
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
    text = config_path.read_text()

    current = _current_username(text) or "admin"
    if args.username:
        username = args.username
    else:
        username = input(f"Username [{current}]: ").strip() or current

    pw = getpass.getpass("New password: ")
    if not pw:
        raise SystemExit("Password must not be empty.")
    if pw != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords did not match.")

    config_path.write_text(set_auth(text, username, hash_password(pw)))
    print(f"Updated dashboard login for '{username}' in {config_path}")
    print("Restart kenzy-server for it to take effect.", file=sys.stderr)


if __name__ == "__main__":
    main()
