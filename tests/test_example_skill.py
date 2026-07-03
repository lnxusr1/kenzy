"""The example skill (examples/skills/example_skill.py) is living documentation:
this test loads it through the REAL overlay loader and exercises every mechanism
it demonstrates, so the example can never rot out of sync with the skill API."""

from __future__ import annotations

from pathlib import Path

import pytest

from kenzy.llm import skills as sk

_EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples" / "skills"


@pytest.fixture(scope="module", autouse=True)
def _load_example():
    # Load exactly the way kenzy-llm loads a user's overlay directory.
    sk._load_dir(_EXAMPLES_DIR)
    sk._dedupe_fast_registry()


def test_example_registers_skills_and_fast_intent():
    info = sk.registry_info()
    names = {s["name"] for s in info["skills"]}
    assert {"get_fortune", "share_fortune"} <= names
    assert any(f["name"] == "fast_fortune" for f in info["fast_intents"])
    # The generated schema carries the docstring + the optional param.
    schema = dict(sk._REGISTRY["get_fortune"][1])["function"]
    assert "fortune" in schema["description"].lower()
    assert "topic" in schema["parameters"]["properties"]
    assert schema["parameters"]["required"] == []  # topic has a default


async def test_example_skill_executes_via_registry():
    out = await sk.execute("get_fortune", {"topic": "work"})
    assert "work" in out and len(out) > 20


async def test_example_fast_intent_hits_and_misses():
    import example_skill  # loaded into sys.modules by the overlay loader

    r = await example_skill.fast_fortune("Give me a fortune!", "office", None)
    assert r.is_handled and "office" in r.text and r.voice_prompt
    r = await example_skill.fast_fortune("what's the weather", "office", None)
    assert r.status == "miss"


async def test_example_action_validates_rooms_and_queues_announce():
    import example_skill

    sk.begin_actions()
    sk.begin_request({"rooms": ["Kitchen", "Office"], "schedules": [], "room_id": "Office"})
    out = await example_skill.share_fortune("kitchen")  # case-insensitive match
    assert "Kitchen" in out
    (action,) = sk.take_actions()
    assert action["type"] == "announce" and action["rooms"] == ["Kitchen"]

    sk.begin_actions()
    out = await example_skill.share_fortune("garage")
    assert "Error" in out and sk.take_actions() == []


def test_example_reads_config_override():
    sk._CONFIG.setdefault("example_skill", {})["fortunes"] = ["Configured fortune."]
    try:
        import example_skill

        assert example_skill._pick_fortune() == "Configured fortune."
    finally:
        sk._CONFIG.pop("example_skill", None)
