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

    The service list is derived from kenzy.config.SERVICES, not hand-listed —
    a hand-list here was a third allowlist to forget when adding a service,
    and kenzy-s2s did exactly that: its card shipped with zero help strings
    while this test stayed green (found by the founder, 2026-08-28).
    """
    from kenzy.config import SERVICES

    documented = _service_help()
    missing: dict[str, list[str]] = {}
    for service in sorted(set(SERVICES) - {"node", "server"}):
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
        src = _js_object(block)
        pat = rf'^ {{{indent}}}"?([\w.]+)"?:\s*"((?:[^"\\]|\\.)*)"'
        for key, text in re.findall(pat, src, re.MULTILINE):
            if len(text) > 200:
                too_long.append(f"{block}.{key} ({len(text)} chars)")
        # An entry wrapped onto its own line (prettier-style) is invisible to
        # the pattern above — the exact silent gap this test exists to stop.
        wrapped = re.findall(rf'^ {{{indent}}}"?([\w.]+)"?:\s*$', src, re.MULTILINE)
        assert not wrapped, (
            f"{block} entries must keep key and string on one line so this "
            f"gate can measure them: {wrapped}"
        )
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

    # Exempt because its default is genuinely DYNAMIC, not a value anyone
    # could type: an empty wakeword list falls back to the bundled model
    # paths. Naming it here is deliberate — a silent gap is what this test
    # exists to stop. (audio_device is NOT here: its DEFAULTS entry is the
    # display phrase "OS default device", the honest rendering of "whatever
    # the OS picks".)
    dynamic = {"wakeword_models"}

    # Exempt because their unset state genuinely IS blank — no group, gain
    # untouched — and a placeholder phrase there read like a value someone
    # set (founder call 2026-08-28). Must match config.js's BLANK_UNSET; the
    # help strings carry the meaning ("Unset = ...") instead.
    blank_re = re.search(r"const BLANK_UNSET = new Set\(\[(.*?)\]\)", js, re.S)
    assert blank_re, "BLANK_UNSET set not found in config.js"
    blank = set(re.findall(r'"(\w+)"', blank_re.group(1)))
    assert blank == {"audio_group", "mic_volume"}, (
        "BLANK_UNSET changed — a key added there stops showing any default "
        "hint, which is only honest when unset truly means blank AND the "
        "help string says so. Update this test deliberately: " + ", ".join(sorted(blank))
    )
    schema = _SCHEMA.read_text()
    for k in blank:
        m = re.search(rf'\b{k}: "([^"]+)"', schema)
        assert m and "Unset" in m.group(1), (
            f"{k} has a blank placeholder, so its help string must say what "
            'unset means ("Unset = ...") — that sentence is the only default '
            "hint the field has left"
        )

    missing = sorted(set(_ALLOWED_OVERRIDE_KEYS) - inherited - known - dynamic - blank)
    assert not missing, (
        'Editable node keys that render as a bare "default" — add the real value '
        "to config.js DEFAULTS (or node_defaults in server.yaml): " + ", ".join(missing)
    )


def test_device_keys_render_as_pickers():
    """The device keys on the node card offer the node's own probe results as
    dropdowns (founder catch 2026-08-27: the wizard picked devices from a
    list while the grid asked for hand-typed names with no hint of what a
    valid value even looks like).

    The options must come from exactly what the wizard writes, so the grid
    and the wizard can never save different classes of value:
    ``volume_button_device`` from the probe's ``volume_key_device``
    annotations (a stable endpoint NAME — an eventN path would change across
    boots), and ``audio_device`` from ``suggested.audio_device`` (the stable
    short name — an "(hw:N,0)" fragment would too).
    """
    import re

    js = (_STATIC / "js/views/config.js").read_text()
    branch = re.search(r'const probeOpts =(.*?)</select>', js, re.S)
    assert branch, "the device-key picker branch is missing from config.js"
    for key, source in [
        ("volume_button_device", "volume_key_device"),
        ("audio_device", "suggested.audio_device"),
    ]:
        assert f'k === "{key}"' in branch.group(1), f"{key} lost its picker branch"
        assert source in branch.group(1), (
            f"{key}'s picker options must come from the probe's {source} — "
            "the same value the audio wizard writes"
        )


def test_server_enum_s2s_profile_matches_the_python_vocabulary():
    """SERVER_ENUMS is a hand-maintained JS copy of the Python PROFILES map
    (kenzy.s2s.profiles) — the 5.0.6 keep-in-sync trap. Pin it so a profile
    added in Python can't silently never appear in the Settings dropdown.
    'hf' is deliberately operator-hidden (a dev-only probe target).
    """
    import re

    from kenzy.s2s.profiles import PROFILES

    src = _SCHEMA.read_text()
    block = re.search(r'"s2s\.profile":\s*\[([^\]]*)\]', src)
    assert block, "SERVER_ENUMS is missing an s2s.profile entry"
    js_profiles = set(re.findall(r'"([^"]+)"', block.group(1)))

    _HIDDEN = {"hf"}  # dev probe target — never offered in the dashboard
    expected = set(PROFILES) - _HIDDEN
    assert js_profiles == expected, (
        "SERVER_ENUMS['s2s.profile'] drifted from kenzy.s2s.profiles.PROFILES: "
        f"JS has {sorted(js_profiles)}, expected {sorted(expected)} "
        f"(hidden: {sorted(_HIDDEN)}). Update schema.js or _HIDDEN deliberately."
    )
