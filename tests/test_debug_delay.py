"""The debug_delay test hook: gated off by default, blocks the pipeline for N
seconds when enabled, reports the MEASURED elapsed (never the requested number),
and never touches the scheduler."""

from __future__ import annotations

import asyncio
import time

import pytest

from kenzy.llm.builtin_skills import debug_delay as dd


@pytest.fixture()
def _enabled(monkeypatch):
    # The skill binds get_config at import, so patch the name in ITS module.
    monkeypatch.setattr(
        dd, "get_config", lambda section, key, default=None: True
        if (section, key) == ("testing", "enabled") else default,
    )


def _capture_sleep(monkeypatch, *, real: bool = False) -> list[float]:
    """Record the values passed to asyncio.sleep. With real=False the sleep is
    a no-op (fast tests); the elapsed the skill reports is then ~0."""
    seen: list[float] = []
    real_sleep = asyncio.sleep

    async def fake(n, *a, **k):  # noqa: ANN001, ANN202
        seen.append(n)
        if real:
            await real_sleep(n)

    monkeypatch.setattr(dd.asyncio, "sleep", fake)
    return seen


async def test_disabled_by_default_misses():
    # No testing.enabled config → the matcher misses (scheduler et al. untouched).
    r = await dd.fast_debug_delay("run a test for 3 seconds", "office", "adam")
    assert r.status == "miss"


async def test_enabled_blocks_then_reports_measured(_enabled):
    t0 = time.monotonic()
    r = await dd.fast_debug_delay("run a test for 1 second", "office", "adam")
    assert time.monotonic() - t0 >= 1.0  # the pipeline actually blocked
    assert r.status == "handled"
    assert r.text == "Waited 1 seconds."  # measured ~1.0s, rounded


async def test_reports_measured_not_requested(_enabled, monkeypatch):
    # THE FIX: if the actual wait is short, the reply must say the SHORT time —
    # never confidently echo the requested number. Here the sleep is a no-op, so
    # the measured elapsed is ~0 even though 30 was requested.
    _capture_sleep(monkeypatch, real=False)
    r = await dd.fast_debug_delay("run a test for 30 seconds", "office", "adam")
    assert r.status == "handled"
    assert r.text == "Waited 0 seconds."  # measured 0, NOT the requested 30


async def test_parses_phrasings(_enabled, monkeypatch):
    for phrase, secs in [
        ("run a test for 2 seconds", 2),
        ("Run a test for 3 seconds.", 3),
        ("run a test 4 seconds", 4),  # "for" is optional
    ]:
        seen = _capture_sleep(monkeypatch, real=False)
        r = await dd.fast_debug_delay(phrase, "office", "adam")
        assert r.status == "handled"
        assert seen == [secs]  # the parsed duration reached asyncio.sleep


async def test_clamped_to_max(_enabled, monkeypatch):
    seen = _capture_sleep(monkeypatch, real=False)
    await dd.fast_debug_delay("run a test for 999 seconds", "office", "adam")
    assert seen == [dd._MAX_DELAY_S]  # clamped, never the raw 999


async def test_founder_30_second_case(_enabled, monkeypatch):
    # The reported case: 30s must actually reach sleep (old cap of 25 ate it).
    seen = _capture_sleep(monkeypatch, real=False)
    await dd.fast_debug_delay("run a test for 30 seconds", "office", "adam")
    assert seen == [30]


async def test_non_matching_and_scheduler_phrasing_miss(_enabled):
    # Ordinary speech and — critically — timer/alarm phrasing never match, so
    # the scheduler (priority 95) always gets its turn.
    for phrase in (
        "wait 10 seconds",  # no "run a test" trigger
        "run a test",  # no duration
        "set a timer for 10 seconds",
        "turn on the lights in 30 seconds",
        "remind me in 5 seconds",
        "run the test suite",
    ):
        r = await dd.fast_debug_delay(phrase, "office", "adam")
        assert r.status == "miss", phrase
