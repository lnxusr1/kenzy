"""Unit tests for the server's schedule store + firing loop (timers/alarms/reminders)."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta

import pytest

from kenzy.server.scheduler import (
    Entry,
    Scheduler,
    next_occurrence,
    normalize_days,
    parse_hhmm,
)


async def _noop(entry: Entry) -> None:
    pass


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_parse_hhmm():
    assert parse_hhmm("07:00") == (7, 0)
    assert parse_hhmm("23:59") == (23, 59)
    for bad in ("24:00", "7", "7:5", "noon", "12:60"):
        with pytest.raises(ValueError):
            parse_hhmm(bad)


def test_normalize_days():
    assert normalize_days(None) == []
    assert normalize_days("") == []
    assert normalize_days("weekdays") == ["mon", "tue", "wed", "thu", "fri"]
    assert normalize_days(["saturday", "sun"]) == ["sat", "sun"]
    assert normalize_days("daily") == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert normalize_days("fri, mon") == ["mon", "fri"]  # canonical week order
    with pytest.raises(ValueError):
        normalize_days("someday")


def test_next_occurrence_today_vs_tomorrow():
    now = datetime(2026, 7, 1, 12, 0).astimezone()  # a Wednesday, noon
    later = next_occurrence(14, 30, [], now)
    assert (later.day, later.hour, later.minute) == (1, 14, 30)
    earlier = next_occurrence(7, 0, [], now)  # already past → tomorrow
    assert earlier.date() == (now + timedelta(days=1)).date()


def test_next_occurrence_respects_days():
    now = datetime(2026, 7, 1, 12, 0).astimezone()  # Wednesday
    sat = next_occurrence(9, 0, ["sat", "sun"], now)
    assert sat.weekday() == 5 and sat.hour == 9
    # Same-day match when the time is still ahead.
    wed = next_occurrence(20, 0, ["wed"], now)
    assert wed.date() == now.date()
    # Same-day time already past → next week.
    next_wed = next_occurrence(8, 0, ["wed"], now)
    assert next_wed.weekday() == 2 and next_wed.date() == (now + timedelta(days=7)).date()


# ---------------------------------------------------------------------------
# Store + persistence
# ---------------------------------------------------------------------------


def test_add_cancel_roundtrip(tmp_path):
    path = tmp_path / "schedules.json"
    s = Scheduler(path, _noop)
    t = s.add("timer", "n-1", "office", label="pizza", seconds=600)
    r = s.add("reminder", "n-1", "office", label="trash", at="18:00")
    a = s.add("alarm", "n-2", "bedroom", at="07:00", days=["weekdays"])
    assert a.days == ["mon", "tue", "wed", "thu", "fri"]
    assert [e.id for e in s.entries()] and len(s.entries("n-1")) == 2

    # Reload from disk — everything survives.
    s2 = Scheduler(path, _noop)
    assert {e.id for e in s2.entries()} == {t.id, r.id, a.id}

    removed = s2.cancel([t.id, "nonsense"])
    assert [e.id for e in removed] == [t.id]
    assert {e.id for e in Scheduler(path, _noop).entries()} == {r.id, a.id}


def test_add_validation(tmp_path):
    s = Scheduler(tmp_path / "s.json", _noop)
    with pytest.raises(ValueError):
        s.add("chore", "n", "r", seconds=5)  # unknown kind
    with pytest.raises(ValueError):
        s.add("timer", "n", "r")  # no duration or time
    with pytest.raises(ValueError):
        s.add("timer", "n", "r", seconds=60, days=["mon"])  # relative can't recur
    with pytest.raises(ValueError):
        s.add("alarm", "n", "r", at="25:00")


def test_load_missed_policy(tmp_path):
    path = tmp_path / "schedules.json"
    now = time.time()

    def raw(id_, kind, fire_at, at="", days=()):
        return {
            "id": id_, "kind": kind, "label": "", "node_id": "n", "room": "r",
            "fire_at": fire_at, "created_at": now - 1000, "at": at, "days": list(days),
        }  # fmt: skip

    path.write_text(
        json.dumps(
            [
                raw("fresh", "timer", now + 60),
                raw("late-ok", "reminder", now - 30),  # within grace → kept (fires late)
                raw("stale", "timer", now - 3600),  # past grace → dropped
                raw("old-alarm", "alarm", now - 30),  # missed one-shot alarm → dropped
                raw("recurring", "alarm", now - 3600, at="07:00", days=["mon"]),  # advanced
            ]
        )
    )
    s = Scheduler(path, _noop)
    ids = {e.id for e in s.entries()}
    assert ids == {"fresh", "late-ok", "recurring"}
    rec = next(e for e in s.entries() if e.id == "recurring")
    assert rec.fire_at > now  # advanced past the downtime


# ---------------------------------------------------------------------------
# Firing
# ---------------------------------------------------------------------------


async def test_fire_due_removes_oneshot_and_advances_recurring(tmp_path):
    fired: list[str] = []

    async def cb(entry: Entry) -> None:
        fired.append(entry.id)

    s = Scheduler(tmp_path / "s.json", cb)
    t = s.add("timer", "n", "r", seconds=0.01)
    a = s.add("alarm", "n", "r", at="07:00", days=["daily"])
    a.fire_at = time.time() - 1  # force due
    await asyncio.sleep(0.02)

    due = await s.fire_due()
    assert {e.id for e in due} == {t.id, a.id}
    assert sorted(fired) == sorted([t.id, a.id])
    remaining = s.entries()
    assert [e.id for e in remaining] == [a.id]  # timer removed, alarm advanced
    assert remaining[0].fire_at > time.time()


async def test_fire_cb_error_does_not_stop_others(tmp_path):
    fired: list[str] = []

    async def cb(entry: Entry) -> None:
        if entry.label == "boom":
            raise RuntimeError("delivery failed")
        fired.append(entry.id)

    s = Scheduler(tmp_path / "s.json", cb)
    s.add("timer", "n", "r", label="boom", seconds=0.01)
    ok = s.add("timer", "n", "r", label="fine", seconds=0.01)
    await asyncio.sleep(0.02)
    await s.fire_due()
    assert fired == [ok.id]
    assert s.entries() == []  # both consumed, even the failed one


async def test_loop_fires_soon_entry(tmp_path):
    fired = asyncio.Event()

    async def cb(entry: Entry) -> None:
        fired.set()

    s = Scheduler(tmp_path / "s.json", cb)
    s.start()
    try:
        s.add("timer", "n", "r", seconds=0.05)
        await asyncio.wait_for(fired.wait(), timeout=2.0)
    finally:
        s.stop()
