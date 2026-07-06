"""Fast path for random tools: bare canonical forms answer instantly; anything
with a tail that needs reasoning falls through to the LLM."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from kenzy.llm import skills as reg

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def rnd():
    reg.set_config({})
    path = ROOT / "src" / "kenzy" / "llm" / "builtin_skills" / "random_tools.py"
    spec = importlib.util.spec_from_file_location("random_tools", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["random_tools"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "utterance,check",
    [
        ("flip a coin", lambda t: t in ("Heads", "Tails")),
        ("heads or tails", lambda t: t in ("Heads", "Tails")),
        ("toss a coin", lambda t: t in ("Heads", "Tails")),
        ("roll a die", lambda t: t.isdigit() and 1 <= int(t) <= 6),
        ("roll dice", lambda t: t.isdigit()),
        ("roll a d20", lambda t: 1 <= int(t) <= 20),
        ("roll d20", lambda t: 1 <= int(t) <= 20),
        ("roll 3d6", lambda t: "total" in t),
        ("roll 2 dice", lambda t: "total" in t),
        ("pick a number between 1 and 10", lambda t: 1 <= int(t) <= 10),
        ("give me a number from 5 to 5", lambda t: t == "5"),
    ],
)
async def test_fast_random_handles_bare_forms(rnd, utterance, check):
    r = await rnd.fast_random(utterance, "office", None)
    assert r.is_handled and check(r.text)


@pytest.mark.parametrize(
    "utterance",
    [
        "flip a coin to decide whether I should paint the house",  # reasoning → LLM
        "roll with the punches",
        "pick a number for my lottery ticket and explain why",
        "what number am I thinking of",
        "roll out the new feature",
        "give me a number",  # no range → LLM asks
    ],
)
async def test_fast_random_defers_complex_forms(rnd, utterance):
    assert (await rnd.fast_random(utterance, "office", None)).status == "miss"
