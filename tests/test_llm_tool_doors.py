"""The llm skill-host doors (v6): /tools serves schemas + the tier policy the
gate enforces; /tool executes ONE approved call with the registry's own tier
guard as defense-in-depth. Endpoint functions called directly — the doors are
plain async functions; transport auth is app-level middleware."""

from __future__ import annotations

from kenzy.llm import skills as sk
from kenzy.llm.llm import ToolExecBody, execute_tool, list_tools


@sk.skill(min_tier="recognized")
async def _door_probe(word: str = "") -> str:
    """A test probe skill that queues one server action."""
    sk.add_action({"type": "probe", "word": word})
    return f"probe:{word}"


async def test_tools_door_serves_tier_filtered_schemas_and_full_policy() -> None:
    hidden = await list_tools(tier="unknown")
    shown = await list_tools(tier="recognized")
    names = lambda d: {t["function"]["name"] for t in d["tools"]}  # noqa: E731
    assert "_door_probe" not in names(hidden)  # withheld below its tier (F1.3)
    assert "_door_probe" in names(shown)
    # the policy is the GATE's ToolRule source — served regardless of tier
    assert hidden["policy"]["_door_probe"] == "recognized"


async def test_tool_door_executes_and_returns_queued_actions() -> None:
    body = ToolExecBody(
        name="_door_probe", arguments={"word": "hi"}, room_id="office",
        speaker="Alex", speaker_tier="recognized",
    )
    data = await execute_tool(body)
    assert data["result"] == "probe:hi"
    assert data["actions"] == [{"type": "probe", "word": "hi"}]  # the server actuates these


async def test_tool_door_refuses_below_tier_as_defense_in_depth() -> None:
    data = await execute_tool(ToolExecBody(name="_door_probe", speaker_tier="unknown"))
    assert "Refused" in data["result"]  # the registry's own guard, behind the gate's
    assert data["actions"] == []
