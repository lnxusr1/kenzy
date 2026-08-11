"""The sample plugin — the plugin contract, pinned by the test suite.

This is the reference implementation of every hook a plugin can define, kept
deliberately trivial so a change that breaks it is a change to the CONTRACT
(and shows up as a failing seam test), never a change to plugin logic. It is
also installable (see its pyproject) so the install matrix can prove the whole
chain — entry point → gate → panel served → frame routed — on a real install.

Observable on purpose: the module-level lists record what the hooks received,
which is what lets tests assert the capability rather than the plumbing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from kenzy.plugins import NodePluginContext, PluginManifest, ServerPluginContext

MANIFEST = PluginManifest(
    id="sample",
    label="Sample",
    api=1,
    roles=("node", "server"),
    ico="✚",
    panel_dir=Path(__file__).parent / "panel",
    config_defaults={"greeting": "hello", "interval_s": 3600},
)

#: server_start appends the config it ran with.
STARTED: list[dict[str, Any]] = []
#: on_plugin_frame appends (node_id, payload) per routed frame.
RECEIVED: list[tuple[str, dict[str, Any]]] = []
#: on_server_event (node half) appends each payload the server half sent.
SERVER_EVENTS: list[dict[str, Any]] = []


async def on_server_event(ctx: NodePluginContext, payload: dict[str, Any]) -> None:
    SERVER_EVENTS.append(dict(payload))


async def server_start(ctx: ServerPluginContext) -> None:
    STARTED.append(dict(ctx.config))


async def on_plugin_frame(ctx: ServerPluginContext, node_id: str, payload: dict[str, Any]) -> None:
    RECEIVED.append((node_id, dict(payload)))


async def panel_state(ctx: ServerPluginContext, query: dict[str, str]) -> dict[str, Any]:
    # The panel data path: GET /api/addons/sample/state answers with this.
    # room_of and the query string ride along so both seams are pinned
    # end-to-end (the query is how a panel scopes what it's asking for).
    return {
        "greeting": ctx.config.get("greeting"),
        "room_of_n1": ctx.room_of("n1"),
        "query": query,
    }


async def node_run(ctx: NodePluginContext) -> None:
    # Announce immediately (the round-trip the tests and the install matrix
    # assert), then idle — a real driver would poll its hardware here.
    await ctx.send_event({"kind": "hello", "config": dict(ctx.config)})
    while True:
        await asyncio.sleep(float(ctx.config.get("interval_s", 3600)))
