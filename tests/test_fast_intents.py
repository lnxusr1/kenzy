"""Tests for the deterministic fast-path layer (FastResult, registry, dispatch)
and the time/date Stage-0 intent."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from kenzy.llm import skills as reg

ROOT = Path(__file__).resolve().parents[1]


def _load_datetime_skill():
    """Import the bundled datetime_skill by path (loaded the way the registry does)."""
    path = ROOT / "src" / "kenzy" / "llm" / "builtin_skills" / "datetime_skill.py"
    spec = importlib.util.spec_from_file_location("datetime_skill", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["datetime_skill"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def clean_fast_registry():
    """Isolate _FAST_REGISTRY so tests don't leak registrations into each other."""
    saved = list(reg._FAST_REGISTRY)
    reg._FAST_REGISTRY.clear()
    try:
        yield
    finally:
        reg._FAST_REGISTRY[:] = saved


# ---------------------------------------------------------------------------
# FastResult contract
# ---------------------------------------------------------------------------

def test_fastresult_handled():
    r = reg.FastResult.handled("hello")
    assert r.status == "handled"
    assert r.is_handled
    assert r.text == "hello"
    assert r.expect_response is False
    assert r.voice_prompt is None


def test_fastresult_miss():
    r = reg.FastResult.miss()
    assert r.status == "miss"
    assert not r.is_handled


def test_fastresult_clarify_sets_expect_response():
    r = reg.FastResult.clarify("which room?")
    assert r.status == "clarify"
    assert r.is_handled
    assert r.expect_response is True


def test_fastresult_handled_with_expect_response():
    r = reg.FastResult.handled("knock knock", voice_prompt="playful", expect_response=True)
    assert r.expect_response is True
    assert r.voice_prompt == "playful"


# ---------------------------------------------------------------------------
# Registry + dispatch
# ---------------------------------------------------------------------------

def test_fast_intent_requires_async():
    with pytest.raises(TypeError):
        reg.fast_intent(lambda u, r, s: None)  # type: ignore[arg-type]


def test_priority_ordering_and_first_match_wins(clean_fast_registry):
    @reg.fast_intent(priority=1)
    async def low(utterance, room_id, speaker):
        return reg.FastResult.handled("low")

    @reg.fast_intent(priority=10)
    async def high(utterance, room_id, speaker):
        return reg.FastResult.handled("high")

    # Registry is sorted by descending priority.
    assert [name for _p, name, _f in reg._FAST_REGISTRY] == ["high", "low"]


async def test_dispatch_returns_highest_priority_handled(clean_fast_registry):
    @reg.fast_intent(priority=1)
    async def low(utterance, room_id, speaker):
        return reg.FastResult.handled("low")

    @reg.fast_intent(priority=10)
    async def high(utterance, room_id, speaker):
        return reg.FastResult.handled("high")

    result = await reg.dispatch_fast("anything", None, None)
    assert result is not None
    assert result.text == "high"


async def test_dispatch_all_miss_returns_none(clean_fast_registry):
    @reg.fast_intent
    async def nope(utterance, room_id, speaker):
        return reg.FastResult.miss()

    assert await reg.dispatch_fast("anything", None, None) is None


async def test_dispatch_skips_raising_matcher(clean_fast_registry):
    @reg.fast_intent(priority=10)
    async def boom(utterance, room_id, speaker):
        raise RuntimeError("kaboom")

    @reg.fast_intent(priority=1)
    async def ok(utterance, room_id, speaker):
        return reg.FastResult.handled("recovered")

    result = await reg.dispatch_fast("anything", None, None)
    assert result is not None
    assert result.text == "recovered"


# ---------------------------------------------------------------------------
# datetime Stage-0 intent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "utterance,expected",
    [
        ("what time is it", "time"),
        ("what's the time", "time"),
        ("tell me the time", "time"),
        ("time", "time"),
        ("what's the date", "date"),
        ("what day is it", "date"),
        ("date", "date"),
        ("what time and date is it", "both"),
        # Negatives that must NOT be hijacked:
        ("set a timer for five minutes", None),
        ("what's the weather today", None),
        ("turn on the lights", None),
        ("is it cold outside", None),
    ],
)
def test_datetime_classify(utterance, expected):
    mod = _load_datetime_skill()
    assert mod.classify(utterance) == expected


async def test_datetime_fast_intent_handles_time(clean_fast_registry):
    reg.set_config({"location": {"timezone": "America/Chicago"}})
    mod = _load_datetime_skill()
    result = await mod.fast_datetime("what time is it", "living_room", "unknown")
    assert result.is_handled
    assert result.text
    assert result.voice_prompt


async def test_datetime_fast_intent_misses_command(clean_fast_registry):
    mod = _load_datetime_skill()
    result = await mod.fast_datetime("turn on the lights", "living_room", "unknown")
    assert result.status == "miss"
