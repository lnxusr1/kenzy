"""Every config key the dashboard can render must be explained in the docs.

The Services editors show exactly the keys present in the packaged configs
(comments don't render — a key doesn't exist for the dashboard until it's a
real key). This test walks every packaged config and asserts each key is
documented: on the service's own reference page, or on the page that owns it
(per-skill keys → the skills docs; node tuning pushed via ``node_defaults``
→ the Node page). Add a key without documenting it and this fails with the
list of what you forgot.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIGS = ROOT / "src" / "kenzy" / "data" / "configs"
DOCS = ROOT / "docs"

#: Where a key family is allowed to be documented, beyond the service's own
#: configuration page. Deliberate cross-page homes, not an escape hatch.
CROSS_PAGES = {
    ("llm", "skills."): [DOCS / "skills" / "builtin.md", DOCS / "skills" / "home-assistant.md"],
    ("server", "node_defaults."): [DOCS / "configuration" / "node.md"],
}


def _flatten(d: dict, prefix: str = "") -> list[str]:
    out: list[str] = []
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out += _flatten(v, key + ".")
        else:
            out.append(key)
    return out


def _documented(key: str, texts: list[str]) -> bool:
    """A key counts as documented when its dotted form or leaf appears in
    backticks (a reference-table row) or as a YAML key in an example block —
    both are legitimate documentation styles in these pages."""
    leaf = key.split(".")[-1]
    yaml_key = re.compile(rf"(?m)^\s*{re.escape(leaf)}\s*:")
    return any(
        f"`{key}`" in t or f"`{leaf}`" in t or yaml_key.search(t) for t in texts
    )


def test_every_packaged_config_key_is_documented():
    problems: list[str] = []
    for cfg_path in sorted(CONFIGS.glob("*.yaml")):
        svc = cfg_path.stem
        doc_path = DOCS / "configuration" / f"{svc}.md"
        assert doc_path.is_file(), f"no reference page for service {svc!r} ({doc_path})"
        own = doc_path.read_text()
        for key in _flatten(yaml.safe_load(cfg_path.read_text())):
            texts = [own]
            for (s, prefix), pages in CROSS_PAGES.items():
                if svc == s and key.startswith(prefix):
                    texts += [p.read_text() for p in pages]
            if not _documented(key, texts):
                problems.append(f"{svc}: {key}")
    assert not problems, (
        "Config keys the dashboard renders but the docs never explain "
        "(add them to docs/configuration/<service>.md or the owning page): "
        + ", ".join(problems)
    )
