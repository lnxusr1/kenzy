"""The task executor — the running half of the async tool contract.

Spec: kenzy-design/app/s2s-design.md, open question 6 (the async tool
contract, ruled 2026-08-29). The ledger (:mod:`kenzy.s2s.ledger`) is the
record; this is the engine room: a deferred tool call becomes a background
task here, executes through the same skill door as any in-turn call, and its
completion is handed to the server's delivery function — which speaks it into
the live conversation (a delivery turn), through the proactive gate's
``tasks`` category, or leaves it in the ledger for pickup at the owner's next
conversation. Denied is never dropped.

Two entry points, one machine:

- :meth:`start` — a deferred-class tool detaches before running (the gate's
  detach verdict, or the classic pipeline's ``task_detach`` action).
- :meth:`adopt` — a working-class tool ALREADY RUNNING stalls past the
  promotion threshold (or the user barges over the wait) and is promoted one
  rung: the in-flight awaitable is re-parented here and the turn gets its
  hand-off string.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from kenzy.s2s.ledger import Task, TaskLedger
from kenzy.taskhandoff import handoff_text

log = logging.getLogger(__name__)

#: How long after the ASK a completion may still speak unprompted (founder,
#: 2026-08-29): within this window the asker is visibly waiting — announcing
#: "that's done" is the answer arriving. Past it, an unprompted announcement
#: is an interruption: the result instead RIDES the next natural exchange
#: ("I've also got those results…") or waits to be asked ("how's that
#: going"). Delivery demotes; it never drops.
ANNOUNCE_WINDOW_S = 30.0


class TaskExecutor:
    """Runs detached work and keeps the ledger honest about it."""

    def __init__(
        self,
        ledger: TaskLedger,
        deliver: Callable[[Task], Awaitable[bool]],
    ) -> None:
        #: deliver(task) -> True when the result was actually spoken/delivered;
        #: False leaves it deliverable in the ledger for the pickup path.
        self._ledger = ledger
        self._deliver = deliver
        self._running: dict[str, asyncio.Task[None]] = {}

    # ----------------------------------------------------------------- writes

    def start(
        self,
        *,
        owner: str,
        title: str,
        origin_room: str = "",
        origin_node: str = "",
        work: Awaitable[str],
    ) -> Task:
        """Detach fresh work. ``work`` is the not-yet-awaited execution (the
        same skill-door call an in-turn tool would make)."""
        return self._launch(owner, title, origin_room, origin_node, work)

    def adopt(
        self,
        *,
        owner: str,
        title: str,
        origin_room: str = "",
        origin_node: str = "",
        work: asyncio.Task[str],
    ) -> Task:
        """Promote in-flight work (the working→deferred rung): the awaitable
        is already running; the ledger takes it over from here."""
        return self._launch(owner, title, origin_room, origin_node, work)

    def cancel(self, task_id: str) -> bool:
        """Stop a running task (the deterministic tier — always works)."""
        runner = self._running.get(task_id)
        if runner is not None:
            runner.cancel()
        return self._ledger.cancel(task_id)

    def mark_delivered(self, task_id: str) -> bool:
        return self._ledger.mark_delivered(task_id)

    # ------------------------------------------------------------------ reads

    def pending_for(self, owner: str) -> list[Task]:
        """Results waiting to be spoken to (or shared with) ``owner`` — the
        next-conversation pickup path reads this."""
        return self._ledger.pending_deliveries(owner)

    def for_owner(self, owner: str) -> list[Task]:
        """The owner's tasks, newest first ("how's that going")."""
        return self._ledger.for_owner(owner)

    # -------------------------------------------------------------- internals

    def _launch(
        self, owner: str, title: str, origin_room: str, origin_node: str, work: Awaitable[str]
    ) -> Task:
        task = self._ledger.create(
            owner, title, origin_room=origin_room, origin_session=origin_node
        )
        runner = asyncio.get_running_loop().create_task(
            self._run(task.id, work), name=f"kenzy-task-{task.id}"
        )
        self._running[task.id] = runner
        log.info("Task %s detached: %s (owner %s)", task.id, title, owner or "?")
        return task

    async def _run(self, task_id: str, work: Awaitable[str]) -> None:
        try:
            try:
                result = await work
                self._ledger.complete(task_id, str(result))
            except asyncio.CancelledError:
                self._ledger.cancel(task_id)
                raise
            except Exception as exc:  # noqa: BLE001 — honest failure is a delivery too
                self._ledger.fail(task_id, str(exc))
        finally:
            self._running.pop(task_id, None)
        # Expire stale results opportunistically before attempting delivery.
        for expired in self._ledger.sweep():
            log.info("Task %s expired undelivered: %s", expired.id, expired.title)
        task = self._ledger.get(task_id)
        if task is None or not task.deliverable:
            return
        try:
            if await self._deliver(task):
                self._ledger.mark_delivered(task_id)
            else:
                log.info(
                    "Task %s finished; delivery deferred (picked up at %s's next conversation)",
                    task_id,
                    task.owner or "?",
                )
        except Exception:  # noqa: BLE001 — delivery failure must not lose the record
            log.exception("Task %s: delivery attempt failed — result stays in the ledger", task_id)


def completion_text(task: Task) -> str:
    """The speakable completion, honest about failure."""
    if task.state == "failed":
        return f"The background task '{task.title}' failed: {task.error}"
    return f"'{task.title}' is finished. {task.result}".strip()


__all__ = ["TaskExecutor", "completion_text", "handoff_text"]  # handoff_text re-exported
