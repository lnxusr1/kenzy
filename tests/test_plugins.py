"""The plugin load gate (kenzy.plugins).

Every path a pip-installed plugin can take at scan time: loads, refused
without import for a wrong API, faulted (visibly, per-plugin) for an import
error or a bad manifest, and never allowed to raise out of the scan. These
pin the contract the sample plugin and kenzy-ld2450 build against.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

from kenzy import plugins
from kenzy.plugins import PluginManifest, scan_plugins


class _Dist:
    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version


class _EP:
    """A stand-in for importlib.metadata.EntryPoint — group, dist, load()."""

    def __init__(self, group: str, module: Any, dist: str = "kenzy-test", version: str = "1.0.0"):
        self.group = group
        self.name = dist
        self.dist = _Dist(dist, version)
        self._module = module

    def load(self) -> Any:
        if isinstance(self._module, Exception):
            raise self._module
        return self._module


def _mod(manifest: Any, **hooks: Any) -> types.ModuleType:
    mod = types.ModuleType("fake_plugin")
    mod.MANIFEST = manifest  # type: ignore[attr-defined]
    for name, fn in hooks.items():
        setattr(mod, name, fn)
    return mod


def _manifest(**over: Any) -> PluginManifest:
    kw: dict[str, Any] = {"id": "sample", "label": "Sample", "api": 1, "roles": ("server",)}
    kw.update(over)
    return PluginManifest(**kw)


def test_a_valid_plugin_loads_with_its_hooks() -> None:
    async def server_start(ctx: Any) -> None: ...

    scan = scan_plugins([_EP("kenzy.plugins.v1", _mod(_manifest(), server_start=server_start))])
    assert not scan.faults
    (p,) = scan.loaded
    assert p.manifest.id == "sample"
    assert p.hook("server_start") is server_start
    assert p.hook("node_run") is None  # undeclared hook: None, not an error
    assert scan.for_role("server") == (p,)
    assert scan.for_role("node") == ()
    assert scan.get("sample") is p and scan.get("nope") is None


def test_an_unsupported_api_is_refused_without_import() -> None:
    """The gate must not even import an api-N plugin this core doesn't speak —
    its code may not survive import here. load() raising proves import was
    attempted; the fault must be 'incompatible', not 'import-error'."""
    ep = _EP("kenzy.plugins.v99", RuntimeError("must not be imported"), dist="kenzy-future")
    scan = scan_plugins([ep])
    assert not scan.loaded
    (f,) = scan.faults
    assert f.kind == "incompatible" and f.api == 99 and f.dist == "kenzy-future"
    assert "v99" in f.error  # the text says why — the dashboard shows this


def test_an_import_error_faults_that_plugin_only() -> None:
    good = _EP("kenzy.plugins.v1", _mod(_manifest()))
    bad = _EP("kenzy.plugins.v1", ImportError("boom"), dist="kenzy-broken")
    scan = scan_plugins([bad, good])
    assert [p.manifest.id for p in scan.loaded] == ["sample"]
    (f,) = scan.faults
    assert f.kind == "import-error" and f.dist == "kenzy-broken" and "boom" in f.error


def test_bad_manifests_are_faulted_with_the_reason() -> None:
    cases = [
        (_mod(None), "not a PluginManifest"),
        (_mod(_manifest(id="Bad Slug!")), "slug"),
        (_mod(_manifest(api=2)), "v1 group"),  # manifest api disagrees with the group
        (_mod(_manifest(roles=())), "roles"),
        (_mod(_manifest(roles=("llm",))), "roles"),
        (_mod(_manifest(panel_dir=Path("/nonexistent/panel"))), "panel_dir"),
    ]
    for module, needle in cases:
        scan = scan_plugins([_EP("kenzy.plugins.v1", module)])
        assert not scan.loaded, needle
        (f,) = scan.faults
        assert f.kind == "bad-manifest" and needle in f.error


def test_a_secret_looking_config_default_is_refused() -> None:
    """The server strips secret-looking key NAMES from every served config
    (the 5.0.4 volume_buttons trap) — a plugin default named like one would be
    born inert, so the gate refuses it with the rename instruction."""
    module = _mod(_manifest(config_defaults={"api_key": "", "interval": 5}))
    scan = scan_plugins([_EP("kenzy.plugins.v1", module)])
    assert not scan.loaded
    (f,) = scan.faults
    assert f.kind == "bad-manifest" and "api_key" in f.error and "rename" in f.error


def test_duplicate_ids_keep_the_first_and_fault_the_second() -> None:
    a = _EP("kenzy.plugins.v1", _mod(_manifest()), dist="kenzy-a")
    b = _EP("kenzy.plugins.v1", _mod(_manifest()), dist="kenzy-b")
    scan = scan_plugins([a, b])
    (p,) = scan.loaded
    assert p.dist == "kenzy-a"
    (f,) = scan.faults
    assert f.kind == "duplicate" and f.dist == "kenzy-b" and "kenzy-a" in f.error


def test_an_unparseable_group_suffix_is_a_fault_not_a_crash() -> None:
    scan = scan_plugins([_EP("kenzy.plugins.vNaN", _mod(_manifest()))])
    assert not scan.loaded
    assert scan.faults[0].kind == "bad-manifest"


def test_the_real_scan_ignores_unrelated_entry_points() -> None:
    """The default scan walks the live environment: whatever is installed
    there (console scripts, pytest plugins…), nothing outside kenzy.plugins.*
    may load or fault."""
    scan = plugins.scan_plugins()
    for p in scan.loaded:
        assert p.manifest.api in plugins.SUPPORTED_APIS
