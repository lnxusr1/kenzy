"""The task executor — the async tool contract's engine room.

Detach (fresh or adopted) → ledger → execute → deliver, with denied-never-
dropped: a delivery the gate declines stays deliverable for the pickup path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from kenzy.s2s.ledger import Task, TaskLedger
from kenzy.server.server import TranscribingServer
from kenzy.server.tasks import TaskExecutor, completion_text, handoff_text


def _ledger(tmp_path: Path) -> TaskLedger:
    return TaskLedger(tmp_path / "tasks.json")


async def test_start_executes_completes_and_delivers(tmp_path):
    delivered: list[Task] = []

    async def deliver(task: Task) -> bool:
        delivered.append(task)
        return True

    ex = TaskExecutor(_ledger(tmp_path), deliver)

    async def work() -> str:
        return "the app is written"

    task = ex.start(owner="John", title="write an app", origin_room="office", work=work())
    for _ in range(20):
        await asyncio.sleep(0)
    rec = ex.for_owner("John")[0]
    assert rec.state == "done" and rec.result == "the app is written"
    assert rec.delivered and delivered[0].id == task.id


async def test_declined_delivery_stays_deliverable_for_pickup(tmp_path):
    async def deliver(_t: Task) -> bool:
        return False  # the proactive gate said not now

    ex = TaskExecutor(_ledger(tmp_path), deliver)

    async def work() -> str:
        return "result"

    ex.start(owner="John", title="job", work=work())
    for _ in range(20):
        await asyncio.sleep(0)
    pending = ex.pending_for("John")
    assert len(pending) == 1 and pending[0].result == "result"
    # And another owner never sees it (private-to-owner default).
    assert ex.pending_for("Alice") == []


async def test_failure_is_a_delivery_too(tmp_path):
    delivered: list[Task] = []

    async def deliver(task: Task) -> bool:
        delivered.append(task)
        return True

    ex = TaskExecutor(_ledger(tmp_path), deliver)

    async def work() -> str:
        raise RuntimeError("compiler exploded")

    ex.start(owner="John", title="doomed job", work=work())
    for _ in range(20):
        await asyncio.sleep(0)
    assert delivered and delivered[0].state == "failed"
    assert "compiler exploded" in completion_text(delivered[0])


async def test_adopt_takes_over_running_work(tmp_path):
    """The working->deferred promotion: the awaitable is already running."""
    delivered: list[Task] = []

    async def deliver(task: Task) -> bool:
        delivered.append(task)
        return True

    ex = TaskExecutor(_ledger(tmp_path), deliver)
    gate = asyncio.Event()

    async def slow() -> str:
        await gate.wait()
        return "finally"

    inflight = asyncio.get_running_loop().create_task(slow())
    ex.adopt(owner="John", title="slow job", work=inflight)
    await asyncio.sleep(0)
    assert ex.for_owner("John")[0].state == "running"
    gate.set()
    for _ in range(20):
        await asyncio.sleep(0)
    assert delivered and delivered[0].result == "finally"


def test_handoff_never_claims_completion():
    text = handoff_text("write an app")
    assert "Started" in text and "not done YET" in text and "invent" in text


# --- the classic pipeline's channel: the task_detach action ------------------


async def test_task_detach_action_starts_the_executor(tmp_path, monkeypatch):
    """The llm host queues task_detach; dispatch starts the same executor —
    one ledger, one delivery path, both pipelines."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    srv = TranscribingServer({})

    ran: list[str] = []
    seen_tier: list[str] = []

    async def fake_execute(call: Any, node_id: str, room: str, speaker: Any) -> str:
        ran.append(call.name)
        seen_tier.append(speaker.tier)
        return "built it"

    delivered: list[Any] = []

    async def fake_deliver(task: Any) -> bool:
        delivered.append(task)
        return True

    monkeypatch.setattr(srv, "_s2s_execute_tool", fake_execute)
    monkeypatch.setattr(srv, "_deliver_task_result", fake_deliver)

    await srv._dispatch_actions(
        [
            {
                "type": "task_detach",
                "name": "build_the_app",
                "arguments": {"spec": "todo list"},
                "owner": "John",
                "speaker_tier": "recognized",
            }
        ],
        "node-1",
        "office",
    )
    for _ in range(20):
        await asyncio.sleep(0)
    assert ran == ["build_the_app"]
    assert seen_tier == ["recognized"]  # the REAL tier from the action
    assert delivered and delivered[0].owner == "John"
    assert delivered[0].result == "built it"
    rec = srv._tasks().for_owner("John")[0]
    assert rec.state == "done" and rec.origin_room == "office"


