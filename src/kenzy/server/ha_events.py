"""Home Assistant event subscription — the v5 occupancy spine, Slice A.

The hose, the filter, and the seam. This module knows about *entities and
rooms*; it never believes anything about the world — that is the tracker's job
(:mod:`kenzy.server.occupancy`). Keeping the boundary sharp is what lets the
connection lifecycle be debugged before any semantics exist to confuse it.

Why the server and not kenzy-llm
--------------------------------
The tracker has three feeds and only one is movable: voice identity is resolved
in the server pipeline before the llm sees a request, and in-node mmWave (5.0.x)
arrives over the node WebSocket, which terminates here. Only the HA socket could
move — and putting it in the llm would mean forwarding voice events out AND
reading the world model back on the server's speech hot path. See
``kenzy-design/app/v5-aware-era.md`` for the full argument; it rests on one
assumption (5.0's proactive gate stays rule-based, so awareness must not inherit
the llm's uptime or a model provider's).

What crosses the service boundary
---------------------------------
Only the **map** — ``entity_id -> {room, kind, scope}`` — fetched from
kenzy-llm's ``GET /ha/map``, because the area knowledge and curation baking live
in ``ha_model.py`` and shouldn't be duplicated. Config does NOT cross: the
server already holds ``HA_API_KEY`` (its own ``.env``) and the HA URL (its
central store, ``_effective_service_config("llm")``). The fast event stream is
consumed entirely here; events never reach the llm.

Evidence kinds
--------------
Normalization tags every event **pulse** or **level**, and the distinction is
load-bearing (see :mod:`kenzy.server.occupancy`): a PIR *pulses* (fires on
motion, silent while you sit still) but mmWave still-presence and ``person.*``
home/away *assert a level* — a healthy sensor still saying "occupied" must never
drain toward unknown just because time passed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: Reconnect backoff (seconds): quick first retry, then ease off. HA restarting
#: for an update is the common case and comes back inside a minute.
_BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)

#: How long the map is trusted before a refresh is attempted. Topology moves at
#: the speed of furniture; a curation save pokes a refresh immediately, so this
#: is only the backstop.
_MAP_TTL_S = 900.0

#: HA WS protocol timeouts.
_AUTH_TIMEOUT_S = 15.0
_PING_INTERVAL_S = 30.0

#: When the HA integration is switched OFF we stop trying rather than backing
#: off — a deliberately-disabled integration must not log a warning forever. We
#: still re-check on this slow beat so re-enabling recovers without a restart,
#: and the dashboard pokes us the moment the toggle flips (see wake()).
_DISABLED_RECHECK_S = 300.0

#: A socket that has produced nothing for this long is reported STALE, so the
#: tracker can say "I don't know" instead of serving a frozen picture. Level
#: evidence is only trustworthy while the socket is healthy.
_STALE_AFTER_S = 120.0

Subscriber = Callable[["Evidence"], None]


class IntegrationDisabled(RuntimeError):
    """The Home Assistant integration is switched off — stop, don't retry.

    Distinct from a transient failure ON PURPOSE: a failure deserves backoff and
    a warning, but an operator who turned the integration off deserves silence.
    """


@dataclass(frozen=True)
class Evidence:
    """One normalized occupancy observation. The seam's only currency."""

    entity_id: str
    #: Kenzy room slug, or "" for house-scope evidence (``person.*``).
    room: str
    #: "pulse" (spike then decay) or "level" (assert until released).
    kind: str
    #: "room" or "house".
    scope: str
    #: True = evidence FOR presence (motion detected / home), False = against.
    present: bool
    ts: float
    #: Display name for house-scope evidence (the person's HA name).
    name: str = ""
    #: False when the entity stopped reporting (``unavailable``/``unknown``, or a
    #: deletion's null new_state). Only ever emitted for a LEVEL, and it is not a
    #: claim about the room — it tells the tracker to stop trusting an assertion
    #: that will otherwise never be released. See occupancy._drop_hold.
    available: bool = True


