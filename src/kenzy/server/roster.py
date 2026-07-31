"""Durable record of the nodes this server knows about.

``AudioServer._nodes`` is a registry of *connections*. When one drops the node
does not become absent — it ceases to exist: gone from the registry, gone from
the dashboard, gone from the fleet count. That is how a four-room house quietly
became a three-room house for two days with nothing, anywhere, saying a word.

This is the other half of the picture: which nodes *exist*, and when each was
last seen. An absent node stays visible and can be escalated, and because the
record outlives the server process, a node that is already missing when the
server restarts is still missing afterwards rather than being forgotten.

Deliberately a small JSON file with an atomic rewrite — the same shape as
``schedules.json``: readable, greppable, and it rides the backup slice.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class RosterEntry:
    """One node the server has seen, connected or not."""

    node_id: str
    room: str | None = None
    #: Unix time of the last register/deregister. While a node is connected this
    #: is when it joined; the interesting reading is always the absent one.
    last_seen: float = 0.0
    version: str | None = None
    ip: str | None = None
    #: Suppresses the offline alert until this unix time — set when *we* asked the
    #: node to go away (restart/upgrade), so expected downtime doesn't cry wolf.
    grace_until: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "node_id": self.node_id,
            "room": self.room,
            "last_seen": self.last_seen,
            "version": self.version,
            "ip": self.ip,
        }
        if self.grace_until:
            out["grace_until"] = self.grace_until
        out.update(self.extra)
        return out


class NodeRoster:
    """Load/store :class:`RosterEntry` records, tolerantly.

    Every write is best-effort: a read-only or missing data root costs the
    roster, never a node's connection. Unknown keys in the file are preserved so
    a newer server's fields survive a downgrade.
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._entries: dict[str, RosterEntry] = {}
        self._load()

    # -- persistence ---------------------------------------------------

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("node roster unreadable (%s) — starting empty", exc)
            return
        if not isinstance(raw, dict):
            return
        for node_id, rec in raw.get("nodes", {}).items():
            if not isinstance(rec, dict):
                continue
            known = {"node_id", "room", "last_seen", "version", "ip", "grace_until"}
            self._entries[str(node_id)] = RosterEntry(
                node_id=str(node_id),
                room=rec.get("room"),
                last_seen=float(rec.get("last_seen") or 0.0),
                version=rec.get("version"),
                ip=rec.get("ip"),
                grace_until=float(rec.get("grace_until") or 0.0),
                extra={k: v for k, v in rec.items() if k not in known},
            )

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            payload = {"nodes": {nid: e.as_dict() for nid, e in self._entries.items()}}
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            log.warning("could not write node roster: %s", exc)

    # -- mutation ------------------------------------------------------

    def touch(
        self,
        node_id: str,
        *,
        room: str | None = None,
        version: str | None = None,
        ip: str | None = None,
        when: float | None = None,
    ) -> None:
        """Record that a node was just seen (registered or disconnected)."""
        now = time.time() if when is None else when
        entry = self._entries.get(node_id) or RosterEntry(node_id=node_id)
        entry.last_seen = now
        if room is not None:
            entry.room = room
        if version is not None:
            entry.version = version
        if ip is not None:
            entry.ip = ip
        entry.grace_until = 0.0  # it came back; any expected-downtime window is over
        self._entries[node_id] = entry
        self._save()

    def grant_grace(self, node_id: str, seconds: float, *, now: float | None = None) -> None:
        """Suppress the offline alert for a node we just told to restart or
        upgrade. Without this the fleet cries wolf every time an operator uses
        the dashboard's own buttons — and an alert people learn to ignore is
        worth less than no alert at all."""
        entry = self._entries.get(node_id)
        if entry is None:
            return
        entry.grace_until = (time.time() if now is None else now) + seconds
        self._save()

    def forget(self, node_id: str) -> bool:
        """Drop a node from the roster (decommissioned). Returns True if present."""
        if self._entries.pop(node_id, None) is None:
            return False
        self._save()
        return True

    # -- reads ---------------------------------------------------------

    def known(self) -> dict[str, RosterEntry]:
        return dict(self._entries)

    def absent(self, connected: Iterable[str]) -> list[RosterEntry]:
        """Known nodes that are not in ``connected``, oldest sighting first."""
        live = set(connected)
        gone = [e for nid, e in self._entries.items() if nid not in live]
        return sorted(gone, key=lambda e: e.last_seen)

    def is_alerting(
        self, entry: RosterEntry, threshold_s: float, *, now: float | None = None
    ) -> bool:
        """True when an absent node has been gone long enough to be a fault, and
        is not inside an expected-downtime grace window."""
        stamp = time.time() if now is None else now
        if threshold_s <= 0:
            return False
        if entry.grace_until and stamp < entry.grace_until:
            return False
        return (stamp - entry.last_seen) >= threshold_s
