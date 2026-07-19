"""The 4.1 Activity breakdown: span collection inside the LLM service —
fast-intent naming, model/tool spans through the tool loop, and the
names-and-durations-only contract."""

from __future__ import annotations

import pytest

from kenzy.llm import llm as llm_app
from kenzy.llm import memory
from kenzy.llm import skills as sk


@pytest.fixture(autouse=True)
def _mem(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "_store", memory.MemoryStore(tmp_path / "facts.jsonl"))
    yield


async def test_fast_path_span_names_the_intent(monkeypatch):
    # Hermetic: the fast registry isn't loaded under TestClient (main() loads
    # it), so stub the dispatcher — the span contract is what's under test.
    from fastapi.testclient import TestClient

    async def fake_dispatch(utterance, room_id, speaker):
        res = sk.FastResult.handled("It's 10:00.")
        res.name = "fast_datetime"
        return res

    monkeypatch.setattr(sk, "dispatch_fast", fake_dispatch)
    client = TestClient(llm_app.app)
    r = client.post("/process", json={"text": "What time is it?", "room_id": "office"})
    body = r.json()
    assert body["fast"] is True
    assert len(body["spans"]) == 1
    span = body["spans"][0]
    assert span["kind"] == "fast" and span["name"] == "fast_datetime"
    assert isinstance(span["ms"], int)


async def test_tool_loop_spans_in_order(monkeypatch):
    # One tool round-trip: model → tool → model. Spans record the order, the
    # model actually used, and the tool name — never arguments or content.
    class _TC:
        id = "t1"

        class function:  # noqa: N801
            name = "flip_coin"
            arguments = "{}"

    class _Msg1:
        content = ""
        tool_calls = [_TC()]

    class _Msg2:
        content = '{"text": "Heads.", "voice_prompt": "", "expect_response": false}'
        tool_calls = None

    msgs = [_Msg1(), _Msg2()]

    async def fake(kwargs, state=None, **_kw):
        class R:
            class C:
                message = msgs.pop(0)

            choices = [C()]

        return R()

    async def fake_exec(name, args):
        return "heads"

    monkeypatch.setattr(sk, "acompletion_with_fallback", fake)
    monkeypatch.setattr(sk, "execute", fake_exec)

    sk.begin_actions()
    memory.begin_touch()
    sk.begin_request({})
    spans: list[dict] = []
    text, _vp, _exp = await llm_app._run_llm("flip a coin", "John", "office", spans=spans)
    assert text == "Heads."
    assert [x["kind"] for x in spans] == ["model", "tool", "model"]
    assert spans[1]["name"] == "flip_coin"
    assert all(set(x) == {"kind", "name", "ms"} for x in spans)  # names+durations ONLY