@dataclass
class HaEventStats:
    """Observability for Slice A — verifiable before any consumer exists."""

    connected: bool = False
    connects: int = 0
    last_event_at: float = 0.0
    last_connect_at: float = 0.0
    received: int = 0  # raw state_changed events off the wire
    emitted: int = 0  # survived the filter
    dropped: int = 0  # filtered out (not in the map)
    map_entities: int = 0
    map_fetched_at: float = 0.0
    last_error: str = ""
    #: The integration is switched off (not broken) — the socket is parked.
    disabled: bool = False

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        age = (now - self.last_event_at) if self.last_event_at else None
        return {
            "connected": self.connected,
            "disabled": self.disabled,
            "stale": self.is_stale(now),
            "connects": self.connects,
            "received": self.received,
            "emitted": self.emitted,
            "dropped": self.dropped,
            "map_entities": self.map_entities,
            "last_event_age": round(age, 1) if age is not None else None,
            "last_error": self.last_error,
        }

    def is_stale(self, now: float | None = None) -> bool:
        """Not connected, or connected but silent past the staleness window."""
        if not self.connected:
            return True
        now = time.monotonic() if now is None else now
        ref = self.last_event_at or self.last_connect_at
        return bool(ref) and (now - ref) > _STALE_AFTER_S


def normalize(
    entity_id: str,
    state: str,
    entry: dict[str, Any],
    *,
    ts: float | None = None,
) -> Evidence | None:
    """Map an HA state to :class:`Evidence` using the map entry, or None.

    Pure: the filter's decision is "is this entity in the map", which is where
    curation's occupancy block (and the never-configurable ``kenzy_*`` self-echo
    rule) already landed — baked in llm-side so this stays dumb.
    """
    kind = str(entry.get("kind") or "")
    scope = str(entry.get("scope") or "room")
    if kind not in ("pulse", "level"):
        return None
    value = (state or "").strip().lower()
    if value in ("unavailable", "unknown", ""):
        # A dropped sensor is not evidence of absence, so a PULSE is simply
        # ignored — its belief already decays on its own. A LEVEL is different:
        # it may be mid-assertion, and this is the ONLY signal that it has
        # stopped talking (a flat battery reads `unavailable`; a deleted entity
        # arrives as a null new_state, i.e. ""). Dropping it here is what pinned
        # a room "occupied" at full confidence until the server restarted.
        if kind != "level":
            return None
        return Evidence(
            entity_id=entity_id,
            room=str(entry.get("room") or ""),
            kind=kind,
            scope=scope,
            present=False,
            ts=time.monotonic() if ts is None else ts,
            name=str(entry.get("name") or ""),
            available=False,
        )
    if scope == "house":
        present = value == "home"
    else:
        present = value == "on"
    return Evidence(
        entity_id=entity_id,
        room=str(entry.get("room") or ""),
        kind=kind,
        scope=scope,
        present=present,
        ts=time.monotonic() if ts is None else ts,
        name=str(entry.get("name") or ""),
    )


