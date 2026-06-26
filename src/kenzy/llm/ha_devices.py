"""
kenzy-ha-devices: list the live Home Assistant topology Kenzy can voice-control.

Pulls the same topology the ``home_assistant`` skill uses (via ``ha_model``),
applies the ``curation.yaml`` exclusion rules, and prints the
``floor -> area -> domain -> entity`` tree with each entity_id and whether it is
included or excluded (and why).  Use it to find the entity_ids to reference in
curation, and to verify your exclude rules.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from kenzy.llm import skills as skill_registry
from kenzy.llm.builtin_skills import ha_model


def _load_local_config(explicit: str | None) -> dict[str, Any]:
    """Load llm.yaml locally (no server pull) so the CLI works offline."""
    import yaml  # type: ignore[import-untyped]

    from kenzy.config import resolve_config

    path = Path(explicit) if explicit else resolve_config("llm")
    try:
        data = yaml.safe_load(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


def _print_tree(entities: list[ha_model.ClassifiedEntity], show_excluded: bool) -> None:
    # floor -> area -> domain -> [entities]
    tree: dict[str, dict[str, dict[str, list[ha_model.ClassifiedEntity]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for e in entities:
        tree[e.floor_name][e.area_name][e.domain].append(e)

    for floor in sorted(tree):
        print(f"\n{floor}")
        for area in sorted(tree[floor]):
            print(f"  {area}")
            for domain in sorted(tree[floor][area]):
                shown = [
                    e for e in tree[floor][area][domain] if show_excluded or e.included
                ]
                if not shown:
                    continue
                print(f"    {domain}")
                for e in sorted(shown, key=lambda x: x.entity_id):
                    mark = "✓" if e.included else "✗"
                    line = f"      {mark} {e.entity_id:<42} {e.name}"
                    if not e.included:
                        line += f"   [{e.reason}]"
                    print(line)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="kenzy-ha-devices",
        description="List the Home Assistant devices Kenzy can voice-control.",
    )
    parser.add_argument("--config", help="Path to llm.yaml (default: resolved locally)")
    parser.add_argument("--url", help="Override the Home Assistant base URL")
    parser.add_argument(
        "--included-only",
        action="store_true",
        help="Hide excluded entities (show only what's voice-controllable)",
    )
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    cfg = _load_local_config(args.config)
    skills_cfg = cfg.get("skills", {}) or {}
    if args.url:
        skills_cfg.setdefault("home_assistant", {})["url"] = args.url
    skill_registry.set_config(skills_cfg)

    base, _ = ha_model.ha_conn()

    try:
        raw = asyncio.run(ha_model.fetch_raw())
    except Exception as exc:
        print(f"Could not reach Home Assistant at {base}: {exc}", file=sys.stderr)
        print(
            "Check skills.home_assistant.url in llm.yaml (or --url) and HA_API_KEY in .env.",
            file=sys.stderr,
        )
        sys.exit(1)

    entities = ha_model.classify(raw, ha_model.load_curation())

    print("Kenzy — Home Assistant device discovery")
    print(f"URL: {base}")

    if not entities:
        print("\nNo voice-controllable entities found.")
        print(
            "Entities must be assigned to an area in HA and be one of: "
            + ", ".join(ha_model._domains())
        )
        return

    _print_tree(entities, show_excluded=not args.included_only)

    total = len(entities)
    included = sum(1 for e in entities if e.included)
    print("\n" + "-" * 56)
    print(f"{total} entities · {included} included · {total - included} excluded")
    print(
        "Reference these entity_ids in data/home_assistant/curation.yaml "
        "to alias, annotate, set room defaults, or exclude."
    )


if __name__ == "__main__":
    main()