# --- the delivery decision: live conversation > proactive gate > pickup ------


async def test_delivery_prefers_the_live_conversation(tmp_path, monkeypatch):
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    srv = TranscribingServer({})

    class _Bridge:
        def active(self, node_id: str) -> bool:
            return node_id == "node-1"

        async def deliver_completion(self, node_id: str, text: str) -> bool:
            spoken.append((node_id, text))
            return True

    spoken: list[tuple[str, str]] = []
    srv._s2s_bridge = _Bridge()
    import time as _t

    task = Task(id="t1", owner="John", title="job", origin_room="office",
                origin_session="node-1", state="done", result="done well",
                created_at=_t.time())
    assert await srv._deliver_task_result(task) is True
    assert spoken and "done well" in spoken[0][1]


async def test_delivery_falls_to_the_proactive_gate_and_respects_it(tmp_path, monkeypatch):
    """No live conversation: the tasks category decides — the first
    NON-EXEMPT category, so policy knobs are real here. Denied returns False
    (the pickup path), never drops."""
    from kenzy.server.proactive import ProactiveGate

    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    srv = TranscribingServer({})
    announced: list[str] = []

    async def fake_announce(msg: str, targets: list[str]) -> int:
        announced.append(msg)
        return len(targets)

    monkeypatch.setattr(srv, "announce", fake_announce)

    class _S:
        room_id = "office"

    srv._nodes["n1"] = _S()
    import time as _t

    task = Task(id="t2", owner="John", title="job", origin_room="office",
                state="done", result="all good", created_at=_t.time())

    srv._proactive = ProactiveGate.from_config({"enabled": True})  # tasks NOT enabled
    assert await srv._deliver_task_result(task) is False
    assert announced == []

    srv._proactive = ProactiveGate.from_config(
        {"enabled": True, "tasks": {"enabled": True}}
    )
    assert await srv._deliver_task_result(task) is True
    assert announced and "all good" in announced[0]


async def test_a_late_completion_stages_instead_of_announcing(tmp_path, monkeypatch):
    """The announce window (founder, 2026-08-29): past ~30 s the asker is no
    longer visibly waiting — a completion must not interrupt. In a live
    conversation it is STAGED to ride the next reply; with none it parks for
    pickup, even with the proactive tasks category enabled."""
    from kenzy.server.proactive import ProactiveGate

    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    srv = TranscribingServer({})

    staged: list[str] = []
    delivered_turns: list[str] = []

    class _Bridge:
        def active(self, node_id: str) -> bool:
            return node_id == "node-1"

        async def deliver_completion(self, node_id: str, text: str) -> bool:
            delivered_turns.append(text)
            return True

        async def stage_completion(self, node_id: str, text: str) -> bool:
            staged.append(text)
            return True

    srv._s2s_bridge = _Bridge()
    import time as _time

    old = Task(id="t9", owner="John", title="long job", origin_room="office",
               origin_session="node-1", state="done", result="finally done",
               created_at=_time.time() - 120)
    # Staging is a best-effort nicety, NOT a confirmed delivery: it injects the
    # context (rides the next reply) but returns False so the result stays
    # PENDING for the durable pickup path if the conversation ends first.
    assert await srv._deliver_task_result(old) is False
    assert staged and not delivered_turns  # staged (no interruption), not delivered

    # No live conversation + stale: parks even with the category enabled.
    srv._s2s_bridge = None
    announced: list[str] = []

    async def fake_announce(msg, targets):
        announced.append(msg)
        return 1

    monkeypatch.setattr(srv, "announce", fake_announce)

    class _S:
        room_id = "office"

    srv._nodes["n1"] = _S()
    srv._proactive = ProactiveGate.from_config({"enabled": True, "tasks": {"enabled": True}})
    old2 = Task(id="t10", owner="John", title="long job", origin_room="office",
                state="done", result="late", created_at=_time.time() - 120)
    assert await srv._deliver_task_result(old2) is False
    assert announced == []