class HaEventClient:
    """Persistent HA ``/api/websocket`` subscription: hose + filter + seam.

    Zero-overhead contract: nothing is constructed unless occupancy is enabled
    AND Home Assistant is configured, and with no subscribers the emit path is a
    list check.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        map_fetcher: Callable[[], Any],
        *,
        on_map: Callable[[set[str]], None] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._fetch_map = map_fetcher
        #: Called with the map's entity ids after every successful refetch. The
        #: tracker uses it to fade out holds from entities the map no longer
        #: covers — A still believes nothing, it just says what it can see.
        self._on_map = on_map
        self._map: dict[str, dict[str, Any]] = {}
        self._subscribers: list[Subscriber] = []
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        #: Set by wake() to cut short a wait — used when the operator re-enables
        #: the integration, so recovery is immediate instead of up to 5 minutes.
        self._wake = asyncio.Event()
        self._msg_id = 1
        self.stats = HaEventStats()

    # -- seam ---------------------------------------------------------------

    def subscribe(self, fn: Subscriber) -> None:
        self._subscribers.append(fn)

    def unsubscribe(self, fn: Subscriber) -> None:
        if fn in self._subscribers:
            self._subscribers.remove(fn)

    def _emit(self, ev: Evidence) -> None:
        for fn in list(self._subscribers):
            try:
                fn(ev)
            except Exception:  # a consumer must never break the socket
                log.debug("occupancy subscriber error", exc_info=True)

    # -- lifecycle ----------------------------------------------------------

    def wake(self) -> None:
        """Re-evaluate now — the HA integration was just toggled.

        Interrupts a LIVE session too, not only a wait: a connected socket
        refetches the map on reconnect or after _MAP_TTL_S, so without this a
        just-disabled integration would keep streaming for up to 15 minutes.
        Marking the map stale is what makes the re-evaluation actually re-ask.
        """
        self.stats.map_fetched_at = 0.0
        self._wake.set()

    async def _sleep(self, seconds: float) -> None:
        """Wait, cut short by stop() or wake().

        Deliberately does NOT clear the wake flag: a poke that lands just before
        we park (the operator toggling while a session attempt is in flight)
        must survive into this wait, or it is swallowed and recovery takes the
        full re-check interval. The flag is cleared at the top of each loop
        iteration instead — "we are acting on the current state now".
        """
        stop = asyncio.create_task(self._stop.wait())
        wake = asyncio.create_task(self._wake.wait())
        try:
            await asyncio.wait({stop, wake}, timeout=seconds,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (stop, wake):
                t.cancel()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="ha-events")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.stats.connected = False

    @property
    def rooms(self) -> set[str]:
        """Room slugs the map can produce evidence for (the Presence view uses this)."""
        return {str(e.get("room")) for e in self._map.values() if e.get("room")}

    async def refresh_map(self) -> bool:
        """Refetch the entity→room map. Called at connect and on curation saves.

        The fetcher returns kenzy-llm's whole ``/ha/map`` envelope so a switched
        -off integration is distinguishable from a broken one.
        """
        try:
            payload = await self._fetch_map()
        except Exception as exc:
            log.warning("Occupancy map fetch failed: %s", exc)
            self.stats.last_error = f"map: {exc}"
            return False
        if not isinstance(payload, dict):
            return False
        if payload.get("disabled"):
            self.stats.disabled = True
            raise IntegrationDisabled(str(payload.get("error") or "integration disabled"))
        self.stats.disabled = False
        entities = payload.get("entities")
        if not isinstance(entities, dict):
            return False
        self._map = entities
        self.stats.map_entities = len(entities)
        self.stats.map_fetched_at = time.monotonic()
        if self._on_map is not None:
            try:
                self._on_map(set(entities))
            except Exception:  # a consumer must never break the socket
                log.debug("occupancy map listener error", exc_info=True)
        log.info("Occupancy map: %d evidence entities across %d room(s)",
                 len(entities), len(self.rooms))
        return True

    # -- the socket ---------------------------------------------------------

    async def _run(self) -> None:
        attempt = 0
        was_disabled = False
        while not self._stop.is_set():
            self._wake.clear()  # about to act on current state; see _sleep()
            try:
                await self._session()
                attempt = 0  # a clean session resets the backoff
            except asyncio.CancelledError:
                raise
            except IntegrationDisabled as exc:
                # Switched off, not broken: park quietly. Logged once per
                # transition, not once per retry.
                if not was_disabled:
                    log.info("Occupancy paused: %s", exc)
                was_disabled = True
                self.stats.connected = False
                self.stats.last_error = ""
                await self._sleep(_DISABLED_RECHECK_S)
                continue
            except Exception as exc:
                self.stats.last_error = str(exc)
                log.warning("HA event socket: %s", exc)
            finally:
                self.stats.connected = False
            if was_disabled:
                log.info("Occupancy resumed: Home Assistant integration is back on")
                was_disabled = False
            if self._stop.is_set():
                break
            delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
            attempt += 1
            log.info("HA event socket reconnecting in %.0fs", delay)
            await self._sleep(delay)

    async def _session(self) -> None:
        import websockets

        from kenzy import tlsutil

        url = self._ws_url()
        # The map must be in hand BEFORE events flow. An empty map is not merely
        # useless — everything would be filtered out while the socket reported
        # "connected", which reads as a working system that simply sees no
        # sensors. Refuse to subscribe and let the backoff retry the map instead;
        # kenzy-llm being slow to start is the ordinary cause.
        if not self._map or (time.monotonic() - self.stats.map_fetched_at) > _MAP_TTL_S:
            await self.refresh_map()
        if not self._map:
            raise RuntimeError("occupancy map unavailable (is kenzy-llm up?)")

        ssl_ctx = tlsutil.httpx_verify() if url.startswith("wss://") else None
        kwargs: dict[str, Any] = {"ping_interval": _PING_INTERVAL_S, "max_size": 4 * 1024 * 1024}
        if isinstance(ssl_ctx, bool):
            ssl_ctx = None  # httpx-style verify flag isn't an ssl context
        if ssl_ctx is not None:
            kwargs["ssl"] = ssl_ctx
        async with websockets.connect(url, **kwargs) as ws:
            await self._authenticate(ws)
            self.stats.connected = True
            self.stats.connects += 1
            self.stats.last_connect_at = time.monotonic()
            self.stats.last_error = ""
            log.info("HA event socket connected (%s)", self._base)
            # Seed BEFORE subscribing: level state (person home/away, mmWave
            # occupied) only emits on CHANGE, so without this a restart would
            # read "unknown" for an all-day-home household all day. This is what
            # makes persisting the tracker unnecessary.
            await self._seed(ws)
            await self._subscribe(ws)
            await self._consume(ws)

    def _ws_url(self) -> str:
        base = self._base
        if base.startswith("https://"):
            return "wss://" + base[len("https://") :] + "/api/websocket"
        if base.startswith("http://"):
            return "ws://" + base[len("http://") :] + "/api/websocket"
        return "ws://" + base + "/api/websocket"

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def _authenticate(self, ws: Any) -> None:
        """HA's handshake: auth_required → auth → auth_ok."""
        raw = await asyncio.wait_for(ws.recv(), timeout=_AUTH_TIMEOUT_S)
        hello = json.loads(raw)
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"unexpected HA greeting: {hello.get('type')!r}")
        await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        raw = await asyncio.wait_for(ws.recv(), timeout=_AUTH_TIMEOUT_S)
        reply = json.loads(raw)
        if reply.get("type") != "auth_ok":
            raise RuntimeError(f"HA auth failed: {reply.get('message') or reply.get('type')}")

    async def _seed(self, ws: Any) -> None:
        """`get_states` once per connect — re-establishes LEVEL evidence."""
        req_id = self._next_id()
        await ws.send(json.dumps({"id": req_id, "type": "get_states"}))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=_AUTH_TIMEOUT_S)
            msg = json.loads(raw)
            if msg.get("id") != req_id or msg.get("type") != "result":
                continue
            if not msg.get("success"):
                log.warning("HA get_states seed failed; level state starts unknown")
                return
            states = msg.get("result") or []
            seeded = 0
            for state in states:
                ev = self._evidence_from(
                    str(state.get("entity_id") or ""), str(state.get("state") or "")
                )
                if ev is not None:
                    self._emit(ev)
                    seeded += 1
            log.info("HA seed: %d occupancy entities re-established", seeded)
            return

    async def _subscribe(self, ws: Any) -> None:
        req_id = self._next_id()
        await ws.send(
            json.dumps({"id": req_id, "type": "subscribe_events", "event_type": "state_changed"})
        )

    async def _consume(self, ws: Any) -> None:
        async for raw in ws:
            if self._stop.is_set() or self._wake.is_set():
                return  # re-evaluate: stopping, or the integration was toggled
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("type") != "event":
                continue
            data = ((msg.get("event") or {}).get("data")) or {}
            entity_id = str(data.get("entity_id") or "")
            new_state = data.get("new_state") or {}
            self.stats.received += 1
            self.stats.last_event_at = time.monotonic()
            ev = self._evidence_from(entity_id, str(new_state.get("state") or ""))
            if ev is None:
                self.stats.dropped += 1
                continue
            self.stats.emitted += 1
            self._emit(ev)

    def _evidence_from(self, entity_id: str, state: str) -> Evidence | None:
        """Filter (map membership) + normalize. The whole edge, in one place."""
        entry = self._map.get(entity_id)
        if entry is None:
            return None
        return normalize(entity_id, state, entry)
