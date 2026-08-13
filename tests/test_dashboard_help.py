"""Every editable setting in the dashboard carries a one-line explanation.

This started as drift, not a decision: the node editor documented all 30 of its
keys, the service editors covered ~60%, and the server editor had no help
mechanism at all — so the Settings page showed 23 bare dotted key names. Docs
exist and are linked from the UI, but a field you're staring at should say what
it does without a round trip.

These tests read the maps straight out of ``schema.js`` (a no-build SPA — the
JS *is* the artifact) and assert coverage against the Python side that decides
which keys are editable, so adding a config key without a description fails the
suite rather than quietly shipping a bare name.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from kenzy.server.server import _ALLOWED_OVERRIDE_KEYS, _SERVER_EDITABLE, _SERVICE_PEERS

# Keys the editors render that appear in NO packaged config, because the server
# injects them into the served config at runtime (_effective_service_config):
# its own TLS pair when it terminates TLS. Checking only the packaged YAML let
# these ship undocumented — the whole reason this constant is spelled out.
_INJECTED_TLS = ("tls.cert", "tls.key")

_STATIC = Path(__file__).resolve().parents[1] / "src/kenzy/server/dashboard_static"
_SCHEMA = _STATIC / "js/schema.js"
_CONFIGS = Path(__file__).resolve().parents[1] / "src/kenzy/data/configs"

# Keys the editors deliberately never render as a plain field, so they need no
# help string. Keep this list short and justified — it is the escape hatch that
# would otherwise let coverage rot silently.
_NOT_RENDERED = {
    "node": {
        # Identity/reachability live in node.yaml (bootstrap-only) and are shown
        # by dedicated UI (the room-name field, the audio-device picker).
        "room_id",
    },
}


def _js_object(name: str) -> str:
    """Return the raw source of a top-level `export const <name> = {…}` block."""
    src = _SCHEMA.read_text()
    start = src.index(f"export const {name} =")
    end = src.index("\n};", start)
    return src[start:end]


def _keys_at_depth(block: str, indent: int) -> set[str]:
    """Object keys at a given indent — quoted or bare, values ignored."""
    pat = re.compile(rf'^ {{{indent}}}"?([A-Za-z_][\w.]*)"?\s*:', re.MULTILINE)
    return set(pat.findall(block))


def _service_help() -> dict[str, set[str]]:
    block = _js_object("SERVICE_HELP")
    out: dict[str, set[str]] = {}
    current: str | None = None
    for line in block.splitlines():
        head = re.match(r"^ {2}(\w+):\s*\{", line)
        if head:
            current = head.group(1)
            out[current] = set()
            continue
        entry = re.match(r'^ {4}"?([A-Za-z_][\w.]*)"?\s*:', line)
        if current and entry:
            out[current].add(entry.group(1))
    return out


def _leaf_keys(data: Any, prefix: str = "") -> list[str]:
    """Dotted paths of every leaf in a packaged service config."""
    keys: list[str] = []
    for key, value in (data or {}).items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            keys.extend(_leaf_keys(value, path + "."))
        else:
            keys.append(path)
    return keys


def test_every_server_setting_has_help():
    """_SERVER_EDITABLE is the allow-list the Settings editor renders."""
    documented = _keys_at_depth(_js_object("SERVER_HELP"), 2)
    missing = sorted(set(_SERVER_EDITABLE) - documented)
    assert not missing, f"SERVER_HELP is missing: {missing}"


def test_every_node_setting_has_help():
    documented = _keys_at_depth(_js_object("NODE_HELP"), 2)
    editable = set(_ALLOWED_OVERRIDE_KEYS) - _NOT_RENDERED["node"]
    missing = sorted(editable - documented)
    assert not missing, f"NODE_HELP is missing: {missing}"


def test_every_service_setting_has_help():
    """The service editors render the EFFECTIVE config, not the packaged file.

    That distinction matters: the server merges in its TLS pair and the peer
    service URLs (_SERVICE_PEERS) before serving a service's config, so those
    fields appear in the editor without existing in any YAML on disk.
    """
    documented = _service_help()
    missing: dict[str, list[str]] = {}
    for service in ("stt", "tts", "llm", "speaker"):
        cfg = yaml.safe_load((_CONFIGS / f"{service}.yaml").read_text())
        rendered = set(_leaf_keys(cfg)) | set(_INJECTED_TLS)
        rendered |= {f"{peer}.url" for peer in _SERVICE_PEERS.get(service, ())}
        gaps = sorted(rendered - documented.get(service, set()))
        if gaps:
            missing[service] = gaps
    assert not missing, f"SERVICE_HELP is missing: {json.dumps(missing, indent=2)}"


def test_help_strings_are_short_enough_to_sit_under_a_field():
    """A help line is a hint, not documentation — the docs link covers depth.

    Long strings wrap to three or four lines under a field and turn the editor
    into a wall of prose, which is how people stop reading them.
    """
    too_long: list[str] = []
    for block, indent in (("SERVER_HELP", 2), ("NODE_HELP", 2), ("SERVICE_HELP", 4)):
        pat = rf'^ {{{indent}}}"?([\w.]+)"?:\s*"((?:[^"\\]|\\.)*)"'
        for key, text in re.findall(pat, _js_object(block), re.MULTILINE):
            if len(text) > 200:
                too_long.append(f"{block}.{key} ({len(text)} chars)")
    assert not too_long, f"Help strings should stay under ~200 chars: {too_long}"


def test_every_editable_server_key_has_a_code_default():
    """The Settings grid shows "inherit (<default>)" — but `inherited` is read
    from the user's OWN server.yaml, so a config file older than the feature has
    nothing to inherit and the UI falls back to settings.js's CODE_DEFAULTS.

    A key missing from that map renders as "unset" for a setting that plainly
    has a value in effect. This map has silently drifted twice already
    (occupancy + fleet in 5.0.0, proactive in 5.0.6), which is why it's pinned:
    nothing else notices, because nothing fails.
    """
    import re

    from kenzy.server.server import _SERVER_EDITABLE

    src = (_STATIC / "js/views/settings.js").read_text()
    block = re.search(r"const CODE_DEFAULTS = \{(.*?)\n\};", src, re.S)
    assert block, "CODE_DEFAULTS map not found in settings.js"
    known = set(re.findall(r'^\s*"?([A-Za-z_][\w.]*)"?\s*:', block.group(1), re.MULTILINE))

    # Service URLs are exempt as a RULE, not a list, so adding a service can't
    # quietly re-open the hole: they have no static default because a blank one
    # means "find it by auto-registration", and "unset" is the honest label for
    # a value that is discovered at runtime rather than defaulted.
    editable = {k for k in _SERVER_EDITABLE if not k.endswith(".url")}
    missing = sorted(editable - known)
    assert not missing, (
        "Editable server keys with no CODE_DEFAULTS entry — the Settings grid "
        'will show "unset" instead of the value actually in effect: ' + ", ".join(missing)
    )


def test_every_editable_node_key_shows_a_real_default():
    """The node editor shows "inherit (<default>)" from the effective config, and
    falls back to config.js's DEFAULTS when the inherited layer doesn't carry the
    key. A key in NEITHER renders as the bare word "default", which tells an
    operator nothing about what is actually in effect.

    Same drift as CODE_DEFAULTS on the server side: a hand-maintained map with a
    "keep in sync with node/client.py" comment and nothing enforcing it.
    """
    import re

    import yaml

    from kenzy.server.server import _ALLOWED_OVERRIDE_KEYS

    packaged = yaml.safe_load((_CONFIGS / "server.yaml").read_text())
    inherited = set((packaged.get("node_defaults") or {}).keys())

    js = (_STATIC / "js/views/config.js").read_text()
    defaults_blk = re.search(r"const DEFAULTS = \{(.*?)\n\};", js, re.S)
    ranges_blk = re.search(r"const RANGES = \{(.*?)\};", js, re.S)
    assert defaults_blk and ranges_blk, "DEFAULTS/RANGES not found in config.js"
    known = set(re.findall(r'^\s*"?([A-Za-z_][\w.]*)"?\s*:', defaults_blk.group(1), re.MULTILINE))
    known |= set(re.findall(r"(\w+):\s*\{", ranges_blk.group(1)))

    # Exempt because their default is genuinely DYNAMIC, not a value anyone
    # could type: the audio device falls back to whatever the OS calls default,
    # and an empty wakeword list falls back to the bundled model paths. Naming
    # them here is deliberate — a silent gap is what this test exists to stop.
    # (mic_volume is NOT here: its DEFAULTS entry is the display phrase
    # "device default", which is the honest rendering of "untouched".)
    dynamic = {"audio_device", "wakeword_models"}

    missing = sorted(set(_ALLOWED_OVERRIDE_KEYS) - inherited - known - dynamic)
    assert not missing, (
        'Editable node keys that render as a bare "default" — add the real value '
        "to config.js DEFAULTS (or node_defaults in server.yaml): " + ", ".join(missing)
    )