async def test_task_detach_carries_the_real_tier_never_fabricates(tmp_path, monkeypatch):
    """The tier-gate fix: the dispatch must build the Speaker from the action's
    ACTUAL tier, never manufacture "recognized" from a non-empty owner name
    (a name is not a voiceprint — F1.3)."""
    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    (tmp_path / "configs").mkdir(parents=True)
    srv = TranscribingServer({})
    seen: list[str] = []

    async def fake_execute(call, node_id, room, speaker):
        seen.append(speaker.tier)
        return "ok"

    monkeypatch.setattr(srv, "_s2s_execute_tool", fake_execute)

    async def fake_deliver(task):
        return True

    monkeypatch.setattr(srv, "_deliver_task_result", fake_deliver)

    # A named-but-unrecognized speaker (owner set, tier unknown) must NOT
    # become "recognized".
    await srv._dispatch_actions(
        [{"type": "task_detach", "name": "x", "owner": "someone",
          "speaker_tier": "unknown"}],
        "node-1", "office",
    )
    for _ in range(20):
        await asyncio.sleep(0)
    assert seen == ["unknown"]


async def test_announce_reaching_zero_nodes_stays_pending(tmp_path, monkeypatch):
    """Delivered means HEARD: announce() returns 0 when TTS is down, so the
    result must NOT be marked delivered — it stays pending for pickup."""
    from kenzy.server.proactive import ProactiveGate

    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    srv = TranscribingServer({})
    srv._s2s_bridge = None

    async def announce_zero(msg, targets):
        return 0  # kenzy-tts down: nothing spoken

    monkeypatch.setattr(srv, "announce", announce_zero)

    class _S:
        room_id = "office"

    srv._nodes["n1"] = _S()
    srv._proactive = ProactiveGate.from_config({"enabled": True, "tasks": {"enabled": True}})
    import time as _t

    task = Task(id="tz", owner="John", title="job", origin_room="office",
                state="done", result="r", created_at=_t.time())
    assert await srv._deliver_task_result(task) is False  # not heard → not delivered


def test_classic_task_context_and_confirm(tmp_path, monkeypatch):
    """The classic-pipeline pickup: a recognized speaker's pending results are
    injected into the /process request, and marked delivered ONLY by
    _confirm_task_updates (after a successful reply), never before."""
    from types import SimpleNamespace

    monkeypatch.setenv("KENZY_HOME", str(tmp_path))
    srv = TranscribingServer({})
    ex = srv._tasks()
    import time as _t

    ex._ledger.create("John", "web search", origin_room="office")
    tid = ex.for_owner("John")[0].id
    ex._ledger.complete(tid, "found it")

    ident = SimpleNamespace(display="John", tier="recognized", person_id="john")
    ctx = srv._task_context(ident)
    assert ctx["pending"] and ctx["pending"][0]["id"] == tid
    # An unknown-tier speaker sees no owner-private results.
    assert srv._task_context(SimpleNamespace(display="John", tier="unknown", person_id="j")) == {}
    # Still pending until confirmed.
    assert ex.pending_for("John")
    srv._confirm_task_updates({"task_updates": ctx})
    assert not ex.pending_for("John")  # confirmed after the (simulated) reply
    _ = _t  # keep import used across edits
