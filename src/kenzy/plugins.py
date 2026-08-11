"""Plugin discovery and the load gate (5.1 seam).

A plugin is a **separate Python distribution** (e.g. ``kenzy-ld2450``) that
registers an entry point in a ``kenzy.plugins.v<N>`` group, where ``N`` is the
plugin-API version it was written against. Installed, it adds capability; not
installed, it has no effect at all — no config keys, no nav entries, no cost.
(A pip extra cannot deliver that: extras gate dependencies, not code.)

The group name carries the API version so the gate can refuse an incompatible
plugin **without importing it** — an api-2 plugin may use imports or syntax
this core can't survive, so "load it and see" is not an option. The manifest
repeats the number (``api=1``) and the two are cross-checked; the pip
dependency range (``kenzy>=5.1,<6``) is what the resolver sees, but it is a
prediction the plugin author made about the future — the integer is something
core can actually verify.

Every failure is **per-plugin and fail-closed**: an incompatible API, a module
that throws on import, a manifest that doesn't validate — each becomes a
:class:`PluginFault` carried in the scan result so the dashboard can say
*why* ("installed · incompatible — needs a newer kenzy"), and the service
keeps running. A plugin must never take the server or a node down.

The entry point resolves to a **module** that defines:

- ``MANIFEST`` — a :class:`PluginManifest` (required).
- ``server_start(ctx)`` — async; called once at server startup (``server`` role).
- ``on_plugin_frame(ctx, node_id, payload)`` — async; a ``plugin_event`` frame
  arrived from a node (``server`` role).
- ``node_run(ctx)`` — async; runs as a task beside the node's loops for the
  node's lifetime (``node`` role). Failure is non-fatal, same philosophy as
  ``_init_audio``.

Hooks are looked up by convention (:meth:`LoadedPlugin.hook`), so the manifest
stays a frozen data object and a plugin only defines what its roles need.
"""

from __future__ import annotations

import importlib.metadata
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("kenzy.plugins")

#: Entry-point group prefix; the suffix is the plugin-API version (`…v1`).
GROUP_PREFIX = "kenzy.plugins.v"

#: Plugin-API versions this core speaks. Grows only on a breaking contract
#: change; every stale plugin then degrades honestly at the gate.
SUPPORTED_APIS: tuple[int, ...] = (1,)

#: Roles a plugin may declare — which service halves it plugs into.
ROLES = ("node", "server")

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")

# Config keys whose NAME matches the server's secret filter are silently
# deleted from every served config (the 5.0.4 volume_buttons trap) — refuse
# them at the gate so a plugin key can't be born inert. Deliberately duplicated
# from kenzy.server (same pattern as the tier constants in kenzy.llm.skills:
# wire contract, no server import from a node-side module).
_SECRET_KEY_RE = re.compile(r"key|token|secret|password|passwd|credential", re.IGNORECASE)


@dataclass(frozen=True)
class PluginManifest:
    """What a plugin declares about itself. A pure data object — hooks live as
    module-level functions, found by convention."""

    #: Short stable slug (``ld2450``) — keys config files, panel URLs, frames.
    id: str
    #: Human name for the Addons nav entry and management card.
    label: str
    #: Plugin-API version — must match the entry-point group's number.
    api: int
    #: Which halves this distribution carries: ``("node",)``, ``("server",)``
    #: or both.
    roles: tuple[str, ...]
    #: Nav icon (single glyph, same convention as the core NAV).
    ico: str = "◈"
    #: Absolute path to a directory of plain-ESM panel files, served at
    #: ``/addons/<id>/`` (``server`` role only). The plugin resolves it itself,
    #: e.g. ``Path(__file__).parent / "panel"``; panels load ONLY from
    #: installed package data — never from the config home.
    panel_dir: Path | None = None
    #: Entry module within ``panel_dir`` (lazy-imported by the shell).
    panel_entry: str = "panel.js"
    #: Server-half config defaults, deep-merged under whatever
    #: ``configs/addons/<id>.yaml`` holds.
    config_defaults: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NodePluginContext:
    """Everything a node-half plugin gets. Deliberately tiny (a run task, its
    config, a frame-send outlet) — grows only when a second plugin demands it."""

    node_id: str
    #: This plugin's slice of the node's effective config (``addons.<id>``),
    #: server-owned like every other node key. Empty dict when unconfigured.
    config: dict[str, Any]
    #: Send a ``plugin_event`` frame to this plugin's server half. Best-effort:
    #: dropped (not queued) while disconnected — stale sensor events arriving
    #: on reconnect would be worse than lost ones.
    send_event: Callable[[dict[str, Any]], Any]
    log: logging.Logger


