"""The task ledger — detach → ledger → execute behind a seam → deliver via the gate.

Spec: kenzy-design/app/s2s-design.md, "The async task pattern". A slow job
detaches audibly ("I'll work on that and let you know"), lands here as an
owner-scoped, restart-safe record, executes as an ordinary skill behind the
action seam, and its result is delivered through the proactive gate wherever
the owner is. Founder direction: async may become the default as agent work
grows — so the ledger is the centerpiece, not a corner case.

Persistence follows the schedules.json pattern: one JSON file, atomic rewrite
(tmp + replace), tolerant per-record up-conversion on load. Two rules with
teeth:

- **Honest failure is a delivery too**: any task found ``running`` at load
  time was orphaned by a restart — it is failed LOUDLY (deliverable to its
  owner), never silently dropped.
- **Private-to-owner is the default**; household-shareable is an explicit
  flag, never a fallback.

Delivery itself (presence routing, the stranger check, quiet hours) is the
server's job — this module only says what is deliverable, to whom, and keeps
the record straight.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

log = logging.getLogger(__name__)

#: Task states. ``running`` is the only live state; everything else is final.
_FINAL_STATES = ("done", "failed", "cancelled", "expired")
#: Delivery priorities (design): interrupt (explicit opt-in, below Tier A's
#: floor) / normal (next natural moment) / quiet (on-ask only).
_PRIORITIES = ("interrupt", "normal", "quiet")


@dataclass
class Task:
    """One detached job's record. Mutate only through the ledger's methods."""

    id: str
    owner: str  # set-time speaker identity (person id / name)
    title: str  # speakable
    origin_room: str = ""
    origin_session: str = ""
    priority: str = "normal"
    shareable: bool = False  # private-to-owner unless explicitly flagged
    state: str = "running"
    notes: list[str] = field(default_factory=list)  # append-only, speakable
    result: str = ""
    error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    ttl_s: float = 86400.0
    delivered: bool = False

    @property
    def deliverable(self) -> bool:
        """Finished with something to say, and not yet said."""
        return self.state in ("done", "failed") and not self.delivered

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner": self.owner,
            "title": self.title,
            "origin_room": self.origin_room,
            "origin_session": self.origin_session,
            "priority": self.priority,
            "shareable": self.shareable,
            "state": self.state,
            "notes": list(self.notes),
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "ttl_s": self.ttl_s,
            "delivered": self.delivered,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Task:
        """Tolerant per-record up-conversion (the memory-ledger convention)."""
        return cls(
            id=str(raw.get("id", "")) or uuid4().hex[:12],
            owner=str(raw.get("owner", "")),
            title=str(raw.get("title", "")),
            origin_room=str(raw.get("origin_room", "")),
            origin_session=str(raw.get("origin_session", "")),
            priority=str(raw.get("priority", "normal")),
            shareable=bool(raw.get("shareable", False)),
            state=str(raw.get("state", "running")),
            notes=[str(n) for n in raw.get("notes", [])],
            result=str(raw.get("result", "")),
            error=str(raw.get("error", "")),
            created_at=float(raw.get("created_at", 0.0)),
            updated_at=float(raw.get("updated_at", 0.0)),
            ttl_s=float(raw.get("ttl_s", 86400.0)),
            delivered=bool(raw.get("delivered", False)),
        )


class TaskLedger:
    """The persisted, owner-scoped record of detached work."""

    def __init__(self, path: Path, *, clock: Callable[[], float] | None = None) -> None:
        self._path = path
        self._clock: Callable[[], float] = clock or time.time
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}
        self._load()

    # ----------------------------------------------------------------- writes

    def create(
        self,
        owner: str,
        title: str,
        *,
        origin_room: str = "",
        origin_session: str = "",
        priority: str = "normal",
        shareable: bool = False,
        ttl_s: float = 86400.0,
    ) -> Task:
        if priority not in _PRIORITIES:
            priority = "normal"
        now = self._clock()
        task = Task(
            id=uuid4().hex[:12],
            owner=owner,
            title=title,
            origin_room=origin_room,
            origin_session=origin_session,
            priority=priority,
            shareable=shareable,
            created_at=now,
            updated_at=now,
            ttl_s=ttl_s,
        )
        with self._lock:
            self._tasks[task.id] = task
            self._save()
        return task

    def note(self, task_id: str, text: str) -> bool:
        """Append one speakable progress note ("how's that going" narrates these)."""
        return self._mutate(task_id, lambda t: t.notes.append(text), running_only=True)

    def complete(self, task_id: str, result: str) -> bool:
        def apply(t: Task) -> None:
            t.state = "done"
            t.result = result

        return self._mutate(task_id, apply, running_only=True)

    def fail(self, task_id: str, error: str) -> bool:
        def apply(t: Task) -> None:
            t.state = "failed"
            t.error = error

        return self._mutate(task_id, apply, running_only=True)

    def cancel(self, task_id: str) -> bool:
        """Cancel a running task (rides the deterministic tier — always works)."""

        def apply(t: Task) -> None:
            t.state = "cancelled"

        return self._mutate(task_id, apply, running_only=True)

    def mark_delivered(self, task_id: str) -> bool:
        def apply(t: Task) -> None:
            t.delivered = True

        return self._mutate(task_id, apply, running_only=False)

    def sweep(self) -> list[Task]:
        """Expire undelivered results past their TTL. Returns what expired this
        pass — the caller's cue to record them to Activity ("expire to
        Activity", never a silent vanish)."""
        now = self._clock()
        expired: list[Task] = []
        with self._lock:
            for task in self._tasks.values():
                if task.deliverable and now >= task.created_at + task.ttl_s:
                    task.state = "expired"
                    task.updated_at = now
                    expired.append(task)
            if expired:
                self._save()
        return expired

    # ------------------------------------------------------------------ reads

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def for_owner(self, owner: str) -> list[Task]:
        """The owner's tasks, newest first — the "how's that going" scope."""
        with self._lock:
            mine = [t for t in self._tasks.values() if t.owner == owner]
        return sorted(mine, key=lambda t: t.created_at, reverse=True)

    def pending_deliveries(self, owner: str | None = None) -> list[Task]:
        """What is waiting to be spoken. With ``owner``: their own results plus
        anything explicitly flagged household-shareable — never other people's
        private results."""
        with self._lock:
            due = [t for t in self._tasks.values() if t.deliverable]
        if owner is not None:
            due = [t for t in due if t.owner == owner or t.shareable]
        return sorted(due, key=lambda t: t.created_at)

    # -------------------------------------------------------------- internals

    def _mutate(
        self, task_id: str, apply: Callable[[Task], None], *, running_only: bool
    ) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or (running_only and task.state != "running"):
                return False
            apply(task)
            task.updated_at = self._clock()
            self._save()
            return True

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.error("Could not read task ledger %s: %s", self._path, exc)
            return
        orphaned = 0
        for entry in raw if isinstance(raw, list) else []:
            task = Task.from_dict(entry)
            if task.state == "running":
                # Honest failure is a delivery too: a restart orphaned this —
                # fail it loudly to its owner, never silently.
                task.state = "failed"
                task.error = "orphaned by a restart before it finished"
                task.updated_at = self._clock()
                orphaned += 1
            self._tasks[task.id] = task
        if orphaned:
            log.warning("Task ledger: %d task(s) orphaned by restart, failed loudly", orphaned)
            self._save()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps([t.to_dict() for t in self._tasks.values()], indent=2))
            tmp.replace(self._path)
        except OSError as exc:
            log.error("Could not persist task ledger %s: %s", self._path, exc)


__all__ = ["Task", "TaskLedger"]
