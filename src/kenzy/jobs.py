"""Background job runner (v4 F5.5, thin) — the ONE place periodic work lives.

The standing rule this module enforces: **no feature spawns its own timer
loop.** Anything periodic (memory consolidation now; watcher polls, token
refresh, queue sweeps in later eras) declares a :class:`Job` and registers
with the hosting service's :class:`JobRunner`. In return the house gets a
single answer to "what runs in the background, when did it last run, and did
it work?" — via ``GET /jobs`` on the hosting service and one INFO line per
run.

Thin-scope decisions (deliberate, revisit with F7.3's dashboard job log):

- **One sequential loop per service.** Jobs run one at a time in due order —
  nothing overlaps, by construction. A slow job delays its peers; fine for
  the maintenance tier this hosts.
- **Error isolation.** A job that raises logs the exception, records a failed
  run, and retries at its next interval. The loop never dies; one bad job
  never starves another.
- **First run = one jittered interval after start** (no run-at-boot: restarts
  stay cheap, boots don't storm). Jobs must therefore be idempotent and
  tolerate having "missed" a run.
- **Run history is in-memory** (a small ring per job). Persistence arrives
  with the panel that would display it (F7.3, v5); structured logs carry the
  audit trail meanwhile.

Stdlib-only (asyncio); usable by any service. FastAPI is imported lazily in
:func:`install_jobs_endpoint` so non-HTTP hosts (the server) can import this
module freely.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: Kept run records per job (in-memory ring; see module docstring).
_HISTORY = 20

#: Fraction of the interval randomized into each wait so co-hosted jobs (and
#: freshly rebooted fleets) don't fire in lockstep.
_JITTER = 0.1


@dataclass
class Job:
    """One periodic task. ``fn`` returns an optional summary dict — it becomes
    the run record and the log line (e.g. ``{"deduped": 2}``).

    ``interval`` is the *backstop* cadence; event-driven jobs are pulled
    earlier via :meth:`JobRunner.kick`. ``cooldown`` rate-limits kicks (a kick
    inside the window lands on the already-scheduled run — bursts coalesce).
    ``retry_after`` reschedules a FAILED run sooner than the backstop (e.g.
    ~15 min) so transient outages self-heal quickly; None = just the interval.
    """

    name: str  # unique within the hosting service
    interval: float  # seconds between runs
    fn: Callable[[], Awaitable[dict[str, Any] | None]]
    owner: str = "system"  # plan of record: owner is a person or "system"
    scope: str = ""  # grouping label for display, e.g. "memory"
    cooldown: float = 0.0  # min seconds between a run's end and a kicked rerun
    retry_after: float | None = None  # reschedule after a FAILED run (None = interval)


@dataclass
class _RunRecord:
    started: float  # wall clock, for humans
    duration_ms: float
    ok: bool
    error: str | None = None
    summary: dict[str, Any] | None = None


@dataclass
class _JobState:
    job: Job
    next_due: float  # monotonic deadline
    last_end: float = 0.0  # monotonic end of the last run (cooldown anchor)
    runs: int = 0
    failures: int = 0
    history: deque[_RunRecord] = field(default_factory=lambda: deque(maxlen=_HISTORY))


class JobRunner:
    """Sequential runner for a service's registered jobs."""

    def __init__(self) -> None:
        self._jobs: dict[str, _JobState] = {}

    def register(self, job: Job) -> None:
        if job.name in self._jobs:
            raise ValueError(f"job {job.name!r} is already registered")
        if job.interval <= 0:
            raise ValueError(f"job {job.name!r} needs a positive interval")
        due = time.monotonic() + job.interval * (1 - _JITTER * random.random())
        self._jobs[job.name] = _JobState(job=job, next_due=due)
        log.info(
            "Job registered: %s (every %.0fs, scope=%s)", job.name, job.interval, job.scope or "-"
        )

    async def run_once(self, name: str) -> _RunRecord:
        """Execute one job immediately and record the run (also the test seam)."""
        state = self._jobs[name]
        started = time.time()
        t0 = time.monotonic()
        try:
            summary = await state.job.fn()
            record = _RunRecord(
                started=started,
                duration_ms=(time.monotonic() - t0) * 1000,
                ok=True,
                summary=summary,
            )
            log.info("Job %s: ok in %.0fms %s", name, record.duration_ms, summary or "")
        except asyncio.CancelledError:  # shutdown — don't swallow, don't record
            raise
        except Exception as exc:  # error isolation: the loop must survive anything
            record = _RunRecord(
                started=started,
                duration_ms=(time.monotonic() - t0) * 1000,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            state.failures += 1
            log.warning("Job %s failed: %s", name, record.error)
        state.runs += 1
        state.history.append(record)
        state.last_end = time.monotonic()
        # Success ⇒ backstop cadence; failure ⇒ the (sooner) retry, so transient
        # outages self-heal without waiting out a long backstop interval.
        delay = state.job.interval if record.ok else (state.job.retry_after or state.job.interval)
        state.next_due = state.last_end + delay
        return record

    def kick(self, name: str) -> None:
        """Event trigger: pull ``name``'s next run to now — rate-limited by its
        ``cooldown`` (a kick inside the window lands on the already-scheduled
        run, so bursts coalesce), and never DELAYING an earlier schedule."""
        state = self._jobs[name]
        due = max(time.monotonic(), state.last_end + state.job.cooldown)
        if due < state.next_due:
            state.next_due = due

    async def run(self) -> None:
        """The loop. Runs whichever job is due next; sleeps in short slices so
        late registrations and cancellation stay responsive."""
        while True:
            if not self._jobs:
                await asyncio.sleep(1.0)
                continue
            name, state = min(self._jobs.items(), key=lambda kv: kv[1].next_due)
            wait = state.next_due - time.monotonic()
            if wait > 0:
                await asyncio.sleep(min(wait, 1.0))
                continue
            await self.run_once(name)

    def status(self) -> list[dict[str, Any]]:
        """The GET /jobs payload: per-job config + last-run outcome + history."""
        now_mono = time.monotonic()
        out = []
        for name, s in sorted(self._jobs.items()):
            last = s.history[-1] if s.history else None
            out.append(
                {
                    "name": name,
                    "interval": s.job.interval,
                    "owner": s.job.owner,
                    "scope": s.job.scope,
                    "runs": s.runs,
                    "failures": s.failures,
                    "next_due_in": max(0.0, round(s.next_due - now_mono, 1)),
                    "last_run": last.started if last else None,
                    "last_ok": last.ok if last else None,
                    "last_error": last.error if last else None,
                    "last_summary": last.summary if last else None,
                    "history": [
                        {
                            "started": r.started,
                            "duration_ms": round(r.duration_ms, 1),
                            "ok": r.ok,
                            "error": r.error,
                            "summary": r.summary,
                        }
                        for r in s.history
                    ],
                }
            )
        return out


def install_jobs_endpoint(app: Any, runner: JobRunner) -> None:
    """Mount ``GET /jobs`` and start/stop the runner with the app's lifespan.

    Auth comes free: ``install_service_auth`` middleware already gates every
    route except ``/health`` on the hosting services. Registration uses the
    non-decorator forms (typed cleanly, and no deprecated ``on_event``).
    """
    task: dict[str, asyncio.Task[None]] = {}

    async def jobs_status() -> dict[str, Any]:
        return {"jobs": runner.status()}

    async def _start() -> None:
        task["runner"] = asyncio.create_task(runner.run(), name="job-runner")

    async def _stop() -> None:
        t = task.pop("runner", None)
        if t is not None:
            t.cancel()

    app.add_api_route("/jobs", jobs_status, methods=["GET"])
    # Starlette's plain handler lists (this FastAPI version has no
    # app.add_event_handler; the @app.on_event decorator is deprecated).
    app.router.on_startup.append(_start)
    app.router.on_shutdown.append(_stop)
