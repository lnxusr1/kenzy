"""Task-ledger tests — persistence, restart honesty, owner scoping, TTL
(spec: kenzy-design/app/s2s-design.md, "The async task pattern")."""

from __future__ import annotations

import json
from pathlib import Path

from kenzy.s2s.ledger import TaskLedger


class _Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def _ledger(tmp_path: Path, clock: _Clock | None = None) -> TaskLedger:
    return TaskLedger(tmp_path / "tasks.json", clock=clock or _Clock())


def test_create_persists_and_reloads(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    task = ledger.create("alex", "book the table", origin_room="office", priority="quiet")
    ledger.complete(task.id, "booked for 7pm")

    reloaded = _ledger(tmp_path)
    got = reloaded.get(task.id)
    assert got is not None
    assert (got.owner, got.title, got.origin_room) == ("alex", "book the table", "office")
    assert got.state == "done" and got.result == "booked for 7pm"
    # atomic-rewrite hygiene: the real file is valid JSON, no tmp left behind
    assert json.loads((tmp_path / "tasks.json").read_text())
    assert not (tmp_path / "tasks.json.tmp").exists()


def test_restart_orphans_fail_loudly_and_stay_deliverable(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    task = ledger.create("alex", "long research job")
    assert task.state == "running"

    reloaded = _ledger(tmp_path)  # the restart
    got = reloaded.get(task.id)
    assert got is not None and got.state == "failed"
    assert "restart" in got.error  # honest failure is a delivery too
    assert got.deliverable  # the owner hears about it — never a silent vanish


def test_finished_tasks_reject_further_mutation(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    task = ledger.create("alex", "job")
    assert ledger.note(task.id, "halfway there")
    assert ledger.complete(task.id, "done it")
    assert not ledger.complete(task.id, "again")  # final states are final
    assert not ledger.note(task.id, "late note")
    assert not ledger.cancel(task.id)
    got = ledger.get(task.id)
    assert got is not None and got.notes == ["halfway there"]


def test_owner_scoping_and_shareable_flag(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    mine = ledger.create("alex", "my private job")
    ledger.create("alice", "her private job")
    shared = ledger.create("alice", "household job", shareable=True)
    for task_id in (mine.id, shared.id):
        ledger.complete(task_id, "done")
    alice_private = ledger.for_owner("alice")
    assert {t.title for t in alice_private} == {"her private job", "household job"}
    # alex hears: his own results + explicitly-shareable ones — never alice's private
    due = ledger.pending_deliveries("alex")
    assert {t.title for t in due} == {"my private job", "household job"}


def test_delivery_and_ttl_expiry(tmp_path: Path) -> None:
    clock = _Clock()
    ledger = _ledger(tmp_path, clock)
    quick = ledger.create("alex", "quick job", ttl_s=10.0)
    slow = ledger.create("alex", "held job", ttl_s=10_000.0)
    ledger.complete(quick.id, "done")
    ledger.complete(slow.id, "done")

    ledger.mark_delivered(slow.id)
    assert [t.id for t in ledger.pending_deliveries()] == [quick.id]

    clock.t += 11.0
    expired = ledger.sweep()  # expire to Activity — the caller records these
    assert [t.id for t in expired] == [quick.id]
    got = ledger.get(quick.id)
    assert got is not None and got.state == "expired"
    assert ledger.pending_deliveries() == []


def test_cancel_only_running(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    task = ledger.create("alex", "job")
    assert ledger.cancel(task.id)
    got = ledger.get(task.id)
    assert got is not None and got.state == "cancelled"
    assert not got.deliverable  # a cancel is the owner's own act — nothing to announce
