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
import shutil
import uuid
from pathlib import Path

from kenzy.config import SERVICES, kenzy_home, packaged_config

_PACKAGE_DATA = Path(__file__).parent / "data"

#: Runtime data subdirectories created under the config home.
_DATA_DIRS = ("speakers", "home_assistant")

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


def _enable_dashboard(server_yaml: Path) -> None:
    """Flip ``dashboard.enabled`` to true in the scaffolded server.yaml.

    The dashboard block is the only ``enabled: false`` in the template (discovery
    defaults to true), so the first false-valued ``enabled:`` key is the one.
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
) -> None:
    home = home.expanduser()
    print(f"Scaffolding Kenzy config home: {home}  (profile: {profile})")

    # A node needs only its bootstrap config (identity + how to reach the server);
    # everything operational (audio, wakeword, sounds, room name) is pulled from
    # the server on every boot. Other service configs/secrets live on the server.
    node_only = profile == "node"
    services = ["node"] if node_only else SERVICES

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
        # Turn the dashboard on in a freshly written server.yaml (installer handoff).
        if svc == "server" and enable_dashboard and action == "write":
            _enable_dashboard(home / "configs" / "server.yaml")
            print("  [edit ] configs/server.yaml (dashboard.enabled: true)")

    if node_only:
        # Nodes pull tuning from the server on connect (config-pull) and run no
        # skills, so they need no .env, skills/, data/, or per-room overrides.
        print("Done. Node config ready — start it with: kenzy-node")
        return

    env_src = _PACKAGE_DATA / ".env.example"
    if env_src.is_file():
        action = _copy(env_src, home / ".env", force)
        print(f"  [{action}] .env")

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
    args = parser.parse_args()

    home = Path(args.home) if args.home else kenzy_home()
    scaffold(
        home,
        force=args.force,
        node_id=args.node_id,
        profile=args.profile,
        enable_dashboard=args.enable_dashboard,
    )


if __name__ == "__main__":
    main()
