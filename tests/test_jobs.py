"""F5.5 thin job runner: registration, due-order firing, error isolation,
run records, and the /jobs endpoint lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from kenzy.jobs import Job, JobRunner


def _job(name, fn, interval=0.05, **kw):
    return Job(name=name, interval=interval, fn=fn, **kw)


async def test_register_validates():
    r = JobRunner()

    async def noop():
        return None

    r.register(_job("a", noop))
    with pytest.raises(ValueError):
        r.register(_job("a", noop))  # duplicate name
    with pytest.raises(ValueError):
        r.register(_job("b", noop, interval=0))  # non-positive interval


async def test_run_once_records_success_and_summary():
    r = JobRunner()
    calls = []

    async def work():
        calls.append(1)
        return {"cleaned": 3}

    r.register(_job("work", work, scope="memory"))
    rec = await r.run_once("work")
    assert rec.ok and rec.summary == {"cleaned": 3} and calls == [1]
    (status,) = r.status()
    assert status["name"] == "work"
    assert status["runs"] == 1 and status["failures"] == 0
    assert status["last_ok"] is True and status["last_summary"] == {"cleaned": 3}
    assert status["scope"] == "memory" and status["owner"] == "system"


async def test_error_isolation_records_failure_and_survives():
    r = JobRunner()

    async def bad():
        raise RuntimeError("boom")

    async def good():
        return {"ok": 1}

    r.register(_job("bad", bad))
    r.register(_job("good", good))
    rec = await r.run_once("bad")
    assert not rec.ok and "RuntimeError: boom" in rec.error
    # The failure is recorded, and other jobs still run fine.
    rec = await r.run_once("good")
    assert rec.ok
    by_name = {s["name"]: s for s in r.status()}
    assert by_name["bad"]["failures"] == 1 and by_name["bad"]["last_error"]
    assert by_name["good"]["failures"] == 0


async def test_loop_fires_due_jobs_and_survives_failures():
    r = JobRunner()
    ticks = []

    async def tick():
        ticks.append(1)
        return None

    async def bad():
        raise RuntimeError("nope")

    r.register(_job("tick", tick, interval=0.03))
    r.register(_job("bad", bad, interval=0.03))
    task = asyncio.create_task(r.run())
    try:
        await asyncio.sleep(0.25)
    finally:
        task.cancel()
    # Both fired repeatedly; the failing job never killed the loop or its peer.
    assert len(ticks) >= 2
    by_name = {s["name"]: s for s in r.status()}
    assert by_name["bad"]["runs"] >= 2 and by_name["bad"]["failures"] >= 2
    assert by_name["tick"]["failures"] == 0


async def test_first_run_is_deferred_not_at_boot():
    r = JobRunner()

    async def work():
        return None

    r.register(_job("later", work, interval=60))
    (status,) = r.status()
    # Due roughly one (jittered) interval out — never immediately at boot.
    assert 50 <= status["next_due_in"] <= 60
    assert status["runs"] == 0


def test_jobs_endpoint_lifecycle():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from kenzy.jobs import install_jobs_endpoint

    app = FastAPI()
    r = JobRunner()

    async def work():
        return {"n": 1}

    r.register(_job("work", work, interval=30))
    install_jobs_endpoint(app, r)
    with TestClient(app) as client:  # context manager runs startup/shutdown
        resp = client.get("/jobs")
        assert resp.status_code == 200
        (job,) = resp.json()["jobs"]
        assert job["name"] == "work" and job["interval"] == 30
    # Shutdown cancelled the runner task without complaint (no lingering task).


# -- kick / cooldown / failure retry (event-driven scheduling) -----------------


async def test_kick_pulls_run_forward():
    r = JobRunner()

    async def work():
        return None

    r.register(_job("w", work, interval=3600))
    (before,) = r.status()
    assert before["next_due_in"] > 3000  # far out on the backstop
    r.kick("w")
    (after,) = r.status()
    assert after["next_due_in"] < 1  # pulled to now


async def test_kick_respects_cooldown_and_coalesces():
    r = JobRunner()

    async def work():
        return None

    r.register(_job("w", work, interval=3600, cooldown=30))
    await r.run_once("w")  # establishes last_end
    r.kick("w")
    (s,) = r.status()
    # Inside the cooldown window: scheduled at the boundary, not immediately.
    assert 25 <= s["next_due_in"] <= 30
    first_due = s["next_due_in"]
    r.kick("w")  # a burst of kicks lands on the SAME scheduled run
    r.kick("w")
    (s,) = r.status()
    assert abs(s["next_due_in"] - first_due) < 1


async def test_kick_never_delays_an_earlier_schedule():
    r = JobRunner()

    async def work():
        return None

    r.register(_job("w", work, interval=3600, cooldown=300))
    await r.run_once("w")
    r.kick("w")  # → cooldown boundary (~300s)
    (s1,) = r.status()
    r.kick("w")  # kicking again must not push it later
    (s2,) = r.status()
    assert s2["next_due_in"] <= s1["next_due_in"] + 0.1


async def test_failed_run_reschedules_at_retry_after():
    r = JobRunner()

    async def bad():
        raise RuntimeError("model down")

    async def good():
        return None

    r.register(_job("bad", bad, interval=86400, retry_after=900))
    r.register(_job("good", good, interval=86400))
    await r.run_once("bad")
    await r.run_once("good")
    by = {s["name"]: s for s in r.status()}
    assert 890 <= by["bad"]["next_due_in"] <= 900  # retry soon, not tomorrow
    assert by["good"]["next_due_in"] > 86000  # success waits out the backstop


async def test_kick_during_run_is_honored():
    # A kick that lands WHILE the job runs (e.g. the run's own writes) must
    # not be clobbered by the end-of-run backstop reschedule.
    import time as _time

    from kenzy.jobs import Job, JobRunner

    runner = JobRunner()

    async def fn():
        runner.kick("j")  # a write during the run kicks the same job
        return "ok"

    runner.register(Job(name="j", interval=86400, fn=fn, cooldown=1))
    await runner.run_once("j")
    state = runner._jobs["j"]
    # Next run is due ~cooldown from now, not a day away.
    assert state.next_due - _time.monotonic() < 5
