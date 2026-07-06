"""Social fast intents: instant greetings + the 'never mind' bail-out.

The discipline (the 'time for dinner' lesson): anchored whole-utterance matches
with a negative list of phrasings that must NOT hijack — a greeting with a tail,
or 'forget the eggs on the list', falls through to the real skill / LLM.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from kenzy.llm import skills as reg

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def social():
    reg.set_config({"location": {"timezone": "America/New_York"}})
    path = ROOT / "src" / "kenzy" / "llm" / "builtin_skills" / "social.py"
    spec = importlib.util.spec_from_file_location("social", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["social"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "utterance",
    ["hello", "Hi.", "hi there", "Howdy!", "greetings", "what's up", "long time no see"],
)
async def test_greetings_handled(social, utterance):
    r = await social.fast_greeting(utterance, "office", None)
    assert r.is_handled and r.text


async def test_time_specific_greetings_echo_daypart(social):
    assert (await social.fast_greeting("good morning", "office", None)).text == "Good morning!"
    assert (await social.fast_greeting("good evening", "office", None)).text == "Good evening!"


@pytest.mark.parametrize("utterance", ["good night", "goodnight", "night night"])
async def test_goodnight_handled(social, utterance):
    r = await social.fast_greeting(utterance, "office", None)
    assert r.is_handled and r.text in social._GOODNIGHTS


@pytest.mark.parametrize(
    "utterance",
    [
        "hello can you turn on the lights",  # greeting with a tail → action
        "good morning what's the weather",
        "hi set a timer for five minutes",
        "thank you",  # gratitude excluded (Whisper hallucinates it)
        "thanks",
        "hey",  # excluded short token
        "what's up with the thermostat",
    ],
)
async def test_greetings_do_not_hijack(social, utterance):
    assert (await social.fast_greeting(utterance, "office", None)).status == "miss"


@pytest.mark.parametrize(
    "utterance", ["never mind", "nevermind", "forget it", "nvm", "Forget that."]
)
async def test_nevermind_handled(social, utterance):
    r = await social.fast_nevermind(utterance, "office", None)
    assert r.is_handled and r.text


@pytest.mark.parametrize(
    "utterance",
    [
        "never mind the eggs, remove them from the list",  # → lists
        "forget the milk on the list",
        "never turn on the lights",
    ],
)
async def test_nevermind_does_not_hijack(social, utterance):
    assert (await social.fast_nevermind(utterance, "office", None)).status == "miss"


async def test_nevermind_does_not_hold_the_floor(social):
    """The bail-out must NOT set expect_response — otherwise it would reopen the
    mic instead of ending the conversation."""
    r = await social.fast_nevermind("never mind", "office", None)
    assert not r.expect_response
