"""
Debug delay — a TEST-ONLY hook that blocks the pipeline for N seconds so the
slow-response scenarios (the processing-cue ladder, backchannel timing, floor
holds) can be forced deterministically instead of waiting on a genuinely slow
model or tool.

Trigger phrase: **"run a test for N seconds"** — deliberately all common,
STT-clean words (an earlier "debug wait …" phrasing was defeated by Whisper
hearing "debug" as "the bug").

OFF by default and inert unless explicitly enabled (under ``skills:``, where
per-skill config lives):

    # llm.yaml (or the dashboard-edited services/llm.yaml override)
    skills:
      testing:
        enabled: true

The delay happens INSIDE the /process (and /process/stream) call — a fast
intent that simply ``asyncio.sleep``s — so the server's cue ladder, which
times from when it dispatches the LLM stage, fires its rungs exactly as it
would for a real slow reply.

Deliberately NOT a scheduler feature and does NOT touch timers/alarms: it is a
fast intent only (the LLM never sees it as a tool), its trigger phrase carries
no timer/alarm/reminder wording (nothing in the scheduler matches it), and it
sits at a priority ABOVE the scheduler so it can never be shadowed. When
testing is disabled the matcher misses immediately, leaving the scheduler
untouched.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from kenzy.llm.skills import FastResult, fast_intent, get_config

log = logging.getLogger(__name__)

# "run a test for 30 seconds" (the canonical form) — "for" is optional and
# whitespace is flexible so STT variants ("run a test 30 seconds") still match.
# Anchored, so it can never fire on ordinary speech; all-common-word phrasing so
# STT transcribes it reliably over the rig.
_DELAY_RE = re.compile(
    r"^run\s+a\s+test(?:\s+for)?\s+(\d{1,3})\s+seconds?[.!?]*$",
    re.IGNORECASE,
)
# Clamp: generous enough to force a genuinely long wait (the founder's 30s), but
# note the server's LLM read timeout will cut a longer delay off with the error
# cue — buffered ~llm.timeout (30s default), streamed ~60s. That timeout→error
# path is itself testable; for a delay that COMPLETES, stay under it.
_MAX_DELAY_S = 60


@fast_intent(priority=96)  # above the scheduler (95); gated, so normally inert
async def fast_debug_delay(
    utterance: str, room_id: str | None, speaker: str | None
) -> FastResult:
    if not get_config("testing", "enabled", False):
        return FastResult.miss()  # test hook disabled — scheduler/others unaffected
    m = _DELAY_RE.match(utterance.strip())
    if m is None:
        return FastResult.miss()
    requested = int(m.group(1))
    seconds = max(1, min(_MAX_DELAY_S, requested))
    if seconds != requested:
        log.info("debug_delay: %ds requested, clamped to %ds (max)", requested, seconds)
    log.info("debug_delay: blocking the pipeline for %ds (test hook)", seconds)
    t0 = time.monotonic()
    await asyncio.sleep(seconds)
    # Report the MEASURED elapsed, not the requested number: a test tool must
    # never overstate. If anything ever cut the wait short (a timeout, a future
    # bug), this exposes it instead of confidently claiming the full duration.
    actual = time.monotonic() - t0
    return FastResult.handled(f"Waited {actual:.0f} seconds.")
