"""``kenzy-init``: scaffold a Kenzy config home from packaged defaults.

Writes ``configs/``, ``skills/``, ``data/`` and a ``.env`` into the config home
(``$KENZY_HOME`` or ``~/.config/kenzy`` by default), copying the bundled default
configs. Safe to re-run: existing files are left untouched unless ``--force`` is
given.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import uuid
from pathlib import Path

from kenzy.config import SERVICES, kenzy_home, packaged_config

_PACKAGE_DATA = Path(__file__).parent / "data"

#: Runtime data subdirectories created under the config home.
_DATA_DIRS = ("speakers", "home_assistant")

_CONSTRAINTS_TEMPLATE = (
    "# Pip constraints for this Kenzy install.\n"
    "#\n"
    "# Pin any dependency that must stay at a specific version for THIS machine\n"
    "# (e.g. GPU/driver or model compatibility). Kenzy honors this file on install\n"
    "# and on every auto-upgrade, so an upgrade won't silently move a pinned package.\n"
    "# If a future release truly can't satisfy a pin, the upgrade fails loudly instead\n"
    "# of breaking the host — resolve the conflict here, then upgrade again.\n"
    "#\n"
    "# Standard pip constraints format, one per line, e.g.:\n"
    "#   transformers==4.30.0\n"
    "#   numpy<2.0\n"
)

_SKILLS_README = (
    "# Custom skills\n\n"
    "Drop your own `@skill` / `@fast_intent` Python files here. They load in "
    "addition to the skills bundled with Kenzy; a file here that defines the "
    "same skill name overrides the built-in one. Disable any skill (built-in or "
    "custom) by name under `skills.disabled` in `configs/llm.yaml`.\n"
)


def _copy(src: Path, dst: Path, force: bool) -> str:
    """Copy src→dst unless dst exists (and not force). Returns 'write' or 'skip'."""
    if dst.exists() and not force:
        return "skip"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return "write"


def _set_node_id(node_yaml: Path, node_id: str) -> None:
    """Set ``node_id:`` in the scaffolded node.yaml (replacing the template line).

    The template ships ``node_id`` commented out, so the live-key regex won't
    match and we append a real key. A re-run with --force replaces it in place.
    """
    value = json.dumps(node_id)  # double-quoted scalar: safe for special chars
    text = node_yaml.read_text()
    new, n = re.subn(r"(?m)^node_id:.*$", f"node_id: {value}", text)
    if n == 0:  # only the commented template line — append a real key
        new = text.rstrip("\n") + f"\nnode_id: {value}\n"
    node_yaml.write_text(new)


def _set_discovery_token(yaml_path: Path, token: str) -> None:
    """Set ``discovery.token`` in a scaffolded config (replacing the commented
    template line, or inserting under ``discovery:`` if absent)."""
    value = json.dumps(token)  # double-quoted scalar
    text = yaml_path.read_text()
    # The only ``token:`` key under discovery ships commented; auth_token: won't
    # match (it isn't ``token:`` at a segment boundary).
    new, n = re.subn(r"(?m)^(\s*)#?\s*token:.*$", rf"\1token: {value}", text, count=1)
    if n == 0:
        new, n = re.subn(r"(?m)^(discovery:\s*)$", rf"\1\n  token: {value}", text, count=1)
    yaml_path.write_text(new)


def _set_env_var(env_path: Path, key: str, value: str) -> None:
    """Set ``KEY="value"`` in a .env file (replacing an existing line or appending)."""
    line = f'{key}="{value}"'
    text = env_path.read_text() if env_path.exists() else ""
    new, n = re.subn(rf"(?m)^{re.escape(key)}=.*$", line, text)
    if n == 0:
        new = (text.rstrip("\n") + "\n" if text.strip() else "") + line + "\n"
    env_path.write_text(new)


def _enable_dashboard(server_yaml: Path) -> None:
    """Flip ``dashboard.enabled`` to true in the scaffolded server.yaml.

    The dashboard now ships enabled by default, so this is normally a no-op
    (idempotent) and ``--enable-dashboard`` is redundant; it stays as a backstop
    in case an operator templates a server.yaml with the dashboard turned off.
    It flips the first false-valued ``enabled:`` key (discovery defaults to true).
    """
    text = server_yaml.read_text()
    new, n = re.subn(r"(?m)^(\s+)enabled:\s*false\b", r"\1enabled: true", text, count=1)
    if n:
        server_yaml.write_text(new)


def scaffold(
    home: Path,
    force: bool = False,
    node_id: str | None = None,
    profile: str = "all",
    enable_dashboard: bool = False,
    token: str | None = None,
    constraints: str | None = None,
) -> None:
    home = home.expanduser()
    print(f"Scaffolding Kenzy config home: {home}  (profile: {profile})")

    # A node needs only its bootstrap config (identity + how to reach the server);
    # everything operational (audio, wakeword, sounds, room name) is pulled from
    # the server on every boot. Other service configs/secrets live on the server.
    node_only = profile == "node"
    services = ["node"] if node_only else SERVICES

    # The join token is the shared secret nodes present at `hello` AND the
    # service-to-service bearer co-located backends use to pull their config. For a
    # server/all scaffold, generate one (secure-by-default) unless --token was given,
    # and apply the *same* value to server.yaml, the co-located node.yaml, and .env's
    # KENZY_SERVICE_TOKEN so they all match. Only written into freshly-created files,
    # so re-runs don't rotate it. A node-only scaffold sets it only when --token given.
    gen_token = token if node_only else (token or secrets.token_urlsafe(24))
    server_written = False

    for svc in services:
        action = _copy(packaged_config(svc), home / "configs" / f"{svc}.yaml", force)
        print(f"  [{action}] configs/{svc}.yaml")
        # Bake a stable node_id into a freshly written node.yaml so the node's
        # server-side config can be pre-seeded by that id. Use the supplied id, or
        # generate one and print it. Skip when the file pre-existed (and not
        # --force) so a re-run never clobbers an existing identity.
        if svc == "node" and action == "write":
            nid = node_id or str(uuid.uuid4())
            _set_node_id(home / "configs" / "node.yaml", nid)
            print(f"  [edit ] configs/node.yaml (node_id: {nid})")
            if gen_token:
                _set_discovery_token(home / "configs" / "node.yaml", gen_token)
                print("  [edit ] configs/node.yaml (discovery.token set)")
        if svc == "server" and action == "write":
            # Secure-by-default join + service token.
            if gen_token:
                _set_discovery_token(home / "configs" / "server.yaml", gen_token)
                server_written = True
                print("  [edit ] configs/server.yaml (discovery.token set)")
            # Turn the dashboard on in a freshly written server.yaml (installer handoff).
            if enable_dashboard:
                _enable_dashboard(home / "configs" / "server.yaml")
                print("  [edit ] configs/server.yaml (dashboard.enabled: true)")

    # Operator pip pins (honored on install + every upgrade) — applies to any venv,
    # so it's scaffolded for every profile. Seed from --constraints if given, else a
    # commented template the operator can fill in later.
    constraints_dst = home / "constraints.txt"
    if not constraints_dst.exists() or force:
        if constraints and Path(constraints).is_file():
            shutil.copyfile(constraints, constraints_dst)
            print("  [write] constraints.txt (from --constraints)")
        else:
            constraints_dst.write_text(_CONSTRAINTS_TEMPLATE)
            print("  [write] constraints.txt (template)")

    if node_only:
        # Nodes pull tuning from the server on connect (config-pull) and run no
        # skills, so they need no .env, skills/, data/, or per-room overrides.
        if gen_token:
            print("Done. Node config ready (join token set) — start it with: kenzy-node")
        else:
            print("Done. Node config ready — start it with: kenzy-node")
            print("Note: if the server requires a join token, re-run with --token <token>.")
        return

    env_src = _PACKAGE_DATA / ".env.example"
    if env_src.is_file():
        action = _copy(env_src, home / ".env", force)
        print(f"  [{action}] .env")
        # Co-located backend services authenticate to the server's /config with this
        # bearer; keep it in sync with the server's freshly-generated join token.
        if gen_token and server_written:
            _set_env_var(home / ".env", "KENZY_SERVICE_TOKEN", gen_token)
            print("  [edit ] .env (KENZY_SERVICE_TOKEN)")

    skills_dir = home / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    readme = skills_dir / "README.md"
    if not readme.exists():
        readme.write_text(_SKILLS_README)
    print("  [dir ] skills/")

    # Per-node overrides (config-pull) live in configs/nodes/<node_id>.yaml — keyed
    # by the node's stable node_id, which isn't known until the node first connects
    # (it's auto-generated then). So we create the directory but no stub here; create
    # a node's override from the dashboard once it has connected, or pre-seed it by
    # node_id (see the --node-id plan in design/centralized-config.md).
    nodes_dir = home / "configs" / "nodes"
    nodes_dir.mkdir(parents=True, exist_ok=True)
    print("  [dir ] configs/nodes/")

    for sub in _DATA_DIRS:
        (home / "data" / sub).mkdir(parents=True, exist_ok=True)
        print(f"  [dir ] data/{sub}/")

    print("Done. Edit configs/*.yaml and .env, then start a service (e.g. kenzy-server).")
    if gen_token and server_written:
        print()
        print(f"  Join token: {gen_token}")
        print("  Add a node with:  kenzy-init --profile node --token <token>")
        print("  (also shown in the dashboard under Settings, with a copy button)")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="kenzy-init",
        description="Scaffold a Kenzy config home (configs, skills, data, .env) "
        "from packaged defaults.",
    )
    parser.add_argument(
        "home",
        nargs="?",
        default=None,
        metavar="DIR",
        help="Target config home (default: $KENZY_HOME or ~/.config/kenzy)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing config and .env files",
    )
    parser.add_argument(
        "--node-id",
        default=None,
        metavar="ID",
        help="Stable node_id to bake into configs/node.yaml (default: a generated "
        "uuid, printed so you can pre-seed configs/nodes/<id>.yaml on the server). "
        "The node's room name is set later from the dashboard, not at install.",
    )
    parser.add_argument(
        "--profile",
        choices=("all", "node", "server"),
        default="all",
        help="What to scaffold: 'node' writes only configs/node.yaml; 'server'/'all' "
        "write the full config home (default: all).",
    )
    parser.add_argument(
        "--enable-dashboard",
        action="store_true",
        help="Set dashboard.enabled: true in a freshly scaffolded server.yaml.",
    )
    parser.add_argument(
        "--token",
        default=None,
        metavar="TOKEN",
        help="Shared join/service token. For 'node', sets discovery.token (paste the "
        "value the server printed / shows in the dashboard). For 'server'/'all', uses "
        "this instead of a generated one (e.g. to share a token across hosts).",
    )
    parser.add_argument(
        "--constraints",
        default=None,
        metavar="FILE",
        help="Seed the config home's constraints.txt from this pip constraints file "
        "(dependency pins honored on install and every auto-upgrade).",
    )
    args = parser.parse_args()

    home = Path(args.home) if args.home else kenzy_home()
    scaffold(
        home,
        force=args.force,
        node_id=args.node_id,
        profile=args.profile,
        enable_dashboard=args.enable_dashboard,
        token=args.token,
        constraints=args.constraints,
    )


if __name__ == "__main__":
    main()
