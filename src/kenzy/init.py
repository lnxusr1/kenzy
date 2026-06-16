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


def _set_room_id(node_yaml: Path, room: str) -> None:
    """Set ``room_id:`` in the scaffolded node.yaml (replacing the template line)."""
    value = json.dumps(room)  # double-quoted scalar: safe for spaces/special chars
    text = node_yaml.read_text()
    new, n = re.subn(r"(?m)^room_id:.*$", f"room_id: {value}", text)
    if n == 0:  # template without a room_id line — append one
        new = text.rstrip("\n") + f"\nroom_id: {value}\n"
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
    room: str | None = None,
    profile: str = "all",
    enable_dashboard: bool = False,
) -> None:
    home = home.expanduser()
    print(f"Scaffolding Kenzy config home: {home}  (profile: {profile})")

    # A node needs only its own identity + audio config; everything else (other
    # service configs, .env secrets, skills, data) lives on the server.
    node_only = profile == "node"
    services = ["node"] if node_only else SERVICES

    for svc in services:
        action = _copy(packaged_config(svc), home / "configs" / f"{svc}.yaml", force)
        print(f"  [{action}] configs/{svc}.yaml")
        # Bake the chosen room name into a freshly written node.yaml. Skip when the
        # file pre-existed (and not --force) so a re-run never clobbers a custom id.
        if svc == "node" and room and action == "write":
            _set_room_id(home / "configs" / "node.yaml", room)
            print(f"  [edit ] configs/node.yaml (room_id: {room})")
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

    # Per-room node overrides (config-pull): configs/nodes/<room_id>.yaml
    nodes_dir = home / "configs" / "nodes"
    nodes_dir.mkdir(parents=True, exist_ok=True)
    print("  [dir ] configs/nodes/")
    if room:
        stub = nodes_dir / f"{room}.yaml"
        if not stub.exists() or force:
            stub.write_text(
                f"# Per-room overrides for '{room}'. The server shallow-merges these\n"
                "# over node_defaults (server.yaml) and pushes them to the node on\n"
                "# connect (config-pull). Add any node.yaml tuning keys here.\n"
            )
            print(f"  [write] configs/nodes/{room}.yaml")

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
        "--room",
        default=None,
        metavar="NAME",
        help="Room/node id to bake into configs/node.yaml (default: the node's "
        "hostname at runtime). Also creates a configs/nodes/<NAME>.yaml stub.",
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
        room=args.room,
        profile=args.profile,
        enable_dashboard=args.enable_dashboard,
    )


if __name__ == "__main__":
    main()