@dataclass(frozen=True)
class ServerPluginContext:
    """Everything a server-half plugin gets: its config plus the seams that
    already exist (occupancy evidence, the integrations hub). ``None`` members
    mean that subsystem isn't running — a plugin must tolerate both."""

    #: Deep merge of manifest ``config_defaults`` ← ``configs/addons/<id>.yaml``.
    config: dict[str, Any]
    #: The occupancy tracker, when the spine is running (evidence injection).
    occupancy: Any | None
    #: The integrations hub, when enabled (publish to MQTT/HA Discovery).
    integrations: Any | None
    log: logging.Logger
    #: The room NAME a connected node is in ("" when unknown/disconnected) —
    #: added for ld2450, whose server half must place a node's evidence in a
    #: room. First real hook-surface growth; the second customer decides the
    #: next one.
    room_of: Callable[[str], str] = lambda _node_id: ""
    #: Send a ``plugin_event`` to this plugin's node half on a connected node
    #: (awaitable → bool: delivered). None on cores that predate it. Added for
    #: ld2450's on-demand target streaming; 5.2's "take a snapshot" is the
    #: same shape. The node half receives it via an ``on_server_event(ctx,
    #: payload)`` module hook. API-skewed nodes are refused at the sender —
    #: the same rule as the inbound gate, in the other direction.
    send_to_node: Callable[[str, dict[str, Any]], Any] | None = None


@dataclass(frozen=True)
class PluginFault:
    """A plugin that is installed but could not be loaded — kept, not dropped,
    so the failure is visible with its reason instead of silently absent."""

    dist: str
    version: str
    kind: str  # "incompatible" | "import-error" | "bad-manifest" | "duplicate"
    error: str
    api: int | None = None


@dataclass(frozen=True)
class LoadedPlugin:
    manifest: PluginManifest
    module: Any
    dist: str
    version: str

    def hook(self, name: str) -> Callable[..., Any] | None:
        """A module-level hook function, or None when the plugin doesn't
        define it (a node-only plugin has no ``server_start``, etc.)."""
        fn = getattr(self.module, name, None)
        return fn if callable(fn) else None


@dataclass(frozen=True)
class PluginScan:
    loaded: tuple[LoadedPlugin, ...] = ()
    faults: tuple[PluginFault, ...] = ()

    def for_role(self, role: str) -> tuple[LoadedPlugin, ...]:
        return tuple(p for p in self.loaded if role in p.manifest.roles)

    def get(self, plugin_id: str) -> LoadedPlugin | None:
        return next((p for p in self.loaded if p.manifest.id == plugin_id), None)


def _dist_info(ep: Any) -> tuple[str, str]:
    """Best-effort distribution name/version for an entry point (for the fault
    report — never worth failing over)."""
    dist = getattr(ep, "dist", None)
    if dist is not None:
        return str(getattr(dist, "name", "?")), str(getattr(dist, "version", "?"))
    return str(getattr(ep, "name", "?")), "?"


def _validate_manifest(manifest: Any, group_api: int) -> str:
    """Why this manifest is unusable, or '' when it's fine."""
    if not isinstance(manifest, PluginManifest):
        return "MANIFEST is not a PluginManifest"
    if not _ID_RE.match(manifest.id):
        return f"id {manifest.id!r} is not a valid slug"
    if manifest.api != group_api:
        return f"manifest api={manifest.api} but registered in the v{group_api} group"
    if not manifest.roles or any(r not in ROLES for r in manifest.roles):
        return f"roles {manifest.roles!r} must be a non-empty subset of {ROLES}"
    if manifest.panel_dir is not None and not Path(manifest.panel_dir).is_dir():
        return f"panel_dir {manifest.panel_dir} is not a directory"
    bad = [k for k in manifest.config_defaults if _SECRET_KEY_RE.search(k)]
    if bad:
        # The server strips secret-looking key NAMES from every served config;
        # a default named like one would be silently deleted downstream.
        return f"config default key(s) {bad} match the secret-name filter — rename them"
    return ""


