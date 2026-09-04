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


@sk.skill(min_tier="recognized", pace="deferred")
async def _deferred_gated_probe(spec: str = "") -> str:
    """A deferred, tier-gated probe — for the withholding test."""
    return f"built:{spec}"


async def test_a_deferred_gated_skill_is_withheld_below_tier() -> None:
    """The tier-gate fix's first layer: a deferred min_tier skill is withheld
    from get_tools() at unknown tier, so the model never sees it — and its
    pace hint never leaks it either."""
    hidden = await list_tools(tier="unknown")
    shown = await list_tools(tier="recognized")
    names = lambda d: {t["function"]["name"] for t in d["tools"]}  # noqa: E731
    assert "_deferred_gated_probe" not in names(hidden)
    assert "_deferred_gated_probe" in names(shown)
    # pace is served for the ones that ARE visible
    assert shown["pace"]["_deferred_gated_probe"] == "deferred"


# ------------------------------------------------- the ask() continuation doors


@sk.skill()
async def _asking_probe(topic: str = "") -> str:
    """A test probe that needs the user's answer mid-flow (the knock-knock
    shape) — and queues an action AFTER the answer, to pin the drain."""
    from kenzy.llm.asking import ask

    answer = await ask(f"Probe question about {topic}?")
    if answer is None:
        return "probe: no answer"
    sk.add_action({"type": "probe_after", "answer": answer})
    return f"probe answered: {answer}"


@sk.skill()
async def _double_asking_probe() -> str:
    """Chained asks — two questions before the result."""
    from kenzy.llm.asking import ask

    first = await ask("First?")
    second = await ask("Second?")
    return f"got {first} then {second}"


async def test_tool_door_without_allow_ask_keeps_todays_error() -> None:
    """The skew guard: an old server (no allow_ask) gets the error string, so
    a new llm never parks a continuation the caller has no door to resume."""
    from kenzy.llm import asking

    before = asking.pending_count()
    data = await execute_tool(ToolExecBody(name="_asking_probe", arguments={"topic": "x"}))
    assert "ask() called outside" in data["result"]
    assert asking.pending_count() == before  # nothing parked


async def test_tool_door_parks_an_ask_and_continue_finishes() -> None:
    from kenzy.llm.llm import ToolContinueBody, continue_tool

    data = await execute_tool(
        ToolExecBody(name="_asking_probe", arguments={"topic": "jokes"}, allow_ask=True)
    )
    assert "result" not in data
    ask_payload = data["ask"]
    assert ask_payload["prompt"] == "Probe question about jokes?"
    assert ask_payload["capture"] == "text" and data["actions"] == []
    done = await continue_tool(
        ToolContinueBody(
            continuation=ask_payload["continuation"],
            text="yes please",
            speaker="Alex",
            speaker_tier="recognized",
        )
    )
    assert done["result"] == "probe answered: yes please"
    # the post-answer action rode the CONTINUE response, drained once
    assert done["actions"] == [{"type": "probe_after", "answer": "yes please"}]


async def test_tool_door_chains_asks_until_the_skill_finishes() -> None:
    from kenzy.llm.llm import ToolContinueBody, continue_tool

    data = await execute_tool(ToolExecBody(name="_double_asking_probe", allow_ask=True))
    assert data["ask"]["prompt"] == "First?"
    step = await continue_tool(
        ToolContinueBody(continuation=data["ask"]["continuation"], text="one")
    )
    assert step["ask"]["prompt"] == "Second?"  # chained under a NEW continuation
    done = await continue_tool(
        ToolContinueBody(continuation=step["ask"]["continuation"], text="two")
    )
    assert done["result"] == "got one then two"


async def test_tool_cancel_discards_and_is_idempotent() -> None:
    from kenzy.llm import asking
    from kenzy.llm.llm import ToolCancelBody, ToolContinueBody, cancel_tool, continue_tool

    data = await execute_tool(
        ToolExecBody(name="_asking_probe", arguments={"topic": "x"}, allow_ask=True)
    )
    cont = data["ask"]["continuation"]
    assert asking.pending(cont) is not None
    assert (await cancel_tool(ToolCancelBody(continuation=cont)))["ok"]
    assert asking.pending(cont) is None  # the skill got None and its result was discarded
    assert (await cancel_tool(ToolCancelBody(continuation=cont)))["ok"]  # no-op re-cancel
    # and the answer that raced the cancel gets an honest 404
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await continue_tool(ToolContinueBody(continuation=cont, text="late"))
    assert exc.value.status_code == 404


async def test_continuation_kind_guards_both_doors() -> None:
    """A tool park can only finish through /tool/continue — /process/continue
    would unpack the raw result string into spoken-reply finishers (silent
    corruption); both doors refuse the other's kind."""
    import pytest
    from fastapi import HTTPException

    from kenzy.llm.llm import ContinueRequest, ToolCancelBody, cancel_tool, process_continue

    data = await execute_tool(
        ToolExecBody(name="_asking_probe", arguments={"topic": "x"}, allow_ask=True)
    )
    cont = data["ask"]["continuation"]
    with pytest.raises(HTTPException) as exc:
        await process_continue(ContinueRequest(continuation=cont, text="hi"))
    assert exc.value.status_code == 409
    await cancel_tool(ToolCancelBody(continuation=cont))  # leave nothing parked


async def test_answerer_identity_reaches_the_resumed_tool_skill() -> None:
    """The classic guarantee, preserved on the tool door: the resumed skill
    sees who ANSWERED, not who originally asked."""
    from kenzy.llm.llm import ToolContinueBody, continue_tool

    @sk.skill()
    async def _who_answered_probe() -> str:
        from kenzy.llm.asking import ask

        await ask("Anyone?")
        return f"answered by {sk.get_request('person_id')}"

    data = await execute_tool(
        ToolExecBody(name="_who_answered_probe", person_id="p-asker", allow_ask=True)
    )
    done = await continue_tool(
        ToolContinueBody(
            continuation=data["ask"]["continuation"], text="me", person_id="p-answerer"
        )
    )
    assert done["result"] == "answered by p-answerer"
