"""Tests for the shopping/to-do lists skill (HA `todo` front-end): spoken-name
resolution (default/aliases/fuzzy), the fast-intent phrasings and their guards,
the LLM tools, and the curation `lists:` block validation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kenzy.llm.builtin_skills import ha_model
from kenzy.llm.builtin_skills import lists as ls


@pytest.fixture
def ha(monkeypatch):
    """Fake HA: two todo lists with an in-memory item store."""
    available = [
        {"entity_id": "todo.shopping_list", "name": "Shopping list"},
        {"entity_id": "todo.errands", "name": "Errands"},
    ]
    items = {"todo.shopping_list": ["Milk", "Eggs"], "todo.errands": []}
    calls: list[tuple] = []
    curation = {
        "lists": {"default": "todo.shopping_list", "aliases": {"todo.errands": ["the chores"]}}
    }

    async def fake_fetch():
        return [dict(entry) for entry in available]

    async def fake_service(service, entity_id, data=None, *, respond=False):
        calls.append((service, entity_id, data))
        if service == "get_items":
            return {
                entity_id: {
                    "items": [{"summary": s, "status": "needs_action"} for s in items[entity_id]]
                }
            }
        if service == "add_item":
            items[entity_id].append(data["item"])
        elif service in ("remove_item", "update_item"):
            items[entity_id].remove(data["item"])
        return None

    monkeypatch.setattr(ha_model, "fetch_todo_lists", fake_fetch)
    monkeypatch.setattr(ha_model, "load_curation", lambda: curation)
    monkeypatch.setattr(ls, "_todo_service", fake_service)
    # The gate: lists only operate with HA configured + the HA skill enabled.
    monkeypatch.setenv("HA_API_KEY", "test-token")
    import kenzy.llm.skills as sk

    monkeypatch.setattr(sk, "_DISABLED", set())
    ls._pending_create.clear()
    return SimpleNamespace(items=items, calls=calls, curation=curation)


# ---------------------------------------------------------------------------
# Fast intents
# ---------------------------------------------------------------------------


async def test_fast_add_to_default_list(ha):
    r = await ls.fast_lists("Add butter to the list.", "kitchen", None)
    assert r.is_handled and r.text == "Added butter to the Shopping list."
    assert "butter" in ha.items["todo.shopping_list"]


async def test_fast_add_splits_multiple_items(ha):
    r = await ls.fast_lists("add bread, jam and coffee to the shopping list", "kitchen", None)
    assert "bread, jam and coffee" in r.text
    assert {"bread", "jam", "coffee"} <= set(ha.items["todo.shopping_list"])


async def test_fast_add_via_alias(ha):
    r = await ls.fast_lists("add stamps to the chores", "kitchen", None)
    assert r.is_handled and "Errands" in r.text
    assert ha.items["todo.errands"] == ["stamps"]


async def test_fast_read_and_empty(ha):
    r = await ls.fast_lists("what's on the shopping list", "kitchen", None)
    assert r.text == "On the Shopping list: Milk and Eggs."
    r = await ls.fast_lists("what's on the chores", "kitchen", None)
    assert "empty" in r.text


async def test_fast_remove_matches_case_insensitively(ha):
    r = await ls.fast_lists("remove milk from the list", "kitchen", None)
    assert "Removed Milk" in r.text
    assert ha.items["todo.shopping_list"] == ["Eggs"]

    r = await ls.fast_lists("take caviar off the list", "kitchen", None)
    assert "didn't see caviar" in r.text


async def test_fast_complete_requires_off_or_done(ha):
    r = await ls.fast_lists("check off eggs", "kitchen", None)
    assert "Checked off Eggs" in r.text
    done = ("update_item", "todo.shopping_list", {"item": "Eggs", "status": "completed"})
    assert done in ha.calls

    # A bare "check X" is a status question, not a list operation.
    r = await ls.fast_lists("check the weather", "kitchen", None)
    assert r.status == "miss"


async def test_fast_guards_miss_non_lists(ha):
    for utterance in ("what's on tv", "add it to my favorites", "read me a story"):
        r = await ls.fast_lists(utterance, "kitchen", None)
        assert r.status == "miss", utterance


async def test_fast_ambiguous_default_clarifies(ha, monkeypatch):
    monkeypatch.setattr(ha_model, "load_curation", lambda: {})  # no default configured
    r = await ls.fast_lists("add butter to the list", "kitchen", None)
    assert r.status == "clarify" and r.expect_response
    assert "Shopping list" in r.text and "Errands" in r.text


async def test_fast_speaks_setup_guidance(ha, monkeypatch):
    async def none():
        return []

    monkeypatch.setattr(ha_model, "fetch_todo_lists", none)
    # Reading with zero lists → the setup instruction (adds get a create offer).
    r = await ls.fast_lists("what's on the shopping list", "kitchen", None)
    assert r.status == "handled" and "Local to-do" in r.text
    # …but only for phrases that are plainly about lists.
    r = await ls.fast_lists("what's on tv", "kitchen", None)
    assert r.status == "miss"
    r = await ls.fast_lists("add it to my favorites", "kitchen", None)
    assert r.status == "miss"

    async def boom():
        raise OSError("down")

    monkeypatch.setattr(ha_model, "fetch_todo_lists", boom)
    r = await ls.fast_lists("what's on the list", "kitchen", None)
    assert r.is_handled and "couldn't reach Home Assistant" in r.text
    r = await ls.fast_lists("what's on tv", "kitchen", None)
    assert r.status == "miss"  # HA being down doesn't make non-list phrases ours


async def test_hard_gate_without_ha(ha, monkeypatch):
    # No HA_API_KEY → the fast intent stays out entirely and tools say why.
    monkeypatch.delenv("HA_API_KEY")
    r = await ls.fast_lists("add eggs to the shopping list", "kitchen", None)
    assert r.status == "miss"
    assert "isn't set up" in await ls.add_to_list(["eggs"], "")

    # HA skill disabled (dashboard Skills toggle) → same gate.
    monkeypatch.setenv("HA_API_KEY", "test-token")
    import kenzy.llm.skills as sk

    monkeypatch.setattr(sk, "_DISABLED", {"home_assistant"})
    r = await ls.fast_lists("add eggs to the shopping list", "kitchen", None)
    assert r.status == "miss"
    assert "isn't set up" in await ls.read_list("")


# ---------------------------------------------------------------------------
# Create-on-confirm (no lists exist yet)
# ---------------------------------------------------------------------------


@pytest.fixture
def no_lists(ha, monkeypatch):
    """Zero todo entities, with a fake create that registers the new list."""
    state: dict = {"lists": [], "created": []}

    async def fetch():
        return [dict(e) for e in state["lists"]]

    async def fake_create(name):
        state["created"].append(name)
        state["lists"].append({"entity_id": "todo.new_list", "name": name})
        ha.items["todo.new_list"] = []
        return True

    monkeypatch.setattr(ha_model, "fetch_todo_lists", fetch)
    monkeypatch.setattr(ls, "_create_local_list", fake_create)
    return state


async def test_add_offers_to_create_then_yes(ha, no_lists):
    r = await ls.fast_lists("add eggs to the shopping list", "kitchen", None)
    assert r.status == "clarify" and r.expect_response
    assert "Should I create one called Shopping list?" in r.text
    assert no_lists["created"] == []  # nothing happens before the yes

    r = await ls.fast_lists("yes", "kitchen", None)
    assert no_lists["created"] == ["Shopping list"]
    assert "Created the Shopping list" in r.text and "eggs" in r.text
    assert ha.items["todo.new_list"] == ["eggs"]


async def test_create_confirmation_no_and_wrong_room(ha, no_lists):
    r = await ls.fast_lists("add eggs to the list", "kitchen", None)
    assert r.status == "clarify"
    # A "yes" from a different room is not this room's confirmation.
    r = await ls.fast_lists("yes", "office", None)
    assert r.status == "miss" and no_lists["created"] == []

    r = await ls.fast_lists("no thanks", "kitchen", None)
    assert r.is_handled and "won't create" in r.text
    assert no_lists["created"] == []


async def test_unrelated_utterance_clears_pending(ha, no_lists):
    await ls.fast_lists("add eggs to the list", "kitchen", None)
    r = await ls.fast_lists("what's on tv", "kitchen", None)
    assert r.status == "miss"  # processed normally, pending consumed
    r = await ls.fast_lists("yes", "kitchen", None)
    assert r.status == "miss" and no_lists["created"] == []


async def test_create_failure_falls_back_to_ha_guidance(ha, no_lists, monkeypatch):
    async def fail(name):
        return False

    monkeypatch.setattr(ls, "_create_local_list", fail)
    await ls.fast_lists("add eggs to the list", "kitchen", None)
    r = await ls.fast_lists("yes", "kitchen", None)
    assert "couldn't create" in r.text and "Local to-do" in r.text
    assert ha.items.get("todo.new_list", []) == []


async def test_create_list_tool(ha, no_lists):
    out = await ls.create_list("Camping list", ["tent", "rope"])
    assert no_lists["created"] == ["Camping list"]
    assert "Created the Camping list" in out and "tent and rope" in out
    assert "Error" in await ls.create_list("  ")


# ---------------------------------------------------------------------------
# LLM tools
# ---------------------------------------------------------------------------


async def test_tools_roundtrip(ha):
    out = await ls.add_to_list(["flour", "sugar"], "shopping")
    assert out == "Added flour and sugar to the Shopping list."
    out = await ls.read_list("")
    assert "flour" in out and "sugar" in out
    out = await ls.complete_list_items(["flour"], "")
    assert "Checked off flour" in out
    out = await ls.remove_from_list(["sugar"], "shopping list")
    assert "Removed sugar" in out
    assert "Error" in await ls.add_to_list([], "")


async def test_tools_report_unknown_list(ha):
    out = await ls.read_list("the lottery numbers")
    assert "couldn't find a list" in out


# ---------------------------------------------------------------------------
# Curation `lists:` block validation
# ---------------------------------------------------------------------------


def test_validate_curation_lists_block():
    cleaned = ha_model.validate_curation(
        {
            "lists": {
                "default": " todo.shopping_list ",
                "aliases": {"todo.errands": ["the chores", " ", ""]},
            }
        }
    )
    assert cleaned["lists"] == {
        "default": "todo.shopping_list",
        "aliases": {"todo.errands": ["the chores"]},
    }
    assert ha_model.validate_curation({"lists": {}}) == {}

    with pytest.raises(ValueError):
        ha_model.validate_curation({"lists": {"bogus": 1}})
    with pytest.raises(ValueError):
        ha_model.validate_curation({"lists": {"aliases": {"todo.x": "not-a-list"}}})