def _entry_points() -> list[Any]:
    # .groups + .select work on both 3.11's SelectableGroups and 3.12+'s
    # EntryPoints; iterating the container directly does NOT (on 3.11 it yields
    # group-name strings via a deprecated dict interface, silently matching
    # nothing).
    eps = importlib.metadata.entry_points()
    found: list[Any] = []
    for group in eps.groups:
        if group.startswith(GROUP_PREFIX):
            found.extend(eps.select(group=group))
    return found


# A pip-installable distribution name (PEP 503-ish). Plugin dist names ride
# pip argv during joint upgrades, so anything weirder is dropped, not passed.
_DIST_NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,62}[A-Za-z0-9])?$")


def installed_plugin_dists(eps: list[Any] | None = None) -> list[str]:
    """Distribution names of every installed plugin — INCLUDING ones the load
    gate refuses. Joint upgrades (the version-skew defense) must move the whole
    set together: pip alone would either strand an incompatible pair with a
    warning nobody reads, or silently drag core forward to satisfy a plugin.
    An api-incompatible plugin especially must ride along — upgrading core
    without it would freeze the very skew the gate is reporting."""
    if eps is None:
        eps = _entry_points()
    names: list[str] = []
    for ep in eps:
        dist, _ = _dist_info(ep)
        if dist not in names and _DIST_NAME_RE.match(dist):
            names.append(dist)
    return sorted(names)


def scan_plugins(eps: list[Any] | None = None) -> PluginScan:
    """Discover installed plugins and gate them by API version.

    ``eps`` overrides the real entry-point scan (tests). Each candidate ends up
    either in ``loaded`` or in ``faults`` with a stated reason — never silently
    dropped, never allowed to raise out of the scan.
    """
    if eps is None:
        eps = _entry_points()

    loaded: list[LoadedPlugin] = []
    faults: list[PluginFault] = []
    seen_ids: dict[str, str] = {}  # plugin id → dist that claimed it first

    for ep in eps:
        dist, version = _dist_info(ep)
        group = str(getattr(ep, "group", ""))
        try:
            api = int(group[len(GROUP_PREFIX) :])
        except ValueError:
            faults.append(
                PluginFault(dist, version, "bad-manifest", f"unparseable group {group!r}")
            )
            continue

        if api not in SUPPORTED_APIS:
            # Refused WITHOUT import — an incompatible plugin's code may not
            # even survive being imported under this core.
            faults.append(
                PluginFault(
                    dist,
                    version,
                    "incompatible",
                    f"plugin API v{api}; this kenzy speaks "
                    f"{', '.join(f'v{a}' for a in SUPPORTED_APIS)}",
                    api=api,
                )
            )
            continue

        try:
            module = ep.load()
        except Exception as exc:  # per-plugin fail-closed: never up the stack
            log.warning("Plugin %s failed to import: %s", dist, exc, exc_info=True)
            faults.append(PluginFault(dist, version, "import-error", str(exc), api=api))
            continue

        manifest = getattr(module, "MANIFEST", None)
        why = _validate_manifest(manifest, api)
        if why:
            faults.append(PluginFault(dist, version, "bad-manifest", why, api=api))
            continue
        assert isinstance(manifest, PluginManifest)  # narrowed by _validate_manifest

        if manifest.id in seen_ids:
            faults.append(
                PluginFault(
                    dist,
                    version,
                    "duplicate",
                    f"id {manifest.id!r} already provided by {seen_ids[manifest.id]}",
                    api=api,
                )
            )
            continue
        seen_ids[manifest.id] = dist

        loaded.append(LoadedPlugin(manifest=manifest, module=module, dist=dist, version=version))
        log.info("Plugin loaded: %s (%s %s, roles=%s)", manifest.id, dist, version, manifest.roles)

    for fault in faults:
        log.warning(
            "Plugin NOT loaded: %s %s — %s: %s", fault.dist, fault.version, fault.kind, fault.error
        )
    return PluginScan(loaded=tuple(loaded), faults=tuple(faults))
